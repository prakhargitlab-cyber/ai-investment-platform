"""Requirement-scoped Radar investigation, reusing the durable readiness contract.

Budgets bound work, never establish readiness. Context propagation follows the
owned acquisition task; unrelated interactive/baseline work has no such scope.
"""
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
from typing import Awaitable, Callable
from uuid import UUID

from app.research_readiness import ResearchRequirementStatus as Status

logger = logging.getLogger(__name__)


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


def build_plan(readiness, paths=(), company_context=None):
    """V1 has one shared rule family; optional concepts do not block its gate."""
    required = tuple(row.requirement_id for row in readiness.requirements if row.mandatory)
    optional = tuple(row.requirement_id for row in readiness.requirements if not row.mandatory)
    na = tuple(row.requirement_id for row in readiness.requirements if row.status == Status.NOT_APPLICABLE)
    excluded_concepts = tuple(f'{row.requirement_id}/{key}' for row in readiness.requirements
                              for key in sorted(row.not_applicable_input_reasons))
    satisfied = tuple(row.requirement_id for row in readiness.requirements if row.status == Status.READY_FRESH)
    # Catalyst research is relevant to an observed catalyst nomination, rather
    # than a compulsory sweep for every stock. Other optional evidence is reused.
    selected = set(required)
    if "EVENT_CATALYST" in paths:
        selected.add("ORDER_BOOK_CAPEX_GUIDANCE")
    needed = tuple(row.requirement_id for row in readiness.requirements
                   if row.requirement_id in selected and row.requirement_id not in (*na, *satisfied))
    context = {str(k): str(v) for k, v in (company_context or {}).items() if v is not None}
    for row in readiness.requirements:
        if row.classification:
            context.setdefault('classification', row.classification)
            context.setdefault('classification_source', row.classification_source or 'UNKNOWN')
    return DeepInvestigationPlan(readiness.global_instrument_id, tuple(sorted(paths)), tuple(sorted(context.items())),
        ("STOCK_RULE_ENGINE_V1",), required, optional, (*na, *excluded_concepts), satisfied, needed)


@dataclass
class RequirementAcquisitionBudget:
    instrument_id: UUID
    requirement_id: str
    as_of: datetime
    sufficient: Callable[[], Awaitable[bool]]
    max_documents: int = 4
    max_queries: int = 2
    lookback: timedelta | None = None
    documents_attempted: int = 0
    queries_reserved: int = 0
    stopped: bool = False
    exhausted: bool = False
    failures: list[str] = field(default_factory=list)
    # Populated by the acquisition layer after discover() runs: maps category ->
    # number of candidates found.  Used for failure-isolation (distinguishing
    # "no candidates found" from "budget exhausted" / "technical failure").
    discovery_outcome: dict[str, int] = field(default_factory=dict)
    discovery_attempted: bool = False

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


_scope: ContextVar[RequirementAcquisitionBudget | None] = ContextVar("deep_requirement_budget", default=None)


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


def acquisition_budget(instrument_id):
    value = _scope.get()
    return value if value is not None and value.instrument_id == instrument_id else None


async def investigate(runtime, instrument_id, *, jurisdiction, nomination_paths=(), company_context=None,
                      identity_headers=None, correlation_id=None):
    from app.research_readiness_runtime import TargetedEnsureResult
    readiness = await runtime.read(instrument_id, jurisdiction=jurisdiction)
    plan = build_plan(readiness, nomination_paths, company_context)
    logger.info("deep_plan_created instrument_id=%s families=%s required=%s acquisition=%s",
                instrument_id, plan.applicable_rule_families, plan.required_evidence, plan.acquisition_needed)
    failures, capabilities, attempted = {}, [], []
    matrix = {}
    # Include fresh quarterly evidence so the existing NSE authority-upgrade
    # decision remains authoritative even when secondary data is fresh.
    selected = set(plan.acquisition_needed) | ({"QUARTERLY_FINANCIALS"} & set(plan.required_evidence))
    for requirement_id in (*plan.required_evidence, *plan.optional_evidence):
        readiness = await runtime.read(instrument_id, jurisdiction=jurisdiction)
        row = readiness.for_requirement(requirement_id)
        matrix[requirement_id] = {"state": str(row.status), "applicability": row.applicability,
            "applicability_reason": row.applicability_reason, "classification": row.classification,
            "classification_source": row.classification_source,
            "excluded_inputs": dict(row.not_applicable_input_reasons), "missing": list(row.missing_input_ids),
            "source": row.source, "failure": None}
        if row.status == Status.NOT_APPLICABLE:
            logger.info("deep_requirement_not_applicable instrument_id=%s requirement=%s reason=%s",
                        instrument_id, requirement_id, row.applicability_reason)
            continue
        if row.status == Status.READY_FRESH and (requirement_id != "QUARTERLY_FINANCIALS"
                or jurisdiction != "INDIA" or (row.source == "NSE" and str(row.source_tier) == "OFFICIAL")):
            logger.info("deep_requirement_satisfied instrument_id=%s requirement=%s", instrument_id, requirement_id)
            continue
        if requirement_id not in selected:
            continue

        async def sufficient(key=requirement_id):
            current = await runtime.read(instrument_id, jurisdiction=jurisdiction, evidence_only=True)
            item = current.for_requirement(key)
            if item.status == Status.NOT_APPLICABLE:
                return True
            if item.status != Status.READY_FRESH:
                return False
            if key == "QUARTERLY_FINANCIALS" and jurisdiction == "INDIA":
                # A fresh secondary source cannot stop an NSE authority upgrade.
                return item.source == "NSE"
            return True

        lookback = row.freshness_policy.maximum_age or row.freshness_policy.scoring_window
        if requirement_id in {"QUARTERLY_FINANCIALS", "GROWTH_FACTS", "BUSINESS_QUALITY_FACTS", "BALANCE_SHEET_FACTS"}:
            # Preserve comparable prior-year quarters and annual histories.
            lookback = timedelta(days=800)
        budget = RequirementAcquisitionBudget(instrument_id, requirement_id, readiness.generated_at,
                                               sufficient, lookback=lookback)
        token = _scope.set(budget)
        logger.info("deep_requirement_acquisition_started instrument_id=%s requirement=%s", instrument_id, requirement_id)
        try:
            result = await runtime.ensure(instrument_id, jurisdiction=jurisdiction,
                requirement_ids=(requirement_id,), identity_headers=identity_headers,
                correlation_id=correlation_id, wait_for_completion=True)
            attempted.extend(result.planned_requirement_ids)
            capabilities.extend(result.executed_capabilities)
            failures.update(result.failures)
        except Exception as exc:
            failures[requirement_id] = type(exc).__name__
        finally:
            _scope.reset(token)
        if not await sufficient():
            # Distinguish root causes rather than always emitting DOCUMENT_BUDGET_EXHAUSTED.
            # Priority order: specific technical failures > discovery outcome > budget state.
            if budget.failures:
                reason = budget.failures[-1]
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
                    reason = f"ACQUISITION_BACKOFF|{prior}" if prior else "ACQUISITION_NOT_DUE"
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
    readiness = await runtime.read(instrument_id, jurisdiction=jurisdiction)
    for row in readiness.requirements:
        matrix[row.requirement_id].update(state=str(row.status), missing=list(row.missing_input_ids),
                                         source=row.source, failure=failures.get(row.requirement_id))
    return TargetedEnsureResult(readiness, tuple(dict.fromkeys(attempted)), tuple(dict.fromkeys(capabilities)),
                                failures=failures), plan, matrix
