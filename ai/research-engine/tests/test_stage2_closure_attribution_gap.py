"""FINAL Stage-2 micro-fix — close the broad-exception attribution gap.

Root cause (residual attribution gap):
``ExistingResearchCapabilityExecutor.execute_primary`` wraps ONE shared
``repository.refresh_targeted_categories`` call (which services a FUSED set of
requirement categories for a single instrument) in a broad
``except Exception as exc:``. The handler then did::

    for requirement_id in requirement_ids & (
        _FINANCIAL_REQUIREMENTS | {"SHAREHOLDING", "ORDER_BOOK_CAPEX_GUIDANCE",
        "CURRENT_NEWS", "GOVERNANCE_HISTORY"}
    ):
        failures[requirement_id] = type(exc).__name__

That call returns a single ``ResearchSummary`` and raises a single exception
with NO per-requirement ownership, so copying ``type(exc).__name`` to every
requirement in the intersected set misattributes an unrelated shared outage
(e.g. ``SEARCH_PROVIDER_DEGRADED`` raised while searching catalyst/GOVERNANCE
documents for ORDER_BOOK_CAPEX_GUIDANCE/GOVERNANCE_HISTORY) to a requirement
whose own channel (SHAREHOLDING's NSE shared/XBRL feed) never ran -- yielding
the forbidden ``SHAREHOLDING: SEARCH_PROVIDER_DEGRADED``.

Per the UNKNOWN-OWNERSHIP invariant:
    UNKNOWN OWNERSHIP OF FAILURE != FAILURE OF EVERY REQUIREMENT

Fix: the shared exception is logged in full (with traceback, never suppressed)
and recorded only on the request-local progress object under a sentinel key
that never flows into any requirement's final classification. It is NOT
distributed to any requirement id in ``failures``. A genuinely failed
requirement is still marked failed by its OWN dedicated acquisition
observation (RepositoryResearchReadinessAdapter's per-requirement persisted
observation and deep_investigation._finalize's per-requirement
budget.requirement_failures), which run before/after this shared call.

These tests construct a minimal ``ExistingResearchCapabilityExecutor`` whose
repository's ``refresh_targeted_categories`` raises, then assert the
attribution invariant: the shared exception must NOT be copied to
SHAREHOLDING (or any participant), while genuine per-requirement failures and
the accepted EVIDENCE_INSUFFICIENT_WITHIN_PLAN verdict are preserved.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

import pytest

from app.research_readiness_runtime import (
    CapabilityExecutionProgress,
    ExistingResearchCapabilityExecutor,
)
from app.research_readiness import ResearchRequirementRegistry
from app.failure_taxonomy import (
    EVIDENCE_UNAVAILABLE,
    TECHNICAL_RETRYABLE,
    classify_reason,
    classify_requirement_failures,
)

import tests.test_research_readiness_runtime as _rt
from tests.test_research_readiness_runtime import (
    INSTRUMENT_ID,
    RecordingOrchestrator,
    RecordingPopulation,
    _capability_executor,
    _profile,
    _target,
)


class _RaisingRepository(_rt.RecordingTargetRepository):
    """A repository whose shared refresh_targeted_categories raises, so we can
    observe how execute_primary attributes (or does not attribute) the shared
    exception to individual requirements."""

    def __init__(self, profile, raise_exc, record_categories=True):
        super().__init__(profile)
        self.raise_exc = raise_exc
        self.record_categories = record_categories

    async def refresh_targeted_categories(self, _instrument_id, categories, **_kwargs):
        if self.record_categories:
            # Mirror the real base class so we can assert which categories the
            # shared call was asked to service (proves SHAREHOLDING_PPATTERN was
            # part of the fused set while SHAREHOLDING still must not inherit).
            self.category_calls.append(set(categories))
        raise self.raise_exc


def _executor_with_raising_repo(raise_exc):
    profile = _profile()
    repo = _RaisingRepository(profile, raise_exc)
    orchestrator = RecordingOrchestrator()
    jobs = SimpleNamespace(population=RecordingPopulation(), settings=repo.settings)
    return ExistingResearchCapabilityExecutor(repo, orchestrator, jobs), repo


SEARCH_DEGRADED = RuntimeError("SEARCH_PROVIDER_DEGRADED")


@pytest.mark.asyncio
async def test_shared_search_exception_does_not_become_shareholding_failure():
    """Root Cause A / attribution gap: a SEARCH_PROVIDER_DEGRADED raised from
    the shared refresh (servicing SHAREHOLDING_PATTERN fused with catalyst/
    governance categories) must NOT become SHAREHOLDING: <ExcName>."""
    executor, repo = _executor_with_raising_repo(SEARCH_DEGRADED)
    result = await executor.execute_primary(
        INSTRUMENT_ID,
        [_target("SHAREHOLDING"), _target("ORDER_BOOK_CAPEX_GUIDANCE"),
         _target("GOVERNANCE_HISTORY")],
        jurisdiction="INDIA", correlation_id=None, identity_headers=None,
    )
    # The shared call was still attempted over the fused category set (the
    # union of ORDER_BOOK_CAPEX_GUIDANCE concept categories + GOVERNANCE_HISTORY
    # governance categories + SHAREHOLDING_PATTERN), proving SHAREHOLDING_PATTERN
    # was part of the fused set while SHAREHOLDING still must not inherit.
    assert repo.category_calls == [{"SHAREHOLDING_PATTERN", "CAPEX", "CONTRACTS",
                                    "GUIDANCE", "MANAGEMENT", "NEW_FACILITIES",
                                    "ORDERS_BACKLOG", "REGULATORY", "RISKS"}]
    # CRITICAL: no participant requirement inherits the shared exception.
    assert "SHAREHOLDING" not in result.failures
    assert "ORDER_BOOK_CAPEX_GUIDANCE" not in result.failures
    assert "GOVERNANCE_HISTORY" not in result.failures
    # The shared exception is NOT suppressed into a requirement; it is recorded
    # only as an unattributed diagnostic (observable via logging).
    assert "SEARCH_PROVIDER_DEGRADED" not in result.failures.values()


@pytest.mark.asyncio
async def test_shared_exception_does_not_become_current_news_failure():
    """A shared refresh exception must NOT be copied to CURRENT_NEWS either --
    CURRENT_NEWS/search must never contaminate financials or SHAREHOLDING, and
    conversely a shared outge must not contaminate CURRENT_NEWS."""
    executor, repo = _executor_with_raising_repo(RuntimeError("SEARCH_PROVIDER_DEGRADED"))
    result = await executor.execute_primary(
        INSTRUMENT_ID,
        [_target("CURRENT_NEWS"), _target("GROWTH_FACTS"), _target("SHAREHOLDING")],
        jurisdiction="INDIA", correlation_id=None, identity_headers=None,
    )
    assert "CURRENT_NEWS" not in result.failures
    assert "GROWTH_FACTS" not in result.failures
    assert "SHAREHOLDING" not in result.failures
    assert "SEARCH_PROVIDER_DEGRADED" not in result.failures.values()


@pytest.mark.asyncio
async def test_genuine_shareholding_specific_failure_still_attributed():
    """A failure that genuinely belongs to SHAREHOLDING (recorded on the
    request-local progress object by a per-requirement path -- here simulated
    by a caller that already recorded a SHAREHOLDING-specific failure before
    the shared refresh runs) must be PRESERVED, not overwritten or clobbered by
    the shared-exception handler. The micro-fix only stops the SHARED exception
    from being COPIED to SHAREHOLDING; it never clears a genuinely-owned
    per-requirement failure.

    (The dedicated SHAREHOLDING channel -- the NSE structured/XBRL feed and the
    NSE official-document fallback -- is exercised end-to-end by
    test_slice2_shareholding_nse_first_authority.py: SHAREHOLDING final
    aggregate = EVIDENCE_INSUFFICIENT_WITHIN_PLAN -> EVIDENCE_UNAVAILABLE,
    never leaked EXTERNAL_CAPABILITY_UNSUPPORTED. Here we pin the narrower
    micro-fix invariant: a real per-requirement SHAREHOLDING failure recorded
    via progress survives the shared-refresh exception path.)"""
    progress = CapabilityExecutionProgress()
    # Simulate a genuine per-requirement SHAREHOLDING failure recorded by its
    # own dedicated NSE shareholding/feed layer BEFORE the (failing) shared
    # refresh runs.
    progress.failed("SHAREHOLDING", "EVIDENCE_INSUFFICIENT_WITHIN_PLAN")
    executor, repo = _executor_with_raising_repo(SEARCH_DEGRADED)
    result = await executor.execute_primary(
        INSTRUMENT_ID,
        [_target("SHAREHOLDING"), _target("ORDER_BOOK_CAPEX_GUIDANCE")],
        jurisdiction="INDIA", correlation_id=None, identity_headers=None,
        progress=progress,
    )
    # The genuinely-owned SHAREHOLDING failure is preserved on progress.
    assert "SHAREHOLDING" in progress.failures
    assert progress.failures["SHAREHOLDING"] == "EVIDENCE_INSUFFICIENT_WITHIN_PLAN"
    # And the shared exception did NOT overwrite it with the exception class.
    assert progress.failures["SHAREHOLDING"] != "RuntimeError"
    assert progress.failures["SHAREHOLDING"] != "SEARCH_PROVIDER_DEGRADED"
    # The shared exception is also recorded, but only under the sentinel.
    assert "UNATTRIBUTED_SHARED_EXECUTION_FAILURE" in progress.failures


@pytest.mark.asyncio
async def test_genuine_financial_specific_failure_still_attributed():
    """A genuine per-financial failure must still stick on the financial
    requirement. We verify via the accepted taxonomy: a financial requirement
    whose only blocking failure is the permanent
    PRIMARY_FINANCIAL_PROVIDER_RETURNED_NO_FACTS classifies non-retryable
    (Root Cause A), and a genuine NETWORK_TIMEOUT stays technical/retryable.
    The micro-fix must not flatten these into an unattributed blob."""
    # Permanent financial verdict -> non-retryable (no repair).
    assert classify_reason("PRIMARY_FINANCIAL_PROVIDER_RETURNED_NO_FACTS") == EVIDENCE_UNAVAILABLE
    result = classify_requirement_failures(
        {"QUARTERLY_FINANCIALS": "PRIMARY_FINANCIAL_PROVIDER_RETURNED_NO_FACTS"},
        ("QUARTERLY_FINANCIALS",),
    )
    assert result == EVIDENCE_UNAVAILABLE
    # Genuine transient -> retryable (repair kept).
    assert classify_reason("NETWORK_TIMEOUT") == TECHNICAL_RETRYABLE


@pytest.mark.asyncio
async def test_unattributed_shared_exception_is_not_copied_to_all_requirements():
    """The shared exception must NOT appear under any requirement id in the
    returned failures mapping. The whole point of the fix: UNKNOWN OWNERSHIP
    must not become a failure of EVERY requirement."""
    executor, repo = _executor_with_raising_repo(RuntimeError("SEARCH_PROVIDER_DEGRADED"))
    result = await executor.execute_primary(
        INSTRUMENT_ID,
        [_target("SHAREHOLDING"), _target("ORDER_BOOK_CAPEX_GUIDANCE"),
         _target("GOVERNANCE_HISTORY"), _target("QUARTERLY_FINANCIALS"),
         _target("CURRENT_NEWS")],
        jurisdiction="INDIA", correlation_id=None, identity_headers=None,
    )
    # No requirement id in the result carries the shared exception.
    for key, value in result.failures.items():
        assert value != "SEARCH_PROVIDER_DEGRADED"
        assert "SEARCH_PROVIDER_DEGRADED" not in str(value)
        # The only sentinel that may appear is the unattributed one, and only
        # if progress was used locally; execute_primary's result.failures dict
        # must not contain it (it is progress-only).
        assert key != "UNATTRIBUTED_SHARED_EXECUTION_FAILURE"


@pytest.mark.asyncio
async def test_shareholding_evidence_insufficient_within_plan_preserved():
    """Preservation #6: the accepted SHAREHOLDING path-B final aggregate
    (EVIDENCE_INSUFFICIENT_WITHIN_PLAN -> EVIDENCE_UNAVAILABLE) is unchanged by
    the attribution-gap micro-fix."""
    assert classify_reason("EVIDENCE_INSUFFICIENT_WITHIN_PLAN") == EVIDENCE_UNAVAILABLE
    assert classify_requirement_failures(
        {"SHAREHOLDING": "EVIDENCE_INSUFFICIENT_WITHIN_PLAN"}, ("SHAREHOLDING",)
    ) == EVIDENCE_UNAVAILABLE


@pytest.mark.asyncio
async def test_durable_permanent_financial_verdict_prevents_repair_preserved():
    """Preservation #7: the durable permanent financial verdict
    (PRIMARY_FINANCIAL_PROVIDER_RETURNED_NO_FACTS) still prevents repair --
    the micro-fix must not reintroduce broad attribution that would mark a
    plan-exhausted financial requirement as TECHNICAL_RETRYABLE."""
    assert classify_reason("PRIMARY_FINANCIAL_PROVIDER_RETURNED_NO_FACTS") == EVIDENCE_UNAVAILABLE
    # A candidate whose only blockers are permanent financial evidence gaps is
    # NOT retryable -> no repair.
    result = classify_requirement_failures(
        {
            "QUARTERLY_FINANCIALS": "PRIMARY_FINANCIAL_PROVIDER_RETURNED_NO_FACTS",
            "GROWTH_FACTS": "PRIMARY_FINANCIAL_PROVIDER_RETURNED_NO_FACTS",
            "SHAREHOLDING": "EVIDENCE_INSUFFICIENT_WITHIN_PLAN",
        },
        ("QUARTERLY_FINANCIALS", "GROWTH_FACTS", "SHAREHOLDING"),
    )
    assert result == EVIDENCE_UNAVAILABLE


@pytest.mark.asyncio
async def test_genuine_transient_network_timeout_remains_retryable_preserved():
    """Preservation #8: a genuine NETWORK_TIMEOUT (Root Cause B) remains
    TECHNICAL_RETRYABLE so repair still attempts it. The micro-fix must not
    flatten it to an unattributed blob or to permanent."""
    assert classify_reason("NETWORK_TIMEOUT") == TECHNICAL_RETRYABLE
    assert classify_reason("OFFICIAL_FILING_FETCH_FAILED:NETWORK_TIMEOUT") == TECHNICAL_RETRYABLE
    result = classify_requirement_failures(
        {"QUARTERLY_FINANCIALS": "OFFICIAL_FILING_FETCH_FAILED:NETWORK_TIMEOUT"},
        ("QUARTERLY_FINANCIALS",),
    )
    assert result == TECHNICAL_RETRYABLE


@pytest.mark.asyncio
async def test_unattributed_sentinel_recorded_on_progress_only():
    """The shared exception is recorded on the request-local progress object
    under a non-requirement sentinel key -- proving it is observed (NOT
    suppressed) but cannot contaminate any requirement's final classification."""
    progress = CapabilityExecutionProgress()
    executor, repo = _executor_with_raising_repo(SEARCH_DEGRADED)
    await executor.execute_primary(
        INSTRUMENT_ID,
        [_target("SHAREHOLDING")],
        jurisdiction="INDIA", correlation_id=None, identity_headers=None,
        progress=progress,
    )
    # The sentinel is the only failure recorded -- under a non-requirement key.
    assert "UNATTRIBUTED_SHARED_EXECUTION_FAILURE" in progress.failures
    assert progress.failures["UNATTRIBUTED_SHARED_EXECUTION_FAILURE"] == "RuntimeError" or \
        progress.failures["UNATTRIBUTED_SHARED_EXECUTION_FAILURE"] == type(SEARCH_DEGRADED).__name__
    # And SHAREHOLDING must NOT be marked failed by the shared exception.
    assert "SHAREHOLDING" not in progress.failures


@pytest.mark.asyncio
async def test_no_shared_exception_leaves_failures_empty():
    """Baseline/control: when the shared refresh succeeds, no failures are
    recorded at all from this path -- the new tests' negative assertions are
    meaningful because the success path is clean."""
    executor, repo, *_ = _capability_executor()
    result = await executor.execute_primary(
        INSTRUMENT_ID,
        [_target("SHAREHOLDING"), _target("ORDER_BOOK_CAPEX_GUIDANCE")],
        jurisdiction="INDIA", correlation_id=None, identity_headers=None,
    )
    assert result.failures == {}
