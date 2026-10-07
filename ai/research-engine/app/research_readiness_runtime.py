"""Durable adapters and targeted runtime execution for research readiness.

The read path in this module performs database and canonical-metadata reads
only.  Provider work is confined to ``ExistingResearchCapabilityExecutor`` and
is reachable only from the explicit ensure command.
"""
from __future__ import annotations

import asyncio
import logging
import time as _time
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse
from uuid import UUID

logger = logging.getLogger(__name__)

from app import cycle_timing
from app.readiness_signals import evidence_notifications, evidence_committed

from app.research_applicability import classify_requirements, CONCEPT_INPUTS, CONCEPT_CATEGORIES
from app.market_sessions import price_session_valid_until
from app.normalization import content_hash

from app.fact_precedence import FactSourceTier, FinancialFact
from app.historical_market_data import HistoricalPriceIdentityConflict, HistoricalPriceProviderError, HistoricalPricePersistenceError, has_year_historical_coverage
from app.structured_market import derive_and_verify_nse_yahoo_mapping, _exchange_family
from app.structured_financial import OfficialFinancialProvider
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
    FreshnessPolicyRegistry,
    ProviderAuthorityRegistry,
    REQUIREMENT_STATUSES_NEEDING_ACQUISITION,
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
# Structured/history acquisition contract for admitted candidates only. The
# full-universe scanner reads persistence without ensuring these requirements.
# Deterministic discovery (including sparse-candidate rotation) admits at most
# shortlist_limit identities before this provider-capable contract is executed.
# Official document discovery, governance and PDF fallback remain deep work;
# final recommendation readiness still uses the full existing requirement set.
BASELINE_REQUIREMENT_IDS = frozenset(
    {
        "LATEST_PRICE",
        "HISTORICAL_PRICE_SERIES",
        "VALUATION_INPUTS",
        "SECTOR_MACRO",
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
        # Field-level (not whole-snapshot) coverage: the same period may
        # have more than one real snapshot (NSE structured, Yahoo MCP,
        # official NSE PDF) each holding different fields -- e.g. NSE has
        # promoter/institutional categories but not pledge, while Yahoo or
        # an NSE PDF supplies pledge. shareholding_for() above still returns
        # only the single best snapshot per period (unchanged, still used
        # by every other existing caller); this grouped accessor exposes
        # every real snapshot for the most recent qualifying period so
        # _append_shareholding can union their coverage without discarding
        # any of them.
        shareholding_period_groups_fn = getattr(self.repository, "shareholding_period_groups", None)
        if callable(shareholding_period_groups_fn):
            shareholding_groups = shareholding_period_groups_fn(global_instrument_id, limit=4)
        else:
            # A lightweight test/fixture repository that predates this
            # accessor: fall back to one single-snapshot group per
            # shareholding_for() result, which is exactly the old
            # behavior (no cross-source field merge, but no regression
            # either).
            shareholding_groups = [[snapshot] for snapshot in shareholding]

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
        from app.research_applicability import shareholding_scorable_ownership_coverage
        self._append_shareholding(
            evidence, shareholding_groups[0] if shareholding_groups else (),
            scorable=shareholding_scorable_ownership_coverage(shareholding),
        )
        self._append_canonical_sector(evidence, global_instrument_id, profile, structured)

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
        applicability = classify_requirements(str(sector) if sector else None, str(industry) if industry else None, classification_source, asset_type=metadata.get("assetType"))
        schedules, exceptions = self._sessions.get(global_instrument_id, ([], []))
        if schedules:
            market = profile.mic or profile.exchange
            for requirement_id in ("LATEST_PRICE", "VALUATION_INPUTS", "HISTORICAL_PRICE_SERIES"):
                if requirement_id not in evidence:
                    continue
                values = []
                for item in evidence[requirement_id]:
                    if item.as_of:
                        # A genuine session close stays usable through the next
                        # expected trading session open (weekends/holidays extend
                        # validity rather than aging it). None -> wall-clock TTL
                        # fallback (fail-closed / conservative).
                        valid_until = price_session_valid_until(market, schedules, exceptions, item.as_of)
                        if valid_until and ('LATEST_USABLE_PRICE' in item.covered_input_ids
                                            or item.evidence_id.startswith('derived-valuation:')
                                            or requirement_id == "HISTORICAL_PRICE_SERIES"):
                            item = replace(item, valid_until=valid_until)
                    values.append(item)
                evidence[requirement_id] = values

        acquisition = {}
        observations_loader = getattr(self.repository, "acquisition_observations_for", None)
        if callable(observations_loader):
            for observation in observations_loader(global_instrument_id):
                key = observation["requirement_id"]
                history = acquisition.get(key, {}).get("history", [])
                # Executor completion describes orchestration, not provider evidence.
                selected = acquisition.get(key, {}) if observation.get("provider") == "READINESS_EXECUTOR" and observation.get("outcome") == "COMPLETED" and history else observation
                acquisition[key] = {**selected, "history": [*history, observation]}
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
        catalyst_checked_at = _completed_authoritative_check(
            acquisition.get("ORDER_BOOK_CAPEX_GUIDANCE", {}).get("history", []))
        if catalyst_checked_at is not None:
            # Explicit zero-result coverage (same principle as a completed news
            # search): the latest authoritative NSE announcement check for the
            # catalyst categories completed. It covers the concept inputs as
            # "checked" -- it is not an event and carries no score. Concepts
            # excluded as NOT_APPLICABLE are removed by applicability as usual.
            # A never-run or FAILED latest check adds nothing (incomplete/retry).
            evidence["ORDER_BOOK_CAPEX_GUIDANCE"].append(ResearchEvidence(
                evidence_id="catalyst-check:NSE:" + catalyst_checked_at.isoformat(),
                requirement_id="ORDER_BOOK_CAPEX_GUIDANCE", source="NSE",
                source_tier=ResearchSourceTier.OFFICIAL, retrieved_at=catalyst_checked_at,
                as_of=catalyst_checked_at, published_at=catalyst_checked_at, event_date=catalyst_checked_at,
                covered_input_ids=("MATERIAL_CATALYST_EVIDENCE", "ORDER_BOOK_OR_MAJOR_CONTRACT",
                                   "CAPACITY_OR_CAPEX_OR_COMMISSIONING", "MANAGEMENT_GUIDANCE")))
        governance_checked_at = _completed_authoritative_check(
            acquisition.get("GOVERNANCE_HISTORY", {}).get("history", []))
        if governance_checked_at is not None:
            # STEP-8A: same principle as the catalyst check above and the
            # CURRENT_NEWS clean search -- the latest authoritative NSE
            # governance-category check (REGULATORY/MANAGEMENT/RISKS)
            # completed. It covers GOVERNANCE_EVIDENCE as "checked"; it is
            # not an event/document and carries no score, and is always an
            # ADDITIONAL evidence entry -- a genuine document/event found by
            # _append_documents/_append_events above is never replaced or
            # shadowed by it. _completed_authoritative_check only recognizes
            # a completed (SUCCESS or SUCCESS_EMPTY) NSE-provider check, so a
            # never-run, FAILED, generic-search-only (provider != NSE), or
            # (per the repository.py partial-budget guard) partially-covered
            # check adds nothing here.
            evidence["GOVERNANCE_HISTORY"].append(ResearchEvidence(
                evidence_id="governance-check:NSE:" + governance_checked_at.isoformat(),
                requirement_id="GOVERNANCE_HISTORY", source="NSE",
                source_tier=ResearchSourceTier.OFFICIAL, retrieved_at=governance_checked_at,
                as_of=governance_checked_at, published_at=governance_checked_at, event_date=governance_checked_at,
                covered_input_ids=("GOVERNANCE_EVIDENCE",)))
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
            document_diagnostics=tuple(_document_diagnostic(document, facts, events, shareholding) for document in documents),
        )

    @staticmethod
    def _append_financial_evidence(
        evidence: dict[str, list[ResearchEvidence]], facts: Sequence[FinancialFact]
    ) -> None:
        real = [fact for fact in facts if fact.source_mode == SourceMode.REAL]
        from app.normalization import normalize_financial_amount
        def unit_for(fact):
            return normalize_financial_amount(Decimal(0), fact.value.unit)[1]
        periods_by_series: dict[tuple, dict[str, set[str]]] = {}
        # Quarterly revenue<->earnings comparability (QUARTERLY_YOY_QOQ_TRENDS /
        # QUARTERLY_FINANCIALS.COMPARABLE_QUARTERS) has two DIFFERENT unit
        # requirements that must not be conflated:
        #   - WITHIN one metric family across time, unit must stay consistent
        #     (a revenue quarter reported in USD is not comparable to one
        #     reported in INR merely because both are "revenue" -- this is
        #     the same protection periods_by_series/REVENUE_HISTORY already
        #     give a single metric, extended here to the family as a whole).
        #   - ACROSS metric families (revenue vs earnings), unit need NOT
        #     match: revenue and earnings are different physical measures by
        #     nature (e.g. INR vs INR/share for EPS) and legitimately never
        #     share a unit, so requiring one would make cross-metric
        #     quarterly-trend coverage unsatisfiable for any issuer whose
        #     earnings are reported per-share (KPIGREEN's NSE EPS).
        # So each (reporting_basis, family) tracks its periods PER UNIT, and
        # the family's own comparable series is its single largest same-unit
        # bucket (the ordinary case has exactly one unit per family, so this
        # is a no-op there) -- family-vs-family intersection then compares
        # two internally-consistent series without requiring their units to
        # agree with each other. Reporting basis (CONSOLIDATED/STANDALONE/
        # UNKNOWN) is still the discriminator that must never be mixed.
        # periods_by_series and annual_periods below are per-metric-own-
        # history checks (REVENUE_HISTORY, EARNINGS_HISTORY,
        # ANNUAL_CAGR_INPUTS all read a single metric's own period count,
        # never a cross-metric intersection), so their existing (basis, unit)
        # key is untouched -- it already gives each metric the same
        # same-unit-across-time protection described above.
        quarterly_metrics: dict[str, dict[str, dict[str, set[str]]]] = {}
        all_quarterly_periods: set[str] = set()
        annual_periods: dict[tuple, set[str]] = {}
        for fact in real:
            metric = _metric(fact.key.metric)
            if not fact.key.period_end:
                continue
            period = fact.key.period_end[:10]
            reporting_basis = fact.key.reporting_basis or "UNKNOWN"
            unit = unit_for(fact)
            basis = (reporting_basis, unit)
            periods_by_series.setdefault((fact.key.period_type, basis), {}).setdefault(metric, set()).add(period)
            if fact.key.period_type == "QUARTERLY":
                all_quarterly_periods.add(period)
                # "eps" is a legitimate earnings measure (see EARNINGS_HISTORY
                # / EARNINGS_BASIS below, which already treat it as such) --
                # it must join the same family as pat/net_income/net_profit
                # here too, or an issuer whose NSE quarterly filings report
                # EPS but no separate PAT/net-income line (KPIGREEN) would
                # never have anything to intersect revenue against.
                family = (
                    "revenue" if metric in {"revenue", "total_revenue"}
                    else "earnings" if metric in {"eps", "pat", "net_income", "net_profit"}
                    else metric
                )
                quarterly_metrics.setdefault(reporting_basis, {}).setdefault(family, {}).setdefault(unit, set()).add(period)
            elif fact.key.period_type == "ANNUAL":
                annual_periods.setdefault(basis, set()).add(period)
        latest_quarter = max(all_quarterly_periods, default=None)

        def _dominant_quarterly_periods(reporting_basis: str, family: str) -> set[str]:
            by_unit = quarterly_metrics.get(reporting_basis, {}).get(family, {})
            return max(by_unit.values(), key=len, default=set())

        for fact in real:
            metric = _metric(fact.key.metric)
            reporting_basis = fact.key.reporting_basis or "UNKNOWN"
            coverage = _financial_fact_coverage(
                fact,
                metric,
                periods_by_series.get((fact.key.period_type, (reporting_basis, unit_for(fact))), {}),
                _dominant_quarterly_periods(reporting_basis, "revenue")
                    & _dominant_quarterly_periods(reporting_basis, "earnings"),
                annual_periods.get((reporting_basis, unit_for(fact)), set()),
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
                                if requirement_id in ('VALUATION_INPUTS','BALANCE_SHEET_FACTS')
                                and fact_name in {'trailingEps','forwardEps','bookValue','totalDebt','debtToEquity','totalCash'} else None),
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
            # DI-12A: a document carries no numerical value, so it must NOT
            # cover numerical QUARTERLY_FINANCIALS inputs (e.g. the mandatory
            # LATEST_QUARTERLY_RESULT) on a loose keyword match such as
            # "financial result"/"quarterly result"/"earnings". Previously this
            # branch let a keyword-only NSE press-release (its parse producing
            # zero persisted FinancialFact rows) independently satisfy
            # LATEST_QUARTERLY_RESULT and become the selected authority (Castrol:
            # READY_FRESH with sourceProvider=NSE, OFFICIAL_NSE_FACTS=0). The
            # authoritative numerical coverage for QUARTERLY_FINANCIALS is
            # derived exclusively from persisted FinancialFact rows in
            # _append_financial_evidence. Documents retain their legitimate
            # non-financial contributions below (governance / sector-macro).
            from app.extraction import governance_disclosure
            if document.normalized_text and governance_disclosure(text):
                evidence["GOVERNANCE_HISTORY"].append(
                    _evidence_from_document(
                        document,
                        "GOVERNANCE_HISTORY",
                        ("GOVERNANCE_EVIDENCE",),
                        unresolved=any(
                            term in text for term in ("pending litigation", "ongoing litigation", "fraud investigation", "regulatory action", "enforcement action")
                        ) and not any(term in text for term in ("resolved", "case closed", "no wrongdoing", "settled and closed")),
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
        evidence: dict[str, list[ResearchEvidence]], period_group: Sequence[Any],
        *, scorable: bool = True,
    ) -> None:
        if not period_group:
            return
        from app.research_applicability import shareholding_field_merge
        # Authority-aware, field-level union across every real snapshot for
        # this one period (NSE structured/XBRL > Yahoo MCP > official NSE
        # PDF -- see shareholding_source_authority_rank()). Each
        # contributing snapshot gets its own ResearchEvidence row, scoped to
        # only the fields it is the highest-authority source for, so a
        # lower-priority source can never be read as overwriting a
        # higher-priority one for the same field+period, and no synthetic
        # merged snapshot/provenance is ever manufactured. The existing
        # coverage aggregation (ShareholdingCoverageService.covered_input_ids)
        # already unions covered_input_ids across every evidence row for a
        # requirement, so emitting more than one row here is sufficient --
        # no change needed there.
        #
        # `scorable` (Stage-2 follow-up: readiness/scoring reconciliation):
        # shareholding_field_merge()/shareholding_input_coverage() answer
        # "is this category present in this one period" -- a per-field
        # bookkeeping question, unrelated to whether the rule engine's
        # StockRuleEngine._shareholding() scorer can actually turn that
        # evidence into a metric (which may additionally require a second,
        # trend-compatible period -- see
        # shareholding_scorable_ownership_coverage()). Only the mandatory
        # PROMOTER_INSTITUTIONAL_PUBLIC_CATEGORIES input is gated by that
        # broader, multi-period check: LATEST_VALID_SHAREHOLDING_PERIOD and
        # the (supporting, optional) PROMOTER_PLEDGE input are untouched,
        # so a genuinely present pledge value is never hidden by this gate.
        for snapshot, covered in shareholding_field_merge(period_group):
            covered = set(covered)
            if not scorable:
                covered.discard("PROMOTER_INSTITUTIONAL_PUBLIC_CATEGORIES")
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
                        else ResearchSourceTier.USER_UPLOAD
                        if snapshot.source_provider.upper() == "USER_UPLOAD"
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
        structured: Sequence[StructuredMarketSnapshotRecord] = (),
    ) -> None:
        metadata = self._canonical_metadata.get(instrument_id, {})
        sector = (
            metadata.get("canonicalSector")
            or metadata.get("sector")
            or metadata.get("industrySector")
        )
        if sector:
            # Canonical registry sector is authoritative. Returning here means
            # the structured-market fallback below is never even consulted --
            # authority ordering (canonical > structured fallback > missing) is
            # enforced procedurally, not just by evidence-tier ranking.
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
            return
        # Canonical registry has no usable sector for this instrument. Fall
        # back to an already-persisted structured-market 'sector' fact -- the
        # same durable snapshot _append_structured_evidence already reads, so
        # this introduces no new network call. The evidence's source/tier
        # explicitly identify it as structured-market/Yahoo-derived (never
        # EXCHANGE_OR_INDEX_PROVIDER/TRUSTED_MARKET_DATA, which would
        # misrepresent it as canonical/NSE provenance), and its timestamp is
        # the fact's own retrieved/as-of time -- never the canonical
        # registry's updatedAt, which does not apply to a value the registry
        # never supplied. Industry is deliberately not used as a stand-in:
        # no existing domain logic treats industry as an acceptable sector
        # substitute, and inventing that mapping here would fabricate data.
        fallback = _structured_sector_fact(structured)
        if fallback is None:
            return
        value, source_name, source_tier, fact_time = fallback
        evidence["SECTOR_MACRO"].append(
            ResearchEvidence(
                evidence_id=f"structured-sector:{instrument_id}:{value.strip().casefold()}",
                requirement_id="SECTOR_MACRO",
                source=source_name,
                source_tier=source_tier,
                retrieved_at=fact_time,
                as_of=fact_time,
                fact_key="structured-sector",
                value_fingerprint=value.strip(),
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
    completed_capabilities: set[str] = field(default_factory=set)

    def executed(self, capability: str) -> None:
        if capability not in self.executed_capabilities:
            self.executed_capabilities.append(capability)

    def failed(self, requirement_id: str, reason: str) -> None:
        self.failures[requirement_id] = _combined_failure_reason(
            self.failures.get(requirement_id), reason
        )

    def completed(self, capability: str) -> None:
        self.completed_capabilities.add(capability)

    def satisfied(self, requirement_id: str) -> None:
        self.satisfied_requirement_ids.add(requirement_id)


async def memoized_assess(
    repository,
    memo: dict,
    global_instrument_id: UUID,
    *,
    jurisdiction: str,
    evidence_only: bool = False,
    now: datetime | None = None,
):
    """Collapse within-call duplicate `ResearchReadinessService.assess` reads.

    Root-cause fix (forensic cycle 89fd630e / Stage-2 amplification): one
    Stage-2 candidate was observed making ~46 ResearchReadinessService.assess
    calls on average (1147 calls / 25 candidates), each one re-running
    RepositoryResearchReadinessAdapter.load_by_global_instrument_id -- eight
    separate repository reads (profile, financial facts, structured market,
    price observations, documents, events, shareholding, shareholding period
    groups) -- through `_run_blocking_persistence`'s thread-dispatch path.

    ``ResearchReadinessRuntime.read()`` already memoizes evidence_only reads
    using ``repository._readiness_mutation_generation`` (bumped on every
    persistence *write*) as the staleness sentinel, but every direct-call
    site inside McpFirstResearchCapabilityExecutor.execute_primary and
    ExistingResearchCapabilityExecutor.execute_primary constructs its own
    throwaway ``ResearchReadinessService(...)`` and calls ``.assess``
    directly, bypassing that cache entirely -- so two assess() calls with
    *no durable write in between* (e.g. an optional provider attempt that
    raised before persisting anything, or two branches of the same
    single acquisition pass both re-checking the same candidate) each pay
    the full reload cost again.

    This helper applies the exact same, already-trusted invalidation rule
    -- not a new caching policy -- scoped to a `memo` dict the caller
    creates fresh at the top of ITS OWN single execute_primary invocation
    (never shared across candidates, never module/global state, never
    outliving one call). A cached result is only ever returned when the
    mutation generation has not advanced since it was captured, so any
    committed write (acquisition, observation, document fetch, ...)
    immediately invalidates it and the next call reloads from current
    durable evidence -- final readiness still always comes from current
    durable evidence (Invariant 1); this only removes work that was
    PROVABLY redundant (identical state, nothing wrote in between).
    """
    key = (global_instrument_id, jurisdiction, bool(evidence_only))
    generation = getattr(repository, "_readiness_mutation_generation", None)
    cached = memo.get(key)
    if cached is not None and generation is not None:
        cached_result, cached_generation = cached
        if cached_generation == generation and now is None:
            return cached_result
    result = await repository._run_blocking_persistence(
        ResearchReadinessService(RepositoryResearchReadinessAdapter(repository)).assess,
        global_instrument_id,
        jurisdiction=jurisdiction,
        evidence_only=evidence_only,
        now=now,
    )
    if generation is not None and now is None:
        memo[key] = (result, generation)
    return result


class ExistingResearchCapabilityExecutor:
    """Map planner targets to the narrow provider capabilities already present."""

    def __init__(self, repository, orchestrator, market_data_population_jobs, *,
                 official_financial_provider: OfficialFinancialProvider | None = None) -> None:
        self.repository = repository
        self.orchestrator = orchestrator
        self.market_data_population_jobs = market_data_population_jobs
        self.official_financial_provider = official_financial_provider

    def retention_stats(self):
        provider = getattr(self.orchestrator, 'structured_provider', None)
        contexts = getattr(provider, 'ticker_contexts', None)
        if contexts is None:
            return {}
        return {f'yahoo_{key}': value for key, value in contexts.retention_stats().items()}

    async def acquire_official_financials(self, global_instrument_id: UUID) -> None:
        """Structured NSE acquisition only; document fallback belongs to execute_primary."""
        if self.official_financial_provider is None:
            return
        facts = await self.official_financial_provider.collect(self.repository.profile(global_instrument_id))
        if any(fact.key.instrument_id != global_instrument_id
               or fact.source_provider != "NSE" or fact.source_tier != FactSourceTier.OFFICIAL_NSE
               or fact.source_mode != SourceMode.REAL or not fact.source_identity
               or urlparse(fact.value.source_url).scheme != "https"
               or not ((urlparse(fact.value.source_url).hostname or "") == "nseindia.com"
                       or (urlparse(fact.value.source_url).hostname or "").endswith(".nseindia.com"))
               for fact in facts):
            raise ValueError("OFFICIAL_STRUCTURED_PROVENANCE_INVALID")
        await self.repository.persist_international_financial_facts_async(facts)
        if facts:
            evidence_committed(global_instrument_id)

    async def execute_primary(
        self,
        global_instrument_id: UUID,
        targets: Sequence[ResearchRefreshTarget],
        *,
        jurisdiction: str,
        correlation_id: str | None,
        identity_headers: Mapping[str, str | None] | None,
        progress: CapabilityExecutionProgress | None = None,
        deadline: float | None = None,
        official_financials_attempted: bool = False,
    ) -> CapabilityExecutionResult:
        requirement_ids = {target.requirement_id for target in targets}
        unknown = requirement_ids - _KNOWN_REQUIREMENTS
        if unknown:
            raise KeyError(f"No targeted capability mapping for {sorted(unknown)}")
        executed: list[str] = []
        failures: dict[str, str] = {}
        # Scoped to this single execute_primary call only -- see
        # memoized_assess's docstring (forensic cycle 89fd630e readiness-
        # amplification fix). Never shared across candidates or calls.
        _assess_memo: dict = {}

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
                    global_instrument_id, structured_classes, baseline_only=True,
                    # Item 2: the current asyncio task is this one ensure()
                    # call's own execution -- execute_primary and a later
                    # execute_approved_fallbacks for the SAME requirement set
                    # run as plain sequential awaits within it (never a
                    # separately spawned task), so sharing this identity lets
                    # them reuse one ticker acquisition for this candidate/
                    # cycle, while a different ensure() call (a different
                    # task) always gets its own fresh context.
                    acquisition_context=asyncio.current_task(),
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
            # Cancellation skips this point, leaving the capability in flight.
            if progress is not None:
                progress.completed("STRUCTURED_MARKET")

        financial = requirement_ids & _FINANCIAL_REQUIREMENTS
        official_structured_failure = None
        repository_categories: set[str] = set()
        if financial:
            executed.append("FINANCIALS")
            if progress is not None:
                progress.executed("FINANCIALS")
            if jurisdiction == "INDIA":
                remaining = financial
                if self.official_financial_provider is not None or official_financials_attempted:
                    try:
                        if not official_financials_attempted:
                            await self.acquire_official_financials(global_instrument_id)
                        current = await memoized_assess(
                            self.repository, _assess_memo, global_instrument_id, jurisdiction=jurisdiction)
                        remaining = {key for key in financial if
                            current.for_requirement(key).status != ResearchRequirementStatus.READY_FRESH
                            or current.for_requirement(key).source != "NSE"}
                        logger.info("radar_acquisition_count operation=pdf_fallback_avoided reason=OFFICIAL_STRUCTURED count=%d",
                                    len(financial - remaining))
                    except Exception as exc:
                        # An optional provider failure never suppresses the
                        # existing official-document path or certifies evidence.
                        official_structured_failure = type(exc).__name__
                        # Preserve the exception message (bounded, no PII --
                        # our own code only raises short internal taxonomy
                        # codes or stdlib TypeError/ValueError text here) and
                        # the traceback, so a deterministic defect is
                        # diagnosable from logs instead of a bare class name.
                        logger.warning("official_structured_financial_failed reason=%s detail=%s",
                                        type(exc).__name__, str(exc)[:200], exc_info=True)
                if remaining:
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
            excluded = set().union(*(set(target.excluded_input_ids) for target in targets
                if target.requirement_id == "ORDER_BOOK_CAPEX_GUIDANCE"))
            for concept, categories in CONCEPT_CATEGORIES.items():
                if CONCEPT_INPUTS[concept] not in excluded:
                    repository_categories.update(categories)
        if "CURRENT_NEWS" in requirement_ids:
            executed.append("GLOBAL_NEWS_SEARCH")
            if progress is not None:
                progress.executed("GLOBAL_NEWS_SEARCH")
            news_worker=getattr(self.repository,'refresh_news_intelligence',None)
            if callable(news_worker):
                try:
                    outcome = await news_worker(global_instrument_id)
                except Exception:
                    failures['CURRENT_NEWS']='NEWS_INTELLIGENCE_UNAVAILABLE'
                else:
                    reason = _incomplete_news_search_reason(outcome)
                    if reason:
                        # A partial/failed search is a technical gap (retryable),
                        # never "no news exists".
                        failures['CURRENT_NEWS'] = reason
            else:
                repository_categories.update({'CATALYSTS','RISKS','REGULATORY','MANAGEMENT','GUIDANCE'})
        if "GOVERNANCE_HISTORY" in requirement_ids:
            executed.append("GOVERNANCE_EVIDENCE")
            if progress is not None:
                progress.executed("GOVERNANCE_EVIDENCE")
            repository_categories.update({"RISKS", "REGULATORY", "MANAGEMENT"})
        if repository_categories:
            try:
                upgrade_options = {}
                financial_targets = [target for target in targets if target.requirement_id in _FINANCIAL_REQUIREMENTS]
                if financial_targets and all(target.reason == ResearchRequirementStatus.READY_FRESH for target in financial_targets):
                    # Fresh targets were added for authority only. Do not turn
                    # the legacy force-refresh flag into broad financial work.
                    upgrade_options["authority_upgrade_categories"] = {"FINANCIAL_RESULTS"}
                await self.repository.refresh_targeted_categories(
                    global_instrument_id,
                    repository_categories,
                    correlation_id=correlation_id,
                    allow_demo=True,
                    **upgrade_options,
                )
            except Exception as exc:
                # A shared executor exception (from ONE
                # repository.refresh_targeted_categories call spanning several
                # requirement categories) must NOT be copied blindly to every
                # requirement that happened to be in the intersected set -- that
                # call returns a single ResearchSummary and raises a single
                # exception with NO per-requirement ownership. Assigning
                # `type(exc).__name__` to (e.g.) SHAREHOLDING just because
                # SHAREHOLDING_PATTERN was part of the fused category set would
                # misattribute an unrelated search/discovery outage
                # (SEARCH_PROVIDER_DEGRADED from a catalyst/governance NEWS_SEARCH)
                # to a requirement whose own channel (NSE structured/XBRL feed)
                # never ran.
                #
                # Per the UNKNOWN-OWNERSHIP invariant:
                #   UNKNOWN OWNERSHIP OF FAILURE != FAILURE OF EVERY REQUIREMENT
                #
                # We therefore do NOT distribute this exception to any
                # requirement id in `failures`. The genuinely-failed requirement
                # is still marked failed by its OWN dedicated acquisition
                # observation (RepositoryResearchReadinessAdapter records a per-
                # requirement persisted observation, and deep_investigation._finalize
                # records requirement_failures on that requirement's own budget
                # before/after this shared call). This shared exception has no
                # such per-requirement owner, so it is logged here -- in full,
                # with traceback -- as an explicit unattributed shared-execution
                # failure, and is NOT suppressed (the traceback is preserved for
                # diagnosis). It is recorded only on the request-local progress
                # object under a sentinel key that never flows into any
                # requirement's final classification (the sentinel contains no
                # requirement id, so it cannot contaminate SHAREHOLDING or any
                # other participant).
                exc_name = type(exc).__name__
                logger.warning(
                    "execute_primary_shared_refresh_failed reason=%s detail=%s "
                    "categories=%s requirements=%s",
                    exc_name, str(exc)[:200], sorted(repository_categories),
                    sorted(requirement_ids), exc_info=True,
                )
                if progress is not None:
                    progress.failed("UNATTRIBUTED_SHARED_EXECUTION_FAILURE", exc_name)

        if official_structured_failure is not None:
            # A failed structured attempt must not disappear behind a PDF path
            # that completed with no supported facts. Clear it only when the
            # fallback actually satisfied that requirement with official facts.
            current = await memoized_assess(
                self.repository, _assess_memo, global_instrument_id, jurisdiction=jurisdiction)
            from app.deep_investigation import acquisition_budget
            budget = acquisition_budget(global_instrument_id)
            for requirement_id in financial:
                row = current.for_requirement(requirement_id)
                if row.status == ResearchRequirementStatus.READY_FRESH and row.source == "NSE":
                    continue
                reason = "|".join(dict.fromkeys(filter(None, (
                    failures.get(requirement_id), official_structured_failure))))
                failures[requirement_id] = reason
                if budget is not None:
                    budget.requirement_failures[requirement_id] = reason
                if progress is not None:
                    progress.failed(requirement_id, reason)

        if "HISTORICAL_PRICE_SERIES" in requirement_ids:
            executed.append("HISTORICAL_MARKET_DATA")
            if progress is not None:
                progress.executed("HISTORICAL_MARKET_DATA")
            try:
                await self._ensure_historical_prices(global_instrument_id)
            except Exception as exc:
                # A stable, truthful technical-failure reason (identity
                # conflict, missing verified mapping, provider/persistence
                # unavailability) must survive into the failure ledger, not
                # collapse into a bare exception class name that discards
                # exactly the information needed to tell a technical failure
                # apart from a genuinely-unavailable one.
                reason = (
                    str(exc)
                    if isinstance(exc, (ValueError, HistoricalPriceIdentityConflict,
                                        HistoricalPriceProviderError, HistoricalPricePersistenceError))
                    and str(exc)
                    else type(exc).__name__
                )
                failures["HISTORICAL_PRICE_SERIES"] = reason
                if progress is not None:
                    progress.failed("HISTORICAL_PRICE_SERIES", reason)
            # Cancellation skips this point, leaving the capability in flight.
            if progress is not None:
                progress.completed("HISTORICAL_MARKET_DATA")

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
                global_instrument_id, {"FUNDAMENTALS"},
                acquisition_context=asyncio.current_task(),
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

    async def _derive_verified_nse_yahoo_mapping(self, profile) -> str | None:
        """Generic global fallback when a profile has no verified Yahoo
        mapping yet (the systemic VERIFIED_YAHOO_MAPPING_REQUIRED failure
        that blocked 75/76 real Stage-2 candidates): derive a candidate from
        the canonical NSE symbol and verify it via the existing live
        Yahoo-resolution identity gate, never a fabricated mapping. Scoped
        to NSE-listed instruments only, matching the production NSE-universe
        contract; never invented for a symbol/company with no evidence.
        """
        if _exchange_family(profile.exchange, profile.ticker) != "XNSE":
            return None
        structured_provider = getattr(self.orchestrator, "structured_provider", None)
        if structured_provider is None:
            return None
        return await derive_and_verify_nse_yahoo_mapping(
            structured_provider,
            nse_ticker=profile.ticker,
            isin=profile.isin,
            company_name=profile.company_name,
            currency=profile.currency,
            instrument_id=profile.instrument_id,
        )

    async def _ensure_historical_prices(self, global_instrument_id: UUID) -> int:
        profile = self.repository.profile(global_instrument_id)
        provider_ticker = profile.provider_instrument_ids.get("YAHOO_FINANCE")
        if not provider_ticker:
            provider_ticker = await self._derive_verified_nse_yahoo_mapping(profile)
            if not provider_ticker:
                raise ValueError("VERIFIED_HISTORICAL_MAPPING_REQUIRED")
            # Reuse durably for the remainder of this process's lifetime:
            # profile is the same process-local CompanyResearchProfile object
            # every subsequent ensure() call for this instrument reads via
            # self.repository.profile(...), so a warm re-run of the same
            # candidate never re-verifies a mapping that is still valid. This
            # is in-memory/process-lifetime reuse, not cross-restart durable
            # persistence -- no schema/store in this codebase currently owns
            # a durable global Yahoo-mapping cache (see Slice 2 report).
            profile.provider_instrument_ids["YAHOO_FINANCE"] = provider_ticker
        observations = (
            await self.repository.market_price_observations_for_instruments(
                {global_instrument_id}
            )
        ).get(global_instrument_id, [])
        now = datetime.now(timezone.utc)
        usable = [value for value in observations if value.price is not None
                  and Decimal(str(value.price)).is_finite() and Decimal(str(value.price)) > 0
                  and value.observed_at <= now]
        # A fresh NSE quote must not hide the missing tail of Yahoo's daily
        # history. Its high-water mark belongs to the historical provider.
        historical = [value for value in usable if value.provider == "YAHOO_FINANCE"]
        observed_at = [value.observed_at for value in historical or usable]
        has_year = bool(observed_at) and has_year_historical_coverage(
            min(observed_at), max(observed_at), len(observed_at)
        )
        initial_start = (now - timedelta(days=self.market_data_population_jobs.settings.market_data_population_initial_lookback_days)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        ranges = []
        receipt_prefix = f"urn:research:price-backfill:v1:{provider_ticker}:"
        loader = getattr(self.repository, "_latest_acquisition_observation", None)
        receipt = None
        if callable(loader):
            receipt = await self.repository._run_blocking_persistence(loader, global_instrument_id, "HISTORICAL_PRICE_BACKFILL", "YAHOO_FINANCE")
        checked_start = None
        if receipt and receipt.get("outcome") == "CHECKED" and (receipt.get("source_url") or "").startswith(receipt_prefix):
            try:
                checked_start = datetime.fromisoformat(receipt["source_url"][len(receipt_prefix):]).date()
            except ValueError:
                pass
        if not observed_at:
            ranges.append((initial_start, end, True))
        else:
            first = min(observed_at).replace(hour=0, minute=0, second=0, microsecond=0)
            if not has_year and initial_start.date() < first.date() and (checked_start is None or checked_start > initial_start.date()):
                ranges.append((initial_start, first, True))
            tail = (max(observed_at) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            if tail.date() < end.date():
                ranges.append((tail, end, False))
        if not ranges:
            return 0
        instrument = {
            "globalInstrumentId": str(global_instrument_id),
            "structuredProviderTicker": provider_ticker,
            "ticker": profile.ticker,
            "currency": profile.currency,
            # Required for the pre-persistence identity gate
            # (verify_historical_price_identity) to compare what Yahoo
            # actually returns against this canonical instrument's own
            # identity, not merely echo the requested symbol back.
            "isin": profile.isin,
            "companyName": profile.company_name,
            "canonicalName": profile.company_name,
            "exchange": profile.exchange,
        }
        written = 0
        for start, stop, backfill in ranges:
            written += await self.market_data_population_jobs.population.populate([instrument], start=start, end=stop)
            # Only a completed provider/persistence operation certifies the
            # attempted prefix (e.g. a recent listing has no earlier prices).
            # This receipt is never price evidence or a freshness extension.
            recorder = getattr(self.repository, "record_acquisition_observation", None)
            if backfill and callable(recorder):
                await recorder(global_instrument_id, "HISTORICAL_PRICE_BACKFILL", "YAHOO_FINANCE", "CHECKED", now,
                               source_url=receipt_prefix + start.date().isoformat())
        return written


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
        # Keyed by instrument, holding every CONCURRENTLY running ensure()
        # flight for that instrument (not just one) -- see the Root Cause
        # comment on the join logic in ensure() below for why a single slot
        # per instrument was a correctness/performance bug, not just an
        # implementation detail.
        self._flights: dict[UUID, list[_EnsureFlight]] = {}
        # Previously a hard cap on concurrent in-flight ensure() flights retained
        # per instrument (8). REMOVED: the old hard-cap eviction blindly
        # cancelled the oldest ACTIVE flight when the cap was reached, which
        # violated single-flight correctness by terminating shared/overlapping
        # work that followers were shield-joining. Active-flight count is now
        # naturally bounded by legitimate concurrency and requirement sets,
        # not by a numerical cap. The done-callback (cleanup) remains the sole
        # reaper for settled entries. Retained only for backward compatibility.
        self._max_flights_per_instrument = 8
        # Bounded per-instrument cache of the most recent durability-check
        # ResearchReadinessResult, reused only for evidence_only=True reads.
        # This collapses the repeated sufficiency pre-checks in deep_investigation
        # (_requirement_sufficient / _group_sufficient / allow_document) that
        # re-read identical durable evidence while no persistence mutation has
        # occurred between them. The cache is keyed by instrument and carries:
        #   - the cached result
        #   - the repository._readiness_mutation_generation observed when cached
        # Reuse is ONLY permitted when (a) evidence_only=True (the caller
        # classifies committed evidence and performs no provider/owning work),
        # (b) the generation is unchanged (no persistence mutation has
        #     committed new evidence/observations since the snapshot), and
        # (c) the requested `now` is compatible: either now is None
        #     (sub-second `generated_at` drift is immaterial -- no freshness
        #      policy has sub-second granularity, so re-classification with a
        #      fresh now cannot change an evidence_only sufficiency outcome), or
        #     `now` is identical to the cached evaluated_at. A durable
        #     read() (evidence_only=False) or any mutation invalidates the
        #     entry, so the final authoritative verification (investigate
        #     L589) and post-mutation re-reads always hit the DB.
        self._evidence_readiness_cache: dict[UUID, tuple[ResearchReadinessResult, int]] = {}
        # Hard cap on the number of instruments retained in the
        # evidence-only cache. The cache is cleared on every durable read and is
        # per-instrument, but a flow that exits early (e.g. on acquisition
        # timeout before the final durable read) can leave evidence_only entries
        # behind; across the FULL 2432-admitted universe this would accumulate
        # one entry per instrument holding a live ResearchReadinessResult. Cap it
        # so the cache stays bounded at O(stage2_concurrency) instruments rather
        # than O(admitted). Eviction drops the LEAST-recently-written entry,
        # which (per the invariants above) is always durability-stale for the
        # current flow, so evicting it cannot affect a same-flow reuse.
        self._max_evidence_readiness_cache_size = 64

    def retention_stats(self):
        """Separate active work from completed reuse state without retaining it."""
        stats = {
            'active_flights': sum(not flight.task.done() for flights in self._flights.values() for flight in flights),
            'completed_flights': sum(flight.task.done() for flights in self._flights.values() for flight in flights),
            'readiness_cache_entries': len(self._evidence_readiness_cache),
        }
        if callable(getattr(type(self.executor), 'retention_stats', None)):
            stats.update(self.executor.retention_stats())
        documents = getattr(self.repository, 'documents', None)
        if callable(getattr(type(documents), 'stats', None)):
            stats.update({f'documents_{key}': value for key, value in documents.stats().items()})
        return stats

    async def read(
        self,
        global_instrument_id: UUID,
        *,
        jurisdiction: str,
        now: datetime | None = None,
        evidence_only: bool = False,
    ) -> ResearchReadinessResult:
        if isinstance(self.data_source, RepositoryResearchReadinessAdapter):
            self.data_source._evaluation_times[global_instrument_id] = now or datetime.now(timezone.utc)
        # Evidence-only reads are durability-classification sufficiency checks
        # (deep_investigation._requirement_sufficient / _group_sufficient /
        # allow_document) that re-read identical committed evidence while no
        # persistence mutation has occurred between them. Reuse the cached
        # result when it is still durability-current; otherwise fall through to
        # the authoritative reload that also refreshes market_session_data and
        # re-runs assess. Durable reads (evidence_only=False) and reads after a
        # mutation (generation changed) bypass the cache so the final
        # verification and post-mutation re-reads stay authoritative.
        #
        # Safety invariants:
        #   1. The cache is cleared on EVERY durable read (evidence_only=False)
        #      at the end of this method, so an evidence-only snapshot cannot
        #      leak across the durable reads that bracket an investigate() flow
        #      (initial read, per-requirement dispatch reads, group-member
        #      reads, the news final read, and the final verification read).
        #      Any evidence_only read therefore always falls between two
        #      durable reads of the same flow.
        #   2. The repository._readiness_mutation_generation counter is
        #      incremented on every persistence *write*, so a cached snapshot
        #      is only reused when no write has committed new evidence/
        #      observations since it was taken.
        #   3. Reuse requires now is None (the callers' mode) -- the cached
        #      result carries its own generated_at, and evidence_only is a
        #      sufficiency pre-check, so a sub-second-old classification over
        #      unchanged durable evidence is equivalent to a fresh one (no
        #      freshness policy has sub-second granularity). An explicit now
        #      is only reused when identical to the cached generated_at.
        if evidence_only:
            cached = self._evidence_readiness_cache.get(global_instrument_id)
            if cached is not None:
                cached_result, cached_generation = cached
                generation = getattr(self.repository, "_readiness_mutation_generation", 0)
                now_compatible = now is None or now == cached_result.generated_at
                if cached_generation == generation and now_compatible:
                    # True LRU: move this entry to the end (most-recently-used)
                    # so eviction drops the genuinely least-recently-used entry,
                    # not just the least-recently-inserted one.
                    del self._evidence_readiness_cache[global_instrument_id]
                    self._evidence_readiness_cache[global_instrument_id] = (cached_result, cached_generation)
                    return cached_result
        if callable(getattr(self.repository, "market_session_data", None)):
            profile = self.repository.profile(global_instrument_id)
            self.data_source._sessions[global_instrument_id] = await self.repository.market_session_data({value for value in (profile.mic, profile.exchange) if value})
        result = await self.repository._run_blocking_persistence(
            self.readiness_service.assess,
            global_instrument_id,
            jurisdiction=jurisdiction,
            now=now,
            evidence_only=evidence_only,
        )
        if evidence_only:
            generation = getattr(self.repository, "_readiness_mutation_generation", 0)
            self._evidence_readiness_cache[global_instrument_id] = (result, generation)
            # Bound the cache: evict the least-recently-written entries beyond the
            # cap. See the invariant comment at the declaration -- this only ever
            # drops durability-stale entries for prior instruments, so it cannot
            # affect correctness of the current flow's evidence_only reuse.
            if len(self._evidence_readiness_cache) > self._max_evidence_readiness_cache_size:
                for _key in list(self._evidence_readiness_cache.keys())[:-self._max_evidence_readiness_cache_size]:
                    self._evidence_readiness_cache.pop(_key, None)
        else:
            # Durable reads supersede any cached evidence-only snapshot so a
            # classification from a previous flow/window can never leak past a
            # fresh authoritative reload.
            self._evidence_readiness_cache.pop(global_instrument_id, None)
        return result

    async def ensure(
        self,
        global_instrument_id: UUID,
        *,
        jurisdiction: str,
        requirement_ids: Sequence[str] | None,
        correlation_id: str | None = None,
        identity_headers: Mapping[str, str | None] | None = None,
        wait_for_completion: bool = False,
    ) -> TargetedEnsureResult:
        """Interactive callers cap plan execution; background owners await it.

        Completion mode is for the persisted cycle worker, not the API route.
        Neither mode treats transport success or elapsed time as readiness.
        """
        selected = self._validated_requirement_ids(requirement_ids)
        _read_started = _time.monotonic()
        readiness = await self.read(global_instrument_id, jurisdiction=jurisdiction)
        cycle_timing.record_readiness_load_elapsed((_time.monotonic() - _read_started) * 1000)
        _plan_started = _time.monotonic()
        plan = self.planner.plan(
            readiness,
            jurisdiction=jurisdiction,
            requirement_ids=selected,
            include_non_mandatory=True,
        )
        # An explicit ensure may improve authority even while secondary facts
        # remain fresh. Reads and the generic freshness planner stay unchanged.
        upgrade_due = getattr(self.repository, "financial_authority_upgrade_due", None)
        quarterly = next((item for item in readiness.requirements
                          if item.requirement_id == "QUARTERLY_FINANCIALS"), None)
        if (jurisdiction == "INDIA" and quarterly is not None
                and (selected is None or "QUARTERLY_FINANCIALS" in selected)
                and not any(target.requirement_id == "QUARTERLY_FINANCIALS" for target in plan.targets)
                and callable(upgrade_due)
                and await self.repository._run_blocking_persistence(
                    upgrade_due, self.repository.profile(global_instrument_id), readiness.generated_at, quarterly,
                )):
            plan = replace(plan, targets=(*plan.targets, ResearchRefreshTarget(
                requirement_id=quarterly.requirement_id,
                rule_engine_area=quarterly.rule_engine_area,
                reason=quarterly.status,
                authority_policy=self.authority_registry.policy_for(quarterly.requirement_id, jurisdiction),
                existing_evidence_ids=quarterly.evidence_ids,
            )))
        cycle_timing.record_planning_elapsed((_time.monotonic() - _plan_started) * 1000)
        planned_ids = tuple(target.requirement_id for target in plan.targets)
        if not plan.targets:
            return TargetedEnsureResult(readiness, (), ())

        # Root Cause (Area 2 / runtime_ensure bottleneck): this single-flight
        # join used to key on the instrument alone, so ANY concurrently
        # running ensure() for this instrument -- regardless of which
        # requirement_ids it was servicing -- was joined and shielded-waited
        # on in full before this call's own (entirely disjoint) plan could
        # even start. CURRENT_NEWS is deliberately run concurrently with the
        # sequential mandatory-group loop in deep_investigation.investigate
        # (see _acquire_news_in_background) specifically so it is NOT stuck
        # behind BUSINESS_QUALITY_FACTS/SHAREHOLDING/etc acquisition -- but
        # because every one of those groups shares the SAME instrument,
        # CURRENT_NEWS's runtime_ensure kept finding an unrelated flight
        # already registered and blocking on it anyway, which is exactly why
        # its own runtime_ensure_elapsed_ms tracked almost the entire
        # investigation wall time instead of just its own acquisition cost.
        # Fix: only join (shield-wait on, and subtract attempted ids from) a
        # prior flight whose OWN requirement_ids actually intersect this
        # call's planned targets. A flight for disjoint requirement_ids is
        # left running untouched and this call proceeds concurrently with
        # it, exactly as the grouping/backgrounding design already intends.
        planned_target_ids = frozenset(target.requirement_id for target in plan.targets)
        existing_flights = self._flights.get(global_instrument_id, [])
        overlapping = [flight for flight in existing_flights if flight.requirement_ids & planned_target_ids]
        if overlapping:
            _single_flight_started = _time.monotonic()
            shared_executions = await asyncio.gather(*(asyncio.shield(flight.task) for flight in overlapping))
            cycle_timing.record_single_flight_wait_elapsed((_time.monotonic() - _single_flight_started) * 1000)
            attempted: set[str] = set()
            shared_failures: dict[str, str] = {}
            for flight, shared_execution in zip(overlapping, shared_executions):
                attempted |= flight.requirement_ids
                shared_failures.update(shared_execution.failures)
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
                or (wait_for_completion and "ACQUISITION_TIMEOUT" in
                    shared_failures.get(target.requirement_id, ""))
            )
            if not remaining_targets:
                return TargetedEnsureResult(
                    readiness, planned_ids, (), True, shared_failures
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
                wait_for_completion=wait_for_completion,
            )
        )
        flight = _EnsureFlight(task, target_ids)
        flights = self._flights.setdefault(global_instrument_id, [])
        # Defense-in-depth against unbounded _flights growth: evict only
        # already-settled (done/cancelled) entries that the cleanup callback
        # may not have reaped yet (e.g. under the timeout path). The hard cap
        # must NEVER evict a legitimately ACTIVE flight merely because a size
        # limit was reached: an active overlapping flight may be shared by
        # shield-joining followers, and cancelling it would break single-flight
        # correctness. Active non-overlapping flights are left untouched so
        # disjoint requirements may execute concurrently. The active-flight count
        # is naturally bounded by legitimate concurrency and requirement sets,
        # not by a hard numerical cap.
        flights[:] = [f for f in flights if not f.task.done()]
        flights.append(flight)

        def cleanup(completed: asyncio.Task[_PlanExecutionResult]) -> None:
            flights = self._flights.get(global_instrument_id)
            if flights is None:
                return
            try:
                flights.remove(flight)
            except ValueError:
                return
            if not flights:
                self._flights.pop(global_instrument_id, None)

        task.add_done_callback(cleanup)
        try:
            execution = await asyncio.shield(task)
        except asyncio.CancelledError:
            if wait_for_completion:
                # The creating background cycle owns its plan's lifetime.
                # Followers above remain shielded and cannot cancel the owner.
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise
        recorder = getattr(self.repository, "record_acquisition_observation", None)
        if callable(recorder):
            _obs_started = _time.monotonic()
            for target in plan.targets:
                failure = execution.failures.get(target.requirement_id)
                await recorder(global_instrument_id, target.requirement_id, "READINESS_EXECUTOR",
                    "FAILED" if failure else "COMPLETED", datetime.now(timezone.utc), failure_reason=failure)
            cycle_timing.record_observation_recording_elapsed((_time.monotonic() - _obs_started) * 1000)
        _final_read_started = _time.monotonic()
        readiness = await self.read(global_instrument_id, jurisdiction=jurisdiction)
        cycle_timing.record_final_readiness_reload_elapsed((_time.monotonic() - _final_read_started) * 1000)
        logger.info("readiness_re_evaluated globalInstrumentId=%s source=DURABLE acquisitionCompleted=true",
                    global_instrument_id)
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
        wait_for_completion: bool = False,
    ) -> _PlanExecutionResult:
        progress = CapabilityExecutionProgress()
        if wait_for_completion:
            # A persisted background opportunity job owns completion. The API
            # latency budget is an observation interval, not its job lifetime.
            # Existing network/document/provider limits still bound operations;
            # do not pass the expired interactive deadline into later targets.
            task = asyncio.create_task(self._execute_plan_until_ready(
                plan, jurisdiction=jurisdiction, correlation_id=correlation_id,
                identity_headers=identity_headers, progress=progress, deadline=None))
            logger.info("acquisition_started globalInstrumentId=%s owner=BACKGROUND_CYCLE",
                        plan.global_instrument_id)
            try:
                done, _ = await asyncio.wait({task}, timeout=self.ensure_timeout_seconds)
                if not done:
                    # OOM/stall root-cause fix: the prior code fell through to
                    # `result = await task` here, which BLOCKED INDEFINITELY on the
                    # still-running _execute_plan_until_ready/_execute_plan task.
                    # When a provider call (STRUCTURED_MARKET,
                    # HISTORICAL_MARKET_DATA via refresh_targeted_categories /
                    # _ensure_historical_prices) never completes, that await never
                    # returns: the owning deep-acquisition worker coroutine is
                    # pinned alive forever holding the full task closure
                    # (progress, failures, executed_capabilities, _assess_memo,
                    # provider response objects, document bytes) in memory. Across
                    # 8 concurrent stage-2 workers each blocked on an un-cancellable
                    # provider call, plus the _flights dict + _evidence_readiness_cache
                    # accumulating one entry per candidate, this produced the
                    # observed monotonic climb 507 -> 1162 MiB and the 50/2432
                    # stall (all 8 workers blocked, no slots free, no new admissions).
                    #
                    # Ownership semantics after timeout: the timeout is an
                    # OBSERVATION budget for the background cycle owner, NOT the
                    # task's lifetime. The owner cancels the task, drains it under
                    # a hard deadline, and returns a bounded failure result -- the
                    # remaining work is reclassified as a retryable acquisition
                    # timeout rather than left running. The task's done-callback
                    # (registered in ensure()) removes it from _flights as soon as
                    # it finishes, so no flight reference is retained past drain.
                    in_flight = [capability for capability in progress.executed_capabilities
                                 if capability not in progress.completed_capabilities
                                 and capability.rsplit(':', 1)[-1] not in progress.failures
                                 and capability.rsplit(':', 1)[-1] not in progress.satisfied_requirement_ids]
                    logger.info("orchestration_wait_expired globalInstrumentId=%s budgetSeconds=%s acquisitionState=RUNNING inFlight=%s",
                                plan.global_instrument_id, self.ensure_timeout_seconds, in_flight)
                    # Hard deadline: the cancelled task must unwind its provider
                    # calls, but it MUST NOT be awaited without bound. The
                    # ensure_timeout_seconds window is the observation budget that
                    # already elapsed; cap the drain at a small multiple so a
                    # shielded/hung provider call cannot pin the worker forever.
                    task.cancel()
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(task, return_exceptions=True),
                            timeout=max(self.ensure_timeout_seconds, 5.0),
                        )
                    except asyncio.TimeoutError:
                        # Even the hard-deadline drain exceeded the budget: the
                        # task is still winding down but MUST NOT be awaited again
                        # here (that is exactly the unbounded-block foot-gun this
                        # rewrite removes). Drop the local reference; the done-
                        # callback will eventually reap the _EnsureFlight entry.
                        logger.warning(
                            "orchestration_drain_timeout globalInstrumentId=%s budgetSeconds=%s "
                            "inFlight=%s abandonedAfterHardDeadline=true",
                            plan.global_instrument_id, self.ensure_timeout_seconds, in_flight)
                    # Build a bounded failure result mirroring the interactive
                    # TimeoutError path so the caller never observes an unbounded
                    # wait -- only the durable evidence state committed before the
                    # budget expired.
                    result = _PlanExecutionResult(
                        tuple(progress.executed_capabilities),
                        {target.requirement_id: _combined_failure_reason(
                            progress.failures.get(target.requirement_id), "ACQUISITION_TIMEOUT")
                         for target in plan.targets},
                    )
                    logger.info("acquisition_timeout_result globalInstrumentId=%s executed=%d failures=%d",
                                plan.global_instrument_id, len(result.executed_capabilities), len(result.failures))
                    return result
                result = await task
                logger.info("acquisition_completed globalInstrumentId=%s failures=%d",
                            plan.global_instrument_id, len(result.failures))
                return result
            finally:
                # Runtime shutdown owns cancellation of its registered flight.
                # Never orphan a child plan when that owner is cancelled. The
                # done-callback in ensure() removes the _EnsureFlight entry once
                # the task settles, so _flights cannot accumulate entries for
                # tasks this path has already cancelled/drain-abandoned.
                if not task.done():
                    task.cancel()
                    try:
                        # The timeout branch above already attempted a full
                        # ensure_timeout_seconds drain; if we reach here the task
                        # is still unwinding. Give it a short bounded window for
                        # cooperative cleanup, then drop the local reference --
                        # the done-callback will reap the _EnsureFlight entry when
                        # the task eventually settles.
                        await asyncio.wait_for(
                            asyncio.gather(task, return_exceptions=True),
                            timeout=5.0,
                        )
                    except asyncio.TimeoutError:
                        pass
        # Deadline for interactive plan execution (initial planning and the
        # final durable read are outside it), computed once so
        # every target attempted inside a sequential per-target acquisition
        # loop (see McpFirstResearchCapabilityExecutor.execute_primary) can
        # tell how much of the SHARED budget genuinely remains, instead of
        # each target assuming its own fresh per-call timeout independent of
        # everything already attempted before it in the same plan.
        deadline = asyncio.get_event_loop().time() + self.ensure_timeout_seconds
        try:
            async with asyncio.timeout(self.ensure_timeout_seconds):
                return await self._execute_plan_until_ready(
                    plan,
                    jurisdiction=jurisdiction,
                    correlation_id=correlation_id,
                    identity_headers=identity_headers,
                    progress=progress,
                    deadline=deadline,
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
            # Best-effort: identify the capability still in flight (attempted
            # but neither failed nor satisfied yet) when the timeout fired --
            # this is the operation that actually consumed the remaining
            # budget, which `requirements=` alone does not reveal (that field
            # is a post-hoc re-read, not a record of what was executing).
            in_flight = sorted(
                capability for capability in progress.executed_capabilities
                if capability not in progress.completed_capabilities
                and capability.rsplit(':', 1)[-1] not in progress.failures
                and capability.rsplit(':', 1)[-1] not in progress.satisfied_requirement_ids
            )
            logger.warning(
                "research_readiness_ensure_timeout globalInstrumentId=%s "
                "budgetSeconds=%s requested=%s inFlight=%s requirements=%s",
                plan.global_instrument_id,
                self.ensure_timeout_seconds,
                sorted(target.requirement_id for target in plan.targets),
                in_flight,
                sorted(failures),
            )
            return _PlanExecutionResult(
                tuple(progress.executed_capabilities), failures
            )

    async def _execute_plan_until_ready(
        self,
        plan: ResearchRefreshPlan,
        *,
        jurisdiction: str,
        correlation_id: str | None,
        identity_headers: Mapping[str, str | None] | None,
        progress: CapabilityExecutionProgress,
        deadline: float | None,
    ) -> _PlanExecutionResult:
        changed = asyncio.Event()
        with evidence_notifications(plan.global_instrument_id, changed):
            task = asyncio.create_task(self._execute_plan(
                plan, jurisdiction=jurisdiction, correlation_id=correlation_id,
                identity_headers=identity_headers, progress=progress, deadline=deadline,
            ))
        wakeup = asyncio.create_task(changed.wait())
        try:
            while True:
                await asyncio.wait({task, wakeup}, return_when=asyncio.FIRST_COMPLETED)
                if task.done():
                    return await task
                changed.clear()
                # A commit is a reason to reassess, not proof of success.
                # Ignore only the REFRESHING overlay; all durable freshness,
                # coverage, conflict and provenance rules still apply.
                self._evidence_readiness_cache.pop(plan.global_instrument_id, None)
                readiness = await self.read(
                    plan.global_instrument_id, jurisdiction=jurisdiction, evidence_only=True,
                )
                if task.done():
                    return await task
                if all(
                    self._requires_acquisition(target.reason)
                    and not self._requires_acquisition(readiness.for_requirement(target.requirement_id).status)
                    and readiness.for_requirement(target.requirement_id).status != ResearchRequirementStatus.REFRESHING
                    for target in plan.targets
                ):
                    # The shared plan owns its child. Retire it before releasing
                    # the flight so no redundant provider tail is orphaned and
                    # followers keep the same single-flight result. Explicit
                    # authority upgrades (targets already fresh at planning)
                    # must still complete their requested authority work.
                    task.cancel()
                    result, = await asyncio.gather(task, return_exceptions=True)
                    if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                        raise result
                    if isinstance(result, _PlanExecutionResult):
                        return result
                    self.data_source.finish_refresh(plan.global_instrument_id)
                    return _PlanExecutionResult(tuple(progress.executed_capabilities), {})
                wakeup = asyncio.create_task(changed.wait())
        finally:
            for child in (task, wakeup):
                if not child.done():
                    child.cancel()
            await asyncio.gather(task, wakeup, return_exceptions=True)

    async def _execute_plan(
        self,
        plan: ResearchRefreshPlan,
        *,
        jurisdiction: str,
        correlation_id: str | None,
        identity_headers: Mapping[str, str | None] | None,
        progress: CapabilityExecutionProgress | None = None,
        deadline: float | None = None,
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
                deadline=deadline,
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
            if result.status not in REQUIREMENT_STATUSES_NEEDING_ACQUISITION:
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
            # A fallback may have persisted structured snapshots that change
            # readiness, so the final failure classification must re-read
            # durable state. When no fallback ran, no durable writes occurred
            # between the after_primary read above and here, so reusing that
            # read avoids a redundant full reload (facts/observations/documents/
            # events/shareholding/acquisition-observations/news + market_session_data)
            # that would observe an identical snapshot.
            final_readiness = await self.read(
                plan.global_instrument_id, jurisdiction=jurisdiction
            )
        else:
            final_readiness = after_primary
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
        return status in REQUIREMENT_STATUSES_NEEDING_ACQUISITION

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


def _document_diagnostic(document, facts, events, shareholding=()):
    import re
    from app.repository import _official_filing_content_type_unsupported
    qualitative = {"GOVERNANCE_HISTORY": [], "SECTOR_MACRO": []}
    RepositoryResearchReadinessAdapter._append_documents(qualitative, [document])
    qualitative_count = sum(len(rows) for rows in qualitative.values())
    extracted_text = re.sub(r"\[PDF_PAGE \d+\]", "", document.normalized_text or "").strip()
    fact_count = sum(fact.source_identity == str(document.document_id) for fact in facts)
    event_count = sum(event.source_document_id == document.document_id for event in events)
    shareholding_count = sum(snapshot.research_document_id == document.document_id for snapshot in shareholding)
    if _official_filing_content_type_unsupported(document.canonical_url):
        state = "UNSUPPORTED_DOCUMENT"
    elif document.status == DocumentStatus.FAILED:
        state = "EXTRACTION_FAILED" if extracted_text else "NO_EXTRACTABLE_TEXT"
    elif fact_count or event_count or shareholding_count or qualitative_count:
        state = "PROCESSED_WITH_EVIDENCE"
    elif not extracted_text:
        state = "NO_EXTRACTABLE_TEXT"
    elif document.status == DocumentStatus.PROCESSED:
        state = "PROCESSED_NO_RELEVANT_EVIDENCE"
    else:
        state = str(document.status)
    return {"documentId": str(document.document_id), "sourceUrl": document.canonical_url,
        "state": state, "normalizedTextAvailable": bool(extracted_text), "financialFactCount": fact_count, "eventCount": event_count, "shareholdingSnapshotCount": shareholding_count, "qualitativeEvidenceCount": qualitative_count}


def _evidence_state(item):
    if item.status == ResearchRequirementStatus.NOT_APPLICABLE:
        return "NOT_APPLICABLE"
    reason = item.missing_reason or ""
    if item.status in {ResearchRequirementStatus.FAILED, ResearchRequirementStatus.MISSING} and "UNAVAILABLE" in reason:
        return "SOURCE_UNAVAILABLE"
    return {"READY_FRESH": "READY", "READY_STALE": "STALE"}.get(item.status.value, item.status.value)


def _acquisition_diagnostics(item):
    states = []
    if item.applicability == "NOT_APPLICABLE":
        return ["NOT_APPLICABLE"]
    if item.applicability == "UNKNOWN" or any(d.state == "UNKNOWN" for d in item.concept_applicability.values()):
        states.append("UNKNOWN_APPLICABILITY")
    for row in (item.acquisition_observation or {}).get("history", []):
        if row.get("provider") == "READINESS_EXECUTOR":
            continue
        if row.get("provider") == "NSE":
            states.append("AUTHORITATIVE_ATTEMPTED")
            if row.get("outcome") in {"FAILED", "SUCCESS_EMPTY"}:
                states.append("AUTHORITATIVE_UNAVAILABLE")
        reason = str(row.get("failure_reason") or "")
        if "TIMEOUT" in reason:
            states.append("ACQUISITION_TIMEOUT")
        elif "EXTRACTION" in reason:
            states.append("EXTRACTION_FAILED")
        elif "FETCH_FAILED" in reason:
            states.append("DOCUMENT_FETCH_FAILED")
        elif row.get("outcome") == "SUCCESS_EMPTY":
            states.append("NO_USABLE_EVIDENCE")
    if item.source_tier in {ResearchSourceTier.APPROVED_SECONDARY, ResearchSourceTier.LICENSED_STRUCTURED, ResearchSourceTier.APPROVED_EXTERNAL_TOOL} and item.evidence_ids:
        states.append("SECONDARY_FALLBACK_USED")
    return list(dict.fromkeys(states))


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
                "evidenceState": _evidence_state(item),
                "acquisitionDiagnostics": _acquisition_diagnostics(item),
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
                "subrequirements": {
                    concept: {
                        "applicability": decision.state,
                        "reason": decision.reason,
                        "evidenceState": item.concept_evidence_states.get(concept, "MISSING"),
                        "covered": CONCEPT_INPUTS[concept] in item.covered_input_ids,
                    } for concept, decision in item.concept_applicability.items()
                },
                "concreteRequirements": [
                    {
                        "inputId": input_.input_id,
                        "importance": input_.importance.value,
                        "covered": input_.input_id in item.covered_input_ids,
                        "applicability": "NOT_APPLICABLE" if item.applicability == "NOT_APPLICABLE" or input_.input_id in item.not_applicable_input_reasons else next((decision.state for concept, decision in item.concept_applicability.items() if CONCEPT_INPUTS[concept] == input_.input_id), item.applicability if item.applicability == "UNKNOWN" else "APPLICABLE"),
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
        "documentDiagnostics": list(value.document_diagnostics),
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
    from app.research_applicability import LENDER_BALANCE_SHEET_INPUT, LENDER_BALANCE_SHEET_METRICS
    if any(metric in aliases for aliases in LENDER_BALANCE_SHEET_METRICS.values()):
        add("BALANCE_SHEET_FACTS", LENDER_BALANCE_SHEET_INPUT)
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
    if fact_name == "roce":
        # Statement-derived structured ROCE is consumed by the quality rules;
        # readiness must see the same input it scores.
        add("BUSINESS_QUALITY_FACTS", "ROCE")
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


_ANNUAL_FINANCIAL_MAX_AGE = FreshnessPolicyRegistry.default().get("ANNUAL_FINANCIALS").maximum_age


def _evidence_from_financial_fact(
    fact: FinancialFact, requirement_id: str, covered_input_ids: tuple[str, ...]
) -> ResearchEvidence:
    source, tier = _financial_source(fact)
    as_of = fact.value.as_of_date or _period_datetime(fact.key.period_end)
    annual_growth = requirement_id == "GROWTH_FACTS" and fact.key.period_type == "ANNUAL"
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
        # Annual CAGR baselines follow the existing annual reporting policy.
        # Applying GROWTH_FACTS' quarterly TTL to them expires March results in
        # July, months before another annual report could exist.
        valid_until=(as_of + _ANNUAL_FINANCIAL_MAX_AGE if annual_growth and as_of else
            as_of + timedelta(days=120 if fact.key.period_type == "QUARTERLY" else 400)
            if requirement_id in ("VALUATION_INPUTS", "BALANCE_SHEET_FACTS") and as_of and fact.key.period_type in {"QUARTERLY", "ANNUAL"} else None),
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
    if fact.source_tier == FactSourceTier.USER_UPLOAD:
        # Dedicated, honest tier -- never the APPROVED_SECONDARY catch-all,
        # which would mis-report manual evidence as equivalent to Yahoo.
        return "USER_UPLOAD", ResearchSourceTier.USER_UPLOAD
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
    # Manual-evidence events (CURRENT_NEWS, ORDER_BOOK_CAPEX_GUIDANCE,
    # GOVERNANCE_HISTORY) are built with raw_evidence_reference ==
    # f"manual-evidence:{content_hash}" -- a literal convention used
    # EXCLUSIVELY by app.manual_evidence (verified: no automated pipeline
    # writes this prefix; app.extraction uses the raw article text and
    # app.yahoo_mcp_acquisition uses the article headline). Without this
    # branch such an event fell through source_classification == OTHER
    # into the generic "REPUTABLE_NEWS"/APPROVED_SECONDARY default below --
    # the SAME authority rank as an actual automated reputable-news event --
    # even though ProviderAuthorityRegistry.default() already declares a
    # dedicated, lowest-authority USER_UPLOAD provider for CURRENT_NEWS,
    # ORDER_BOOK_CAPEX_GUIDANCE and GOVERNANCE_HISTORY. This closes that
    # gap so a manually uploaded event is correctly ranked below every
    # automated tier (OFFICIAL/REGULATORY/APPROVED_SECONDARY/
    # APPROVED_EXTERNAL_TOOL) and can only ever fill a genuine gap, never
    # outrank or silently shadow stronger automated evidence.
    if (event.raw_evidence_reference or "").startswith("manual-evidence:"):
        return "USER_UPLOAD", ResearchSourceTier.USER_UPLOAD
    # independence_key is a fixed-width CHAR(64) SHA-256 hex digest (see
    # YahooMcpResultPersister._persist_article), not a readable "PROVIDER:..."
    # string, so provenance is verified by recomputing the same
    # content_hash(...) over this event's own source_url with the Yahoo MCP
    # convention's exact prefix/casefold shape, rather than string-matching a
    # literal prefix that no longer exists in the stored value.
    if event.independence_key and event.independence_key == content_hash(
        f"YAHOO_FINANCE_MCP:{(event.source_url or '').casefold()}"
    ):
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


def _structured_sector_fact(
    structured: Sequence[StructuredMarketSnapshotRecord],
) -> tuple[str, str, ResearchSourceTier, datetime] | None:
    """Best available structured-market 'sector' fact, if any: (value,
    source, source_tier, fact_time). Used only as a fallback when canonical
    registry metadata has no sector at all -- see _append_canonical_sector.
    Prefers the same ordering already used for applicability's sector
    derivation above: OFFICIAL-tier providers first, then most recently
    retrieved. Only an actual 'sector' fact is used; industry is never
    substituted in.
    """
    for record in sorted(
        structured,
        key=lambda record: (
            int(_structured_source_tier(record.provider) != ResearchSourceTier.OFFICIAL),
            -record.retrieved_at.timestamp(),
        ),
    ):
        fact = record.snapshot.facts.get("sector")
        if fact is None or fact.value in (None, ""):
            continue
        fact_time = fact.retrieved_at or record.retrieved_at
        return (
            str(fact.value),
            _market_source(record.provider),
            _structured_source_tier(record.provider),
            fact_time,
        )
    return None


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


def _completed_authoritative_check(history: Sequence[Mapping[str, Any]]) -> datetime | None:
    """Time of the latest authoritative (NSE) check when it completed.

    SUCCESS (qualifying events found) and SUCCESS_EMPTY (check completed, no
    qualifying event) both mean the check ran; FAILED, a missing observation
    or an executor-only row never count as a completed check.
    """
    rows = [row for row in history if row.get("provider") == "NSE"
            and _aware_datetime(row.get("observed_at")) is not None]
    if not rows:
        return None
    latest = max(rows, key=lambda row: _aware_datetime(row.get("observed_at")))
    if latest.get("outcome") not in {"SUCCESS", "SUCCESS_EMPTY"}:
        return None
    return _aware_datetime(latest.get("observed_at"))


def _incomplete_news_search_reason(outcome: Any) -> str | None:
    run = outcome[0] if isinstance(outcome, tuple) and outcome else outcome
    state = getattr(run, "outcome", None)
    if state not in {"SEARCH_PARTIAL", "SEARCH_FAILED"}:
        return None
    codes = sorted({provider.failure_code for provider in getattr(run, "providers", ())
                    if getattr(provider, "failure_code", None)})
    return "|".join(codes) if codes else "SEARCH_PROVIDER_UNAVAILABLE"


def _is_india(profile: CompanyResearchProfile) -> bool:
    return jurisdiction_for_profile(profile) == "INDIA"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
