"""Requirement-scoped Radar investigation, reusing the durable readiness contract.

Budgets bound work, never establish readiness. Context propagation follows the
owned acquisition task; unrelated interactive/baseline work has no such scope.
"""
import asyncio
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
from typing import Awaitable, Callable
from uuid import UUID

from app.research_readiness import ResearchRequirementStatus as Status
from app import cycle_timing
from app.failure_taxonomy import EVIDENCE_UNAVAILABLE, classify_reason

logger = logging.getLogger(__name__)


def deferred_investigation():
    """Scheduling state only: never a provider observation or evidence of absence."""
    from app.research_readiness import ResearchRequirementRegistry
    return {
        "rule_evaluated": False, "disposition": "DEFERRED",
        "requirements": {row.requirement_id: {"acquisition_state": "DEFERRED", "attempted": False}
                         for row in ResearchRequirementRegistry.default().requirements},
    }


@dataclass(frozen=True)
class DeepInvestigationPlan:
    instrument_id: UUID
    nomination_paths: tuple[str, ...]
    company_context: tuple[tuple[str, str], ...]
    applicable_rule_families: tuple[str, ...]
    required_evidence: tuple[str, ...]
    optional_evidence: tuple[str, ...]
    not_applicable_evidence: tuple[str, ...]
    already_satisfied: tuple[str, ...]
    acquisition_needed: tuple[str, ...]
    deferred_evidence: tuple[str, ...] = ()


def build_plan(readiness, paths=(), company_context=None, *, deep_selected=True):
    """V1 has one shared rule family; optional concepts do not block its gate."""
    required = tuple(row.requirement_id for row in readiness.requirements if row.mandatory)
    optional = tuple(row.requirement_id for row in readiness.requirements if not row.mandatory)
    na = tuple(row.requirement_id for row in readiness.requirements if row.status == Status.NOT_APPLICABLE)
    excluded_concepts = tuple(f'{row.requirement_id}/{key}' for row in readiness.requirements
                              for key in sorted(row.not_applicable_input_reasons))
    satisfied = tuple(row.requirement_id for row in readiness.requirements if row.status == Status.READY_FRESH)
    # The rule engine requires applicable catalyst coverage even without an
    # event nomination. A genuine completed empty check is valid; an unattempted
    # check is not. Before selection these costs are explicitly deferred.
    selected = set(required) | {"ORDER_BOOK_CAPEX_GUIDANCE"}
    deferred = ()
    if not deep_selected:
        from app.research_readiness_runtime import BASELINE_REQUIREMENT_IDS
        deferred = tuple(row.requirement_id for row in readiness.requirements
                         if row.requirement_id not in BASELINE_REQUIREMENT_IDS
                         and row.requirement_id not in (*na, *satisfied))
        selected &= set(BASELINE_REQUIREMENT_IDS)
    needed = tuple(row.requirement_id for row in readiness.requirements
                   if row.requirement_id in selected and row.requirement_id not in (*na, *satisfied))
    context = {str(k): str(v) for k, v in (company_context or {}).items() if v is not None}
    for row in readiness.requirements:
        if row.classification:
            context.setdefault('classification', row.classification)
            context.setdefault('classification_source', row.classification_source or 'UNKNOWN')
    return DeepInvestigationPlan(readiness.global_instrument_id, tuple(sorted(paths)), tuple(sorted(context.items())),
        ("STOCK_RULE_ENGINE_V1",), required, optional, (*na, *excluded_concepts), satisfied, needed, deferred)


# Singleton per-requirement acquisition defaults (pre-batching model). Each
# requirement acquired on its own previously received exactly these allowances
# for its single shared capability/discovery pass.
_SINGLETON_MAX_DOCUMENTS = 4
_SINGLETON_MAX_QUERIES = 2


@dataclass
class RequirementAcquisitionBudget:
    instrument_id: UUID
    requirement_id: str
    as_of: datetime
    sufficient: Callable[[], Awaitable[bool]]
    max_documents: int = _SINGLETON_MAX_DOCUMENTS
    max_queries: int = _SINGLETON_MAX_QUERIES
    lookback: timedelta | None = None
    documents_attempted: int = 0
    queries_reserved: int = 0
    stopped: bool = False
    exhausted: bool = False
    failures: list[str] = field(default_factory=list)
    requirement_failures: dict[str, str] = field(default_factory=dict)
    # Populated by the acquisition layer after discover() runs: maps category ->
    # number of candidates found.  Used for failure-isolation (distinguishing
    # "no candidates found" from "budget exhausted" / "technical failure").
    discovery_outcome: dict[str, int] = field(default_factory=dict)
    discovery_attempted: bool = False
    # When this budget owns a capability-aligned batch, ``member_requirement_ids``
    # holds the group members and ``per_requirement_max_documents`` /
    # ``per_requirement_max_queries`` record each member's logical singleton
    # share.  The physical ``max_documents`` / ``max_queries`` ceiling is scaled
    # to ``len(members) * singleton_default`` so the ONE shared capability call
    # (execute_primary already services the whole group with a single provider
    # pass) has at least as much total acquisition capacity as the N singleton
    # calls it replaced -- preventing member A's document consumption from
    # starving member B out of B's own evidence opportunity.
    member_requirement_ids: tuple[str, ...] = ()
    per_requirement_max_documents: int = _SINGLETON_MAX_DOCUMENTS
    per_requirement_max_queries: int = _SINGLETON_MAX_QUERIES

    async def allow_document(self):
        if self.stopped or await self.sufficient():
            self.stopped = True
            return False
        if self.documents_attempted >= self.max_documents:
            self.exhausted = True
            return False
        self.documents_attempted += 1
        return True

    def reserve_queries(self, requested):
        allowed = min(requested, self.max_queries - self.queries_reserved)
        self.queries_reserved += allowed
        if allowed == 0:
            self.exhausted = True
        return allowed

    def accepts_date(self, value):
        # Unknown publication dates cannot prove membership in a bounded window.
        return self.lookback is None or (value is not None and
            value.tzinfo is not None and timedelta(0) <= self.as_of - value <= self.lookback)

    def member_exhausted(self, requirement_id: str) -> bool:
        """Whether a specific GROUP member has consumed its own logical
        singleton share of the physical budget.

        Used by _finalize() so that DOCUMENT_BUDGET_EXHAUSTED /
        DISCOVERY_QUERY_BUDGET_EXHAUSTED is attributed truthfully: a member is
        only classified as budget-exhausted when the *shared* pass that serves
        the entire group has genuinely consumed that member's own per-requirement
        allowance (i.e. the group-level ceiling was reached, which means every
        member's share was consumed by the single shared discovery/provider
        pass).  When only the group total is below the scaled ceiling, a member
        is never falsely starved -- it simply has EVIDENCE_INSUFFICIENT_WITHIN_PLAN
        at worst, exactly as a singleton would if its own 4 documents did not
        satisfy it.
        """
        if not self.member_requirement_ids:
            # Singleton budget: exhaustion is simply the global ceiling.
            return self.exhausted
        return self.documents_attempted >= self.max_documents


_scope: ContextVar[RequirementAcquisitionBudget | None] = ContextVar("deep_requirement_budget", default=None)


# Capability-aligned batching (Performance Fix #2): groups of requirement_ids
# that app.research_readiness_runtime.ExistingResearchCapabilityExecutor.
# execute_primary() already services with exactly ONE underlying capability
# call regardless of how many of the group's members are requested together
# (traced directly from execute_primary's own branch structure):
#   - STRUCTURED_MARKET: one orchestrator.ensure_structured_market() call.
#   - FINANCIALS: one refresh_international_fundamentals() call (non-INDIA)
#     or one shared FINANCIAL_RESULTS-category
#     repository.refresh_targeted_categories() call (INDIA).
#   - the targeted-categories group below: each member contributes its own
#     repository categories (SHAREHOLDING_PATTERN / catalyst-concept
#     categories / RISKS+REGULATORY+MANAGEMENT) into ONE shared,
#     single-flight-guarded repository.refresh_targeted_categories() call.
# Submitting a group's members together collapses what were previously N
# sequential, independently-awaited runtime.ensure() calls for the SAME
# underlying operation into one. HISTORICAL_PRICE_SERIES has no group: it
# has its own dedicated _ensure_historical_prices() call, shared with
# nothing else. CURRENT_NEWS is deliberately never grouped: it stays off
# the mandatory blocking path entirely (Performance Fix #1) and must never
# be batched into a blocking mandatory group.
_CAPABILITY_GROUPS: tuple[tuple[str, ...], ...] = (
    ("VALUATION_INPUTS", "LATEST_PRICE", "SECTOR_MACRO"),
    ("BUSINESS_QUALITY_FACTS", "GROWTH_FACTS", "BALANCE_SHEET_FACTS", "QUARTERLY_FINANCIALS"),
    ("ORDER_BOOK_CAPEX_GUIDANCE", "SHAREHOLDING", "GOVERNANCE_HISTORY"),
)
_GROUP_FOR_REQUIREMENT: dict[str, tuple[str, ...]] = {
    requirement_id: group for group in _CAPABILITY_GROUPS for requirement_id in group
}

# Stage-2 final closure (Root Cause A): requirements whose own authorized
# acquisition plan is a dedicated, non-search channel (its OWN failures are
# always recorded per-requirement in budget.requirement_failures -- see
# repository.py's official shareholding feed) and which never themselves
# append to the SHARED budget.failures list. SHAREHOLDING is batched with
# ORDER_BOOK_CAPEX_GUIDANCE/GOVERNANCE_HISTORY above purely for provider-call
# collapsing (Performance Fix #2); those two genuinely share one generic
# search/document-discovery pass and may legitimately attribute a shared
# budget.failures entry to each other, but SHAREHOLDING's ownership path
# (NSE structured/XBRL -> Yahoo -> NSE official document) never runs through
# that generic search mechanism at all, so a budget.failures entry seen by
# _finalize() for SHAREHOLDING can only ever be some OTHER group member's
# unrelated failure (e.g. a generic SEARCH_PROVIDER_* outage). Add a
# requirement id here only when its acquisition is similarly self-contained.
_NO_SHARED_BUDGET_FAILURE_ATTRIBUTION: frozenset[str] = frozenset({"SHAREHOLDING"})

# Budget-exhaustion reason strings are CONSEQUENCES of the per-refresh
# document/query ceiling being consumed, not root-cause failures. They are kept
# here (mirroring failure_taxonomy.TECHNICAL_REASONS) so _finalize() can prefer a
# genuine causal technical failure (e.g. PDF_EXTRACTION_TIMEOUT, NETWORK_TIMEOUT,
# PARSER_FAILED) that was recorded EARLIER in the same acquisition pass over a
# later budget-exhaustion entry that merely records that the shared pass starved
# a subsequent filing. All of these remain TECHNICAL_RETRYABLE (a repair attempt
# gets a fresh per-refresh budget), so this only reorders WHICH technical reason
# is surfaced -- it never reclassifies a genuine technical failure as permanent.
_BUDGET_EXHAUSTION_CONSEQUENCES = frozenset({
    "DOCUMENT_BUDGET_EXHAUSTED",
    "DISCOVERY_QUERY_BUDGET_EXHAUSTED",
    "BUDGET_EXHAUSTED",
})


def _prior_failure(runtime, instrument_id, requirement_id):
    loader = getattr(getattr(runtime, "repository", None), "acquisition_observations_for", None)
    if not callable(loader):
        return None
    rows = [row for row in loader(instrument_id) or ()
            if row.get("requirement_id") == requirement_id and row.get("provider") != "READINESS_EXECUTOR"]
    if not rows:
        return None
    latest = max(rows, key=lambda row: str(row.get("observed_at") or ""))
    if latest.get("outcome") != "FAILED":
        return None
    return str(latest.get("failure_reason") or "PRIOR_ACQUISITION_FAILED")


def _prior_readiness_executor_verdict(runtime, instrument_id, requirement_id):
    """The most recent FINAL classification this module itself already
    reached for `requirement_id` (provider == 'READINESS_EXECUTOR', written
    by _finalize()'s own record_acquisition_observation call below) --
    distinct from _prior_failure/_prior_success_empty, which deliberately
    exclude these rows because they look at genuine PROVIDER attempts.

    Returns the failure_reason only when it already classifies
    EVIDENCE_UNAVAILABLE (deterministic, permanent for that attempt's
    unchanged evidence) -- never for a TECHNICAL_RETRYABLE verdict, which
    must always get a fresh attempt. Used by _requires_acquisition so a
    repair pass does not rerun an identical acquisition for a requirement
    whose last completed plan already proved the gap permanent, while an
    already-satisfied readiness state (checked by the caller BEFORE this)
    still always takes priority -- this only short-circuits the "still
    unresolved" case, on the assumption that a requirement whose status has
    not advanced to satisfied has not had its evidence meaningfully change.
    """
    loader = getattr(getattr(runtime, "repository", None), "acquisition_observations_for", None)
    if not callable(loader):
        return None
    rows = [row for row in loader(instrument_id) or ()
            if row.get("requirement_id") == requirement_id and row.get("provider") == "READINESS_EXECUTOR"]
    if not rows:
        return None
    latest = max(rows, key=lambda row: str(row.get("observed_at") or ""))
    if latest.get("outcome") != "FAILED":
        return None
    reason = latest.get("failure_reason")
    if not reason or classify_reason(reason) != EVIDENCE_UNAVAILABLE:
        return None
    # Issue 2 (production: 0dfbe654-9788-42ae-923c-b1ce0aceaa4b SHAREHOLDING
    # permanently EXTERNAL_CAPABILITY_UNSUPPORTED, with NEITHER
    # shareholding_routing_decision NOR shareholding_final_reason_emitted
    # ever appearing -- proving execute_primary's generic per-target loop
    # never runs again for this requirement). Every OTHER
    # PERMANENT_REASONS entry describes a DOCUMENT this module actually
    # discovered/parsed (content, discovery outcome, parser result) --
    # unchanged evidence genuinely cannot produce a different verdict on
    # retry, so skipping re-dispatch is correct. EXTERNAL_CAPABILITY_
    # UNSUPPORTED is different in kind: it is never decided by this
    # repository at all (see HttpExternalResearchToolGateway.
    # acquire_requirement in app.yahoo_mcp_acquisition) -- it is read
    # verbatim from an EXTERNAL gateway response, and whether that
    # gateway is even CALLED (and with what region) is entirely decided
    # by this instrument's LOCAL jurisdiction/profile computation at
    # execute_primary time. That computation is exactly what two prior
    # stabilization passes in this project corrected (country/exchange
    # hydration fixes to _global_master_instrument /
    # _hydrate_verified_exchange_mappings) -- fixes that can only ever
    # take effect on an instrument's SHAREHOLDING requirement if
    # execute_primary is actually invoked for it again. Treating a
    # pre-fix verdict recorded under what may have been a wrong
    # jurisdiction as a permanent, un-retriable fact would durably lock
    # that instrument out of ever benefiting from a jurisdiction/
    # identity correction. This narrowly excludes ONLY this one reason
    # from _requires_acquisition's cross-cycle skip -- classify_reason
    # and PERMANENT_REASONS themselves are UNCHANGED (every other use of
    # this classification -- repair-budget retryability, candidate-level
    # disposition in app.global_opportunity_orchestration, etc. -- still
    # treats EXTERNAL_CAPABILITY_UNSUPPORTED as EVIDENCE_UNAVAILABLE
    # exactly as before). If the gateway genuinely still declines it on
    # the next attempt, the SAME reason is recorded again and nothing
    # about readiness/success semantics changes -- this only lets a
    # requirement whose last verdict depended on local routing actually
    # get re-evaluated through CURRENT routing instead of being
    # permanently skipped.
    components = {part.strip() for part in str(reason).split("|") if part.strip()}
    if "EXTERNAL_CAPABILITY_UNSUPPORTED" in components:
        return None
    return str(reason)


def _prior_success_empty(runtime, instrument_id, requirement_id):
    # Distinct from _prior_failure: the requirement's latest recorded
    # observation completed (it is not a FAILED attempt) and genuinely found
    # no evidence (SUCCESS_EMPTY), as opposed to no observation existing at
    # all or a technical FAILED attempt. Used only when this cycle attempted
    # nothing (ACQUISITION_NOT_DUE territory) so a prior completed empty
    # check is not misreported as a mere scheduling skip -- see PERMANENT_REASONS
    # in failure_taxonomy.py ("PRIOR_ACQUISITION_EMPTY") for why this must
    # classify EVIDENCE_UNAVAILABLE rather than TECHNICAL_RETRYABLE.
    loader = getattr(getattr(runtime, "repository", None), "acquisition_observations_for", None)
    if not callable(loader):
        return False
    rows = [row for row in loader(instrument_id) or ()
            if row.get("requirement_id") == requirement_id and row.get("provider") != "READINESS_EXECUTOR"]
    if not rows:
        return False
    latest = max(rows, key=lambda row: str(row.get("observed_at") or ""))
    return latest.get("outcome") == "SUCCESS_EMPTY"


def acquisition_budget(instrument_id):
    value = _scope.get()
    return value if value is not None and value.instrument_id == instrument_id else None


async def investigate(runtime, instrument_id, *, jurisdiction, nomination_paths=(), company_context=None,
                      identity_headers=None, correlation_id=None, background_tasks=None):
    """background_tasks: an optional object supporting `.add(task)` (a
    plain `set` -- the CALLER owns it for its own scope's lifetime -- or a
    longer-lived registry such as
    app.background_task_registry.BoundedBackgroundTaskRegistry, whose
    process-lifetime singleton Stage-2 passes so this task can outlive a
    single phase/cycle; see that module's docstring). CURRENT_NEWS is
    already exempt from every mandatory eligibility/ranking gate
    (StockRuleEngineEligibilityPolicy /
    _current_news_is_optional_and_non_blocking) -- when background_tasks is
    given, its acquisition is started concurrently with the mandatory
    requirements below instead of being awaited inline: the task is added to
    background_tasks (a live reference is retained there, never an
    untracked fire-and-forget asyncio.create_task()) so it cannot be
    garbage-collected mid-flight. Whoever owns background_tasks is
    responsible for eventually observing each task's outcome (a plain
    `set`-owning caller drains it directly; a BoundedBackgroundTaskRegistry
    does this itself via a done-callback, so nothing needs to await it) --
    either way the acquisition's outcome, once it completes, is persisted
    via runtime.ensure()'s own record_acquisition_observation call. If
    background_tasks also exposes `has_capacity()` and it returns False,
    CURRENT_NEWS is acquired inline for this call instead of being added
    (bounded backpressure without losing evidence -- see the call site).
    When background_tasks is None (the default, and every call site that
    has not explicitly opted in), CURRENT_NEWS is acquired inline exactly
    as before: fully backward compatible.

    Mandatory requirements that share one underlying acquisition capability
    (see _CAPABILITY_GROUPS) are submitted to runtime.ensure() together in a
    single call instead of one call per requirement -- see _acquire_group.
    Every requirement's own readiness/matrix/failure accounting remains
    fully independent regardless of whether it was acquired solo or as part
    of a group (see _finalize)."""
    from app.research_readiness_runtime import TargetedEnsureResult
    _readiness_load_started = time.monotonic()
    readiness = await runtime.read(instrument_id, jurisdiction=jurisdiction)
    cycle_timing.record_readiness_load_elapsed((time.monotonic() - _readiness_load_started) * 1000)
    plan = build_plan(readiness, nomination_paths, company_context)
    logger.info("deep_plan_created instrument_id=%s families=%s required=%s acquisition=%s",
                instrument_id, plan.applicable_rule_families, plan.required_evidence, plan.acquisition_needed)
    logger.info("radar_acquisition_count operation=pdf_fallback_avoided reason=DURABLE_EVIDENCE count=%d",
                len(set(plan.already_satisfied) & {"QUARTERLY_FINANCIALS", "GROWTH_FACTS",
                    "BUSINESS_QUALITY_FACTS", "BALANCE_SHEET_FACTS", "ORDER_BOOK_CAPEX_GUIDANCE", "GOVERNANCE_HISTORY"}))
    failures, capabilities, attempted = {}, [], []
    matrix = {}
    # Include fresh quarterly evidence so the existing NSE authority-upgrade
    # decision remains authoritative even when secondary data is fresh.
    selected = set(plan.acquisition_needed) | ({"QUARTERLY_FINANCIALS"} & set(plan.required_evidence))

    def _requirement_satisfied_from(readiness, requirement_id):
        # Pure classification: whether THIS specific requirement's own
        # evidence is already satisfied, evaluated against an already-fetched
        # readiness snapshot. Identical logic to the pre-batching per-requirement
        # sufficient() closure (status / source / NOT_APPLICABLE), just taking
        # the snapshot explicitly so the grouped path (_group_sufficient) can
        # check every member against a SINGLE read instead of one read per
        # member. The singleton / finalize path (_requirement_sufficient) below
        # still owns its own fresh read for its independent classification.
        item = readiness.for_requirement(requirement_id)
        if item.status == Status.NOT_APPLICABLE:
            return True
        if item.status != Status.READY_FRESH:
            return False
        if requirement_id == "QUARTERLY_FINANCIALS" and jurisdiction == "INDIA":
            # A fresh secondary source cannot stop an NSE authority upgrade.
            return item.source == "NSE"
        return True

    async def _requirement_sufficient(requirement_id):
        # Whether THIS specific requirement's own evidence is already
        # satisfied, checked fresh against readiness now on file. Identical
        # to the pre-batching per-requirement `sufficient()` closure, just
        # taking requirement_id explicitly so both the singleton path
        # (_acquire) and the grouped path (_acquire_group / _group_sufficient)
        # can reuse it for any member, independently.
        _reload_started = time.monotonic()
        current = await runtime.read(instrument_id, jurisdiction=jurisdiction, evidence_only=True)
        cycle_timing.record_final_readiness_reload_elapsed((time.monotonic() - _reload_started) * 1000)
        return _requirement_satisfied_from(current, requirement_id)

    def _lookback_for(requirement_id, row):
        lookback = row.freshness_policy.maximum_age or row.freshness_policy.scoring_window
        if requirement_id in {"QUARTERLY_FINANCIALS", "GROWTH_FACTS", "BUSINESS_QUALITY_FACTS", "BALANCE_SHEET_FACTS"}:
            # Preserve comparable prior-year quarters and annual histories.
            lookback = timedelta(days=800)
        return lookback

    async def _finalize(requirement_id, budget):
        # Truthful, independent classification for ONE requirement, shared
        # by the singleton and grouped acquisition paths so both classify
        # identically -- extracted unchanged from the pre-batching inline
        # logic. budget may be scoped to just this one requirement
        # (singleton path) or to the whole batch it was acquired alongside
        # (grouped path); either way, THIS requirement's own sufficiency,
        # checked fresh here, is what decides whether it ends up marked
        # failed at all -- a requirement a shared call genuinely satisfied
        # is never marked failed merely because another requirement in the
        # same batch remains outstanding.
        if not await _requirement_sufficient(requirement_id):
            # Distinguish root causes rather than always emitting DOCUMENT_BUDGET_EXHAUSTED.
            # Priority order: specific technical failures > discovery outcome > budget state.
            if requirement_id in budget.requirement_failures:
                reason = budget.requirement_failures[requirement_id]
            elif budget.failures and requirement_id not in _NO_SHARED_BUDGET_FAILURE_ATTRIBUTION:
                # Stage-2 final closure (Root Cause A): budget.failures is a
                # single flat list shared by every member of a capability
                # group (e.g. SHAREHOLDING batched alongside ORDER_BOOK_
                # CAPEX_GUIDANCE/GOVERNANCE_HISTORY, which genuinely share
                # ONE underlying generic search/document-discovery pass).
                # A requirement in _NO_SHARED_BUDGET_FAILURE_ATTRIBUTION has
                # its OWN dedicated, non-search acquisition channel that
                # already reports its own failures through
                # budget.requirement_failures (handled above) and never
                # appends to this shared list itself -- so any entry found
                # here can only have come from a DIFFERENT group member's
                # unrelated operation (e.g. a generic SEARCH_PROVIDER_*
                # failure from GOVERNANCE_HISTORY's RISKS/REGULATORY search).
                # Borrowing it would violate "search/news provider state
                # must never determine SHAREHOLDING readiness" -- skip
                # straight to the discovery-outcome/budget-state
                # classification below, which correctly reports this
                # requirement's OWN plan state instead.
                # Preserve the CAUSAL technical classification: a real attempted
                # failure (NETWORK_TIMEOUT, PDF_EXTRACTION_TIMEOUT, PARSER_FAILED,
                # HTTP_FETCH_FAILED, ...) recorded earlier in this acquisition pass
                # must take priority over a later DOCUMENT_BUDGET_EXHAUSTED /
                # DISCOVERY_QUERY_BUDGET_EXHAUSTED / BUDGET_EXHAUSTED that is merely
                # a CONSEQUIENCE of the per-refresh document/query ceiling being
                # consumed by the same pass (e.g. a PDF extraction timeout on
                # filing A caused the shared budget to starve filing B, which was
                # then recorded as budget-exhausted). Both are technically
                # retryable (budget exhaustion is listed in TECHNICAL_REASONS with
                # an explicit "a repair attempt gets a fresh budget" comment), so
                # this selection only changes WHICH technical reason is surfaced --
                # never the technical-vs-genuine classification -- and it only
                # reorders the recorded list, it never drops a failure. When every
                # recorded failure is itself a budget-exhaustion consequence (i.e.
                # the pass was purely starved with no attempted-failure), fall back
                # to the last failure so genuine pure-starvation cases keep
                # surfacing DOCUMENT_BUDGET_EXHAUSTED as documented at the
                # documents_attempted >= max_documents branch below.
                causal = next(
                    (f for f in budget.failures
                     if f not in _BUDGET_EXHAUSTION_CONSEQUENCES),
                    None,
                )
                reason = causal if causal is not None else budget.failures[-1]
            elif failures.get(requirement_id):
                reason = failures[requirement_id]
            else:
                # No technical failure recorded — classify by what discovery found.
                candidates_found = sum(budget.discovery_outcome.values())
                if not budget.discovery_attempted and budget.documents_attempted == 0 and budget.queries_reserved == 0:
                    # The plan attempted nothing (source not due: cooldown /
                    # backoff). Report the durable prior outcome, never a
                    # "no candidates" claim that nothing verified.
                    prior = _prior_failure(runtime, instrument_id, requirement_id)
                    if prior:
                        reason = f"ACQUISITION_BACKOFF|{prior}"
                    elif _prior_success_empty(runtime, instrument_id, requirement_id):
                        # The last real (non-executor) observation for this
                        # requirement already completed and found nothing;
                        # this cycle simply never re-attempted it (cooldown /
                        # not due). Report the durable empty outcome rather
                        # than a scheduling-only ACQUISITION_NOT_DUE, which
                        # would misclassify genuinely-checked-empty evidence
                        # as technical/retryable.
                        reason = "PRIOR_ACQUISITION_EMPTY"
                    else:
                        reason = "ACQUISITION_NOT_DUE"
                elif candidates_found == 0:
                    # discover() ran but found NO candidates at all for the requested
                    # category — documents were filtered at the discovery boundary.
                    req_category = (
                        "FINANCIAL_RESULTS" if requirement_id == "QUARTERLY_FINANCIALS"
                        else requirement_id
                    )
                    reason = f"DISCOVERY_NO_{req_category}_CANDIDATES" if req_category == "FINANCIAL_RESULTS" else "DISCOVERY_NO_CANDIDATES"
                elif budget.documents_attempted == 0:
                    # Candidates existed but every one was reused/unsupported/skipped
                    # before consuming a document slot (no-waste budget).
                    reason = "DISCOVERY_ALL_CANDIDATES_REUSED_OR_UNSUPPORTED"
                elif budget.documents_attempted >= budget.max_documents:
                    # The shared acquisition pass consumed the ENTIRE scaled
                    # group budget (N * singleton_default). This means every
                    # member's logical singleton share was consumed by the
                    # single shared discovery/provider pass -- the only case
                    # in which a group member is truthfully classified as
                    # budget-exhausted.  (member_exhausted() returns
                    # self.exhausted for singleton budgets and the group-ceiling
                    # check for groups.)
                    reason = "DOCUMENT_BUDGET_EXHAUSTED"
                elif budget.exhausted:
                    reason = "DISCOVERY_QUERY_BUDGET_EXHAUSTED"
                else:
                    reason = "EVIDENCE_INSUFFICIENT_WITHIN_PLAN"
            failures[requirement_id] = reason
            recorder = getattr(runtime.repository, 'record_acquisition_observation', None)
            if callable(recorder):
                await recorder(instrument_id, requirement_id, 'READINESS_EXECUTOR', 'FAILED',
                               datetime.now(timezone.utc), failure_reason=reason)
            if "DISCOVERY_NO" in reason:
                event = "deep_requirement_no_candidates"
            elif "BUDGET_EXHAUSTED" in reason or "QUERY_BUDGET" in reason:
                event = "deep_requirement_budget_exhausted"
            elif "UNAVAILABLE" in reason:
                event = "deep_requirement_source_unavailable"
            elif "REUSED_OR_UNSUPPORTED" in reason:
                event = "deep_requirement_candidates_skipped"
            else:
                event = "deep_requirement_evidence_insufficient"
            logger.info("%s instrument_id=%s requirement=%s reason=%s documents=%d queries=%d discovery=%s",
                        event, instrument_id, requirement_id, reason, budget.documents_attempted,
                        budget.queries_reserved, budget.discovery_outcome)
        else:
            failures.pop(requirement_id, None)
        logger.info("deep_requirement_acquisition_completed instrument_id=%s requirement=%s", instrument_id, requirement_id)

    async def _acquire(requirement_id, row, readiness):
        # Single-requirement acquisition -- used for CURRENT_NEWS's own
        # inline fallback (background_tasks=None) and for any requirement
        # with no shared-capability group (HISTORICAL_PRICE_SERIES today).
        # Wall-clock/blocking semantics are unchanged from before batching.
        budget = RequirementAcquisitionBudget(
            instrument_id, requirement_id, readiness.generated_at,
            lambda rid=requirement_id: _requirement_sufficient(rid),
            lookback=_lookback_for(requirement_id, row),
            max_documents=_SINGLETON_MAX_DOCUMENTS,
            max_queries=_SINGLETON_MAX_QUERIES,
        )
        token = _scope.set(budget)
        logger.info("deep_requirement_acquisition_started instrument_id=%s requirement=%s", instrument_id, requirement_id)
        with cycle_timing.track_requirement(instrument_id, requirement_id):
            try:
                ensure_started = time.monotonic()
                result = await runtime.ensure(instrument_id, jurisdiction=jurisdiction,
                    requirement_ids=(requirement_id,), identity_headers=identity_headers,
                    correlation_id=correlation_id, wait_for_completion=True)
                cycle_timing.record_runtime_ensure_elapsed((time.monotonic() - ensure_started) * 1000)
                attempted.extend(result.planned_requirement_ids)
                capabilities.extend(result.executed_capabilities)
                failures.update(result.failures)
            except Exception as exc:
                cycle_timing.record_runtime_ensure_elapsed((time.monotonic() - ensure_started) * 1000)
                failures[requirement_id] = type(exc).__name__
            finally:
                _scope.reset(token)
            finalize_started = time.monotonic()
            await _finalize(requirement_id, budget)
            cycle_timing.record_finalization_elapsed((time.monotonic() - finalize_started) * 1000)

    async def _acquire_group(requirement_ids, rows, readiness):
        # Several requirements that execute_primary already services with
        # exactly ONE underlying capability call (see _CAPABILITY_GROUPS)
        # are submitted together in ONE runtime.ensure(requirement_ids=(...))
        # call instead of one call per requirement -- collapsing what were
        # previously N sequential, independently-awaited provider/discovery
        # passes for the SAME underlying operation into one.
        #
        # The group shares ONE acquisition budget: its document/query
        # ceiling applies to the batch as a whole, exactly mirroring how a
        # single requirement's own acquisition today already spans multiple
        # documents/categories under one budget (e.g. ORDER_BOOK_CAPEX_
        # GUIDANCE alone already spans several concept categories). This is
        # a deliberate, minimal adaptation: RequirementAcquisitionBudget has
        # no per-member sub-accounting, and the underlying shared call (one
        # discovery/provider pass) has no natural way to divide its own
        # document/query spend by requirement either -- so the budget models
        # the batch's total acquisition cost, not N independent ones.
        #
        # Truthful classification afterward is nonetheless fully independent
        # per requirement: _finalize() re-checks EACH requirement's own
        # sufficiency against the readiness now on file, so a requirement
        # whose evidence the shared call genuinely satisfied is never marked
        # failed just because another requirement in the same batch remains
        # outstanding (proven by the partial-group-failure test). Only a
        # failure that genuinely affects the whole shared call (a raised
        # exception) is attributed to every requirement that depended on it
        # -- identical to how execute_primary's own capability-level
        # exception handling already broadcasts a shared-call failure even
        # for a single requirement today.
        async def _group_sufficient():
            # ONE evidence-only snapshot serves every member: the member loop
            # below is synchronous between member checks (no intervening
            # persistence write), so all members observe the identical durable
            # evidence. This collapses N per-member read()/assess calls into a
            # single read per sufficient() invocation -- a provably-equivalent
            # classification because _requirement_satisfied_from only inspects
            # status/source for a single requirement_id against the snapshot,
            # never mutating state. The generation-based cache invalidation in
            # read() still guarantees a fresh reload whenever a persistence write
            # occurs between two sufficient() invocations, so mutation
            # invalidation is preserved exactly.
            _reload_started = time.monotonic()
            current = await runtime.read(instrument_id, jurisdiction=jurisdiction, evidence_only=True)
            cycle_timing.record_final_readiness_reload_elapsed((time.monotonic() - _reload_started) * 1000)
            for requirement_id in requirement_ids:
                if not _requirement_satisfied_from(current, requirement_id):
                    return False
            return True

        lookback = None
        for requirement_id in requirement_ids:
            member_lookback = _lookback_for(requirement_id, rows[requirement_id])
            if member_lookback is None:
                lookback = None
                break
            lookback = member_lookback if lookback is None else max(lookback, member_lookback)

        label = "+".join(requirement_ids)
        # Scale the SHARED physical budget to len(members) * singleton_default.
        # execute_primary already services the entire group with exactly ONE
        # underlying capability/discovery pass (see _CAPABILITY_GROUPS), so
        # scaling the physical ceiling does NOT multiply provider requests --
        # it only allows the single shared pass to examine up to N× more
        # documents/queries before stopping, preserving each member's singleton
        # evidence opportunity.  Without this scaling, a 4-member group received
        # the same 4-doc / 2-query budget a single singleton previously had,
        # letting one member's consumption starve its siblings into
        # DOCUMENT_BUDGET_EXHAUSTED / QUERY_BUDGET_EXHAUSTED purely due to
        # batching.
        member_count = len(requirement_ids)
        budget = RequirementAcquisitionBudget(
            instrument_id, label, readiness.generated_at,
            _group_sufficient,
            max_documents=_SINGLETON_MAX_DOCUMENTS * member_count,
            max_queries=_SINGLETON_MAX_QUERIES * member_count,
            lookback=lookback,
            member_requirement_ids=tuple(requirement_ids),
            per_requirement_max_documents=_SINGLETON_MAX_DOCUMENTS,
            per_requirement_max_queries=_SINGLETON_MAX_QUERIES,
        )
        token = _scope.set(budget)
        logger.info("deep_requirement_acquisition_started instrument_id=%s requirement=%s", instrument_id, label)
        # One shared underlying call services every member (see the scaling
        # note above), so its elapsed time is attributed to EACH member's
        # own timing record via record_elapsed_on -- mirroring how the
        # group's document/query budget is already shared across members.
        # A single _current_span slot cannot represent several members at
        # once, so members beyond the first (which carries granular
        # network/PDF/persistence sub-metrics as a representative member)
        # only receive the two elapsed totals measured here directly.
        group_recorder = cycle_timing.active_recorder()
        member_records = [group_recorder.start_requirement(instrument_id, requirement_id)
                          for requirement_id in requirement_ids] if group_recorder is not None else []
        try:
            try:
                ensure_started = time.monotonic()
                # Bind the first member's record as the active span while the
                # shared ensure() runs. The group has N members but only ONE
                # _current_span slot, so one representative member carries the
                # granular descendant sub-timing (provider/network/PDF/
                # persistence/discovery) that _record() routes to the active
                # span -- exactly the intent stated in the comment above. The
                # group's totals are still written to EVERY member below via
                # record_elapsed_on(). Without this bind, _current_span is None
                # for all members here, so every descendant recorder silently
                # drops per-requirement attribution while only the cycle
                # aggregate survives -- leaving the requirement records'
                # per-operation fields at 0.0 despite non-zero cycle totals.
                span_token = cycle_timing.bind_span(member_records[0] if member_records else None)
                try:
                    result = await runtime.ensure(instrument_id, jurisdiction=jurisdiction,
                        requirement_ids=tuple(requirement_ids), identity_headers=identity_headers,
                        correlation_id=correlation_id, wait_for_completion=True)
                    ensure_elapsed_ms = (time.monotonic() - ensure_started) * 1000
                    attempted.extend(result.planned_requirement_ids)
                    capabilities.extend(result.executed_capabilities)
                    failures.update(result.failures)
                except Exception as exc:
                    ensure_elapsed_ms = (time.monotonic() - ensure_started) * 1000
                    for requirement_id in requirement_ids:
                        failures[requirement_id] = type(exc).__name__
                finally:
                    cycle_timing.unbind_span(span_token)
            finally:
                _scope.reset(token)
            for record in member_records:
                cycle_timing.record_elapsed_on(record, "runtime_ensure_elapsed_ms", ensure_elapsed_ms)
            for requirement_id, record in zip(requirement_ids, member_records or [None] * len(requirement_ids)):
                finalize_started = time.monotonic()
                await _finalize(requirement_id, budget)
                cycle_timing.record_elapsed_on(record, "finalization_elapsed_ms", (time.monotonic() - finalize_started) * 1000)
        finally:
            if group_recorder is not None:
                for record in member_records:
                    group_recorder.complete_requirement(record)

    async def _acquire_news_in_background(row, readiness):
        # Same acquisition + truthful classification as _acquire, just not
        # awaited inline by the mandatory loop below. The loop-closing
        # catch-up read further down (which gives every OTHER requirement
        # its final truthful matrix state from one fresh readiness read)
        # runs synchronously before this background task is guaranteed to
        # have finished, so it cannot be relied on to capture CURRENT_NEWS's
        # true outcome. This wrapper refreshes matrix["CURRENT_NEWS"] itself,
        # once acquisition actually completes, so the diagnostic is never
        # left permanently showing a stale pre-acquisition snapshot.
        await _acquire("CURRENT_NEWS", row, readiness)
        final = await runtime.read(instrument_id, jurisdiction=jurisdiction)
        final_row = final.for_requirement("CURRENT_NEWS")
        matrix["CURRENT_NEWS"].update(state=str(final_row.status), missing=list(final_row.missing_input_ids),
                                       source=final_row.source, failure=failures.get("CURRENT_NEWS"))

    def _snapshot(requirement_id, row):
        matrix[requirement_id] = {"state": str(row.status), "applicability": row.applicability,
            "applicability_reason": row.applicability_reason, "classification": row.classification,
            "classification_source": row.classification_source,
            "excluded_inputs": dict(row.not_applicable_input_reasons), "missing": list(row.missing_input_ids),
            "source": row.source, "failure": None}

    def _requires_acquisition(requirement_id, row):
        if row.status == Status.NOT_APPLICABLE:
            logger.info("deep_requirement_not_applicable instrument_id=%s requirement=%s reason=%s",
                        instrument_id, requirement_id, row.applicability_reason)
            return False
        if row.status == Status.READY_FRESH and (requirement_id != "QUARTERLY_FINANCIALS"
                or jurisdiction != "INDIA" or (row.source == "NSE" and str(row.source_tier) == "OFFICIAL")):
            logger.info("deep_requirement_satisfied instrument_id=%s requirement=%s", instrument_id, requirement_id)
            return False
        if requirement_id not in selected:
            return False
        # Stage-2 final closure (Root Cause B/D, Invariant 2): this
        # requirement is still unresolved by CURRENT durable evidence (the
        # checks above already re-read it fresh), but a previous completed
        # plan for this exact requirement already proved EVIDENCE_UNAVAILABLE
        # (deterministic, permanent) and nothing has advanced it to
        # satisfied since. Re-running the identical authorized acquisition
        # against unchanged evidence cannot discover a different answer, so
        # a repair pass must not spend budget on it again -- reuse the
        # durable verdict instead. A TECHNICAL_RETRYABLE prior verdict is
        # deliberately NOT short-circuited here: that is exactly the case
        # that must keep getting a fresh attempt.
        prior_permanent = _prior_readiness_executor_verdict(runtime, instrument_id, requirement_id)
        if prior_permanent is not None:
            logger.info(
                "deep_requirement_skipped_permanent_verdict instrument_id=%s requirement=%s reason=%s",
                instrument_id, requirement_id, prior_permanent,
            )
            failures[requirement_id] = prior_permanent
            return False
        return True

    handled: set[str] = set()
    for requirement_id in (*plan.required_evidence, *plan.optional_evidence):
        if requirement_id in handled:
            # Already processed as another dispatching member's batch-mate
            # below (snapshotted, skip-logged, and either acquired or left
            # alone) -- never re-snapshot or re-log the same requirement.
            continue
        readiness = await runtime.read(instrument_id, jurisdiction=jurisdiction)
        row = readiness.for_requirement(requirement_id)
        _snapshot(requirement_id, row)
        handled.add(requirement_id)
        if not _requires_acquisition(requirement_id, row):
            continue
        if requirement_id == "CURRENT_NEWS" and background_tasks is not None:
            # Optional/non-blocking: start concurrently instead of awaiting
            # inline (see the investigate() docstring and
            # _acquire_news_in_background above). background_tasks is owned
            # by the caller for however long it needs to be (a plain
            # per-call `set`, or -- from Stage-2 -- the process-lifetime
            # `app.background_task_registry.current_news_background_tasks`
            # registry; both support `.add(task)`).
            #
            # Bounded backpressure: a registry that also exposes
            # `has_capacity()` (the process-lifetime one does; a plain
            # `set` does not, so this check is a no-op for any caller still
            # passing one) lets us avoid unbounded background growth
            # without ever dropping evidence. At capacity, this instrument's
            # CURRENT_NEWS is simply acquired inline instead -- slower for
            # this one instrument, but still correct, still eventually
            # persisted, and it keeps process-wide concurrent background
            # news acquisition bounded.
            if not getattr(background_tasks, "has_capacity", lambda: True)():
                await _acquire("CURRENT_NEWS", row, readiness)
                continue
            background_tasks.add(asyncio.ensure_future(_acquire_news_in_background(row, readiness)))
            continue
        group = _GROUP_FOR_REQUIREMENT.get(requirement_id)
        if group is None:
            await _acquire(requirement_id, row, readiness)
            continue
        # This is the first member of `group` this loop has reached that
        # still needs acquisition. Look up every OTHER member of the SAME
        # group directly (rather than waiting for the loop's own iteration
        # to reach them later) so they can all be submitted to
        # runtime.ensure() together, in one call, right now.
        member_rows = {requirement_id: row}
        for other_id in group:
            if other_id == requirement_id or other_id in handled:
                continue
            other_readiness = await runtime.read(instrument_id, jurisdiction=jurisdiction)
            other_row = other_readiness.for_requirement(other_id)
            _snapshot(other_id, other_row)
            handled.add(other_id)
            if _requires_acquisition(other_id, other_row):
                member_rows[other_id] = other_row
        await _acquire_group(tuple(member_rows), member_rows, readiness)
    readiness = await runtime.read(instrument_id, jurisdiction=jurisdiction)
    for row in readiness.requirements:
        matrix[row.requirement_id].update(state=str(row.status), missing=list(row.missing_input_ids),
                                         source=row.source, failure=failures.get(row.requirement_id))
    return TargetedEnsureResult(readiness, tuple(dict.fromkeys(attempted)), tuple(dict.fromkeys(capabilities)),
                                failures=failures), plan, matrix
