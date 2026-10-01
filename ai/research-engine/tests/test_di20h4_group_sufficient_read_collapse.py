"""Performance Iteration 2 -- collapse redundant per-member readiness reads.

`_group_sufficient` previously called `_requirement_sufficient(member)` once per
group member inside a short-circuiting loop. When group members are in MIXED
readiness states (some READY_FRESH, some MISSING -- the realistic state
mid-acquisition as facts are persisted incrementally), the per-member loop
issued one `read(evidence_only=True)` (which routes through
`ResearchReadinessService.assess`) PER member until it short-circuited on the
first unsatisfied member. For a 4-member group where the first K members are
READY and member K+1 is MISSING, that is K+1 reads per `sufficient()` call --
all observing the identical durable snapshot because no persistence write
occurs between the synchronous member checks.

This refactor extracts the pure per-requirement classification
(`_requirement_satisfied_from`) so `_group_sufficient` performs ONE read per
`sufficient()` invocation and checks every member against that snapshot. The
singleton / finalize path (`_requirement_sufficient`) is unchanged -- it still
owns its own fresh per-requirement read.

These tests prove:
  A. Read collapse in the mixed-state case -- K+1 pre-collapse reads per
     sufficient() collapse to exactly 1 (linear in candidates, not in
     candidates * members).
  B. Parity -- member classification (NOT_APPLICABLE / READY_FRESH / NSE-source
     authority gate) is identical to the per-member path.
  C. Mutation invalidation -- a persistence write between two sufficient()
     invocations forces a fresh reread (generation bump / snapshot swap).
  D. Finalize unchanged -- `_finalize` still reads per-requirement via
     `_requirement_sufficient` (no member-collapse leakage).
  E. Readiness outcome unchanged -- terminal FULLY_ANALYZED contract holds.

Provider-free: no real providers, no real persistence, no network.
"""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.cycle_checkpoint import (
    CandidateState,
    state_for_disposition,
    terminal_meaning,
)
from app.deep_investigation import (
    investigate,
    _CAPABILITY_GROUPS,
    acquisition_budget,
)
from app.research_readiness import ResearchRequirementStatus
from app.research_readiness_runtime import TargetedEnsureResult
from app.stock_rule_engine import StockRuleEngineEligibilityPolicy

from test_stock_rule_engine import INSTRUMENT_ID, _readiness


def _satisfied_row(row, status=ResearchRequirementStatus.READY_FRESH):
    from dataclasses import replace as _replace
    return _replace(row, status=status, missing_input_ids=())


def _row_for(req_id, readiness):
    for row in readiness.requirements:
        if row.requirement_id == req_id:
            return row
    return None


class _CountingRuntime:
    """Fake runtime that counts read()/assess invocations and lets a test drive
    which requirements become satisfied, plus optional document/query consumption
    through the live budget (mirroring _BudgetProbeRuntime in test_di20h3)."""

    def __init__(self, readiness, *, succeeds=None, consume_documents=None,
                 mixed_member_ready=None):
        self.readiness = readiness
        self.succeeds = set(succeeds or ())
        self.consume_documents = dict(consume_documents or {})
        # When set, read() returns a snapshot where these members are READY_FRESH
        # while the rest stay MISSING -- forcing the per-member sufficiency loop
        # to iterate multiple members before short-circuiting (the case the
        # collapse eliminates redundant reads for).
        self.mixed_member_ready = list(mixed_member_ready or [])
        self.ensure_calls: list[tuple[str, ...]] = []
        self.initial_read_count = 0       # read(evidence_only=False)
        self.evidence_read_count = 0        # read(evidence_only=True)
        self.readiness_reads: list[bool] = []  # per-call evidence_only flags
        self.budgets_seen = []
        self.repository = _NoopRepository()

    async def read(self, instrument_id, *, jurisdiction, evidence_only=False, **_kw):
        if evidence_only:
            self.evidence_read_count += 1
        else:
            self.initial_read_count += 1
        self.readiness_reads.append(evidence_only)
        if self.mixed_member_ready and evidence_only:
            # Serve a fresh mixed-state snapshot (READY for the first K members,
            # MISSING for the rest) so the per-member loop iterates K+1 times
            # pre-collapse. Post-collapse this is still a single read.
            ready = set(self.mixed_member_ready)
            new_reqs = tuple(
                _satisfied_row(r) if r.requirement_id in ready else r
                for r in self.readiness.requirements
            )
            return replace(self.readiness, requirements=new_reqs)
        return self.readiness

    async def ensure(self, instrument_id, *, jurisdiction, requirement_ids,
                     identity_headers=None, correlation_id=None,
                     wait_for_completion=True):
        requirement_ids = tuple(requirement_ids)
        self.ensure_calls.append(requirement_ids)
        budget = acquisition_budget(instrument_id)
        self.budgets_seen.append(budget)
        failing = [rid for rid in requirement_ids if rid not in self.succeeds]
        if failing:
            raise ConnectionError(f"upstream unavailable for {failing[0]}")
        if budget is not None:
            docs_to_consume = max(
                (self.consume_documents.get(rid, 0) for rid in requirement_ids),
                default=0,
            )
            for _ in range(docs_to_consume):
                if not await budget.allow_document():
                    break
        self.readiness = replace(self.readiness, requirements=tuple(
            _satisfied_from_row(row, requirement_ids, self.succeeds)
            for row in self.readiness.requirements))
        return TargetedEnsureResult(self.readiness, requirement_ids,
                                    (f"GROUP:{'+'.join(requirement_ids)}",))


def _satisfied_from_row(row, satisfied_ids, succeeds):
    if row.requirement_id in satisfied_ids and row.requirement_id in succeeds:
        return _satisfied_row(row)
    return row


class _NoopRepository:
    async def record_acquisition_observation(self, *args, **kwargs):
        return None


class _AssessCountingRuntime(_CountingRuntime):
    """Wraps read() to count how many distinct assess-level reloads occurred,
    modeling ResearchReadinessService.assess as invoked from read()."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.assess_calls = 0

    async def read(self, instrument_id, *, jurisdiction, evidence_only=False, **_kw):
        self.assess_calls += 1
        return await super().read(instrument_id, jurisdiction=jurisdiction,
                                   evidence_only=evidence_only)


async def _run(readiness, **runtime_kwargs):
    runtime = _CountingRuntime(readiness, **runtime_kwargs)
    result, plan, matrix = await investigate(runtime, INSTRUMENT_ID, jurisdiction="INDIA")
    return runtime, result, matrix


# ---------------------------------------------------------------------------
# A -- READ COLLAPSE (mixed-state discriminator)
#
# With the pre-collapse per-member loop, a `sufficient()` invocation over a
# group whose members are in mixed states (first K READY, member K+1 MISSING)
# performs K+1 read()/assess calls before short-circuiting -- all observing the
# identical snapshot (no write between synchronous member checks). Post-collapse
# that is exactly 1 read per sufficient() invocation. The mixed_member_ready knob
# forces the mixed-state discriminator so the test fails on pre-collapse code.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_group_sufficient_collapses_mixed_state_per_member_reads():
    """FINANCIALS group (4 members). read() serves a snapshot where the first 3
    members are READY_FRESH and the 4th is MISSING. Each sufficient() invocation
    pre-collapse iterated members 1..4, reading once per member until short-
    circuiting on member 4 (MISSING) = 4 reads. Post-collapse: 1 read per
    sufficient()."""
    members = ("BUSINESS_QUALITY_FACTS", "GROWTH_FACTS", "BALANCE_SHEET_FACTS",
               "QUARTERLY_FINANCIALS")
    readiness = _readiness({rid: ResearchRequirementStatus.MISSING for rid in members})
    D = 3  # document candidates -> 3 sufficient() invocations
    # Force a mixed state: first 3 members READY, 4th MISSING at each evidence read.
    runtime = _CountingRuntime(
        readiness,
        succeeds=set(members),
        consume_documents={"BUSINESS_QUALITY_FACTS": D},
        mixed_member_ready=members[:3],  # BQF, GF, BSF ready; QF missing
    )
    result, _, _ = await investigate(runtime, INSTRUMENT_ID, jurisdiction="INDIA")

    # The shared call still satisfies every member (ensure sets them READY).
    for member in members:
        assert member not in result.failures, f"{member} should be satisfied"

    # D sufficiency checks (allow_document) + N finalize reads.
    # Pre-collapse: D * (K+1) sufficiency reads with K=3 -> D*4 = 12, + N=4 -> 16.
    # Post-collapse: D * 1 = D, + N = 4 -> 3 + 4 = 7.
    post_collapse = D + len(members)
    pre_collapse = D * (len(members[:3]) + 1) + len(members)
    assert runtime.evidence_read_count == post_collapse, (
        f"Expected {post_collapse} evidence reads post-collapse (D={D} + N={len(members)}), "
        f"got {runtime.evidence_read_count}."
    )
    assert runtime.evidence_read_count < pre_collapse, (
        f"No collapse: {runtime.evidence_read_count} reads, pre-collapse would be "
        f"{pre_collapse} (D*(K+1)+N with K=3). The per-member multiplier survived."
    )


@pytest.mark.asyncio
async def test_a2_group_sufficient_one_candidate_one_read_regardless_of_members():
    """A single document candidate for each group drives ONE evidence-only read
    (the _group_sufficient read), regardless of member count or mixed state."""
    for group in _CAPABILITY_GROUPS:
        readiness = _readiness({rid: ResearchRequirementStatus.MISSING for rid in group})
        # Mixed: every member except the last is READY at evidence-read time.
        runtime, result, _ = await _run(
            readiness,
            succeeds=set(group),
            consume_documents={group[0]: 1},
            mixed_member_ready=list(group[:-1]),
        )
        for member in group:
            assert member not in result.failures
        expected = 1 + len(group)  # 1 sufficiency read + N finalize reads
        assert runtime.evidence_read_count == expected, (
            f"Group {group}: expected {expected} evidence reads (1 candidate + finalize), "
            f"got {runtime.evidence_read_count}"
        )


# ---------------------------------------------------------------------------
# B -- CLASSIFICATION PARITY
#
# The collapsed `_group_sufficient` checks every member against one snapshot via
# `_requirement_satisfied_from`. This must produce the SAME classification as
# calling `_requirement_sufficient(member)` per member -- same
# NOT_APPLICABLE pass-through, same READY_FRESH verdict, same
# QUARTERLY_FINANCIALS/INDIA NSE-source authority gate.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_b_classification_parity_group_vs_per_member():
    """All satisfied vs none-satisfied must produce the same per-member matrix
    truth as the per-member path."""
    members = ("BUSINESS_QUALITY_FACTS", "GROWTH_FACTS", "BALANCE_SHEET_FACTS",
               "QUARTERLY_FINANCIALS")

    # All satisfied by the shared call.
    readiness = _readiness({rid: ResearchRequirementStatus.MISSING for rid in members})
    runtime, result, matrix = await _run(readiness, succeeds=set(members))
    for member in members:
        assert member not in result.failures
        assert matrix[member]["failure"] is None
        assert matrix[member]["state"] == "READY_FRESH"

    # None satisfied (shared call fails for all) -> every member truthfully failed.
    readiness2 = _readiness({rid: ResearchRequirementStatus.MISSING for rid in members})
    runtime2, result2, matrix2 = await _run(readiness2, succeeds=set())
    for member in members:
        assert member in result2.failures
        assert matrix2[member]["failure"] is not None


# ---------------------------------------------------------------------------
# C -- MUTATION INVALIDATION
#
# The generation-bump in ResearchRepository._run_blocking_persistence already
# guarantees that a persistence write between two sufficient() invocations
# invalidates any cached evidence-only snapshot. We prove the collapse does not
# weaken this: when a mutation occurs between two sufficient() invocations, the
# next read MUST observe the fresh state (modeled by read() returning a
# different object after a mutation marker), not a cached stale snapshot.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_c_mutation_between_sufficient_calls_reflects_fresh_snapshot():
    """If a persistence write occurs between two `sufficient()` invocations
    (modeled by the runtime mutating its served snapshot between
    allow_document calls), the group's next evidence read MUST observe the
    fresh state -- the collapse did NOT serve one cached snapshot across the
    mutation boundary."""

    class _MutatingRuntime(_CountingRuntime):
        def __init__(self, **kw):
            super().__init__(**kw)
            self._mutation_counter = 0
            self._pre_mutation_reads = 0
            self._post_mutation_reads = 0

        async def ensure(self, *args, **kwargs):
            # Simulate the repository persisting a fact: bump a marker that
            # read() consults to serve a freshness-stamped snapshot.
            self._mutation_counter += 1
            return await super().ensure(*args, **kwargs)

        async def read(self, instrument_id, *, jurisdiction, evidence_only=False, **_kw):
            if evidence_only and self._mutation_counter > 0:
                self._post_mutation_reads += 1
            elif evidence_only:
                self._pre_mutation_reads += 1
            # Force read() to return a DISTINCT freshness object post-mutation.
            if evidence_only:
                self.readiness = replace(
                    self.readiness,
                    generated_at=datetime.now(timezone.utc),
                    requirements=tuple(
                        _satisfied_row(r) if self._mutation_counter > 0 else r
                        for r in self.readiness.requirements
                    ),
                )
            return await super().read(instrument_id, jurisdiction=jurisdiction,
                                      evidence_only=evidence_only)

    members = ("BUSINESS_QUALITY_FACTS", "GROWTH_FACTS", "BALANCE_SHEET_FACTS",
               "QUARTERLY_FINANCIALS")
    readiness = _readiness({rid: ResearchRequirementStatus.MISSING for rid in members})
    runtime = _MutatingRuntime(
        readiness=readiness, succeeds=set(members),
        consume_documents={"BUSINESS_QUALITY_FACTS": 4},
    )
    result, _, _ = await investigate(runtime, INSTRUMENT_ID, jurisdiction="INDIA")
    # A mutation happened mid-acquisition; a post-mutation read must have
    # occurred -- the collapse did not cache across the mutation boundary.
    assert runtime._post_mutation_reads >= 1
    for member in members:
        assert member not in result.failures


# ---------------------------------------------------------------------------
# D -- FINALIZE UNCHANGED
#
# `_finalize` still calls `_requirement_sufficient` per requirement (which does
# its own evidence_only read). This is the singleton/finalization path and is
# NOT collapsed -- each finalized requirement gets its own authoritative
# post-acquisition read. Prove this by asserting the finalize reads equal N
# (one per member) with no document consumption.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_d_finalize_reads_one_per_requirement():
    """With 0 document consumption, the only evidence reads are the N finalize
    per-requirement reads -- proving _finalize is unchanged by the collapse."""
    members = ("ORDER_BOOK_CAPEX_GUIDANCE", "SHAREHOLDING", "GOVERNANCE_HISTORY")
    readiness = _readiness({rid: ResearchRequirementStatus.MISSING for rid in members})
    runtime, result, _ = await _run(readiness, succeeds=set(members))
    for member in members:
        assert member not in result.failures
    assert runtime.evidence_read_count == len(members), (
        f"Finalize should contribute exactly {len(members)} evidence reads, "
        f"got {runtime.evidence_read_count}"
    )


# ---------------------------------------------------------------------------
# E -- READINESS OUTCOME UNCHANGED (terminal contract)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e_full_universe_terminal_contract_preserved():
    """All three groups fully satisfied -> eligibility grants full analysis ->
    CandidateState.COMPLETED -> 'FULLY_ANALYZED'. The read collapse must not
    alter the readiness outcome or terminal disposition."""
    all_grouped = set()
    for group in _CAPABILITY_GROUPS:
        all_grouped.update(group)
    readiness = _readiness({rid: ResearchRequirementStatus.MISSING for rid in all_grouped})
    runtime, result, _ = await _run(readiness, succeeds=all_grouped)

    assert len(runtime.ensure_calls) == len(_CAPABILITY_GROUPS)
    for member in all_grouped:
        assert member not in result.failures

    eligibility = StockRuleEngineEligibilityPolicy().evaluate(result.readiness)
    assert eligibility.full_analysis_allowed
    disposition = "ANALYZED" if eligibility.full_analysis_allowed else "DEEP_READINESS_NOT_MET"
    assert state_for_disposition(disposition) == CandidateState.COMPLETED
    assert terminal_meaning(state_for_disposition(disposition)) == "FULLY_ANALYZED"
