"""Performance Fix #2 -- capability-aligned requirement batching.

app.deep_investigation.investigate() used to submit every requirement to
runtime.ensure() one at a time, even though
ExistingResearchCapabilityExecutor.execute_primary() already services
several requirements with exactly ONE underlying capability call whenever
they are requested together (STRUCTURED_MARKET for VALUATION_INPUTS /
LATEST_PRICE / SECTOR_MACRO; FINANCIALS for the four financial-fact
requirements; one shared repository.refresh_targeted_categories() call for
ORDER_BOOK_CAPEX_GUIDANCE / SHAREHOLDING / GOVERNANCE_HISTORY). investigate()
now submits each such group together in one runtime.ensure() call
(_CAPABILITY_GROUPS / _acquire_group), collapsing duplicate sequential
provider/discovery passes -- while keeping every requirement's own
readiness/reason/failure/matrix accounting fully independent (_finalize is
still evaluated once per requirement, regardless of whether it was acquired
solo or as part of a batch).

This file uses a fully controllable fake runtime (mirroring the
SimpleNamespace(read=..., ensure=..., repository=...) pattern already used
throughout tests/test_di20h_discovery_investigation.py and
tests/test_current_news_optional_non_blocking.py) that records every
ensure() call's requirement_ids, so batching can be proven directly: how
many underlying calls were made, which requirements rode along in each one,
and what each requirement's own truthful outcome was afterward.
"""
import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.deep_investigation import investigate, _CAPABILITY_GROUPS, _GROUP_FOR_REQUIREMENT
from app.research_readiness import ResearchRequirementStatus
from app.research_readiness_runtime import TargetedEnsureResult
from app.stock_rule_engine import StockRuleEngineEligibilityPolicy
from app.cycle_checkpoint import CandidateState, DISPOSITION_STATE, state_for_disposition, terminal_meaning

from test_stock_rule_engine import INSTRUMENT_ID, _readiness


class _FakeGroupRuntime:
    """Controllable runtime for investigate(): records every ensure() call's
    requirement_ids (proving how many underlying calls -- and which
    requirements rode along in each -- actually happened), lets a caller
    choose exactly which requested requirements become READY_FRESH once a
    call completes (so a shared call can succeed for only SOME of its
    members, exactly like a real discovery pass that finds evidence for one
    category but not another), and can delay or fail specific requirements.
    """

    def __init__(self, readiness, *, succeeds=None, fail=None, delays=None):
        self.readiness = readiness
        self.succeeds = set(succeeds or ())
        self.fail = dict(fail or {})
        self.delays = dict(delays or {})
        self.ensure_calls: list[tuple[str, ...]] = []
        # One entry per underlying shared acquisition capability actually
        # executed -- mirrors execute_primary() making exactly one call per
        # requested group regardless of how many members it services.
        self.provider_executions: list[tuple[str, ...]] = []
        self.observations: list[tuple] = []
        self.repository = SimpleNamespace(record_acquisition_observation=self._record)

    async def _record(self, *args, **kwargs):
        self.observations.append((args, kwargs))

    async def read(self, instrument_id, *, jurisdiction, evidence_only=False):
        return self.readiness

    async def ensure(self, instrument_id, *, jurisdiction, requirement_ids, identity_headers=None,
                     correlation_id=None, wait_for_completion=True):
        requirement_ids = tuple(requirement_ids)
        self.ensure_calls.append(requirement_ids)
        self.provider_executions.append(requirement_ids)
        delay = max((self.delays.get(rid, 0) for rid in requirement_ids), default=0)
        if delay:
            await asyncio.sleep(delay)
        failing = [rid for rid in requirement_ids if rid in self.fail]
        if failing:
            raise self.fail[failing[0]]
        self.readiness = replace(self.readiness, requirements=tuple(
            replace(row, status=ResearchRequirementStatus.READY_FRESH, missing_input_ids=())
            if row.requirement_id in requirement_ids and row.requirement_id in self.succeeds else row
            for row in self.readiness.requirements))
        return TargetedEnsureResult(self.readiness, requirement_ids, (f"GROUP:{'+'.join(requirement_ids)}",))


async def _run(readiness, runtime_cls=_FakeGroupRuntime, **runtime_kwargs):
    runtime = runtime_cls(readiness, **runtime_kwargs)
    result, plan, matrix = await investigate(runtime, INSTRUMENT_ID, jurisdiction="INDIA")
    return runtime, result, matrix


# 1 -- SAFE BATCH GROUPS DISCOVERED (sanity: the module's own groups match
# what this file's docstring / the final report claim). --------------------
def test_capability_groups_match_execute_primary_and_exclude_current_news():
    assert _CAPABILITY_GROUPS == (
        ("VALUATION_INPUTS", "LATEST_PRICE", "SECTOR_MACRO"),
        ("BUSINESS_QUALITY_FACTS", "GROWTH_FACTS", "BALANCE_SHEET_FACTS", "QUARTERLY_FINANCIALS"),
        ("ORDER_BOOK_CAPEX_GUIDANCE", "SHAREHOLDING", "GOVERNANCE_HISTORY"),
    )
    assert "CURRENT_NEWS" not in _GROUP_FOR_REQUIREMENT
    assert "HISTORICAL_PRICE_SERIES" not in _GROUP_FOR_REQUIREMENT


# A -- Equivalence test ------------------------------------------------------
@pytest.mark.asyncio
async def test_a_equivalence_grouped_matches_singleton_semantics():
    # Reference: each FINANCIALS-group member acquired completely on its
    # OWN, one investigate() call at a time -- a "group of 1" every time,
    # which is exactly the pre-batching singleton call shape
    # (requirement_ids=(member,)).
    reference = {}
    for member in ("BUSINESS_QUALITY_FACTS", "GROWTH_FACTS"):
        readiness = _readiness({member: ResearchRequirementStatus.MISSING})
        runtime, result, matrix = await _run(readiness, succeeds={member})
        assert runtime.ensure_calls == [(member,)]  # confirms this reference run really is singleton
        reference[member] = {
            "status": result.readiness.for_requirement(member).status,
            "failure": result.failures.get(member),
            "matrix_state": matrix[member]["state"],
            "matrix_failure": matrix[member]["failure"],
        }

    # Grouped: BOTH members MISSING together -- batching now applies.
    grouped_readiness = _readiness({"BUSINESS_QUALITY_FACTS": ResearchRequirementStatus.MISSING,
                                    "GROWTH_FACTS": ResearchRequirementStatus.MISSING})
    runtime, result, matrix = await _run(grouped_readiness,
        succeeds={"BUSINESS_QUALITY_FACTS", "GROWTH_FACTS"})
    assert len(runtime.ensure_calls) == 1
    assert set(runtime.ensure_calls[0]) == {"BUSINESS_QUALITY_FACTS", "GROWTH_FACTS"}

    for member in ("BUSINESS_QUALITY_FACTS", "GROWTH_FACTS"):
        assert result.readiness.for_requirement(member).status == reference[member]["status"]
        assert result.failures.get(member) == reference[member]["failure"]
        assert matrix[member]["state"] == reference[member]["matrix_state"]
        assert matrix[member]["failure"] == reference[member]["matrix_failure"]

    # Final disposition / checkpoint terminal-meaning equivalence, using the
    # real production DISPOSITION_STATE/terminal_meaning contract (see
    # app.cycle_checkpoint and tests/test_truthful_final_accounting.py):
    # both requirements READY_FRESH with no failures means the mandatory
    # data this group represents is complete, i.e. an "ANALYZED" disposition
    # -> CandidateState.COMPLETED -> "FULLY_ANALYZED", identically whether
    # acquired singly or together.
    eligibility = StockRuleEngineEligibilityPolicy().evaluate(result.readiness)
    assert eligibility.full_analysis_allowed
    disposition = "ANALYZED" if eligibility.full_analysis_allowed else "DEEP_READINESS_NOT_MET"
    assert state_for_disposition(disposition) == CandidateState.COMPLETED
    assert terminal_meaning(state_for_disposition(disposition)) == "FULLY_ANALYZED"


# B -- Shared-acquisition test -----------------------------------------------
@pytest.mark.asyncio
async def test_b_grouped_requirements_share_one_underlying_call():
    members = ("VALUATION_INPUTS", "LATEST_PRICE", "SECTOR_MACRO")
    readiness = _readiness({member: ResearchRequirementStatus.MISSING for member in members})
    runtime, result, matrix = await _run(readiness, succeeds=set(members))

    # BEFORE (pre-batching singleton semantics): 3 separate ensure() calls,
    # one per requirement -- 3 underlying provider/discovery executions.
    # AFTER (batched): exactly 1.
    assert len(runtime.ensure_calls) == 1
    assert set(runtime.ensure_calls[0]) == set(members)
    assert len(runtime.provider_executions) == 1

    for member in members:
        assert result.readiness.for_requirement(member).status == ResearchRequirementStatus.READY_FRESH
        assert member not in result.failures
        assert matrix[member]["failure"] is None


# C -- Partial-group failure test --------------------------------------------
@pytest.mark.asyncio
async def test_c_partial_group_failure_stays_independent():
    readiness = _readiness({
        "ORDER_BOOK_CAPEX_GUIDANCE": ResearchRequirementStatus.MISSING,
        "SHAREHOLDING": ResearchRequirementStatus.MISSING,
        "GOVERNANCE_HISTORY": ResearchRequirementStatus.MISSING,
    })
    # The shared call succeeds (no exception) but only SHAREHOLDING's
    # evidence genuinely materializes -- exactly like a real discovery pass
    # that finds candidates for one category but not the others.
    runtime, result, matrix = await _run(readiness, succeeds={"SHAREHOLDING"})
    assert len(runtime.ensure_calls) == 1

    assert result.readiness.for_requirement("SHAREHOLDING").status == ResearchRequirementStatus.READY_FRESH
    assert "SHAREHOLDING" not in result.failures
    assert matrix["SHAREHOLDING"]["failure"] is None

    for member in ("ORDER_BOOK_CAPEX_GUIDANCE", "GOVERNANCE_HISTORY"):
        assert result.readiness.for_requirement(member).status == ResearchRequirementStatus.MISSING
        assert member in result.failures  # truthfully classified as still-unsatisfied -- not silently dropped
        assert matrix[member]["failure"] is not None

    # record_acquisition_observation was called for exactly the two
    # requirements that actually remained unsatisfied -- never for
    # SHAREHOLDING, whose own evidence the shared call genuinely satisfied.
    observed_ids = {args[1] for args, kwargs in runtime.observations}
    assert observed_ids == {"ORDER_BOOK_CAPEX_GUIDANCE", "GOVERNANCE_HISTORY"}


# D -- Budget isolation test --------------------------------------------------
@pytest.mark.asyncio
async def test_d_budget_state_does_not_leak_between_groups_or_requirements():
    from app.deep_investigation import acquisition_budget

    seen_during_calls: list[tuple[tuple[str, ...], str | None, int | None]] = []

    class _ProbeRuntime(_FakeGroupRuntime):
        async def ensure(self, instrument_id, **kwargs):
            budget = acquisition_budget(instrument_id)
            seen_during_calls.append((
                tuple(kwargs["requirement_ids"]),
                budget.requirement_id if budget is not None else None,
                budget.documents_attempted if budget is not None else None,
            ))
            if budget is not None:
                # Simulate real acquisition work consuming the ACTIVE budget.
                budget.documents_attempted += 2
            return await super().ensure(instrument_id, **kwargs)

    readiness = _readiness({
        "VALUATION_INPUTS": ResearchRequirementStatus.MISSING,
        "LATEST_PRICE": ResearchRequirementStatus.MISSING,
        "QUARTERLY_FINANCIALS": ResearchRequirementStatus.MISSING,
    })
    assert acquisition_budget(INSTRUMENT_ID) is None  # nothing active before investigate() runs
    runtime, result, matrix = await _run(readiness, runtime_cls=_ProbeRuntime,
        succeeds={"VALUATION_INPUTS", "LATEST_PRICE", "QUARTERLY_FINANCIALS"})
    assert acquisition_budget(INSTRUMENT_ID) is None  # nothing left active afterward

    # STRUCTURED_MARKET group call ({VALUATION_INPUTS, LATEST_PRICE}) and
    # FINANCIALS group call ({QUARTERLY_FINANCIALS}) -- two SEPARATE
    # underlying calls, each must see its OWN fresh budget.
    assert len(seen_during_calls) == 2
    for requirement_ids, budget_label, documents_attempted_before in seen_during_calls:
        # Each call saw a budget active labeled for exactly the
        # requirement_ids THAT call requested -- never a leftover budget
        # (or leftover document count) from a different group/requirement's
        # own acquisition.
        assert budget_label == "+".join(requirement_ids)
        assert documents_attempted_before == 0


# E -- CURRENT_NEWS regression ------------------------------------------------
@pytest.mark.asyncio
async def test_e_current_news_stays_off_blocking_path_after_batching():
    assert "CURRENT_NEWS" not in _GROUP_FOR_REQUIREMENT  # never grouped with anything

    readiness = _readiness({
        "VALUATION_INPUTS": ResearchRequirementStatus.MISSING,
        "LATEST_PRICE": ResearchRequirementStatus.MISSING,
        "SECTOR_MACRO": ResearchRequirementStatus.MISSING,
        "CURRENT_NEWS": ResearchRequirementStatus.MISSING,
    })
    slow_news_seconds = 0.3
    runtime = _FakeGroupRuntime(readiness,
        succeeds={"VALUATION_INPUTS", "LATEST_PRICE", "SECTOR_MACRO", "CURRENT_NEWS"},
        delays={"CURRENT_NEWS": slow_news_seconds})
    background_tasks = set()
    started = time.monotonic()
    result, plan, matrix = await investigate(runtime, INSTRUMENT_ID, jurisdiction="INDIA",
                                              background_tasks=background_tasks)
    assert time.monotonic() - started < slow_news_seconds / 2

    # The mandatory STRUCTURED_MARKET batch completed fully, unaffected by
    # slow CURRENT_NEWS.
    for member in ("VALUATION_INPUTS", "LATEST_PRICE", "SECTOR_MACRO"):
        assert result.readiness.for_requirement(member).status == ResearchRequirementStatus.READY_FRESH

    # CURRENT_NEWS was never folded into any batched ensure() call.
    assert all(call == ("CURRENT_NEWS",) or "CURRENT_NEWS" not in call for call in runtime.ensure_calls)
    assert len(background_tasks) == 1
    await asyncio.gather(*background_tasks, return_exceptions=True)
    assert matrix["CURRENT_NEWS"]["state"] == "READY_FRESH"


# F -- Mandatory blocking regression ------------------------------------------
@pytest.mark.asyncio
async def test_f_slow_grouped_mandatory_capability_still_fully_blocks():
    readiness = _readiness({
        "VALUATION_INPUTS": ResearchRequirementStatus.MISSING,
        "LATEST_PRICE": ResearchRequirementStatus.MISSING,
        "SECTOR_MACRO": ResearchRequirementStatus.MISSING,
    })
    slow_seconds = 0.25
    runtime = _FakeGroupRuntime(readiness,
        succeeds={"VALUATION_INPUTS", "LATEST_PRICE", "SECTOR_MACRO"},
        delays={"VALUATION_INPUTS": slow_seconds})
    started = time.monotonic()
    result, plan, matrix = await investigate(runtime, INSTRUMENT_ID, jurisdiction="INDIA")
    assert time.monotonic() - started >= slow_seconds * 0.8

    for member in ("VALUATION_INPUTS", "LATEST_PRICE", "SECTOR_MACRO"):
        assert result.readiness.for_requirement(member).status == ResearchRequirementStatus.READY_FRESH
    eligibility = StockRuleEngineEligibilityPolicy().evaluate(result.readiness)
    assert eligibility.full_analysis_allowed


# G -- deterministic before/after call-count + synthetic timing measurement --
@pytest.mark.asyncio
async def test_measurement_before_after_call_counts_and_synthetic_timing():
    # Deterministic, synthetic measurement only -- NOT a production speedup
    # claim (see the report's SYNTHETIC TIMING RESULT section). Each
    # simulated ensure() call costs a fixed per-call latency
    # (per_call_seconds), standing in for a fixed per-round-trip
    # discovery/provider cost regardless of how many requirement_ids ride
    # along in that one call.
    per_call_seconds = 0.05
    members = ("VALUATION_INPUTS", "LATEST_PRICE", "SECTOR_MACRO")
    readiness = _readiness({member: ResearchRequirementStatus.MISSING for member in members})
    runtime = _FakeGroupRuntime(readiness, succeeds=set(members),
        delays={member: per_call_seconds for member in members})
    started = time.monotonic()
    result, plan, matrix = await investigate(runtime, INSTRUMENT_ID, jurisdiction="INDIA")
    after_elapsed = time.monotonic() - started

    after_calls = len(runtime.ensure_calls)
    before_calls = len(members)  # BEFORE: one singleton ensure() call per requirement
    before_elapsed_estimate = before_calls * per_call_seconds  # sequential, same per-call cost each

    assert before_calls == 3
    assert after_calls == 1
    assert after_elapsed < before_elapsed_estimate * 0.7
    print(f"MEASUREMENT before_calls={before_calls} after_calls={after_calls} "
          f"before_elapsed_estimate={before_elapsed_estimate:.3f}s after_elapsed_measured={after_elapsed:.3f}s")
