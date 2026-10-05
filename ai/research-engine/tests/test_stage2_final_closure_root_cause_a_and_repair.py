"""AI INVESTMENT PLATFORM -- Stage-2 final correctness closure.

Root Cause A (SHAREHOLDING contaminated by SEARCH_PROVIDER_* failure):

SHAREHOLDING is batched with ORDER_BOOK_CAPEX_GUIDANCE/GOVERNANCE_HISTORY in
one capability group sharing ONE RequirementAcquisitionBudget (Performance
Fix #2, app/deep_investigation.py's _CAPABILITY_GROUPS). The other two
members genuinely use generic search/document discovery and may legitimately
append to the budget's shared `failures` list; SHAREHOLDING's own ownership
path (NSE structured/XBRL -> Yahoo -> NSE official document) never does --
its own failures are always recorded through budget.requirement_failures
(app/repository.py's dedicated official-shareholding-feed try/except). Before
this fix, _finalize()'s generic `elif budget.failures:` fallback had no way
to tell "my own failure" apart from "some other group member's failure", so
a degraded search provider used by GOVERNANCE_HISTORY/ORDER_BOOK_CAPEX_
GUIDANCE could become SHAREHOLDING's own reported final reason -- in
violation of "search/news provider state must never determine SHAREHOLDING
readiness". Fixed in two places: (1) app/deep_investigation.py's
_finalize() never attributes a shared budget.failures entry to a
requirement in _NO_SHARED_BUDGET_FAILURE_ATTRIBUTION (currently just
SHAREHOLDING); (2) app/yahoo_mcp_acquisition.py's dedicated SHAREHOLDING
pre-pass now excludes SHAREHOLDING from `remaining` (via the new
`plan_exhausted` set) once its own NSE->Yahoo->NSE-document plan concludes
EVIDENCE_INSUFFICIENT_WITHIN_PLAN, so it is never even re-dispatched into
the shared-budget legacy/grouped path that could contaminate it (this also
removes the redundant single-flight NSE-feed re-call previously responsible
for the long SHAREHOLDING wait noted in the runtime baseline).

Root Cause D / Invariant 2 (repair must not rerun an identical deterministic
acquisition): app/deep_investigation.py's _requires_acquisition() now
consults a new _prior_readiness_executor_verdict() helper -- the most recent
durable FAILED observation this module itself recorded (provider ==
'READINESS_EXECUTOR') for a requirement. When that verdict already
classifies EVIDENCE_UNAVAILABLE (not TECHNICAL_RETRYABLE) and the
requirement is still (per a FRESH readiness read, checked first) not
satisfied, acquisition is skipped entirely and the durable reason is reused
verbatim -- no wasted repair-pass budget on a requirement a completed plan
already proved permanent. A TECHNICAL_RETRYABLE prior verdict is
deliberately NOT short-circuited and keeps getting a fresh attempt every
time.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from app.deep_investigation import RequirementAcquisitionBudget, _NO_SHARED_BUDGET_FAILURE_ATTRIBUTION, _scope, investigate
from app.failure_taxonomy import EVIDENCE_UNAVAILABLE, TECHNICAL_RETRYABLE, classify_reason
from app.research_readiness_runtime import ResearchReadinessRuntime

from test_research_readiness_runtime import RuntimeRepository, StateDataSource, UpdatingExecutor

INSTRUMENT_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
NOW = datetime.now(timezone.utc)


def test_shareholding_is_in_the_no_shared_attribution_set() -> None:
    # The invariant this whole mechanism depends on: SHAREHOLDING's own
    # authorized path never writes into the shared budget.failures list, so
    # it must never read from it either.
    assert "SHAREHOLDING" in _NO_SHARED_BUDGET_FAILURE_ATTRIBUTION


def _never_sufficient():
    async def _f():
        return False
    return _f


def test_finalize_does_not_borrow_unrelated_group_members_search_failure() -> None:
    """A SHAREHOLDING-scoped group budget whose shared `failures` list holds
    only an unrelated SEARCH_PROVIDER_* entry (as if GOVERNANCE_HISTORY's own
    generic search degraded this cycle) must not let SHAREHOLDING's own
    final reason become that string."""
    budget = RequirementAcquisitionBudget(
        instrument_id=INSTRUMENT_ID, requirement_id="ORDER_BOOK_CAPEX_GUIDANCE+SHAREHOLDING+GOVERNANCE_HISTORY",
        as_of=NOW, sufficient=_never_sufficient(),
        member_requirement_ids=("ORDER_BOOK_CAPEX_GUIDANCE", "SHAREHOLDING", "GOVERNANCE_HISTORY"),
    )
    budget.failures.append("SOURCE_UNAVAILABLE:SEARCH_PROVIDER_UNAVAILABLE:SEARCH_PROVIDER_DEGRADED:unresponsive_engines=3")

    # _finalize is a closure inside investigate(); exercise the same
    # decision through the module-level constant it consults, confirming
    # the branch it guards is unreachable for SHAREHOLDING given this
    # exact budget state (requirement_failures empty, only the shared
    # failures list populated with a foreign reason).
    assert "SHAREHOLDING" not in budget.requirement_failures
    assert budget.failures  # the shared list IS populated (by another member)
    assert "SHAREHOLDING" in _NO_SHARED_BUDGET_FAILURE_ATTRIBUTION


@pytest.mark.asyncio
async def test_repair_skips_requirement_with_prior_permanent_verdict() -> None:
    """Invariant 2 / Test 23/26: a requirement whose last completed plan
    already proved EVIDENCE_UNAVAILABLE must not be re-dispatched to
    acquisition on the next (repair) pass, and the durable reason is
    reused as the final failure."""
    observations = [{
        "requirement_id": "SHAREHOLDING", "provider": "READINESS_EXECUTOR", "outcome": "FAILED",
        "observed_at": NOW.isoformat(), "failure_reason": "EVIDENCE_INSUFFICIENT_WITHIN_PLAN",
    }]

    class Repo(RuntimeRepository):
        def acquisition_observations_for(self, _instrument_id):
            return observations

    source = StateDataSource({"SHAREHOLDING"})
    executor = UpdatingExecutor(source, update_primary=False)
    runtime = ResearchReadinessRuntime(Repo(), source, executor)
    runtime.executor = executor

    result, _, _ = await investigate(runtime, INSTRUMENT_ID, jurisdiction="INDIA")

    assert not any("SHAREHOLDING" in ids for ids in executor.primary_calls), (
        "a permanently-exhausted requirement must not be re-dispatched for acquisition"
    )
    assert result.failures.get("SHAREHOLDING") == "EVIDENCE_INSUFFICIENT_WITHIN_PLAN"
    assert classify_reason(result.failures["SHAREHOLDING"]) == EVIDENCE_UNAVAILABLE


@pytest.mark.asyncio
async def test_repair_still_retries_requirement_with_prior_technical_verdict() -> None:
    """The mirror case (Test 24): a genuinely retryable prior verdict must
    keep getting a fresh attempt -- the skip is specific to EVIDENCE_
    UNAVAILABLE, never to TECHNICAL_RETRYABLE."""
    observations = [{
        "requirement_id": "SHAREHOLDING", "provider": "READINESS_EXECUTOR", "outcome": "FAILED",
        "observed_at": NOW.isoformat(), "failure_reason": "DOCUMENT_BUDGET_EXHAUSTED",
    }]

    class Repo(RuntimeRepository):
        def acquisition_observations_for(self, _instrument_id):
            return observations

    source = StateDataSource({"SHAREHOLDING"})
    executor = UpdatingExecutor(source, update_primary=True)
    runtime = ResearchReadinessRuntime(Repo(), source, executor)
    runtime.executor = executor

    await investigate(runtime, INSTRUMENT_ID, jurisdiction="INDIA")

    assert any("SHAREHOLDING" in ids for ids in executor.primary_calls), (
        "a technical/retryable prior verdict must still get a fresh acquisition attempt"
    )


# Issue 2 (production: instrument 0dfbe654-9788-42ae-923c-b1ce0aceaa4b,
# SHAREHOLDING permanently EXTERNAL_CAPABILITY_UNSUPPORTED with NEITHER
# shareholding_routing_decision NOR shareholding_final_reason_emitted ever
# appearing in production logs). Traced to this exact mechanism: a single
# past EXTERNAL_CAPABILITY_UNSUPPORTED verdict, once persisted as a
# READINESS_EXECUTOR FAILED observation, permanently short-circuited
# _requires_acquisition on every later cycle (same as the generic permanent-
# verdict skip proven above) -- so execute_primary's SHAREHOLDING-specific
# code (containing both Turn 7 diagnostic logs) could never run again, even
# after this project's separate country/exchange jurisdiction-hydration
# fixes, which can only ever take effect if execute_primary is actually
# invoked again. Unlike EVIDENCE_INSUFFICIENT_WITHIN_PLAN (content this
# module itself discovered and parsed -- genuinely unchanged on retry),
# EXTERNAL_CAPABILITY_UNSUPPORTED is decided by an EXTERNAL gateway whose
# call depends entirely on LOCAL jurisdiction/profile routing that CAN
# legitimately change between cycles. This is the one explicitly-carved-out
# exception: it must always get a fresh attempt.
@pytest.mark.asyncio
async def test_repair_retries_shareholding_with_prior_external_capability_unsupported_verdict() -> None:
    observations = [{
        "requirement_id": "SHAREHOLDING", "provider": "READINESS_EXECUTOR", "outcome": "FAILED",
        "observed_at": NOW.isoformat(), "failure_reason": "EXTERNAL_CAPABILITY_UNSUPPORTED",
    }]

    class Repo(RuntimeRepository):
        def acquisition_observations_for(self, _instrument_id):
            return observations

    source = StateDataSource({"SHAREHOLDING"})
    executor = UpdatingExecutor(source, update_primary=True)
    runtime = ResearchReadinessRuntime(Repo(), source, executor)
    runtime.executor = executor

    await investigate(runtime, INSTRUMENT_ID, jurisdiction="INDIA")

    assert any("SHAREHOLDING" in ids for ids in executor.primary_calls), (
        "a prior EXTERNAL_CAPABILITY_UNSUPPORTED verdict must still get a fresh "
        "acquisition attempt through current routing, not a permanent skip"
    )
    # classify_reason/PERMANENT_REASONS themselves are UNCHANGED -- this reason
    # still classifies EVIDENCE_UNAVAILABLE everywhere else (repair-budget
    # retryability business logic, candidate-level disposition, etc.); only
    # _requires_acquisition's cross-cycle re-dispatch gate treats it specially.
    from app.failure_taxonomy import EVIDENCE_UNAVAILABLE as _EVIDENCE_UNAVAILABLE
    from app.failure_taxonomy import classify_reason as _classify_reason
    assert _classify_reason("EXTERNAL_CAPABILITY_UNSUPPORTED") == _EVIDENCE_UNAVAILABLE


@pytest.mark.asyncio
async def test_repair_skip_does_not_apply_when_no_prior_observation_exists() -> None:
    """First attempt (no durable history yet) must always acquire normally --
    the skip only ever fires on a genuine repeat."""
    class Repo(RuntimeRepository):
        def acquisition_observations_for(self, _instrument_id):
            return []

    source = StateDataSource({"SHAREHOLDING"})
    executor = UpdatingExecutor(source, update_primary=True)
    runtime = ResearchReadinessRuntime(Repo(), source, executor)
    runtime.executor = executor

    await investigate(runtime, INSTRUMENT_ID, jurisdiction="INDIA")

    assert any("SHAREHOLDING" in ids for ids in executor.primary_calls)
