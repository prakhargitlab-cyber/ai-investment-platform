"""Durable adapters and targeted runtime execution for research readiness.

The read path in this module performs database and canonical-metadata reads
only.  Provider work is confined to ``ExistingResearchCapabilityExecutor`` and
is reachable only from the explicit ensure command.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse
from uuid import UUID

logger = logging.getLogger(__name__)

from app.research_applicability import classify_requirements
from app.market_sessions import latest_completed_session, next_session_open

from app.fact_precedence import FactSourceTier, FinancialFact
from app.historical_market_data import has_year_historical_coverage
from app.models import (
    CompanyResearchProfile,
    DocumentStatus,
    EventImpact,
    ResearchDocument,
    ResearchEvent,
    ResearchEventType,
    ResearchLifecycleStatus,
    ShareholdingCategory,
    SourceClassification,
    SourceMode,
    StructuredMarketSnapshotRecord,
)
from app.research_readiness import (
    DurableResearchSnapshot,
    ProviderAuthorityRegistry,
    ResearchEvidence,
    ResearchReadinessResult,
    ResearchReadinessService,
    ResearchRefreshPlan,
    ResearchRefreshPlanner,
    ResearchRefreshTarget,
    ResearchRequirement,
    ResearchRequirementImportance,
    ResearchRequirementRegistry,
    ResearchRequirementStatus,
    ResearchSourceTier,
    RuleEngineArea,
)


_FINANCIAL_REQUIREMENTS = frozenset(
    {
        "BUSINESS_QUALITY_FACTS",
        "GROWTH_FACTS",
        "BALANCE_SHEET_FACTS",
        "QUARTERLY_FINANCIALS",
    }
)
_KNOWN_REQUIREMENTS = frozenset(
    item.requirement_id for item in ResearchRequirementRegistry.default().requirements
)
_ORDER_EVENTS = frozenset(
    {
        ResearchEventType.NEW_ORDER,
        ResearchEventType.ORDER_BACKLOG_CHANGE,
        ResearchEventType.MAJOR_CONTRACT,
        ResearchEventType.GOVERNMENT_CONTRACT,
        ResearchEventType.ORDER_CANCELLED,
        ResearchEventType.CAPEX,
        ResearchEventType.FACTORY_EXPANSION,
        ResearchEventType.CAPACITY_EXPANSION,
        ResearchEventType.NEW_FACILITY,
        ResearchEventType.PROJECT_DELAY,
        ResearchEventType.MANAGEMENT_GUIDANCE,
        ResearchEventType.GUIDANCE_RAISED,
        ResearchEventType.GUIDANCE_LOWERED,
        ResearchEventType.GUIDANCE_CUT,
        ResearchEventType.GUIDANCE_MAINTAINED,
        ResearchEventType.REVENUE_GUIDANCE,
        ResearchEventType.MARGIN_GUIDANCE,
    }
)
_GOVERNANCE_EVENTS = frozenset(
    {
        ResearchEventType.MANAGEMENT_CHANGE,
        ResearchEventType.REGULATORY_EVENT,
        ResearchEventType.CREDIT_RATING,
        ResearchEventType.BORROWING_CHANGE,
    }
)
_GOVERNANCE_TERMS = (
    "auditor",
    "governance",
    "regulatory",
    "litigation",
    "fraud",
    "promoter",
    "management change",
)
_MACRO_TERMS = (
    "geopolitical",
    "war",
    "sanction",
    "tariff",
    "supply chain",
    "commodity",
    "currency",
    "interest rate",
)


class RepositoryResearchReadinessAdapter:
    """Translate existing durable records into provider-neutral evidence."""

    def __init__(self, repository) -> None:
        self.repository = repository
        self._canonical_metadata: dict[UUID, dict[str, Any]] = {}
        self._evaluation_times: dict[UUID, datetime] = {}
        self._refreshing: dict[UUID, frozenset[str]] = {}
        self._failures: dict[UUID, dict[str, str]] = {}
        self._sessions: dict[UUID, tuple] = {}

    def remember_canonical_metadata(
        self, global_instrument_id: UUID, metadata: Mapping[str, Any]
    ) -> None:
        self._canonical_metadata[global_instrument_id] = dict(metadata)

    def canonical_metadata_for(self, global_instrument_id: UUID) -> dict[str, Any]:
        """Return public canonical metadata without exposing mutable adapter state."""
        return dict(self._canonical_metadata.get(global_instrument_id, {}))

    def mark_refreshing(self, global_instrument_id: UUID, requirement_ids: Sequence[str]) -> None:
        self._refreshing[global_instrument_id] = frozenset(
            str(value).strip().upper() for value in requirement_ids
        )

    def finish_refresh(
        self,
        global_instrument_id: UUID,
        failures: Mapping[str, str] | None = None,
    ) -> None:
        self._refreshing.pop(global_instrument_id, None)
        if failures:
            self._failures[global_instrument_id] = {
                str(key).strip().upper(): str(value) for key, value in failures.items()
            }
        else:
            self._failures.pop(global_instrument_id, None)

    def load_by_global_instrument_id(
        self,
        global_instrument_id: UUID,
        requirements: Sequence[ResearchRequirement],
    ) -> DurableResearchSnapshot:
        profile = self.repository.profile(global_instrument_id)
        facts = self.repository.financial_facts_for(global_instrument_id)
        structured = self.repository.structured_market_snapshots_for(
            {global_instrument_id}
        ).get(global_instrument_id, [])
        observations = self.repository.market_price_observations_for(
            {global_instrument_id}
        ).get(global_instrument_id, [])
        documents = self.repository.documents_for(
            global_instrument_id, source_mode=SourceMode.REAL
        )
        events = self.repository.events_for(
            global_instrument_id, source_mode=SourceMode.REAL
        )
        shareholding = self.repository.shareholding_for(global_instrument_id, limit=4)

        evidence: dict[str, list[ResearchEvidence]] = {
            requirement.requirement_id: [] for requirement in requirements
        }
        self._append_financial_evidence(evidence, facts)
        self._append_structured_evidence(evidence, structured)
        self._append_market_observations(evidence, observations)
        from app.valuation_evidence import materialize_valuation
        valuation_now = self._evaluation_times.get(global_instrument_id, datetime.now(timezone.utc))
        for name, value in materialize_valuation(structured, observations, now=valuation_now).items():
            evidence['VALUATION_INPUTS'].append(ResearchEvidence(evidence_id='derived-valuation:'+name+':'+str(value.as_of_date),
                requirement_id='VALUATION_INPUTS',source='LICENSED_STRUCTURED',source_tier=ResearchSourceTier.LICENSED_STRUCTURED,
                retrieved_at=value.retrieved_at,as_of=value.as_of_date,value_fingerprint=str(value.value),
                source_url=value.source_url,covered_input_ids=('PE' if name=='trailingPE' else 'PB',)))
        self._append_documents(evidence, documents)
        self._append_events(evidence, events)
        self._append_shareholding(evidence, shareholding)
        self._append_canonical_sector(evidence, global_instrument_id, profile)

        metadata = self.canonical_metadata_for(global_instrument_id)
        sector = metadata.get("canonicalSector") or metadata.get("sector")
        industry = metadata.get("officialIndustry") or metadata.get("industry")
        classification_source = "CANONICAL_REFERENCE" if industry else None
        for record in sorted(structured, key=lambda record: (int(_structured_source_tier(record.provider) != ResearchSourceTier.OFFICIAL), -record.retrieved_at.timestamp())):
            facts_by_name = record.snapshot.facts
            sector_fact, industry_fact = facts_by_name.get("sector"), facts_by_name.get("industry")
            sector = sector or (sector_fact.value if sector_fact else None)
            if not industry and industry_fact and industry_fact.value:
                industry, classification_source = str(industry_fact.value), industry_fact.source_url
        applicability = classify_requirements(str(sector) if sector else None, str(industry) if industry else None, classification_source)
        schedules, exceptions = self._sessions.get(global_instrument_id, ([], []))
        if schedules:
            market = profile.mic or profile.exchange
            for requirement_id in ("LATEST_PRICE", "VALUATION_INPUTS"):
                values = []
                for item in evidence[requirement_id]:
                    if item.as_of:
                        session = latest_completed_session(market, schedules, exceptions, item.as_of + timedelta(minutes=15))
                        # Only a real close observation is reusable during a closed session.
                        if session and abs((session - item.as_of).total_seconds()) <= 15 * 60:
                            valid_until = next_session_open(market, schedules, exceptions, session)
                            if valid_until and ('LATEST_USABLE_PRICE' in item.covered_input_ids or
                                    item.evidence_id.startswith('derived-valuation:')):
                                item = replace(item, valid_until=valid_until)
                    values.append(item)
                evidence[requirement_id] = values

        acquisition = {}
        observations_loader = getattr(self.repository, "acquisition_observations_for", None)
        if callable(observations_loader):
            for observation in observations_loader(global_instrument_id):
                key = observation["requirement_id"]
                history = acquisition.get(key, {}).get("history", [])
                acquisition[key] = {**observation, "history": [*history, observation]}
        news_loader = getattr(self.repository, 'news_records_for', None)
        if callable(news_loader):
            from app.news_intelligence import SearchRun, search_state
            evaluated_at = self._evaluation_times.get(global_instrument_id, datetime.now(timezone.utc))
            runs = news_loader(global_instrument_id, SearchRun, as_of=evaluated_at)
            if runs:
                run = runs[-1]
                state = search_state(run, evaluated_at)
                acquisition['CURRENT_NEWS'] = {'news_readiness':state, 'coverage':run.coverage,
                    'run_id':str(run.run_id), 'observed_at':run.completed_at.isoformat(), 'history':[]}
                if state in {'READY_WITH_EVENTS','READY_NO_EVENTS'}:
                    evidence['CURRENT_NEWS'] = [ResearchEvidence(evidence_id='search-run:'+str(run.run_id),
                        requirement_id='CURRENT_NEWS',source='SEARCH_COVERAGE',source_tier=ResearchSourceTier.APPROVED_SECONDARY,
                        retrieved_at=run.completed_at,as_of=run.completed_at,event_date=run.completed_at,
                        valid_until=run.completed_at+timedelta(days=1),confidence=run.coverage,
                        covered_input_ids=('RELEVANT_CURRENT_EVENT_EVIDENCE',))]
        news_checks = [row for row in acquisition.get("CURRENT_NEWS", {}).get("history", [])
            if row.get("outcome") in {"SUCCESS", "SUCCESS_EMPTY"}]
        if news_checks:
            checked_at = _aware_datetime(news_checks[-1].get("observed_at"))
            if checked_at:
                evidence["CURRENT_NEWS"] = [replace(item, valid_until=checked_at + timedelta(days=1))
                    for item in evidence["CURRENT_NEWS"]]
        persisted_failures = {key: value["failure_reason"] for key, value in acquisition.items()
            if value.get("outcome") == "FAILED" and value.get("failure_reason")}
        supported = set(evidence)
        if not _is_india(profile) and not shareholding:
            supported.discard("SHAREHOLDING")
        return DurableResearchSnapshot(
            global_instrument_id,
            {key: tuple(_deduplicate_evidence(values)) for key, values in evidence.items()},
            supported_requirement_ids=frozenset(supported),
            refreshing_requirement_ids=self._refreshing.get(global_instrument_id, frozenset()),
            failure_reasons={**persisted_failures, **self._failures.get(global_instrument_id, {})},
            acquisition_observations=acquisition,
            applicability_by_requirement=applicability,
        )

    @staticmethod
    def _append_financial_evidence(
        evidence: dict[str, list[ResearchEvidence]], facts: Sequence[FinancialFact]
    ) -> None:
        real = [fact for fact in facts if fact.source_mode == SourceMode.REAL]
        periods_by_series: dict[tuple[str, str], dict[str, set[str]]] = {}
        quarterly_metrics: dict[str, dict[str, set[str]]] = {}
        all_quarterly_periods: set[str] = set()
        annual_periods: set[str] = set()
        for fact in real:
            metric = _metric(fact.key.metric)
            if not fact.key.period_end:
                continue
            period = fact.key.period_end[:10]
            basis = fact.key.reporting_basis or "UNKNOWN"
            periods_by_series.setdefault((fact.key.period_type, basis), {}).setdefault(metric, set()).add(period)
            if fact.key.period_type == "QUARTERLY":
                all_quarterly_periods.add(period)
                family = "revenue" if metric in {"revenue", "total_revenue"} else "pat" if metric in {"pat", "net_income", "net_profit"} else metric
                quarterly_metrics.setdefault(basis, {}).setdefault(family, set()).add(period)
            elif fact.key.period_type == "ANNUAL":
                annual_periods.add(period)
        latest_quarter = max(all_quarterly_periods, default=None)

        for fact in real:
            metric = _metric(fact.key.metric)
            coverage = _financial_fact_coverage(
                fact,
                metric,
                periods_by_series.get((fact.key.period_type, fact.key.reporting_basis or "UNKNOWN"), {}),
                quarterly_metrics.get(fact.key.reporting_basis or "UNKNOWN", {}).get("revenue", set())
                    & quarterly_metrics.get(fact.key.reporting_basis or "UNKNOWN", {}).get("pat", set()),
                annual_periods,
                latest_quarter,
            )
            for requirement_id, covered_inputs in coverage.items():
                evidence[requirement_id].append(
                    _evidence_from_financial_fact(
                        fact, requirement_id, tuple(sorted(covered_inputs))
                    )
                )

    @staticmethod
    def _append_structured_evidence(
        evidence: dict[str, list[ResearchEvidence]],
        records: Sequence[StructuredMarketSnapshotRecord],
    ) -> None:
        for record in records:
            for fact_name, value in record.snapshot.facts.items():
                if fact_name == "latestPrice" and not _positive_number(value.value):
                    continue
                coverage = _structured_fact_coverage(fact_name, record.snapshot.facts)
                for requirement_id, covered_inputs in coverage.items():
                    evidence[requirement_id].append(
                        ResearchEvidence(
                            evidence_id=(
                                f"structured:{record.provider}:{fact_name}:"
                                f"{record.retrieved_at.isoformat()}"
                            ),
                            requirement_id=requirement_id,
                            source=_market_source(record.provider),
                            source_tier=_structured_source_tier(record.provider),
                            retrieved_at=value.retrieved_at or record.retrieved_at,
                            as_of=value.as_of_date or record.market_as_of,
                            published_at=value.published_at,
                            fact_key=f"{fact_name}:{_date_key(value.as_of_date or record.market_as_of)}",
                            value_fingerprint=_fingerprint(value.value),
                            complete=value.value is not None,
                            confidence=value.confidence,
                            source_url=value.source_url or record.source_url,
                            covered_input_ids=tuple(sorted(covered_inputs)),
                            valid_until=((value.as_of_date or value.published_at or value.retrieved_at)+timedelta(days=120)
                                if requirement_id=='VALUATION_INPUTS' and fact_name in {'trailingEps','forwardEps','bookValue'} else None),
                        )
                    )

    @staticmethod
    def _append_market_observations(
        evidence: dict[str, list[ResearchEvidence]], observations: Sequence[Any]
    ) -> None:
        usable = sorted(
            (
                value
                for value in observations
                if value.price is not None
                and Decimal(str(value.price)).is_finite()
                and Decimal(str(value.price)) > 0
            ),
            key=lambda value: value.observed_at,
        )
        if not usable:
            return
        latest = usable[-1]
        source = _market_source(latest.provider)
        source_tier = _structured_source_tier(latest.provider)
        price_evidence = ResearchEvidence(
            evidence_id=f"price:{latest.provider}:{latest.observed_at.isoformat()}",
            requirement_id="LATEST_PRICE",
            source=source,
            source_tier=source_tier,
            retrieved_at=latest.retrieved_at,
            as_of=latest.observed_at,
            fact_key=f"latest-price:{latest.observed_at.isoformat()}",
            value_fingerprint=_fingerprint(latest.price),
            source_url=latest.source_url,
            covered_input_ids=("LATEST_USABLE_PRICE",),
        )
        evidence["LATEST_PRICE"].append(price_evidence)
        evidence["VALUATION_INPUTS"].append(
            _copy_evidence(
                price_evidence,
                "VALUATION_INPUTS",
                ("LATEST_USABLE_PRICE",),
            )
        )
        covered = {"DURABLE_PRICE_OBSERVATIONS"}
        if len(usable) >= 50:
            covered.add("FIFTY_OBSERVATION_TECHNICAL_BASIS")
        if len(usable) >= 150:
            covered.add("ONE_HUNDRED_FIFTY_OBSERVATION_TECHNICAL_BASIS")
        evidence["HISTORICAL_PRICE_SERIES"].append(
            ResearchEvidence(
                evidence_id=(
                    f"price-series:{latest.provider}:{usable[0].observed_at.isoformat()}:"
                    f"{latest.observed_at.isoformat()}:{len(usable)}"
                ),
                requirement_id="HISTORICAL_PRICE_SERIES",
                source=source,
                source_tier=source_tier,
                retrieved_at=max(item.retrieved_at for item in usable),
                as_of=latest.observed_at,
                source_url=latest.source_url,
                covered_input_ids=tuple(sorted(covered)),
            )
        )

    @staticmethod
    def _append_documents(
        evidence: dict[str, list[ResearchEvidence]], documents: Sequence[ResearchDocument]
    ) -> None:
        for document in documents:
            if document.status not in {DocumentStatus.PARSED, DocumentStatus.PROCESSED}:
                continue
            text = f"{document.title or ''} {document.normalized_text or ''}".casefold()
            latest_fact_period = max((item.as_of for item in evidence["QUARTERLY_FINANCIALS"]
                if item.as_of and "LATEST_QUARTERLY_RESULT" in item.covered_input_ids), default=None)
            document_at = document.published_at or document.retrieved_at
            if (any(term in text for term in ("financial result", "quarterly result", "earnings"))
                    and (latest_fact_period is None or document_at >= latest_fact_period)):
                evidence["QUARTERLY_FINANCIALS"].append(
                    _evidence_from_document(
                        document,
                        "QUARTERLY_FINANCIALS",
                        ("LATEST_QUARTERLY_RESULT",),
                    )
                )
            if any(term in text for term in _GOVERNANCE_TERMS):
                evidence["GOVERNANCE_HISTORY"].append(
                    _evidence_from_document(
                        document,
                        "GOVERNANCE_HISTORY",
                        ("GOVERNANCE_EVIDENCE",),
                        unresolved=any(
                            term in text for term in ("litigation", "fraud", "regulatory")
                        ),
                    )
                )
            if any(term in text for term in _MACRO_TERMS):
                evidence["SECTOR_MACRO"].append(
                    _evidence_from_document(
                        document,
                        "SECTOR_MACRO",
                        ("RELEVANT_MACRO_EVENT_EXPOSURE",),
                    )
                )

    @staticmethod
    def _append_events(
        evidence: dict[str, list[ResearchEvidence]], events: Sequence[ResearchEvent]
    ) -> None:
        seen_news: set[tuple[str, str, str]] = set()
        for event in events:
            event_key = (
                event.source_url.casefold(),
                event.title.strip().casefold(),
                _date_key(event.event_date or event.published_at),
            )
            if event_key not in seen_news:
                seen_news.add(event_key)
                evidence["CURRENT_NEWS"].append(
                    _evidence_from_event(
                        event,
                        "CURRENT_NEWS",
                        ("RELEVANT_CURRENT_EVENT_EVIDENCE",),
                    )
                )
            if event.event_type in _ORDER_EVENTS:
                covered = {"MATERIAL_CATALYST_EVIDENCE"}
                if event.event_type in {
                    ResearchEventType.NEW_ORDER,
                    ResearchEventType.ORDER_BACKLOG_CHANGE,
                    ResearchEventType.MAJOR_CONTRACT,
                    ResearchEventType.GOVERNMENT_CONTRACT,
                    ResearchEventType.ORDER_CANCELLED,
                }:
                    covered.add("ORDER_BOOK_OR_MAJOR_CONTRACT")
                if event.event_type in {
                    ResearchEventType.CAPEX,
                    ResearchEventType.FACTORY_EXPANSION,
                    ResearchEventType.CAPACITY_EXPANSION,
                    ResearchEventType.NEW_FACILITY,
                    ResearchEventType.PROJECT_DELAY,
                }:
                    covered.add("CAPACITY_OR_CAPEX_OR_COMMISSIONING")
                if "GUIDANCE" in str(event.event_type):
                    covered.add("MANAGEMENT_GUIDANCE")
                evidence["ORDER_BOOK_CAPEX_GUIDANCE"].append(
                    _evidence_from_event(
                        event,
                        "ORDER_BOOK_CAPEX_GUIDANCE",
                        tuple(sorted(covered)),
                    )
                )
            if event.event_type in _GOVERNANCE_EVENTS:
                unresolved = (
                    event.status != ResearchLifecycleStatus.REJECTED
                    and event.impact
                    in {
                        EventImpact.NEGATIVE,
                        EventImpact.STRONG_NEGATIVE,
                        EventImpact.UNCERTAIN,
                    }
                )
                evidence["GOVERNANCE_HISTORY"].append(
                    _evidence_from_event(
                        event,
                        "GOVERNANCE_HISTORY",
                        ("GOVERNANCE_EVIDENCE",),
                        unresolved=unresolved,
                    )
                )

    @staticmethod
    def _append_shareholding(
        evidence: dict[str, list[ResearchEvidence]], snapshots: Sequence[Any]
    ) -> None:
        if not snapshots:
            return
        snapshot = snapshots[0]
        categories = {str(value.category) for value in snapshot.values}
        covered = {"LATEST_VALID_SHAREHOLDING_PERIOD"}
        if categories & {
            ShareholdingCategory.PROMOTER.value,
            ShareholdingCategory.FII_FPI.value,
            ShareholdingCategory.DII.value,
            ShareholdingCategory.PUBLIC_RETAIL.value,
        }:
            covered.add("PROMOTER_INSTITUTIONAL_PUBLIC_CATEGORIES")
        if ShareholdingCategory.PROMOTER_PLEDGE.value in categories:
            covered.add("PROMOTER_PLEDGE")
        evidence["SHAREHOLDING"].append(
            ResearchEvidence(
                evidence_id=f"shareholding:{snapshot.id}",
                requirement_id="SHAREHOLDING",
                source=(
                    "NSE"
                    if snapshot.source_provider.upper() == "NSE"
                    else "APPROVED_EXTERNAL_TOOL"
                    if snapshot.source_provider.upper() == "YAHOO_FINANCE_MCP"
                    else snapshot.source_provider
                ),
                source_tier=(
                    ResearchSourceTier.OFFICIAL
                    if snapshot.source_provider.upper() == "NSE"
                    else ResearchSourceTier.APPROVED_EXTERNAL_TOOL
                    if snapshot.source_provider.upper() == "YAHOO_FINANCE_MCP"
                    else ResearchSourceTier.LICENSED_STRUCTURED
                ),
                retrieved_at=snapshot.retrieved_at,
                as_of=snapshot.period_end,
                published_at=snapshot.published_at,
                source_url=snapshot.source_url,
                confidence=float(snapshot.confidence),
                covered_input_ids=tuple(sorted(covered)),
            )
        )

    def _append_canonical_sector(
        self,
        evidence: dict[str, list[ResearchEvidence]],
        instrument_id: UUID,
        profile: CompanyResearchProfile,
    ) -> None:
        metadata = self._canonical_metadata.get(instrument_id, {})
        sector = (
            metadata.get("canonicalSector")
            or metadata.get("sector")
            or metadata.get("industrySector")
        )
        if not sector:
            return
        retrieved_at = _aware_datetime(
            metadata.get("updatedAt") or metadata.get("retrievedAt")
        ) or datetime.now(timezone.utc)
        evidence["SECTOR_MACRO"].append(
            ResearchEvidence(
                evidence_id=f"canonical-sector:{instrument_id}:{str(sector).strip().casefold()}",
                requirement_id="SECTOR_MACRO",
                source="EXCHANGE_OR_INDEX_PROVIDER",
                source_tier=ResearchSourceTier.TRUSTED_MARKET_DATA,
                retrieved_at=retrieved_at,
                as_of=retrieved_at,
                fact_key="canonical-sector",
                value_fingerprint=str(sector).strip(),
                covered_input_ids=("CANONICAL_SECTOR",),
            )
        )


@dataclass(frozen=True)
class CapabilityExecutionResult:
    executed_capabilities: tuple[str, ...] = ()
    failures: Mapping[str, str] = field(default_factory=dict)
    satisfied_requirement_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "failures", dict(self.failures or {}))
        object.__setattr__(
            self,
            "satisfied_requirement_ids",
            tuple(dict.fromkeys(self.satisfied_requirement_ids or ())),
        )


@dataclass
class CapabilityExecutionProgress:
    """Request-local acquisition progress retained if the budget cancels execution."""

    executed_capabilities: list[str] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)
    satisfied_requirement_ids: set[str] = field(default_factory=set)

    def executed(self, capability: str) -> None:
        if capability not in self.executed_capabilities:
            self.executed_capabilities.append(capability)

    def failed(self, requirement_id: str, reason: str) -> None:
        self.failures[requirement_id] = _combined_failure_reason(
            self.failures.get(requirement_id), reason
        )

    def satisfied(self, requirement_id: str) -> None:
        self.satisfied_requirement_ids.add(requirement_id)


class ExistingResearchCapabilityExecutor:
    """Map planner targets to the narrow provider capabilities already present."""

    def __init__(self, repository, orchestrator, market_data_population_jobs) -> None:
        self.repository = repository
        self.orchestrator = orchestrator
        self.market_data_population_jobs = market_data_population_jobs

    async def execute_primary(
        self,
        global_instrument_id: UUID,
        targets: Sequence[ResearchRefreshTarget],
        *,
        jurisdiction: str,
        correlation_id: str | None,
        identity_headers: Mapping[str, str | None] | None,
        progress: CapabilityExecutionProgress | None = None,
    ) -> CapabilityExecutionResult:
        requirement_ids = {target.requirement_id for target in targets}
        unknown = requirement_ids - _KNOWN_REQUIREMENTS
        if unknown:
            raise KeyError(f"No targeted capability mapping for {sorted(unknown)}")
        executed: list[str] = []
        failures: dict[str, str] = {}

        structured_classes: set[str] = set()
        if "VALUATION_INPUTS" in requirement_ids:
            structured_classes.update({"PRICE", "VALUATION", "FUNDAMENTALS"})
        if "LATEST_PRICE" in requirement_ids:
            structured_classes.add("PRICE")
        if "SECTOR_MACRO" in requirement_ids:
            structured_classes.add("FUNDAMENTALS")
        if structured_classes:
            executed.append("STRUCTURED_MARKET")
            if progress is not None:
                progress.executed("STRUCTURED_MARKET")
            try:
                outcome = await self.orchestrator.ensure_structured_market(
                    global_instrument_id, structured_classes
                )
                if outcome.error:
                    for requirement_id in requirement_ids & {
                        "VALUATION_INPUTS", "LATEST_PRICE", "SECTOR_MACRO"
                    }:
                        failures[requirement_id] = outcome.error
                        if progress is not None:
                            progress.failed(requirement_id, outcome.error)
            except Exception as exc:
                for requirement_id in requirement_ids & {
                    "VALUATION_INPUTS", "LATEST_PRICE", "SECTOR_MACRO"
                }:
                    failures[requirement_id] = type(exc).__name__
                    if progress is not None:
                        progress.failed(requirement_id, type(exc).__name__)

        financial = requirement_ids & _FINANCIAL_REQUIREMENTS
        repository_categories: set[str] = set()
        if financial:
            executed.append("FINANCIALS")
            if progress is not None:
                progress.executed("FINANCIALS")
            if jurisdiction == "INDIA":
                repository_categories.add("FINANCIAL_RESULTS")
            else:
                try:
                    profile = self.repository.profile(global_instrument_id)
                    result = await self.orchestrator.refresh_international_fundamentals(
                        profile,
                        correlation_id=correlation_id,
                        identity_headers=dict(identity_headers or {}),
                    )
                    if result is None or not result.facts:
                        for requirement_id in financial:
                            failures[requirement_id] = "PRIMARY_FINANCIAL_PROVIDER_RETURNED_NO_FACTS"
                            if progress is not None:
                                progress.failed(
                                    requirement_id,
                                    "PRIMARY_FINANCIAL_PROVIDER_RETURNED_NO_FACTS",
                                )
                except Exception as exc:
                    for requirement_id in financial:
                        failures[requirement_id] = type(exc).__name__
                        if progress is not None:
                            progress.failed(requirement_id, type(exc).__name__)

        if "SHAREHOLDING" in requirement_ids:
            executed.append("SHAREHOLDING")
            if progress is not None:
                progress.executed("SHAREHOLDING")
            repository_categories.add("SHAREHOLDING_PATTERN")
        if "ORDER_BOOK_CAPEX_GUIDANCE" in requirement_ids:
            executed.append("ORDER_BOOK_CAPACITY_CATALYSTS")
            if progress is not None:
                progress.executed("ORDER_BOOK_CAPACITY_CATALYSTS")
            repository_categories.update(
                {"ORDERS_BACKLOG", "CONTRACTS", "CAPEX", "NEW_FACILITIES", "GUIDANCE"}
            )
        if "CURRENT_NEWS" in requirement_ids:
            executed.append("GLOBAL_NEWS_SEARCH")
            if progress is not None:
                progress.executed("GLOBAL_NEWS_SEARCH")
            news_worker=getattr(self.repository,'refresh_news_intelligence',None)
            if callable(news_worker):
                try:
                    await news_worker(global_instrument_id)
                except Exception:
                    failures['CURRENT_NEWS']='NEWS_INTELLIGENCE_UNAVAILABLE'
            else:
                repository_categories.update({'CATALYSTS','RISKS','REGULATORY','MANAGEMENT','GUIDANCE'})
        if "GOVERNANCE_HISTORY" in requirement_ids:
            executed.append("GOVERNANCE_EVIDENCE")
            if progress is not None:
                progress.executed("GOVERNANCE_EVIDENCE")
            repository_categories.update({"RISKS", "REGULATORY", "MANAGEMENT"})
        if repository_categories:
            try:
                await self.repository.refresh_targeted_categories(
                    global_instrument_id,
                    repository_categories,
                    correlation_id=correlation_id,
                    allow_demo=True,
                )
            except Exception as exc:
                for requirement_id in requirement_ids & (
                    _FINANCIAL_REQUIREMENTS
                    | {
                        "SHAREHOLDING",
                        "ORDER_BOOK_CAPEX_GUIDANCE",
                        "CURRENT_NEWS",
                        "GOVERNANCE_HISTORY",
                    }
                ):
                    failures[requirement_id] = type(exc).__name__
                    if progress is not None:
                        progress.failed(requirement_id, type(exc).__name__)

        if "HISTORICAL_PRICE_SERIES" in requirement_ids:
            executed.append("HISTORICAL_MARKET_DATA")
            if progress is not None:
                progress.executed("HISTORICAL_MARKET_DATA")
            try:
                await self._ensure_historical_prices(global_instrument_id)
            except Exception as exc:
                failures["HISTORICAL_PRICE_SERIES"] = type(exc).__name__
                if progress is not None:
                    progress.failed("HISTORICAL_PRICE_SERIES", type(exc).__name__)

        return CapabilityExecutionResult(tuple(dict.fromkeys(executed)), failures)

    async def execute_approved_fallbacks(
        self,
        global_instrument_id: UUID,
        targets: Sequence[ResearchRefreshTarget],
    ) -> CapabilityExecutionResult:
        financial = {target.requirement_id for target in targets} & _FINANCIAL_REQUIREMENTS
        if not financial:
            return CapabilityExecutionResult()
        try:
            outcome = await self.orchestrator.ensure_structured_market(
                global_instrument_id, {"FUNDAMENTALS"}
            )
            failures = (
                {requirement_id: outcome.error for requirement_id in financial}
                if outcome.error
                else {}
            )
            return CapabilityExecutionResult(("APPROVED_STRUCTURED_FINANCIAL_FALLBACK",), failures)
        except Exception as exc:
            return CapabilityExecutionResult(
                ("APPROVED_STRUCTURED_FINANCIAL_FALLBACK",),
                {requirement_id: type(exc).__name__ for requirement_id in financial},
            )

    async def _ensure_historical_prices(self, global_instrument_id: UUID) -> int:
        profile = self.repository.profile(global_instrument_id)
        provider_ticker = profile.provider_instrument_ids.get("YAHOO_FINANCE")
        if not provider_ticker:
            raise ValueError("VERIFIED_HISTORICAL_MAPPING_REQUIRED")
        observations = (
            await self.repository.market_price_observations_for_instruments(
                {global_instrument_id}
            )
        ).get(global_instrument_id, [])
        now = datetime.now(timezone.utc)
        observed_at = [value.observed_at for value in observations]
        has_year = bool(observed_at) and has_year_historical_coverage(
            min(observed_at), max(observed_at), len(observed_at)
        )
        start = (
            max(observed_at) + timedelta(days=1)
            if has_year
            else now
            - timedelta(
                days=self.market_data_population_jobs.settings.market_data_population_initial_lookback_days
            )
        )
        end = now + timedelta(days=1)
        if has_year and start.date() >= end.date():
            return 0
        instrument = {
            "globalInstrumentId": str(global_instrument_id),
            "structuredProviderTicker": provider_ticker,
            "ticker": profile.ticker,
            "currency": profile.currency,
        }
        return await self.market_data_population_jobs.population.populate(
            [instrument], start=start, end=end
        )


@dataclass(frozen=True)
class TargetedEnsureResult:
    readiness: ResearchReadinessResult
    planned_requirement_ids: tuple[str, ...]
    executed_capabilities: tuple[str, ...]
    reused_single_flight: bool = False
    failures: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "failures", dict(self.failures or {}))


@dataclass(frozen=True)
class _PlanExecutionResult:
    executed_capabilities: tuple[str, ...] = ()
    failures: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "failures", dict(self.failures or {}))


@dataclass(frozen=True)
class _EnsureFlight:
    task: asyncio.Task[_PlanExecutionResult]
    requirement_ids: frozenset[str]


class ResearchReadinessRuntime:
    """DB-first application service shared by GET and targeted ensure routes."""

    def __init__(
        self,
        repository,
        data_source: RepositoryResearchReadinessAdapter,
        executor: ExistingResearchCapabilityExecutor,
        requirement_registry: ResearchRequirementRegistry | None = None,
        authority_registry: ProviderAuthorityRegistry | None = None,
        ensure_timeout_seconds: float = 25.0,
    ) -> None:
        if ensure_timeout_seconds <= 0:
            raise ValueError("ensure_timeout_seconds must be positive")
        self.repository = repository
        self.data_source = data_source
        self.requirement_registry = requirement_registry or ResearchRequirementRegistry.default()
        self.authority_registry = authority_registry or ProviderAuthorityRegistry.default()
        self.readiness_service = ResearchReadinessService(
            data_source,
            requirement_registry=self.requirement_registry,
            authority_registry=self.authority_registry,
        )
        self.planner = ResearchRefreshPlanner(self.authority_registry)
        self.executor = executor
        self.ensure_timeout_seconds = ensure_timeout_seconds
        self._flights: dict[UUID, _EnsureFlight] = {}

    async def read(
        self,
        global_instrument_id: UUID,
        *,
        jurisdiction: str,
        now: datetime | None = None,
    ) -> ResearchReadinessResult:
        if isinstance(self.data_source, RepositoryResearchReadinessAdapter):
            self.data_source._evaluation_times[global_instrument_id] = now or datetime.now(timezone.utc)
        if callable(getattr(self.repository, "market_session_data", None)):
            profile = self.repository.profile(global_instrument_id)
            self.data_source._sessions[global_instrument_id] = await self.repository.market_session_data({value for value in (profile.mic, profile.exchange) if value})
        return await self.repository._run_blocking_persistence(
            self.readiness_service.assess,
            global_instrument_id,
            jurisdiction=jurisdiction,
            now=now,
        )

    async def ensure(
        self,
        global_instrument_id: UUID,
        *,
        jurisdiction: str,
        requirement_ids: Sequence[str] | None,
        correlation_id: str | None = None,
        identity_headers: Mapping[str, str | None] | None = None,
    ) -> TargetedEnsureResult:
        selected = self._validated_requirement_ids(requirement_ids)
        readiness = await self.read(global_instrument_id, jurisdiction=jurisdiction)
        plan = self.planner.plan(
            readiness,
            jurisdiction=jurisdiction,
            requirement_ids=selected,
            include_non_mandatory=True,
        )
        planned_ids = tuple(target.requirement_id for target in plan.targets)
        if not plan.targets:
            return TargetedEnsureResult(readiness, (), ())

        existing = self._flights.get(global_instrument_id)
        if existing is not None:
            shared_execution = await asyncio.shield(existing.task)
            attempted = existing.requirement_ids
            readiness = await self.read(global_instrument_id, jurisdiction=jurisdiction)
            remaining = self.planner.plan(
                readiness,
                jurisdiction=jurisdiction,
                requirement_ids=selected,
                include_non_mandatory=True,
            )
            remaining_targets = tuple(
                target
                for target in remaining.targets
                if target.requirement_id not in attempted
            )
            if not remaining_targets:
                return TargetedEnsureResult(
                    readiness, planned_ids, (), True, shared_execution.failures
                )
            plan = ResearchRefreshPlan(
                global_instrument_id, remaining_targets, remaining.created_at
            )

        target_ids = frozenset(target.requirement_id for target in plan.targets)
        task = asyncio.create_task(
            self._execute_plan_bounded(
                plan,
                jurisdiction=jurisdiction,
                correlation_id=correlation_id,
                identity_headers=identity_headers,
            )
        )
        flight = _EnsureFlight(task, target_ids)
        self._flights[global_instrument_id] = flight

        def cleanup(completed: asyncio.Task[_PlanExecutionResult]) -> None:
            if self._flights.get(global_instrument_id) is flight:
                self._flights.pop(global_instrument_id, None)

        task.add_done_callback(cleanup)
        execution = await asyncio.shield(task)
        recorder = getattr(self.repository, "record_acquisition_observation", None)
        if callable(recorder):
            for target in plan.targets:
                failure = execution.failures.get(target.requirement_id)
                await recorder(global_instrument_id, target.requirement_id, "READINESS_EXECUTOR",
                    "FAILED" if failure else "COMPLETED", datetime.now(timezone.utc), failure_reason=failure)
        readiness = await self.read(global_instrument_id, jurisdiction=jurisdiction)
        return TargetedEnsureResult(
            readiness,
            planned_ids,
            execution.executed_capabilities,
            failures=execution.failures,
        )

    async def _execute_plan_bounded(
        self,
        plan: ResearchRefreshPlan,
        *,
        jurisdiction: str,
        correlation_id: str | None,
        identity_headers: Mapping[str, str | None] | None,
    ) -> _PlanExecutionResult:
        progress = CapabilityExecutionProgress()
        try:
            async with asyncio.timeout(self.ensure_timeout_seconds):
                return await self._execute_plan(
                    plan,
                    jurisdiction=jurisdiction,
                    correlation_id=correlation_id,
                    identity_headers=identity_headers,
                    progress=progress,
                )
        except TimeoutError:
            # The interactive route owns a smaller execution budget than the
            # API gateway. Provider work is cancelled here so an unavailable
            # capability becomes a bounded requirement failure instead of a
            # downstream gateway timeout. Any facts already committed remain
            # durable and are reflected by the read below.
            self.data_source.finish_refresh(plan.global_instrument_id)
            readiness = await self.read(
                plan.global_instrument_id, jurisdiction=jurisdiction
            )
            failures = {}
            for target in plan.targets:
                if self._requires_acquisition(
                    readiness.for_requirement(target.requirement_id).status
                ):
                    failures[target.requirement_id] = _combined_failure_reason(
                        progress.failures.get(target.requirement_id),
                        "ACQUISITION_TIMEOUT",
                    )
            self.data_source.finish_refresh(plan.global_instrument_id, failures)
            logger.warning(
                "research_readiness_ensure_timeout globalInstrumentId=%s "
                "budgetSeconds=%s requirements=%s",
                plan.global_instrument_id,
                self.ensure_timeout_seconds,
                sorted(failures),
            )
            return _PlanExecutionResult(
                tuple(progress.executed_capabilities), failures
            )

    async def _execute_plan(
        self,
        plan: ResearchRefreshPlan,
        *,
        jurisdiction: str,
        correlation_id: str | None,
        identity_headers: Mapping[str, str | None] | None,
        progress: CapabilityExecutionProgress | None = None,
    ) -> _PlanExecutionResult:
        ids = tuple(target.requirement_id for target in plan.targets)
        self.data_source.mark_refreshing(plan.global_instrument_id, ids)
        failures: dict[str, str] = {}
        executed: list[str] = []
        completed: set[str] = set()
        try:
            primary = await self.executor.execute_primary(
                plan.global_instrument_id,
                plan.targets,
                jurisdiction=jurisdiction,
                correlation_id=correlation_id,
                identity_headers=identity_headers,
                progress=progress,
            )
            executed.extend(primary.executed_capabilities)
            if progress is not None:
                for capability in primary.executed_capabilities:
                    progress.executed(capability)
                for requirement_id, reason in primary.failures.items():
                    progress.failed(requirement_id, reason)
                for requirement_id in primary.satisfied_requirement_ids:
                    progress.satisfied(requirement_id)
            for requirement_id, reason in primary.failures.items():
                failures[requirement_id] = _combined_failure_reason(
                    failures.get(requirement_id), reason
                )
            completed.update(primary.satisfied_requirement_ids)
        finally:
            # Re-read durable state before exposing provider failures so an
            # unavailable primary can be classified as missing/partial and
            # considered for an approved existing fallback.
            self.data_source.finish_refresh(plan.global_instrument_id)

        after_primary = await self.read(plan.global_instrument_id, jurisdiction=jurisdiction)
        unresolved: list[ResearchRefreshTarget] = []
        for target in plan.targets:
            if target.requirement_id in completed:
                continue
            result = after_primary.for_requirement(target.requirement_id)
            if result.status not in {
                ResearchRequirementStatus.MISSING,
                ResearchRequirementStatus.PARTIAL,
                ResearchRequirementStatus.CONFLICTING,
                ResearchRequirementStatus.FAILED,
            }:
                continue
            if target.authority_policy.fallback_policy.permits(result.status):
                unresolved.append(target)
        if unresolved:
            fallback = await self.executor.execute_approved_fallbacks(
                plan.global_instrument_id, unresolved
            )
            executed.extend(fallback.executed_capabilities)
            if progress is not None:
                for capability in fallback.executed_capabilities:
                    progress.executed(capability)
                for requirement_id, reason in fallback.failures.items():
                    progress.failed(requirement_id, reason)
            for requirement_id, reason in fallback.failures.items():
                failures[requirement_id] = _combined_failure_reason(
                    failures.get(requirement_id), reason
                )
        final_readiness = await self.read(
            plan.global_instrument_id, jurisdiction=jurisdiction
        )
        unresolved_failures = {
            requirement_id: reason
            for requirement_id, reason in failures.items()
            if self._requires_acquisition(
                final_readiness.for_requirement(requirement_id).status
            )
        }
        self.data_source.finish_refresh(plan.global_instrument_id, unresolved_failures)
        return _PlanExecutionResult(
            tuple(dict.fromkeys(executed)), unresolved_failures
        )

    @staticmethod
    def _requires_acquisition(status: ResearchRequirementStatus) -> bool:
        return status in {
            ResearchRequirementStatus.MISSING,
            ResearchRequirementStatus.PARTIAL,
            ResearchRequirementStatus.CONFLICTING,
            ResearchRequirementStatus.FAILED,
        }

    def _validated_requirement_ids(
        self, requirement_ids: Sequence[str] | None
    ) -> tuple[str, ...] | None:
        if requirement_ids is None:
            return None
        known_ids = {
            item.requirement_id for item in self.requirement_registry.requirements
        }
        area_ids = {area.value for area in RuleEngineArea}
        expanded: list[str] = []
        unknown: set[str] = set()
        for value in requirement_ids:
            normalized = str(value).strip().upper()
            if normalized in known_ids:
                expanded.append(normalized)
            elif normalized in area_ids:
                expanded.extend(
                    item.requirement_id
                    for item in self.requirement_registry.for_area(
                        RuleEngineArea(normalized)
                    )
                )
            else:
                unknown.add(normalized)
        if unknown:
            raise ValueError(f"UNKNOWN_RESEARCH_REQUIREMENT:{','.join(sorted(unknown))}")
        return tuple(dict.fromkeys(expanded))


def readiness_response(
    value: ResearchReadinessResult,
    registry: ResearchRequirementRegistry,
    *,
    ensure: TargetedEnsureResult | None = None,
) -> dict[str, Any]:
    requirements = []
    for item in value.requirements:
        contract = registry.get(item.requirement_id)
        requirements.append(
            {
                "requirementId": item.requirement_id,
                "area": item.rule_engine_area.value,
                "areaWeightPct": int(registry.area_weights[item.rule_engine_area] * 100),
                "importance": item.importance.value,
                "mandatory": item.mandatory,
                "status": item.status.value,
                "applicability": item.applicability,
                "applicabilityReason": item.applicability_reason,
                "businessClassification": item.classification,
                "classificationSource": item.classification_source,
                "acquisitionObservation": item.acquisition_observation,
                "sourceProvider": item.source,
                "sourceTier": item.source_tier.value if item.source_tier else None,
                "sourceUrl": item.source_url,
                "asOf": _iso(item.as_of),
                "retrievedAt": _iso(item.retrieved_at),
                "ageSeconds": int(item.age.total_seconds()) if item.age is not None else None,
                "freshnessPolicy": {
                    "policyId": item.freshness_policy.policy_id,
                    "mode": item.freshness_policy.mode.value,
                    "maximumAgeSeconds": (
                        int(item.freshness_policy.maximum_age.total_seconds())
                        if item.freshness_policy.maximum_age is not None
                        else None
                    ),
                    "scoringWindowDays": (
                        item.freshness_policy.scoring_window.days
                        if item.freshness_policy.scoring_window is not None
                        else None
                    ),
                },
                "evidenceIds": list(item.evidence_ids),
                "coveredInputIds": list(item.covered_input_ids),
                "missingInputIds": list(item.missing_input_ids),
                "concreteRequirements": [
                    {
                        "inputId": input_.input_id,
                        "importance": input_.importance.value,
                        "covered": input_.input_id in item.covered_input_ids,
                        "applicability": "NOT_APPLICABLE" if item.applicability == "NOT_APPLICABLE" or input_.input_id in item.not_applicable_input_reasons else "APPLICABLE",
                        "applicabilityReason": item.not_applicable_input_reasons.get(input_.input_id) or (item.applicability_reason if item.applicability == "NOT_APPLICABLE" else None),
                    }
                    for input_ in contract.inputs
                ],
                "coveragePct": item.coverage_pct,
                "criticalCoveragePct": item.critical_coverage_pct,
                "missingReason": item.missing_reason,
                "conflictReason": item.conflict_reason,
                "supportedActions": [action.value for action in item.supported_actions],
            }
        )
    response: dict[str, Any] = {
        "globalInstrumentId": str(value.global_instrument_id),
        "overallStatus": value.overall_status.value,
        "overallCompletenessPct": value.overall_completeness_pct,
        "criticalCompletenessPct": value.critical_completeness_pct,
        "confidence": value.confidence.value,
        "confidencePct": value.confidence_pct,
        "generatedAt": value.generated_at.isoformat(),
        "requirements": requirements,
    }
    if ensure is not None:
        response["refreshState"] = {
            "plannedRequirements": list(ensure.planned_requirement_ids),
            "executedCapabilities": list(ensure.executed_capabilities),
            "reusedSingleFlight": ensure.reused_single_flight,
            "failureReasons": dict(ensure.failures),
        }
    return response


def jurisdiction_for_profile(profile: CompanyResearchProfile) -> str:
    country = profile.country.strip().upper()
    exchange = profile.exchange.strip().upper()
    if country in {"IN", "IND", "INDIA"} or exchange in {"NSE", "XNSE", "BSE", "XBOM"}:
        return "INDIA"
    if country in {"US", "USA", "UNITED STATES"}:
        return "USA"
    if country in {
        "AT", "BE", "CH", "CZ", "DE", "DK", "ES", "EU", "FI", "FR", "GB", "IE",
        "IT", "LU", "NL", "NO", "PL", "PT", "SE", "UK",
    }:
        return "EUROPE"
    return "GLOBAL"


def _financial_fact_coverage(
    fact: FinancialFact,
    metric: str,
    periods_by_metric: Mapping[str, set[str]],
    quarterly_periods: set[str],
    annual_periods: set[str],
    latest_quarter: str | None,
) -> dict[str, set[str]]:
    coverage: dict[str, set[str]] = {}

    def add(requirement_id: str, *input_ids: str) -> None:
        coverage.setdefault(requirement_id, set()).update(input_ids)

    if metric in {"eps", "pat", "net_income", "net_profit"}:
        add("VALUATION_INPUTS", "EARNINGS_BASIS")
    if metric in {"pat", "net_income", "net_profit", "roe", "return_on_equity", "roce", "return_on_capital_employed", "operating_margin", "profit_margin", "net_margin", "operating_cash_flow"}:
        if len(periods_by_metric.get(metric, set())) >= 2:
            add("BUSINESS_QUALITY_FACTS", "PROFITABILITY_HISTORY")
    if metric in {"roe", "return_on_equity"}:
        add("BUSINESS_QUALITY_FACTS", "ROE")
    if metric in {"roce", "return_on_capital_employed"}:
        add("BUSINESS_QUALITY_FACTS", "ROCE")
    if metric in {"operating_margin", "profit_margin", "ebitda_margin", "gross_margin"}:
        add("BUSINESS_QUALITY_FACTS", "MARGINS")
    if metric in {
        "free_cash_flow", "operating_cash_flow", "cash_flow_from_operating_activities"
    }:
        add("BUSINESS_QUALITY_FACTS", "CASH_CONVERSION_OR_FCF_QUALITY")
    if metric in {"revenue", "total_revenue"} and len(periods_by_metric.get(metric, set())) >= 2:
        add("GROWTH_FACTS", "REVENUE_HISTORY")
    if metric in {"eps", "pat", "net_income", "net_profit"} and len(
        periods_by_metric.get(metric, set())
    ) >= 2:
        add("GROWTH_FACTS", "EARNINGS_HISTORY")
    if fact.key.period_type == "QUARTERLY" and len(quarterly_periods) >= 2:
        add("GROWTH_FACTS", "QUARTERLY_YOY_QOQ_TRENDS")
    if fact.key.period_type == "ANNUAL" and len(annual_periods) >= 2:
        add("GROWTH_FACTS", "ANNUAL_CAGR_INPUTS")
    if metric in {"total_debt", "debt", "debt_or_borrowings", "borrowings"}:
        add("BALANCE_SHEET_FACTS", "DEBT")
    if metric in {"total_equity", "equity", "net_worth"}:
        add("BALANCE_SHEET_FACTS", "EQUITY")
    if metric in {"cash", "total_cash", "cash_and_cash_equivalents", "cash_and_equivalents"}:
        add("BALANCE_SHEET_FACTS", "CASH")
    if metric in {"interest_expense", "finance_cost", "finance_costs", "ebit", "operating_profit"}:
        add("BALANCE_SHEET_FACTS", "INTEREST_COVERAGE_INPUTS")
    if metric in {"current_assets", "current_liabilities", "current_ratio"}:
        add("BALANCE_SHEET_FACTS", "LIQUIDITY_CURRENT_RATIO_INPUTS")
    if fact.key.period_type == "QUARTERLY":
        if fact.key.period_end and fact.key.period_end[:10] == latest_quarter:
            add("QUARTERLY_FINANCIALS", "LATEST_QUARTERLY_RESULT")
        if len(quarterly_periods) >= 2:
            add("QUARTERLY_FINANCIALS", "COMPARABLE_QUARTERS")
        if metric in {"revenue", "total_revenue"}:
            add("QUARTERLY_FINANCIALS", "QUARTERLY_REVENUE")
        if metric in {"pat", "net_income", "net_profit"}:
            add("QUARTERLY_FINANCIALS", "QUARTERLY_PAT")
        if metric in {"ebitda", "operating_profit", "operating_income"}:
            add("QUARTERLY_FINANCIALS", "QUARTERLY_EBITDA_OR_OPERATING_PROFIT")
        if metric == "eps":
            add("QUARTERLY_FINANCIALS", "QUARTERLY_EPS")
        if metric in {"operating_margin", "profit_margin", "ebitda_margin"}:
            add("QUARTERLY_FINANCIALS", "QUARTERLY_MARGINS")
    return coverage


def _structured_fact_coverage(
    fact_name: str, facts: Mapping[str, Any]
) -> dict[str, set[str]]:
    coverage: dict[str, set[str]] = {}

    def add(requirement_id: str, *input_ids: str) -> None:
        coverage.setdefault(requirement_id, set()).update(input_ids)

    if fact_name == "latestPrice":
        add("LATEST_PRICE", "LATEST_USABLE_PRICE")
        add("VALUATION_INPUTS", "LATEST_USABLE_PRICE")
    if fact_name in {"trailingEps", "forwardEps"}:
        add("VALUATION_INPUTS", "EARNINGS_BASIS")
    if fact_name in {"trailingPE", "forwardPE"}:
        add("VALUATION_INPUTS", "PE")
    if fact_name == "priceToBook":
        add("VALUATION_INPUTS", "PB")
    if fact_name == "evToEbitda":
        add("VALUATION_INPUTS", "EV_EBITDA")
    if fact_name == "freeCashFlow" and facts.get("marketCap") is not None:
        add("VALUATION_INPUTS", "FCF_YIELD")
    if fact_name == "roe":
        add("BUSINESS_QUALITY_FACTS", "ROE")
    if fact_name in {"profitMargin", "operatingMargin"}:
        add("BUSINESS_QUALITY_FACTS", "MARGINS")
    if fact_name in {"freeCashFlow", "operatingCashFlow"}:
        add("BUSINESS_QUALITY_FACTS", "CASH_CONVERSION_OR_FCF_QUALITY")
    if fact_name == "revenueGrowth":
        add("GROWTH_FACTS", "REVENUE_HISTORY")
    if fact_name == "earningsGrowth":
        add("GROWTH_FACTS", "EARNINGS_HISTORY")
    if fact_name in {"totalDebt", "debtToEquity"}:
        add("BALANCE_SHEET_FACTS", "DEBT")
    if fact_name in {"bookValue", "debtToEquity"}:
        add("BALANCE_SHEET_FACTS", "EQUITY")
    if fact_name == "totalCash":
        add("BALANCE_SHEET_FACTS", "CASH")
    if fact_name == "currentRatio":
        add("BALANCE_SHEET_FACTS", "LIQUIDITY_CURRENT_RATIO_INPUTS")
    if fact_name == "sector":
        add("SECTOR_MACRO", "CANONICAL_SECTOR")
    return coverage


def _positive_number(value: Any) -> bool:
    try:
        number = Decimal(str(value))
        return number.is_finite() and number > 0
    except Exception:
        return False


def _evidence_from_financial_fact(
    fact: FinancialFact, requirement_id: str, covered_input_ids: tuple[str, ...]
) -> ResearchEvidence:
    source, tier = _financial_source(fact)
    as_of = fact.value.as_of_date or _period_datetime(fact.key.period_end)
    return ResearchEvidence(
        evidence_id=(
            f"financial:{fact.source_identity}:{fact.key.metric}:{fact.key.period_end}:"
            f"{fact.key.period_type}:{requirement_id}"
        ),
        requirement_id=requirement_id,
        source=source,
        source_tier=tier,
        retrieved_at=fact.value.retrieved_at,
        as_of=as_of,
        published_at=fact.value.published_at,
        fact_key=(
            f"{_metric(fact.key.metric)}:{fact.key.period_end}:"
            f"{fact.key.period_type}:{fact.key.reporting_basis}"
        ),
        value_fingerprint=_fingerprint(fact.value.value),
        confidence=fact.value.confidence,
        source_url=fact.value.source_url,
        covered_input_ids=covered_input_ids,
        valid_until=(as_of + timedelta(days=120 if fact.key.period_type == "QUARTERLY" else 400)
            if requirement_id == "VALUATION_INPUTS" and as_of and fact.key.period_type in {"QUARTERLY", "ANNUAL"} else None),
    )


def _evidence_from_document(
    document: ResearchDocument,
    requirement_id: str,
    covered_input_ids: tuple[str, ...],
    *,
    unresolved: bool = False,
) -> ResearchEvidence:
    source, tier = _document_source(document, requirement_id)
    return ResearchEvidence(
        evidence_id=f"document:{document.document_id}:{requirement_id}",
        requirement_id=requirement_id,
        source=source,
        source_tier=tier,
        retrieved_at=document.retrieved_at,
        as_of=document.published_at or document.retrieved_at,
        published_at=document.published_at,
        event_date=document.published_at,
        confidence=document.entity_resolution_confidence,
        unresolved=unresolved,
        source_url=document.canonical_url,
        covered_input_ids=covered_input_ids,
    )


def _evidence_from_event(
    event: ResearchEvent,
    requirement_id: str,
    covered_input_ids: tuple[str, ...],
    *,
    unresolved: bool = False,
) -> ResearchEvidence:
    source, tier = _event_source(event, requirement_id)
    return ResearchEvidence(
        evidence_id=f"event:{event.event_id}:{requirement_id}",
        requirement_id=requirement_id,
        source=source,
        source_tier=tier,
        retrieved_at=event.retrieved_at or event.detected_at,
        as_of=event.event_date or event.published_at,
        published_at=event.published_at,
        event_date=event.event_date,
        confidence=event.confidence,
        unresolved=unresolved,
        source_url=event.source_url,
        covered_input_ids=covered_input_ids,
    )


def _copy_evidence(
    value: ResearchEvidence,
    requirement_id: str,
    covered_input_ids: tuple[str, ...],
) -> ResearchEvidence:
    return ResearchEvidence(
        evidence_id=f"{value.evidence_id}:{requirement_id}",
        requirement_id=requirement_id,
        source=value.source,
        source_tier=value.source_tier,
        retrieved_at=value.retrieved_at,
        as_of=value.as_of,
        published_at=value.published_at,
        event_date=value.event_date,
        valid_until=value.valid_until,
        fact_key=value.fact_key,
        value_fingerprint=value.value_fingerprint,
        complete=value.complete,
        confidence=value.confidence,
        unresolved=value.unresolved,
        source_url=value.source_url,
        covered_input_ids=covered_input_ids,
    )


def _financial_source(fact: FinancialFact) -> tuple[str, ResearchSourceTier]:
    provider = fact.source_provider.strip().upper()
    if fact.source_tier == FactSourceTier.OFFICIAL_NSE:
        return "NSE", ResearchSourceTier.OFFICIAL
    if fact.source_tier == FactSourceTier.OFFICIAL_REGULATORY:
        return ("SEC_EDGAR" if "SEC" in provider else "REGULATORY_FILING"), ResearchSourceTier.REGULATORY
    if fact.source_tier == FactSourceTier.STRUCTURED_FUNDAMENTALS:
        return provider or "LICENSED_STRUCTURED", ResearchSourceTier.LICENSED_STRUCTURED
    if provider == "YAHOO_FINANCE_MCP":
        return "APPROVED_EXTERNAL_TOOL", ResearchSourceTier.APPROVED_EXTERNAL_TOOL
    if fact.source_tier == FactSourceTier.YAHOO:
        return "YAHOO_FINANCE", ResearchSourceTier.APPROVED_SECONDARY
    return provider or "APPROVED_SECONDARY", ResearchSourceTier.APPROVED_SECONDARY


def _document_source(
    document: ResearchDocument, requirement_id: str
) -> tuple[str, ResearchSourceTier]:
    if document.discovery_provider == "YAHOO_FINANCE_MCP":
        return "APPROVED_EXTERNAL_TOOL", ResearchSourceTier.APPROVED_EXTERNAL_TOOL
    host = (urlparse(document.canonical_url).hostname or "").casefold()
    if document.source_classification == SourceClassification.EXCHANGE:
        return ("NSE" if "nseindia" in host or "nse" in document.source_name.casefold() else "COMPANY_FILING"), ResearchSourceTier.OFFICIAL
    if document.source_classification == SourceClassification.REGULATORY:
        return ("SEC_EDGAR" if "sec.gov" in host else "REGULATORY_FILING"), ResearchSourceTier.REGULATORY
    if document.source_classification == SourceClassification.OFFICIAL_COMPANY:
        return (
            "OFFICIAL_COMPANY" if requirement_id == "CURRENT_NEWS" else "COMPANY_FILING",
            ResearchSourceTier.OFFICIAL,
        )
    if document.source_classification == SourceClassification.REPUTABLE_NEWS:
        return "REPUTABLE_NEWS", ResearchSourceTier.APPROVED_SECONDARY
    return "APPROVED_SECONDARY", ResearchSourceTier.APPROVED_SECONDARY


def _event_source(
    event: ResearchEvent, requirement_id: str
) -> tuple[str, ResearchSourceTier]:
    if str(event.independence_key or "").startswith("YAHOO_FINANCE_MCP:"):
        return "APPROVED_EXTERNAL_TOOL", ResearchSourceTier.APPROVED_EXTERNAL_TOOL
    host = (urlparse(event.source_url).hostname or "").casefold()
    if event.source_classification == SourceClassification.EXCHANGE:
        return ("NSE" if "nseindia" in host else "REGULATORY_FILING"), ResearchSourceTier.OFFICIAL
    if event.source_classification == SourceClassification.REGULATORY:
        return (
            "REGULATOR_OR_COURT_RECORD"
            if requirement_id == "GOVERNANCE_HISTORY"
            else "REGULATORY_FILING",
            ResearchSourceTier.REGULATORY,
        )
    if event.source_classification == SourceClassification.OFFICIAL_COMPANY:
        return (
            "OFFICIAL_COMPANY" if requirement_id == "CURRENT_NEWS" else "COMPANY_FILING",
            ResearchSourceTier.OFFICIAL,
        )
    return "REPUTABLE_NEWS", ResearchSourceTier.APPROVED_SECONDARY


def _structured_source_tier(provider: str) -> ResearchSourceTier:
    normalized = str(provider).strip().upper()
    if normalized == "YAHOO_FINANCE_MCP":
        return ResearchSourceTier.APPROVED_EXTERNAL_TOOL
    if normalized in {"NSE", "NSE_STRUCTURED", "EXCHANGE_MARKET_DATA"}:
        return ResearchSourceTier.TRUSTED_MARKET_DATA
    if normalized == "EODHD":
        return ResearchSourceTier.LICENSED_STRUCTURED
    return ResearchSourceTier.APPROVED_SECONDARY


def _market_source(provider: str) -> str:
    normalized = str(provider).strip().upper()
    if normalized in {"NSE", "NSE_STRUCTURED"}:
        return "EXCHANGE_MARKET_DATA"
    return normalized or "CONFIGURED_MARKET_DATA"


def _combined_failure_reason(existing: str | None, additional: str | None) -> str:
    values: list[str] = []
    for candidate in (existing, additional):
        for value in str(candidate or "").split("|"):
            normalized = value.strip()
            if normalized and normalized not in values:
                values.append(normalized)
    return "|".join(values)


def _deduplicate_evidence(values: Sequence[ResearchEvidence]) -> list[ResearchEvidence]:
    unique: dict[tuple[str, str | None, str | None], ResearchEvidence] = {}
    for item in values:
        key = (item.evidence_id, item.fact_key, item.value_fingerprint)
        unique.setdefault(key, item)
    return list(unique.values())


def _metric(value: str) -> str:
    return str(value).strip().casefold().replace("-", "_").replace(" ", "_")


def _period_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = datetime.combine(parsed.date(), time.max, tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _aware_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _fingerprint(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return str(value).strip()


def _date_key(value: datetime | None) -> str:
    return value.astimezone(timezone.utc).isoformat() if value is not None else "UNKNOWN"


def _is_india(profile: CompanyResearchProfile) -> bool:
    return jurisdiction_for_profile(profile) == "INDIA"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
