"""STEP 3 budget-scaling tests for capability-aligned batching (DeepInvestigation).

Performance Fix #2 batched requirements that share one underlying
``execute_primary`` capability call into a single ``runtime.ensure()`` call.
However, ``_acquire_group`` initially constructed the shared
``RequirementAcquisitionBudget`` with the singleton defaults
(``max_documents=4, max_queries=2``) -- i.e. a 4-member group received the same
budget a single singleton previously had, instead of the 16-doc / 8-query total
the 4 singletons collectively had.  This let one member's document consumption
starve its siblings into ``DOCUMENT_BUDGET_EXHAUSTED`` purely due to batching,
violating the production invariant:

  "Performance optimization must not reduce the evidence opportunity available
   to any mandatory/applicable requirement compared with the previous singleton
   execution model."

The fix scales the group's SHARED physical budget to
``len(members) * singleton_default`` while keeping one underlying call, per-
requirement truthful classification via ``_finalize``, and CURRENT_NEWS on the
non-blocking path.

These tests prove:
  A. Singleton parity -- each batch member gets >= its singleton evidence opportunity.
  B. No budget starvation -- one member exhausting its share can't starve a sibling.
  C. Boundedness -- every budget ceiling is finite and explicit.
  D. Shared-work preservation -- one ensure()/provider pass for N group members.
  E. Failure truthfulness -- genuine exhaustion yields technical-failure states,
     not READY.
  F. CURRENT_NEWS regression green.
  G. FULLY_ANALYZED / terminal-accounting regression green.
"""
import asyncio
import time
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.deep_investigation import (
    investigate,
    _CAPABILITY_GROUPS,
    _GROUP_FOR_REQUIREMENT,
    acquisition_budget,
    RequirementAcquisitionBudget,
    _SINGLETON_MAX_DOCUMENTS,
    _SINGLETON_MAX_QUERIES,
)
from app.research_readiness import ResearchRequirementStatus
from app.research_readiness_runtime import TargetedEnsureResult
from app.stock_rule_engine import StockRuleEngineEligibilityPolicy
from app.cycle_checkpoint import (
    CandidateState,
    DISPOSITION_STATE,
    state_for_disposition,
    terminal_meaning,
)

from test_stock_rule_engine import INSTRUMENT_ID, _readiness


# ---------------------------------------------------------------------------
# Shared fake runtime: records ensure() calls and lets a caller drive exactly
# which requirements become satisfied, fail, or delay -- plus a budget probe
# so tests can inspect the scaled ceiling and per-requirement consumption.
# ---------------------------------------------------------------------------

class _BudgetProbeRuntime:
    """Fake runtime that records ensure() calls AND captures the active
    RequirementAcquisitionBudget (including its scaled ceilings) at each call.

    It also lets tests simulate real document/query consumption via
    ``allow_document`` / ``reserve_queries`` on the live budget inside the
    simulated ensure() call -- so we can prove starvation resistance directly
    against the production consumption methods.
    """

    def __init__(self, readiness, *, succeeds=None, fail=None, delays=None,
                 consume_documents=None, consume_queries=None):
        self.readiness = readiness
        self.succeeds = set(succeeds or ())
        self.fail = dict(fail or {})
        self.delays = dict(delays or {})
        # requirement_id -> number of documents to consume from the live budget
        self.consume_documents = dict(consume_documents or {})
        self.consume_queries = dict(consume_queries or {})
        self.ensure_calls: list[tuple[str, ...]] = []
        self.provider_executions: list[tuple[str, ...]] = []
        self.observations: list[tuple] = []
        self.budgets_seen: list[RequirementAcquisitionBudget] = []
        self.repository = SimpleNamespace(record_acquisition_observation=self._record)

    async def _record(self, *args, **kwargs):
        self.observations.append((args, kwargs))

    async def read(self, instrument_id, *, jurisdiction, evidence_only=False):
        return self.readiness

    async def ensure(self, instrument_id, *, jurisdiction, requirement_ids,
                     identity_headers=None, correlation_id=None,
                     wait_for_completion=True):
        requirement_ids = tuple(requirement_ids)
        self.ensure_calls.append(requirement_ids)
        self.provider_executions.append(requirement_ids)
        budget = acquisition_budget(instrument_id)
        self.budgets_seen.append(budget)

        delay = max((self.delays.get(rid, 0) for rid in requirement_ids), default=0)
        if delay:
            await asyncio.sleep(delay)

        failing = [rid for rid in requirement_ids if rid in self.fail]
        if failing:
            raise self.fail[failing[0]]

        # Simulate real acquisition-layer consumption of the live budget.
        if budget is not None:
            docs_to_consume = max(
                (self.consume_documents.get(rid, 0) for rid in requirement_ids),
                default=0,
            )
            for _ in range(docs_to_consume):
                if not await budget.allow_document():
                    break
            queries_to_consume = max(
                (self.consume_queries.get(rid, 0) for rid in requirement_ids),
                default=0,
            )
            if queries_to_consume:
                budget.reserve_queries(queries_to_consume)

        self.readiness = replace(self.readiness, requirements=tuple(
            replace(row, status=ResearchRequirementStatus.READY_FRESH, missing_input_ids=())
            if row.requirement_id in requirement_ids and row.requirement_id in self.succeeds else row
            for row in self.readiness.requirements))
        return TargetedEnsureResult(self.readiness, requirement_ids,
                                    (f"GROUP:{'+'.join(requirement_ids)}",))


async def _run(readiness, **runtime_kwargs):
    runtime = _BudgetProbeRuntime(readiness, **runtime_kwargs)
    result, plan, matrix = await investigate(runtime, INSTRUMENT_ID, jurisdiction="INDIA")
    return runtime, result, matrix


# ---------------------------------------------------------------------------
# A -- SINGLETON PARITY
#
# For each batching group, every member gets at least the same logical
# evidence opportunity it had under singleton execution.  Pre-batching each
# singleton had max_documents=4, max_queries=2.  The group's scaled physical
# budget must be >= N * 4 documents and >= N * 2 queries.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_singleton_parity_group_budget_scales_by_member_count():
    """For each _CAPABILITY_GROUPS entry, the shared budget's physical ceiling
    equals len(members) * singleton_default -- giving the single shared
    execute_primary pass at least as much total acquisition capacity as the N
    independent singleton calls it replaced."""
    for group in _CAPABILITY_GROUPS:
        readiness = _readiness({rid: ResearchRequirementStatus.MISSING for rid in group})
        runtime, _, _ = await _run(readiness, succeeds=set(group))
        assert len(runtime.ensure_calls) == 1
        budget = runtime.budgets_seen[0]
        n = len(group)
        assert budget.max_documents == _SINGLETON_MAX_DOCUMENTS * n, (
            f"Group {group}: max_documents={budget.max_documents} "
            f"expected {_SINGLETON_MAX_DOCUMENTS * n}"
        )
        assert budget.max_queries == _SINGLETON_MAX_QUERIES * n, (
            f"Group {group}: max_queries={budget.max_queries} "
            f"expected {_SINGLETON_MAX_QUERIES * n}"
        )
        assert budget.member_requirement_ids == group
        assert budget.per_requirement_max_documents == _SINGLETON_MAX_DOCUMENTS
        assert budget.per_requirement_max_queries == _SINGLETON_MAX_QUERIES


@pytest.mark.asyncio
async def test_a2_singleton_parity_each_member_has_singleton_share():
    """Each member's per-requirement logical share equals the singleton default
    (4 docs / 2 queries), so no member is entitled to less than it had when
    acquired alone.  When a group member is the only one needing acquisition,
    investigate() still routes it through _acquire_group with member_count=1,
    so its scaled budget (1 * 4) equals the singleton ceiling -- parity holds."""
    for group in _CAPABILITY_GROUPS:
        for member in group:
            readiness = _readiness({member: ResearchRequirementStatus.MISSING})
            runtime, _, _ = await _run(readiness, succeeds={member})
            budget = runtime.budgets_seen[0]
            # Whether singleton or 1-member group, the ceiling is 4 docs / 2 queries.
            assert budget.max_documents == _SINGLETON_MAX_DOCUMENTS, (
                f"{member}: max_documents={budget.max_documents} "
                f"expected {_SINGLETON_MAX_DOCUMENTS}"
            )
            assert budget.max_queries == _SINGLETON_MAX_QUERIES
            # The per-requirement logical share is always the singleton default.
            assert budget.per_requirement_max_documents == _SINGLETON_MAX_DOCUMENTS
            assert budget.per_requirement_max_queries == _SINGLETON_MAX_QUERIES


# ---------------------------------------------------------------------------
# B -- NO BUDGET STARVATION
#
# One member consuming its full document/query allowance cannot cause an
# otherwise-satisfiable sibling requirement to become
# DOCUMENT_BUDGET_EXHAUSTED / QUERY_BUDGET_EXHAUSTED solely due to batching.
#
# With the OLD budget model (max_documents=4 for a 4-member group), member A
# consuming 4 documents exhausts the entire group budget, starving B/C/D.
# With the fix (max_documents=16), member A consuming up to 16 documents is
# needed to exhaust -- but more importantly, member A consuming its own 4
# (singleton share) must NOT starve siblings.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_b_no_starvation_one_member_consuming_singleton_share():
    """Member A consumes exactly its singleton share (4 documents). The group
    budget is 16. Sibling B is satisfiable and must NOT be classified as
    DOCUMENT_BUDGET_EXHAUSTED -- it must get its own truthful outcome."""
    members = ("BUSINESS_QUALITY_FACTS", "GROWTH_FACTS", "BALANCE_SHEET_FACTS",
               "QUARTERLY_FINANCIALS")
    readiness = _readiness({rid: ResearchRequirementStatus.MISSING for rid in members})
    runtime, result, matrix = await _run(
        readiness,
        succeeds=set(members),
        consume_documents={"BUSINESS_QUALITY_FACTS": 4},  # singleton share
    )
    budget = runtime.budgets_seen[0]
    # The group budget absorbed 4 documents without hitting its 16 ceiling.
    assert budget.documents_attempted == 4
    assert not budget.exhausted
    # No member is failed -- all were satisfied by the shared call regardless.
    for member in members:
        assert member not in result.failures, (
            f"{member} should not be failed -- it was satisfied by the shared call"
        )


@pytest.mark.asyncio
async def test_b2_no_starvation_one_member_consumes_all_documents_still_not_exhausted_for_sibling():
    """One member tries to consume ALL available documents (its full singleton
    share * group_size = the entire scaled budget). Even so, the shared call
    satisfied the siblings' evidence, so they are NOT marked failed. The
    budget IS genuinely exhausted, but only those still unsatisfied get
    DOCUMENT_BUDGET_EXHAUSTED -- not those the shared pass satisfied."""
    members = ("BUSINESS_QUALITY_FACTS", "GROWTH_FACTS", "BALANCE_SHEET_FACTS",
               "QUARTERLY_FINANCIALS")
    readiness = _readiness({rid: ResearchRequirementStatus.MISSING for rid in members})
    n = len(members)
    # Consume the ENTIRE scaled budget through ONE member's document requests.
    runtime, result, matrix = await _run(
        readiness,
        succeeds=set(members),
        consume_documents={"BUSINESS_QUALITY_FACTS": _SINGLETON_MAX_DOCUMENTS * n},
    )
    budget = runtime.budgets_seen[0]
    assert budget.documents_attempted == _SINGLETON_MAX_DOCUMENTS * n
    # The group budget ceiling was hit (16 docs consumed for a 4-member group).
    assert budget.documents_attempted >= budget.max_documents
    # Siblings that WERE satisfied are never marked failed -- truthful
    # classification checks each requirement's own sufficiency first.
    for member in members:
        assert member not in result.failures


@pytest.mark.asyncio
async def test_b3_no_starvation_query_budget_scales():
    """One member consuming the full singleton query budget (2) must not
    exhaust the group query budget (2 * N = 8 for a 4-member group)."""
    members = ("BUSINESS_QUALITY_FACTS", "GROWTH_FACTS", "BALANCE_SHEET_FACTS",
               "QUARTERLY_FINANCIALS")
    readiness = _readiness({rid: ResearchRequirementStatus.MISSING for rid in members})
    runtime, result, _ = await _run(
        readiness,
        succeeds=set(members),
        consume_queries={"GROWTH_FACTS": 2},  # singleton query share
    )
    budget = runtime.budgets_seen[0]
    assert budget.queries_reserved == 2
    assert not budget.exhausted
    for member in members:
        assert member not in result.failures


# ---------------------------------------------------------------------------
# C -- BOUNDEDNESS
#
# The corrected group remains explicitly bounded. No unlimited
# documents/queries/provider calls.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_c_boundedness_group_budgets_are_finite():
    """Every group budget has an explicit, finite ceiling."""
    for group in _CAPABILITY_GROUPS:
        readiness = _readiness({rid: ResearchRequirementStatus.MISSING for rid in group})
        runtime, _, _ = await _run(readiness, succeeds=set(group))
        budget = runtime.budgets_seen[0]
        assert isinstance(budget.max_documents, int)
        assert isinstance(budget.max_queries, int)
        assert budget.max_documents > 0
        assert budget.max_queries > 0
        # Bounded = finite (not -1, not None, not unlimited sentinel).
        assert budget.max_documents <= _SINGLETON_MAX_DOCUMENTS * len(group)
        assert budget.max_queries <= _SINGLETON_MAX_QUERIES * len(group)


def test_c2_boundedness_singleton_budget_defaults():
    """A freshly-constructed singleton budget has finite defaults."""
    budget = RequirementAcquisitionBudget(
        INSTRUMENT_ID, "TEST_REQ", datetime.now(timezone.utc), lambda: asyncio.sleep(0),
    )
    assert budget.max_documents == _SINGLETON_MAX_DOCUMENTS
    assert budget.max_queries == _SINGLETON_MAX_QUERIES


# ---------------------------------------------------------------------------
# D -- SHARED-WORK PRESERVATION
#
# The representative grouped acquisition still performs ONE shared underlying
# capability/discovery execution rather than reverting to N singleton provider
# passes.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_d_shared_work_one_ensure_call_per_group():
    """For each batch group: exactly 1 ensure() / 1 provider execution, even
    though all N members needed acquisition."""
    for group in _CAPABILITY_GROUPS:
        readiness = _readiness({rid: ResearchRequirementStatus.MISSING for rid in group})
        runtime, result, _ = await _run(readiness, succeeds=set(group))
        assert len(runtime.ensure_calls) == 1
        assert set(runtime.ensure_calls[0]) == set(group)
        assert len(runtime.provider_executions) == 1  # one shared provider pass


# ---------------------------------------------------------------------------
# E -- FAILURE TRUTHFULNESS
#
# Real provider exhaustion/failure still produces the appropriate technical
# failure/retryable state. Do not turn genuine budget exhaustion into READY
# or unavailable.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e_failure_truthfulness_genuine_exhaustion_marks_failed():
    """When the shared call genuinely fails to satisfy a member (simulated by
    NOT succeeding it), _finalize truthfully classifies it as failed -- not
    READY and not silently dropped."""
    members = ("VALUATION_INPUTS", "LATEST_PRICE", "SECTOR_MACRO")
    readiness = _readiness({rid: ResearchRequirementStatus.MISSING for rid in members})
    # Only ONE member succeeds; the other two remain MISSING after the shared
    # call -- simulating a real discovery pass that found evidence for one
    # category but not the others.
    runtime, result, matrix = await _run(
        readiness, succeeds={"LATEST_PRICE"}
    )
    assert len(runtime.ensure_calls) == 1
    # The one that WAS satisfied: no failure.
    assert "LATEST_PRICE" not in result.failures
    # The two that were NOT satisfied: truthfully marked failed.
    for unsatisfied in ("VALUATION_INPUTS", "SECTOR_MACRO"):
        assert unsatisfied in result.failures, f"{unsatisfied} must be truthfully failed"
        assert result.failures[unsatisfied] is not None
        assert matrix[unsatisfied]["failure"] is not None
    # record_acquisition_observation called for exactly the unsatisfied ones.
    observed_ids = {args[1] for args, kwargs in runtime.observations}
    assert observed_ids == {"VALUATION_INPUTS", "SECTOR_MACRO"}


@pytest.mark.asyncio
async def test_e2_failure_truthfulness_provider_exception_broadcasts_to_all_members():
    """A genuine provider exception (raised from ensure/execute_primary) is
    attributed to EVERY requirement in the batch -- identical to how
    execute_primary's own capability-level exception handling broadcasts a
    shared-call failure."""
    members = ("ORDER_BOOK_CAPEX_GUIDANCE", "SHAREHOLDING", "GOVERNANCE_HISTORY")
    readiness = _readiness({rid: ResearchRequirementStatus.MISSING for rid in members})
    runtime, result, matrix = await _run(
        readiness,
        succeeds=set(members),
        fail={"SHAREHOLDING": ConnectionError("upstream provider unavailable")},
    )
    assert len(runtime.ensure_calls) == 1
    for member in members:
        assert member in result.failures
        assert "ConnectionError" in result.failures[member]


@pytest.mark.asyncio
async def test_e3_failure_truthfulness_not_ready_on_exhaustion():
    """Genuine budget exhaustion must NOT produce READY -- the requirement
    must be marked FAILED with a budget-exhausted reason."""
    # We test this at the unit level: construct a group budget, consume it
    # fully, and verify member_exhausted() returns True and the member would
    # be classified as DOCUMENT_BUDGET_EXHAUSTED (not READY).
    group = ("VAL1", "VAL2", "VAL3", "VAL4")
    budget = RequirementAcquisitionBudget(
        INSTRUMENT_ID, "+".join(group), datetime.now(timezone.utc),
        lambda: asyncio.sleep(0),
        max_documents=_SINGLETON_MAX_DOCUMENTS * len(group),
        max_queries=_SINGLETON_MAX_QUERIES * len(group),
        member_requirement_ids=group,
        per_requirement_max_documents=_SINGLETON_MAX_DOCUMENTS,
    )
    # Consume the entire scaled budget.
    for _ in range(_SINGLETON_MAX_DOCUMENTS * len(group)):
        assert await budget.allow_document() is True
    # The next document must be refused -- genuine exhaustion.
    assert await budget.allow_document() is False
    assert budget.exhausted
    for member in group:
        assert budget.member_exhausted(member) is True


# ---------------------------------------------------------------------------
# F -- CURRENT_NEWS regression (non-blocking + budget bridge)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_f_current_news_regresses_unchanged_after_batching():
    """CURRENT_NEWS is never grouped, stays off the blocking path, and its
    singleton budget is unaffected by the group-budget scaling."""
    assert "CURRENT_NEWS" not in _GROUP_FOR_REQUIREMENT

    readiness = _readiness({
        "VALUATION_INPUTS": ResearchRequirementStatus.MISSING,
        "LATEST_PRICE": ResearchRequirementStatus.MISSING,
        "SECTOR_MACRO": ResearchRequirementStatus.MISSING,
        "CURRENT_NEWS": ResearchRequirementStatus.MISSING,
    })
    slow_news_seconds = 0.3
    runtime = _BudgetProbeRuntime(
        readiness,
        succeeds={"VALUATION_INPUTS", "LATEST_PRICE", "SECTOR_MACRO", "CURRENT_NEWS"},
        delays={"CURRENT_NEWS": slow_news_seconds},
    )
    background_tasks = set()
    started = time.monotonic()
    result, plan, matrix = await investigate(
        runtime, INSTRUMENT_ID, jurisdiction="INDIA",
        background_tasks=background_tasks,
    )
    elapsed = time.monotonic() - started
    # CURRENT_NEWS was launched in background and did NOT block the mandatory
    # group completion.
    assert elapsed < slow_news_seconds / 2

    # Mandatory STRUCTURED_MARKET group completed fully.
    for member in ("VALUATION_INPUTS", "LATEST_PRICE", "SECTOR_MACRO"):
        assert result.readiness.for_requirement(member).status == ResearchRequirementStatus.READY_FRESH

    # CURRENT_NEWS was never folded into any batched ensure() call.
    assert all(
        call == ("CURRENT_NEWS",) or "CURRENT_NEWS" not in call
        for call in runtime.ensure_calls
    )
    # The STRUCTURED_MARKET group budget was scaled to 3 * 4 = 12.
    structured_budget = runtime.budgets_seen[0]
    assert structured_budget.max_documents == _SINGLETON_MAX_DOCUMENTS * 3
    assert structured_budget.max_queries == _SINGLETON_MAX_QUERIES * 3

    # Wait for CURRENT_NEWS background task to finish so its singleton budget
    # has been captured by the probe runtime.
    assert len(background_tasks) == 1
    await asyncio.gather(*background_tasks, return_exceptions=True)

    # The CURRENT_NEWS singleton budget has singleton defaults (not scaled).
    news_budget = runtime.budgets_seen[1]
    assert news_budget.requirement_id == "CURRENT_NEWS"
    assert news_budget.max_documents == _SINGLETON_MAX_DOCUMENTS
    assert news_budget.max_queries == _SINGLETON_MAX_QUERIES
    assert news_budget.member_requirement_ids == ()  # singleton, not a group

    assert matrix["CURRENT_NEWS"]["state"] == "READY_FRESH"


# ---------------------------------------------------------------------------
# G -- FULLY_ANALYZED / terminal-accounting regression
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_g_fully_analyzed_terminal_accounting_green():
    """When all mandatory requirements in each group are satisfied by the
    shared acquisition, the eligibility policy allows full analysis -> ANALYZED
    -> CandidateState.COMPLETED -> terminal 'FULLY_ANALYZED'. This must hold
    identically whether requirements are grouped or singleton-acquired."""
    # Use all three groups, every member MISSING -> all satisfied.
    all_grouped = set()
    for group in _CAPABILITY_GROUPS:
        all_grouped.update(group)
    readiness = _readiness({rid: ResearchRequirementStatus.MISSING for rid in all_grouped})
    runtime, result, _ = await _run(readiness, succeeds=all_grouped)

    # One ensure() call per group (3 groups -> 3 calls), not one per requirement.
    assert len(runtime.ensure_calls) == len(_CAPABILITY_GROUPS)

    # No failures.
    for member in all_grouped:
        assert member not in result.failures

    # Eligibility + terminal disposition contract.
    eligibility = StockRuleEngineEligibilityPolicy().evaluate(result.readiness)
    assert eligibility.full_analysis_allowed
    disposition = "ANALYZED" if eligibility.full_analysis_allowed else "DEEP_READINESS_NOT_MET"
    assert state_for_disposition(disposition) == CandidateState.COMPLETED
    assert terminal_meaning(state_for_disposition(disposition)) == "FULLY_ANALYZED"


@pytest.mark.asyncio
async def test_g2_fully_analyzed_equivalence_singleton_vs_grouped():
    """Terminal accounting equivalence: acquiring all FINANCIALS members
    grouped produces the same FULLY_ANALYZED outcome as acquiring them one
    at a time (each singleton run producing READY_FRESH).

    Since both members share the FINANCIALS capability group, investigate()
    always batches them together.  So for the 'singleton' reference we run
    each member ALONE (a 1-member group) in its own investigate() call, then
    compare the terminal outcome against the 2-member grouped run."""
    members = ("GROWTH_FACTS", "BALANCE_SHEET_FACTS")

    # Singleton reference: each member acquired alone in its own investigate().
    singleton_full_analysis = []
    for member in members:
        readiness = _readiness({member: ResearchRequirementStatus.MISSING})
        runtime, result, _ = await _run(readiness, succeeds={member})
        assert len(runtime.ensure_calls) == 1
        assert runtime.ensure_calls[0] == (member,)
        elig = StockRuleEngineEligibilityPolicy().evaluate(result.readiness)
        assert result.readiness.for_requirement(member).status == ResearchRequirementStatus.READY_FRESH
        assert member not in result.failures
        singleton_full_analysis.append(elig.full_analysis_allowed)

    # Grouped: both members together in one investigate() call.
    grouped_readiness = _readiness({m: ResearchRequirementStatus.MISSING for m in members})
    runtime, result, _ = await _run(grouped_readiness, succeeds=set(members))
    assert len(runtime.ensure_calls) == 1  # one shared call
    assert set(runtime.ensure_calls[0]) == set(members)

    grouped_elig = StockRuleEngineEligibilityPolicy().evaluate(result.readiness)

    # SAME terminal accounting for both paths.
    assert grouped_elig.full_analysis_allowed
    assert all(singleton_full_analysis)
    assert terminal_meaning(
        state_for_disposition("ANALYZED" if grouped_elig.full_analysis_allowed else "DEEP_READINESS_NOT_MET")
    ) == "FULLY_ANALYZED"


# ---------------------------------------------------------------------------
# H -- BONUS: pre-fix regression (proves the bug existed and is now fixed)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_h_pre_fix_regression_old_budget_would_starve():
    """Demonstrates the bug: with the OLD unscaled budget (max_documents=4
    for a 4-member group), consuming 4 documents would exhaust the group
    budget and starve siblings. With the FIX (max_documents=16), 4 documents
    consumed by one member does NOT exhaust the group."""
    members = ("BUSINESS_QUALITY_FACTS", "GROWTH_FACTS", "BALANCE_SHEET_FACTS",
               "QUARTERLY_FINANCIALS")
    readiness = _readiness({rid: ResearchRequirementStatus.MISSING for rid in members})

    # --- OLD behavior simulation: budget with old unscaled ceiling ---
    old_budget = RequirementAcquisitionBudget(
        INSTRUMENT_ID, "+".join(members), datetime.now(timezone.utc),
        lambda: asyncio.sleep(0),
        max_documents=4,  # OLD: unscaled
        max_queries=2,    # OLD: unscaled
    )
    for _ in range(4):
        await old_budget.allow_document()
    # One more attempt is refused -- sets exhausted=True.
    assert await old_budget.allow_document() is False
    assert old_budget.exhausted  # OLD: 4 documents exhausted the unscaled budget

    # --- NEW behavior: the actual investigate() uses scaled budget ---
    runtime, result, _ = await _run(
        readiness,
        succeeds=set(members),
        consume_documents={"BUSINESS_QUALITY_FACTS": 4},
    )
    new_budget = runtime.budgets_seen[0]
    assert new_budget.max_documents == 16  # scaled: 4 members * 4 singleton
    assert new_budget.documents_attempted == 4
    assert not new_budget.exhausted  # NEW: 4 documents do NOT exhaust scaled budget
    for member in members:
        assert member not in result.failures
