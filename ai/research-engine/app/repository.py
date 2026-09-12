from __future__ import annotations

import asyncio
import re
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from uuid import UUID

from app.deduplication import DocumentDeduplicator
from app.entity_resolution import EntityResolver
from app.events import company_updated, document_event, research_event_extracted
from app.extraction import RuleBasedEventExtractor
from app.models import (
    CompanyResearchProfile,
    DocumentSubtype,
    DocumentStatus,
    DocumentType,
    EtfResearchProfile,
    PlatformEvent,
    ReliabilityLevel,
    ResearchDocument,
    ResearchEvidenceSource,
    ResearchEvent,
    ResearchEventType,
    ResearchSummary,
    ProvenancedValue,
    SourceClassification,
    SourceMode,
    ShareholdingSnapshot,
    SourceType,
    StructuredMarketSnapshotRecord,
)
from app.normalization import canonicalize_url, content_hash, detect_document_type, extract_published_at, extract_text, normalize_text
from app.research_fetching import FetchError, HttpResearchFetcher, PdfExtractionTimeoutError, RestrictedFetchError, TransportFetchError
from app.scoring import CatalystScorer, canonical_read_model_score
from app.settings import Settings
from app.shareholding import parse_nse_shareholding_xbrl, parse_official_shareholding
from app.persistence import ResearchPersistence, SqliteResearchPersistence, persistence_from_settings
from app.source_discovery import (
    ApprovedSourceDiscovery,
    BraveCompatibleSearchDiscoveryProvider,
    DisabledSearchDiscoveryProvider,
    DiscoveryResult,
    GoogleCompatibleSearchDiscoveryProvider,
    OfficialFilingDiscovery,
    OfficialNseShareholdingDiscovery,
    SearchDiscoveryProvider,
    SearchProviderConfigurationError,
    SearchProviderError,
    SearchDiscoveryService,
    SearxngSearchDiscoveryProvider,
)
from app.source_registry import RegisteredResearchSource, registered_sources_for
from app.structured_research import financial_result_history_from_facts, financial_statement_history_from_facts, latest_quarterly_result_from_facts, latest_quarterly_result, parsed_nse_balance_sheet_periods, parsed_nse_cash_flow_periods, parsed_nse_income_statement_periods
from app.fact_precedence import FinancialFact, FinancialFactKey, FactSourceTier

logger = logging.getLogger(__name__)
_TRUSTED_NSE_PROFILE_IDENTITY = object()


@dataclass(frozen=True)
class _InstrumentRefreshGate:
    missing_categories: set[str]
    shareholding_backfill_needed: bool
    shareholding_category_enrichment_needed: bool
    state: str

    @property
    def requires_provider_work(self) -> bool:
        return bool(
            self.missing_categories
            or self.shareholding_backfill_needed
            or self.shareholding_category_enrichment_needed
        )


@dataclass(frozen=True)
class _CategoryRefreshStrategy:
    name: str
    lightweight_check_interval: timedelta


_SHORT_TTL = _CategoryRefreshStrategy("SHORT_TTL", timedelta(minutes=5))
_DAILY_LIGHTWEIGHT = _CategoryRefreshStrategy("DAILY_LIGHTWEIGHT", timedelta(days=1))
_PERIODIC_SLOW = _CategoryRefreshStrategy("PERIODIC_SLOW", timedelta(days=7))
_QUARTERLY_WINDOW = _CategoryRefreshStrategy("QUARTERLY_WINDOW", timedelta(days=3))
_ANNUAL_WINDOW = _CategoryRefreshStrategy("ANNUAL_WINDOW", timedelta(days=14))

_CATEGORY_STRATEGIES: dict[str, _CategoryRefreshStrategy] = {
    "SHAREHOLDING_PATTERN": _QUARTERLY_WINDOW,
    "FINANCIAL_RESULTS": _QUARTERLY_WINDOW,
    "ANNUAL_REPORT": _ANNUAL_WINDOW,
    "VALUATION": _SHORT_TTL,
    "ANALYST_OPINION": _DAILY_LIGHTWEIGHT,
    "ANALYST_TARGETS": _DAILY_LIGHTWEIGHT,
    "ORDERS_BACKLOG": _DAILY_LIGHTWEIGHT,
    "CONTRACTS": _DAILY_LIGHTWEIGHT,
    "CAPEX": _DAILY_LIGHTWEIGHT,
    "NEW_FACILITIES": _DAILY_LIGHTWEIGHT,
    "ACQUISITIONS": _DAILY_LIGHTWEIGHT,
    "CLIENTS": _DAILY_LIGHTWEIGHT,
    "GUIDANCE": _DAILY_LIGHTWEIGHT,
    "MANAGEMENT": _DAILY_LIGHTWEIGHT,
    "REGULATORY": _DAILY_LIGHTWEIGHT,
    "CATALYSTS": _DAILY_LIGHTWEIGHT,
    "ORDERS_BACKLOG": _DAILY_LIGHTWEIGHT,
    "CAPEX": _PERIODIC_SLOW,
    "NEW_FACILITIES": _PERIODIC_SLOW,
    "CLIENTS": _PERIODIC_SLOW,
    "PRODUCTS": _PERIODIC_SLOW,
    "GROWTH": _PERIODIC_SLOW,
}

_REFRESH_CATEGORY_ALIASES = {
    "FINANCIAL RESULTS": "FINANCIAL_RESULTS",
    "SHAREHOLDING PATTERN": "SHAREHOLDING_PATTERN",
    "ANNUAL REPORT": "ANNUAL_REPORT",
    "ANALYST OPINION": "ANALYST_OPINION",
    "ANALYST TARGETS": "ANALYST_TARGETS",
    "INSTITUTIONAL ACTIVITY": "INSTITUTIONAL_ACTIVITY",
    "GUIDANCE": "GUIDANCE",
    "GROWTH": "GROWTH",
    "CUSTOMERS": "CLIENTS",
    "NEW CUSTOMERS": "CLIENTS",
    "CLIENTS": "CLIENTS",
    "ORDERS BACKLOG": "ORDERS_BACKLOG",
    "NEW ORDERS": "ORDERS_BACKLOG",
    "ORDERS_BACKLOG": "ORDERS_BACKLOG",
    "CAPEX CAPACITY": "CAPEX",
    "CAPEX": "CAPEX",
    "OWNERSHIP": "INSTITUTIONAL_ACTIVITY",
    "REGULATORY": "REGULATORY",
    "MANAGEMENT": "MANAGEMENT",
}


class ResearchRepository:
    def __init__(
        self,
        settings: Settings | None = None,
        fetcher: HttpResearchFetcher | None = None,
        discovery: ApprovedSourceDiscovery | None = None,
        search_discovery: SearchDiscoveryService | None = None,
        official_filing_discovery: OfficialFilingDiscovery | None = None,
        official_shareholding_discovery: OfficialNseShareholdingDiscovery | None = None,
        persistence: ResearchPersistence | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.profiles = _demo_profiles()
        self.etf_profiles: list[EtfResearchProfile] = []
        self.documents: dict[UUID, ResearchDocument] = {}
        self.events: dict[UUID, ResearchEvent] = {}
        self.shareholding_snapshots: dict[UUID, ShareholdingSnapshot] = {}
        self.platform_events: list[PlatformEvent] = []
        self.last_refresh: dict[UUID, datetime] = {}
        self.last_live_error: dict[UUID, str] = {}
        self._category_refresh: dict[tuple[UUID, str], datetime] = {}
        # A successful provider response with no qualifying evidence is not
        # freshness. Keep its check cadence separate from durable evidence so
        # normal refreshes do not rediscover the same missing category.
        self._category_successful_no_change_checks: dict[tuple[UUID, str], datetime] = {}
        self._deduplicator = DocumentDeduplicator()
        self._event_keys: set[tuple[UUID, ResearchEventType, str, str | None, str | None]] = set()
        self._resolver = EntityResolver(self.profiles)
        self._extractor = RuleBasedEventExtractor()
        self._scorer = CatalystScorer()
        self._fetcher = fetcher or HttpResearchFetcher(self.settings)
        self._persistence = persistence or persistence_from_settings(self.settings)
        self._discovery = discovery or ApprovedSourceDiscovery()
        self._search_discovery = search_discovery or SearchDiscoveryService(
            _search_provider_from_settings(self.settings),
            max_queries_per_category=self.settings.research_search_max_queries_per_category,
            max_results_per_query=self.settings.research_search_max_results_per_query,
            max_documents_per_refresh=self.settings.research_search_max_documents_per_refresh,
            allowed_domains=self.settings.research_search_allowed_domains,
        )
        self._official_filing_discovery = official_filing_discovery or OfficialFilingDiscovery(
            announcements_url=self.settings.nse_announcements_url
        )
        self._official_shareholding_discovery = official_shareholding_discovery or OfficialNseShareholdingDiscovery(
            shareholdings_url=self.settings.nse_shareholdings_url
        )
        # This is deliberately process-local.  Persistence remains the durable
        # cross-process deduplication boundary.
        self._official_filing_flights: dict[tuple[UUID, str], asyncio.Task[ResearchDocument]] = {}
        self._instrument_refresh_flights: dict[UUID, asyncio.Task[ResearchSummary]] = {}
        # The production persistence adapter owns one synchronous database
        # connection.  Worker operations are serialized per repository so two
        # refreshes do not interleave transactions on that connection.
        self._persistence_worker_lock = threading.RLock()
        self._seed_demo_data()
        self._load_persisted_research()

    def list_profiles(self) -> list[CompanyResearchProfile]:
        return self.profiles

    @property
    def persistence(self):
        return self._persistence

    def list_etf_profiles(self) -> list[EtfResearchProfile]:
        return self.etf_profiles

    def financial_facts_for(self, instrument_id: UUID):
        return [fact for fact in self._persistence.load_financial_facts() if fact.key.instrument_id == instrument_id]

    async def financial_facts_for_instruments(self, instrument_ids: set[UUID]) -> dict[UUID, list[FinancialFact]]:
        started = time.perf_counter()
        facts = await self._run_blocking_persistence(self._persistence.load_financial_facts)
        grouped: dict[UUID, list[FinancialFact]] = {instrument_id: [] for instrument_id in instrument_ids}
        for fact in facts:
            if fact.key.instrument_id in grouped:
                grouped[fact.key.instrument_id].append(fact)
        logger.info(
            "portfolio_summary_stage stage=FINANCIAL_FACTS durationMs=%s requestedInstrumentCount=%s returnedFactCount=%s",
            round((time.perf_counter() - started) * 1000),
            len(instrument_ids),
            sum(len(values) for values in grouped.values()),
        )
        return grouped

    def persist_yahoo_statement_facts(self, instrument_id: UUID, snapshot) -> None:
        for raw in getattr(snapshot, "statement_facts", []):
            if not isinstance(raw, dict) or raw.get("value") is None or not raw.get("periodEnd") or raw.get("periodType") not in {"ANNUAL", "QUARTERLY"}:
                continue
            self._persistence.upsert_financial_fact(FinancialFact(
                FinancialFactKey(instrument_id, str(raw["metric"]), str(raw["periodEnd"]), str(raw["periodType"]), "UNKNOWN"),
                ProvenancedValue(value=raw["value"], unit=raw.get("unit"), source_url=str(raw["sourceUrl"]),
                    source_name=str(raw["sourceName"]), source_type=str(raw["sourceType"]), retrieved_at=raw["retrievedAt"], confidence=raw.get("confidence")),
                FactSourceTier.YAHOO, "YAHOO_FINANCE", f"{snapshot.resolution.provider_ticker}:{raw['metric']}:{raw['periodEnd']}:{raw['periodType']}", SourceMode.REAL,
            ))

    async def persist_yahoo_statement_facts_async(self, instrument_id: UUID, snapshot) -> None:
        await self._run_blocking_persistence(self.persist_yahoo_statement_facts, instrument_id, snapshot)

    def persist_international_financial_facts(self, facts: list[FinancialFact]) -> int:
        """Persist provider-neutral international facts through the existing durable model."""
        written = 0
        for fact in facts:
            if self._persistence.upsert_financial_fact(fact):
                written += 1
        return written

    async def persist_international_financial_facts_async(self, facts: list[FinancialFact]) -> int:
        return await self._run_blocking_persistence(self.persist_international_financial_facts, facts)

    def structured_market_snapshots_for(self, instrument_ids: set[UUID]) -> dict[UUID, list[StructuredMarketSnapshotRecord]]:
        grouped = {instrument_id: [] for instrument_id in instrument_ids}
        for record in self._persistence.load_structured_market_snapshots(instrument_ids):
            grouped.setdefault(record.instrument_id, []).append(record)
        return grouped

    async def structured_market_snapshots_for_instruments(self, instrument_ids: set[UUID]):
        return await self._run_blocking_persistence(self.structured_market_snapshots_for, instrument_ids)

    def market_price_observations_for(self, instrument_ids: set[UUID]):
        grouped = {instrument_id: [] for instrument_id in instrument_ids}
        for observation in self._persistence.load_market_price_observations(instrument_ids):
            grouped.setdefault(observation.instrument_id, []).append(observation)
        return grouped

    async def market_price_observations_for_instruments(self, instrument_ids: set[UUID]):
        return await self._run_blocking_persistence(self.market_price_observations_for, instrument_ids)

    async def market_price_coverage_for_instruments(self, instrument_ids: set[UUID]):
        return await self._run_blocking_persistence(
            self._persistence.load_market_price_coverage, instrument_ids
        )

    async def upsert_market_price_observation_async(self, observation) -> None:
        await self._run_blocking_persistence(self._persistence.upsert_market_price_observation, observation)

    async def stock_rule_engine_result(
        self,
        global_instrument_id: UUID,
        rule_engine_version: str,
        input_fingerprint: str,
    ) -> dict | None:
        return await self._run_blocking_persistence(
            self._persistence.load_stock_rule_engine_result,
            global_instrument_id,
            rule_engine_version,
            input_fingerprint,
        )

    async def persist_stock_rule_engine_result(self, result: dict) -> None:
        await self._run_blocking_persistence(
            self._persistence.upsert_stock_rule_engine_result, result
        )

    async def persist_structured_market_snapshot_async(self, record: StructuredMarketSnapshotRecord) -> None:
        await self._run_blocking_persistence(self._persistence.upsert_structured_market_snapshot, record)

    async def record_structured_market_failure_async(self, instrument_id: UUID, provider: str, code: str, message: str) -> None:
        await self._run_blocking_persistence(self._persistence.record_structured_market_failure, instrument_id, provider, datetime.now(timezone.utc), code, message)

    async def market_session_data(self, markets: set[str]):
        schedules, exceptions = await asyncio.gather(
            self._run_blocking_persistence(self._persistence.load_market_schedules, markets),
            self._run_blocking_persistence(self._persistence.load_market_calendar_exceptions, markets),
        )
        return schedules, exceptions

    async def _run_blocking_persistence(self, operation, *args, **kwargs):
        """Keep production database work out of the request event loop.

        The in-memory SQLite adapter is deliberately single-thread-affine and
        is used only by the local/test persistence configuration.  Production
        uses the psycopg adapter, whose synchronous operations are safe to run
        in a worker thread.  Keeping this compatibility branch here avoids
        moving repository-owned state or changing the SQLite adapter contract.
        """
        if isinstance(self._persistence, SqliteResearchPersistence) and self._persistence.__class__.__module__ != "app.postgres_persistence":
            return operation(*args, **kwargs)
        return await asyncio.to_thread(self._run_serialized_persistence_operation, operation, args, kwargs)

    def _run_serialized_persistence_operation(self, operation, args, kwargs):
        with self._persistence_worker_lock:
            return operation(*args, **kwargs)

    def etf_profile(self, instrument_id: UUID) -> EtfResearchProfile:
        return next(profile for profile in self.etf_profiles if profile.instrument_id == instrument_id)

    def profile(self, instrument_id: UUID) -> CompanyResearchProfile:
        return next(profile for profile in self.profiles if profile.instrument_id == instrument_id)

    def instrument_refresh_state(self, instrument_id: UUID) -> str:
        """Return the global public-research gate without doing provider work."""
        return self._instrument_refresh_gate(
            self.profile(instrument_id), set(), datetime.now(timezone.utc)
        ).state

    async def backfill(self, instrument_id: UUID, correlation_id: str | None = None) -> ResearchSummary:
        """Intentional, global deep repair for missing supported evidence.

        This bypasses normal due scheduling only; durable document/snapshot
        reuse and process-local per-instrument single-flight remain in force.
        """
        existing = self._instrument_refresh_flights.get(instrument_id)
        if existing is not None:
            return await asyncio.shield(existing)
        task = asyncio.create_task(self._backfill_once(instrument_id, correlation_id))
        self._instrument_refresh_flights[instrument_id] = task

        def cleanup(completed: asyncio.Task[ResearchSummary]) -> None:
            if self._instrument_refresh_flights.get(instrument_id) is completed:
                self._instrument_refresh_flights.pop(instrument_id, None)

        task.add_done_callback(cleanup)
        return await asyncio.shield(task)

    async def _backfill_once(self, instrument_id: UUID, correlation_id: str | None) -> ResearchSummary:
        profile = self.profile(instrument_id)
        if self.settings.research_live_enabled:
            await self._refresh_live(instrument_id, set(), force=True)
        self.last_refresh[instrument_id] = datetime.now(timezone.utc)
        logger.info("research_backfill_complete globalInstrumentId=%s correlationId=%s", instrument_id, correlation_id or "NONE")
        return self.summary(instrument_id, allow_demo=True)

    def documents_for(self, instrument_id: UUID, source_mode: SourceMode | None = None) -> list[ResearchDocument]:
        return sorted(
            [
                doc
                for doc in self.documents.values()
                if doc.instrument_id == instrument_id and (source_mode is None or doc.source_mode == source_mode)
            ],
            key=lambda doc: doc.published_at or doc.retrieved_at,
            reverse=True,
        )

    def register_etf_profile(self, profile: EtfResearchProfile) -> EtfResearchProfile:
        for existing in self.etf_profiles:
            if existing.instrument_id == profile.instrument_id:
                return existing
            if profile.provider and profile.provider_instrument_id:
                if (
                    existing.provider
                    and existing.provider.upper() == profile.provider.upper()
                    and existing.provider_instrument_id
                    and existing.provider_instrument_id.upper() == profile.provider_instrument_id.upper()
                ):
                    return existing
            if profile.isin and existing.isin and profile.isin.upper() == existing.isin.upper():
                return existing
        self.etf_profiles.append(profile)
        return profile

    def events_for(
        self,
        instrument_id: UUID,
        event_type: ResearchEventType | None = None,
        impact: str | None = None,
        reliability: ReliabilityLevel | None = None,
        source_mode: SourceMode | None = None,
    ) -> list[ResearchEvent]:
        values = [event for event in self.events.values() if event.instrument_id == instrument_id]
        if event_type:
            values = [event for event in values if event.event_type == event_type]
        if impact:
            values = [event for event in values if event.impact == impact]
        if reliability:
            values = [event for event in values if event.reliability == reliability]
        if source_mode:
            values = [event for event in values if event.source_mode == source_mode]
        return sorted(values, key=lambda event: event.event_date or event.detected_at, reverse=True)

    def shareholding_for(self, instrument_id: UUID, *, limit: int = 4) -> list[ShareholdingSnapshot]:
        ordered = sorted(
            [snapshot for snapshot in self.shareholding_snapshots.values()
             if snapshot.instrument_id == instrument_id
             and snapshot.source_mode == SourceMode.REAL
             and _is_quarter_end(snapshot.period_end)],
            key=lambda snapshot: (snapshot.period_end, snapshot.published_at or datetime.min.replace(tzinfo=timezone.utc), snapshot.retrieved_at), reverse=True,
        )
        latest_by_period: list[ShareholdingSnapshot] = []
        periods: set[datetime] = set()
        for snapshot in ordered:
            if snapshot.period_end in periods:
                continue
            periods.add(snapshot.period_end)
            latest_by_period.append(snapshot)
            if len(latest_by_period) == limit:
                break
        return latest_by_period

    def persist_shareholding_snapshot(self, snapshot: ShareholdingSnapshot) -> bool:
        if not snapshot.values or snapshot.source_mode != SourceMode.REAL:
            return False
        try:
            created = self._persistence.upsert_shareholding_snapshot(snapshot)
        except Exception as exc:
            raise FetchError("SHAREHOLDING_PERSIST_FAILED") from exc
        self._record_persisted_shareholding_snapshot(snapshot, created)
        return created

    async def _persist_shareholding_snapshot_async(self, snapshot: ShareholdingSnapshot) -> bool:
        if not snapshot.values or snapshot.source_mode != SourceMode.REAL:
            return False
        try:
            created = await self._run_blocking_persistence(self._persistence.upsert_shareholding_snapshot, snapshot)
        except Exception as exc:
            raise FetchError("SHAREHOLDING_PERSIST_FAILED") from exc
        self._record_persisted_shareholding_snapshot(snapshot, created)
        return created

    async def persist_external_mcp_shareholding_async(
        self, snapshot: ShareholdingSnapshot
    ) -> bool:
        """Persist exact normalized MCP ownership categories after adapter validation."""
        if snapshot.source_provider != "YAHOO_FINANCE_MCP":
            raise ValueError("UNAPPROVED_EXTERNAL_MCP_PROVIDER")
        return await self._persist_shareholding_snapshot_async(snapshot)

    async def record_acquisition_observation(self, instrument_id, requirement_id, provider, outcome, observed_at, source_url=None, failure_reason=None, evidence_count=0):
        await self._run_blocking_persistence(self._persistence.upsert_acquisition_observation,
            instrument_id, requirement_id, provider, outcome, observed_at, source_url, failure_reason, evidence_count)

    def acquisition_observations_for(self, instrument_id):
        loader = getattr(self._persistence, "load_acquisition_observations", None)
        return loader(instrument_id) if loader else []

    async def persist_external_mcp_evidence_async(
        self, document: ResearchDocument, event: ResearchEvent
    ) -> None:
        """Persist normalized MCP metadata without provider-text reclassification."""
        if (
            document.discovery_provider != "YAHOO_FINANCE_MCP"
            or event.instrument_id != document.instrument_id
            or event.company_id != document.company_id
            or event.source_url != document.canonical_url
        ):
            raise ValueError("INVALID_EXTERNAL_MCP_EVIDENCE")
        accepted = await self._apply_prepared_ingested_document_async(
            document, document_status=DocumentStatus.PROCESSED
        )
        if accepted.status == DocumentStatus.DUPLICATE and accepted.duplicate_of_document_id:
            event.source_document_id = accepted.duplicate_of_document_id
        await self._apply_ingested_event_async(event, source_mode=SourceMode.REAL)

    def _record_persisted_shareholding_snapshot(self, snapshot: ShareholdingSnapshot, created: bool) -> None:
        if created:
            self.shareholding_snapshots[snapshot.id] = snapshot
            logger.info("shareholding_snapshot_persisted globalInstrumentId=%s periodEnd=%s values=%s outcome=SUCCESS",
                        snapshot.instrument_id, snapshot.period_end.date().isoformat(), len(snapshot.values))

    def summary(self, instrument_id: UUID, *, allow_demo: bool = True) -> ResearchSummary:
        profile = self.profile(instrument_id)
        real_events = self.events_for(instrument_id, source_mode=SourceMode.REAL)
        real_documents = self.documents_for(instrument_id, source_mode=SourceMode.REAL)
        if real_events or real_documents:
            events = real_events
            documents = real_documents
            data_freshness = "REAL"
            demo = False
        elif allow_demo and self.settings.research_demo_enabled:
            events = self.events_for(instrument_id, source_mode=SourceMode.DEMO)
            documents = self.documents_for(instrument_id, source_mode=SourceMode.DEMO)
            data_freshness = "DEMO_FALLBACK" if self.settings.research_live_enabled else "DEMO"
            demo = True
        else:
            events = []
            documents = []
            data_freshness = "SOURCE_UNAVAILABLE" if self.last_live_error.get(instrument_id) else "UNAVAILABLE"
            demo = False
        score = self._scorer.score(instrument_id, events)
        source_mix: dict[str, int] = {}
        for document in documents:
            key = str(document.source_type)
            source_mix[key] = source_mix.get(key, 0) + 1
        shareholding = self.shareholding_for(instrument_id)
        financial_facts = self.financial_facts_for(instrument_id)
        return ResearchSummary(
            profile=profile,
            catalyst_score=score,
            recent_events=events[:10],
            documents=documents[:10],
            last_refresh_at=self.last_refresh.get(instrument_id),
            data_freshness=data_freshness,
            demo=demo,
            source_mix=source_mix,
            shareholding_snapshots=shareholding,
            shareholding_freshness="REAL" if shareholding else "UNAVAILABLE",
            latest_quarterly_result=latest_quarterly_result_from_facts(financial_facts),
            financial_result_history=financial_result_history_from_facts(financial_facts),
            balance_sheet_history=financial_statement_history_from_facts(
                financial_facts, period_type={"AS_AT", "QUARTERLY", "ANNUAL"}, metrics={
                    "total_assets", "total_liabilities", "total_equity", "equity",
                    "cash_and_cash_equivalents", "cash_and_equivalents", "total_debt",
                    "debt_or_borrowings", "current_assets", "current_liabilities",
                },
            ),
            cash_flow_history=financial_statement_history_from_facts(
                financial_facts, period_type={"QUARTERLY", "ANNUAL"}, metrics={
                    "operating_cash_flow", "investing_cash_flow", "financing_cash_flow",
                    "cash_flow_from_operating_activities", "cash_flow_from_investing_activities",
                    "cash_flow_from_financing_activities",
                },
            ),
        )

    def persisted_canonical_read_model_score(self, instrument_id: UUID):
        """Project the existing canonical score from durable real research events.

        This intentionally does not require a process-local profile: callers that
        already have an authoritative global instrument ID can read the same
        scorer input used by ``summary`` without restoring or creating a profile.
        No events means there is no usable persisted research read model.
        """
        events = self.events_for(instrument_id, source_mode=SourceMode.REAL)
        if not events:
            return None
        return canonical_read_model_score(self._scorer.score(instrument_id, events))

    def ingest_fixture(
        self,
        *,
        original_url: str,
        source_type: SourceType,
        source_name: str,
        publisher: str,
        content_type: str,
        body: str,
        reliability: ReliabilityLevel,
        published_at: datetime | None = None,
        source_mode: SourceMode = SourceMode.DEMO,
        source_classification: SourceClassification = SourceClassification.OTHER,
        discovered_at: datetime | None = None,
        discovery_provider: str | None = None,
        expected_profile: CompanyResearchProfile | None = None,
        document_status: DocumentStatus = DocumentStatus.PARSED,
        allow_empty_content: bool = False,
        _trusted_profile_identity: object | None = None,
        document_subtype: DocumentSubtype | None = None,
        _metadata_only_nse_financial_result: bool = False,
    ) -> ResearchDocument:
        document = self._prepare_ingested_document(
            original_url=original_url, source_type=source_type, source_name=source_name, publisher=publisher,
            content_type=content_type, body=body, reliability=reliability, published_at=published_at,
            source_mode=source_mode, source_classification=source_classification, discovered_at=discovered_at,
            discovery_provider=discovery_provider, expected_profile=expected_profile,
            document_status=document_status, allow_empty_content=allow_empty_content,
            trusted_profile_identity=_trusted_profile_identity,
            document_subtype=document_subtype,
        )
        return self._apply_prepared_ingested_document(
            document,
            document_status=document_status,
            metadata_only_nse_financial_result=_metadata_only_nse_financial_result,
        )

    async def _apply_prepared_ingested_document_async(
        self,
        document: ResearchDocument,
        *,
        document_status: DocumentStatus,
        metadata_only_nse_financial_result: bool = False,
    ) -> ResearchDocument:
        duplicate = self._deduplicator.add(document)
        if duplicate:
            document.status = DocumentStatus.DUPLICATE
            document.duplicate_of_document_id = duplicate.document_id
            return document
        if document_status != DocumentStatus.FAILED:
            document.status = DocumentStatus.PROCESSED
        self.documents[document.document_id] = document
        if document.source_mode == SourceMode.REAL:
            started = time.monotonic()
            try:
                await self._run_blocking_persistence(
                    self._persist_ingested_document,
                    document,
                    nse_financial_result=metadata_only_nse_financial_result,
                )
            except Exception as exc:
                self.documents.pop(document.document_id, None)
                logger.info("document_ingest_stage globalInstrumentId=%s documentId=%s stage=DOCUMENT_PERSIST elapsedMs=%s outcome=FAILED", document.instrument_id, document.document_id, _elapsed_ms(started))
                raise FetchError("DOCUMENT_PERSIST_FAILED") from exc
            logger.info("document_ingest_stage globalInstrumentId=%s documentId=%s stage=DOCUMENT_PERSIST elapsedMs=%s outcome=SUCCESS", document.instrument_id, document.document_id, _elapsed_ms(started))
        if (document.instrument_id and document.source_mode == SourceMode.REAL and document_status != DocumentStatus.FAILED
                and document.source_classification in {SourceClassification.EXCHANGE, SourceClassification.REGULATORY, SourceClassification.OFFICIAL_COMPANY}):
            started = time.monotonic()
            try:
                parsed, _ = await self._run_blocking_persistence(self._persist_official_financial_facts, document)
            except Exception:
                logger.info("document_ingest_stage globalInstrumentId=%s documentId=%s stage=FINANCIAL_FACTS elapsedMs=%s outcome=FAILED", document.instrument_id, document.document_id, _elapsed_ms(started))
                raise
            logger.info("document_ingest_stage globalInstrumentId=%s documentId=%s stage=FINANCIAL_FACTS elapsedMs=%s outcome=%s", document.instrument_id, document.document_id, _elapsed_ms(started), "SUCCESS" if parsed else "SKIPPED")
        extraction_started = time.monotonic()
        try:
            candidates = await asyncio.to_thread(self._extract_ingested_event_candidates, document, document_status=document_status)
        except Exception:
            logger.info("document_ingest_stage globalInstrumentId=%s documentId=%s stage=EVENT_EXTRACT elapsedMs=%s outcome=FAILED", document.instrument_id, document.document_id, _elapsed_ms(extraction_started))
            raise
        logger.info("document_ingest_stage globalInstrumentId=%s documentId=%s stage=EVENT_EXTRACT elapsedMs=%s outcome=%s", document.instrument_id, document.document_id, _elapsed_ms(extraction_started), "SUCCESS" if candidates else "SKIPPED")
        self.platform_events.append(document_event("research.document.processed", document))
        for event in candidates:
            await self._apply_ingested_event_async(event, source_mode=document.source_mode)
        return await self._continue_ingested_document_after_events_async(document)

    def _apply_prepared_ingested_document(
        self,
        document: ResearchDocument,
        *,
        document_status: DocumentStatus,
        metadata_only_nse_financial_result: bool = False,
    ) -> ResearchDocument:
        """Apply a prepared document using the legacy synchronous semantics."""
        duplicate = self._deduplicator.add(document)
        if duplicate:
            document.status = DocumentStatus.DUPLICATE
            document.duplicate_of_document_id = duplicate.document_id
            return document
        # Reuse eligibility is durable. Persist the successful terminal state,
        # rather than leaving a pre-processing status in storage and only
        # changing the in-memory object afterwards.
        if document_status != DocumentStatus.FAILED:
            document.status = DocumentStatus.PROCESSED
        self.documents[document.document_id] = document
        if document.source_mode == SourceMode.REAL:
            try:
                self._persist_ingested_document(
                    document,
                    nse_financial_result=metadata_only_nse_financial_result,
                )
            except Exception as exc:
                self.documents.pop(document.document_id, None)
                raise FetchError("DOCUMENT_PERSIST_FAILED") from exc
        return self._continue_ingested_document_after_persistence(document, document_status=document_status)

    def _continue_ingested_document_after_persistence(self, document: ResearchDocument, *, document_status: DocumentStatus, financial_processed: bool = False, event_candidates: list[ResearchEvent] | None = None) -> ResearchDocument:
        if (not financial_processed and document.instrument_id and document.source_mode == SourceMode.REAL and document_status != DocumentStatus.FAILED
                and document.source_classification in {SourceClassification.EXCHANGE, SourceClassification.REGULATORY, SourceClassification.OFFICIAL_COMPANY}):
            self._persist_official_financial_facts(document)
        self.platform_events.append(document_event("research.document.processed", document))
        for event in (event_candidates if event_candidates is not None else self._extract_ingested_event_candidates(document, document_status=document_status)):
            self._apply_ingested_event(event, source_mode=document.source_mode)
        return self._continue_ingested_document_after_events(document)

    def _continue_ingested_document_after_events(self, document: ResearchDocument) -> ResearchDocument:
        if document.instrument_id:
            snapshot = parse_official_shareholding(document)
            if snapshot is not None:
                self.persist_shareholding_snapshot(snapshot)
            self.last_refresh[document.instrument_id] = datetime.now(timezone.utc)
            self.platform_events.append(company_updated(document.instrument_id))
        return document

    async def _continue_ingested_document_after_events_async(self, document: ResearchDocument) -> ResearchDocument:
        snapshot = await asyncio.to_thread(parse_official_shareholding, document) if document.instrument_id else None
        if snapshot is not None:
            await self._persist_shareholding_snapshot_async(snapshot)
        if document.instrument_id:
            self.last_refresh[document.instrument_id] = datetime.now(timezone.utc)
            self.platform_events.append(company_updated(document.instrument_id))
        return document

    def _apply_ingested_event(self, event: ResearchEvent, *, source_mode: SourceMode) -> None:
        event_key = _event_key(event)
        if event_key in self._event_keys:
            return
        self._event_keys.add(event_key)
        self.events[event.event_id] = event
        if source_mode == SourceMode.REAL:
            self._persist_ingested_event(event)
        self.platform_events.append(research_event_extracted(event))

    async def _apply_ingested_event_async(self, event: ResearchEvent, *, source_mode: SourceMode) -> None:
        event_key = _event_key(event)
        if event_key in self._event_keys:
            return
        self._event_keys.add(event_key)
        self.events[event.event_id] = event
        if source_mode == SourceMode.REAL:
            started = time.monotonic()
            try:
                await self._run_blocking_persistence(self._persist_ingested_event, event)
            except Exception:
                logger.info("document_ingest_stage globalInstrumentId=%s documentId=%s eventId=%s stage=EVENT_PERSIST elapsedMs=%s outcome=FAILED", event.instrument_id, event.source_document_id, event.event_id, _elapsed_ms(started))
                raise
            logger.info("document_ingest_stage globalInstrumentId=%s documentId=%s eventId=%s stage=EVENT_PERSIST elapsedMs=%s outcome=SUCCESS", event.instrument_id, event.source_document_id, event.event_id, _elapsed_ms(started))
        self.platform_events.append(research_event_extracted(event))

    def _persist_ingested_event(self, event: ResearchEvent) -> None:
        """Durably store an accepted event without touching repository state."""
        self._persistence.upsert_event(event)

    def _extract_ingested_event_candidates(self, document: ResearchDocument, *, document_status: DocumentStatus) -> list[ResearchEvent]:
        if document_status == DocumentStatus.FAILED:
            return []
        candidates = self._extractor.extract(document)
        for event in candidates:
            event.source_mode = document.source_mode
            event.source_classification = document.source_classification
            event.published_at = document.published_at
            event.retrieved_at = document.retrieved_at
            event.independence_key = document.source_independence_key
            event.supporting_sources = [_evidence_source(document)]
        return candidates

    def _persist_ingested_document(
        self,
        document: ResearchDocument,
        *,
        nse_financial_result: bool = False,
    ) -> None:
        """Durably store a non-duplicate prepared document."""
        persisted = self._metadata_only_nse_quarterly_document(
            document,
            nse_financial_result=nse_financial_result,
        )
        self._persistence.upsert_document(persisted)

    def _metadata_only_nse_quarterly_document(
        self,
        document: ResearchDocument,
        *,
        nse_financial_result: bool = False,
    ) -> ResearchDocument:
        """Keep new official NSE quarterly PDF writes free of document content.

        PDF bytes are already transient in ``HttpResearchFetcher``.  This
        final persistence guard also omits extracted text when official
        discovery classified the attachment as a financial result, or when
        the parser proved that it contains normalized quarterly facts.  The
        in-memory document remains available to the existing extraction path
        for the duration of this request.
        """
        if (
            document.content_type != "application/pdf"
            or document.discovery_provider != "NSE_OFFICIAL_API"
            or document.source_classification != SourceClassification.EXCHANGE
            or not _is_nse_official_document_url(document.canonical_url)
        ):
            return document
        has_quarterly_facts = any(
            fact.key.period_type == "QUARTERLY"
            for fact in self._official_financial_fact_candidates(document)
        )
        if not nse_financial_result and not has_quarterly_facts:
            return document
        logger.info(
            "official_document_persist provider=NSE globalInstrumentId=%s documentId=%s "
            "storage=URL_METADATA_AND_NORMALIZED_FACTS",
            document.instrument_id,
            document.document_id,
        )
        return document.model_copy(update={"raw_text": None, "normalized_text": None})

    def _prepare_ingested_document(self, *, original_url: str, source_type: SourceType, source_name: str,
                                   publisher: str, content_type: str, body: str, reliability: ReliabilityLevel,
                                   published_at: datetime | None, source_mode: SourceMode,
                                   source_classification: SourceClassification, discovered_at: datetime | None,
                                   discovery_provider: str | None, expected_profile: CompanyResearchProfile | None,
                                   document_status: DocumentStatus, allow_empty_content: bool,
                                   trusted_profile_identity: object | None,
                                   document_subtype: DocumentSubtype | None = None) -> ResearchDocument:
        canonical = canonicalize_url(original_url)
        title, extracted = extract_text(body, content_type)
        normalized = normalize_text(extracted or "")
        if source_mode == SourceMode.REAL and not allow_empty_content:
            if not normalized:
                raise FetchError("CONTENT_EMPTY")
            if len(normalized) < 40:
                raise FetchError("CONTENT_TOO_SHORT")
        parsed_published_at = published_at or extract_published_at(normalized)
        resolution = self._resolver.resolve(title, normalized, canonical)
        if expected_profile is not None and not allow_empty_content:
            if trusted_profile_identity is _TRUSTED_NSE_PROFILE_IDENTITY:
                resolution = resolution.model_copy(update={
                    "instrument_id": expected_profile.instrument_id,
                    "company_id": expected_profile.company_id,
                    "confidence": 0.99,
                })
            else:
                self._validate_document_relevance(expected_profile, resolution.instrument_id, resolution.confidence)
        if expected_profile is not None and allow_empty_content:
            resolution = resolution.model_copy(update={
                "instrument_id": expected_profile.instrument_id,
                "company_id": expected_profile.company_id,
                "confidence": 0.95,
            })
        document = ResearchDocument(
            canonical_url=canonical,
            original_url=original_url,
            title=title,
            source_type=source_type,
            source_classification=source_classification,
            source_name=source_name,
            publisher=publisher,
            published_at=parsed_published_at,
            content_type=content_type,
            document_type=detect_document_type(content_type, canonical),
            document_subtype=document_subtype,
            raw_text=body if len(body) < 20_000 else None,
            normalized_text=normalized,
            content_hash=content_hash(normalized or canonical),
            instrument_id=resolution.instrument_id,
            company_id=resolution.company_id,
            status=document_status,
            reliability_level=reliability,
            entity_resolution_confidence=resolution.confidence,
            source_mode=source_mode,
            freshness=source_mode.value,
            discovered_at=discovered_at,
            discovery_provider=discovery_provider,
            source_independence_key=content_hash(normalized or canonical),
        )
        return document

    def _persist_official_financial_facts(self, document: ResearchDocument) -> tuple[bool, int]:
        """Persist explicit NSE quarterly/annual income-statement facts."""
        if not document.instrument_id:
            return False, 0
        facts = self._official_financial_fact_candidates(document)
        if not facts:
            return False, 0
        existing = {fact.key: fact for fact in self._persistence.load_financial_facts()}
        written = 0
        for fact in facts:
            prior = existing.get(fact.key)
            same_document_correction = (
                prior is not None
                and prior.source_tier == FactSourceTier.OFFICIAL_NSE
                and prior.source_identity == str(document.document_id)
            )
            if self._persistence.upsert_financial_fact(fact, allow_same_tier_correction=same_document_correction):
                written += 1
        return True, written

    def _reconcile_persisted_official_financial_document(self, document: ResearchDocument) -> tuple[bool, int]:
        """Atomically reconcile a known trusted document without retrieval.

        This is deliberately explicit: normal reads and ordinary completeness
        repair retain their existing missing-fact selection behavior.
        """
        facts = self._official_financial_fact_candidates(document)
        if not facts:
            return False, 0
        return True, self._persistence.reconcile_financial_facts_for_source(
            document.instrument_id,
            str(document.document_id),
            facts,
        )

    @staticmethod
    def _official_financial_fact_candidates(document: ResearchDocument) -> list[FinancialFact]:
        if not document.instrument_id:
            return []
        periods = [
            *parsed_nse_income_statement_periods([document]),
            *parsed_nse_balance_sheet_periods([document]),
            *parsed_nse_cash_flow_periods([document]),
        ]
        return [
            FinancialFact(
                FinancialFactKey(document.instrument_id, metric, period.period_end, period.period_type, period.reporting_basis),
                value,
                FactSourceTier.OFFICIAL_NSE,
                "NSE",
                str(document.document_id),
                SourceMode.REAL,
            )
            for period in periods
            for metric, value in period.metrics
        ]

    def _validate_document_relevance(
        self,
        profile: CompanyResearchProfile,
        instrument_id: UUID | None,
        confidence: float,
    ) -> None:
        if instrument_id != profile.instrument_id or confidence < 0.30:
            raise FetchError("COMPANY_RELEVANCE_FAILED")

    async def refresh(
        self,
        instrument_id: UUID,
        correlation_id: str | None = None,
        *,
        allow_demo: bool = True,
        pre_resolved_categories: set[str] | None = None,
    ) -> ResearchSummary:
        existing = self._instrument_refresh_flights.get(instrument_id)
        if existing is not None:
            logger.info(
                "research_refresh_gate globalInstrumentId=%s category=ALL outcome=REUSED_IN_FLIGHT",
                instrument_id,
            )
            return await asyncio.shield(existing)

        task = asyncio.create_task(
            self._refresh_once(
                instrument_id,
                correlation_id=correlation_id,
                allow_demo=allow_demo,
                pre_resolved_categories=pre_resolved_categories or set(),
            )
        )
        self._instrument_refresh_flights[instrument_id] = task

        def cleanup(completed: asyncio.Task[ResearchSummary]) -> None:
            if self._instrument_refresh_flights.get(instrument_id) is completed:
                self._instrument_refresh_flights.pop(instrument_id, None)

        task.add_done_callback(cleanup)
        return await asyncio.shield(task)

    async def refresh_targeted_categories(
        self,
        instrument_id: UUID,
        categories: set[str],
        *,
        correlation_id: str | None = None,
        allow_demo: bool = True,
    ) -> ResearchSummary:
        """Run only planner-selected legacy capabilities under the existing flight.

        The public refresh method above retains its compatibility behavior.
        This boundary deliberately canonicalizes and filters categories before
        reaching discovery so a readiness ensure cannot widen into an ALL
        refresh.
        """
        selected = {_canonical_refresh_category(value) for value in categories if str(value).strip()}
        if not selected:
            return self.summary(instrument_id, allow_demo=allow_demo)
        existing = self._instrument_refresh_flights.get(instrument_id)
        if existing is not None:
            logger.info(
                "research_refresh_gate globalInstrumentId=%s categories=%s outcome=REUSED_IN_FLIGHT",
                instrument_id,
                sorted(selected),
            )
            return await asyncio.shield(existing)
        task = asyncio.create_task(
            self._refresh_targeted_categories_once(
                instrument_id,
                selected,
                correlation_id=correlation_id,
                allow_demo=allow_demo,
            )
        )
        self._instrument_refresh_flights[instrument_id] = task

        def cleanup(completed: asyncio.Task[ResearchSummary]) -> None:
            if self._instrument_refresh_flights.get(instrument_id) is completed:
                self._instrument_refresh_flights.pop(instrument_id, None)

        task.add_done_callback(cleanup)
        # The synchronous readiness request owns a newly-created targeted
        # refresh. Propagate its budget cancellation into provider/search work.
        # A refresh started by another caller remains shielded in the branch
        # above and is still managed by its original owner.
        return await task

    async def _refresh_targeted_categories_once(
        self,
        instrument_id: UUID,
        categories: set[str],
        *,
        correlation_id: str | None,
        allow_demo: bool,
    ) -> ResearchSummary:
        profile = self.profile(instrument_id)
        run = await self._run_blocking_persistence(
            self._persistence.start_refresh_run,
            instrument_id=profile.instrument_id,
            company_id=profile.company_id,
            correlation_id=correlation_id,
            mode="TARGETED_LIVE" if self.settings.research_live_enabled else "TARGETED_DEMO",
        )
        documents_before = len(self.documents_for(instrument_id, source_mode=SourceMode.REAL))
        events_before = len(self.events_for(instrument_id, source_mode=SourceMode.REAL))
        try:
            if self.settings.research_live_enabled:
                await self._refresh_live(
                    instrument_id,
                    set(),
                    force=True,
                    requested_categories=categories,
                )
        except asyncio.CancelledError:
            await self._run_blocking_persistence(
                self._persistence.complete_refresh_run,
                run,
                status="FAILED",
                documents_discovered=0,
                documents_accepted=0,
                events_extracted=0,
                events_created=0,
                events_updated=0,
                deduplicated_count=0,
                safe_error_code="ACQUISITION_CANCELLED",
                safe_error_message="Synchronous readiness execution budget exhausted",
            )
            logger.info(
                "research_targeted_ensure_cancelled globalInstrumentId=%s categories=%s",
                instrument_id,
                sorted(categories),
            )
            raise
        except Exception as exc:
            await self._run_blocking_persistence(
                self._persistence.complete_refresh_run,
                run,
                status="FAILED",
                documents_discovered=0,
                documents_accepted=0,
                events_extracted=0,
                events_created=0,
                events_updated=0,
                deduplicated_count=0,
                safe_error_code=type(exc).__name__,
                safe_error_message=str(exc)[:500],
            )
            raise
        self.last_refresh[instrument_id] = datetime.now(timezone.utc)
        documents_after = len(self.documents_for(instrument_id, source_mode=SourceMode.REAL))
        events_after = len(self.events_for(instrument_id, source_mode=SourceMode.REAL))
        await self._run_blocking_persistence(
            self._persistence.complete_refresh_run,
            run,
            status="COMPLETED",
            documents_discovered=max(documents_after - documents_before, 0),
            documents_accepted=max(documents_after - documents_before, 0),
            events_extracted=max(events_after - events_before, 0),
            events_created=max(events_after - events_before, 0),
            events_updated=0,
            deduplicated_count=0,
        )
        logger.info(
            "research_targeted_ensure_complete globalInstrumentId=%s categories=%s",
            instrument_id,
            sorted(categories),
        )
        return self.summary(instrument_id, allow_demo=allow_demo)

    async def _refresh_once(
        self,
        instrument_id: UUID,
        *,
        correlation_id: str | None,
        allow_demo: bool,
        pre_resolved_categories: set[str],
    ) -> ResearchSummary:
        profile = self.profile(instrument_id)
        gate = self._instrument_refresh_gate(profile, pre_resolved_categories, datetime.now(timezone.utc))
        if not gate.requires_provider_work:
            # A fresh category gate is intentionally about provider work, not
            # about whether canonical facts have already been materialised.
            # Let the live boundary perform its persisted-official-document
            # repair check before returning the existing fresh summary.
            if self.settings.research_live_enabled:
                await self._refresh_live(instrument_id, pre_resolved_categories)
            logger.info(
                "research_refresh_gate globalInstrumentId=%s category=ALL outcome=REUSE_FRESH state=%s",
                instrument_id,
                gate.state,
            )
            return self.summary(instrument_id, allow_demo=allow_demo)
        logger.info(
            "research_refresh_gate globalInstrumentId=%s category=ALL outcome=TARGETED_REFRESH reason=%s missingCategories=%s",
            instrument_id,
            gate.state,
            sorted(gate.missing_categories),
        )
        run = await self._run_blocking_persistence(self._persistence.start_refresh_run,
            instrument_id=profile.instrument_id,
            company_id=profile.company_id,
            correlation_id=correlation_id,
            mode="LIVE" if self.settings.research_live_enabled else "DEMO",
        )
        documents_before = len(self.documents_for(instrument_id, source_mode=SourceMode.REAL))
        events_before = len(self.events_for(instrument_id, source_mode=SourceMode.REAL))
        if self.settings.research_live_enabled:
            try:
                await self._refresh_live(instrument_id, pre_resolved_categories)
            except Exception as exc:
                await self._run_blocking_persistence(self._persistence.complete_refresh_run,
                    run,
                    status="FAILED",
                    documents_discovered=0,
                    documents_accepted=0,
                    events_extracted=0,
                    events_created=0,
                    events_updated=0,
                    deduplicated_count=0,
                    safe_error_code=type(exc).__name__,
                    safe_error_message=str(exc)[:500],
                )
                raise
        self.last_refresh[instrument_id] = datetime.now(timezone.utc)
        documents_after = len(self.documents_for(instrument_id, source_mode=SourceMode.REAL))
        events_after = len(self.events_for(instrument_id, source_mode=SourceMode.REAL))
        await self._run_blocking_persistence(self._persistence.complete_refresh_run,
            run,
            status="COMPLETED",
            documents_discovered=max(documents_after - documents_before, 0),
            documents_accepted=max(documents_after - documents_before, 0),
            events_extracted=max(events_after - events_before, 0),
            events_created=max(events_after - events_before, 0),
            events_updated=0,
            deduplicated_count=0,
        )
        return self.summary(instrument_id, allow_demo=allow_demo)

    async def refresh_etf(self, instrument_id: UUID, correlation_id: str | None = None) -> EtfResearchProfile:
        profile = self.etf_profile(instrument_id)
        run = await self._run_blocking_persistence(self._persistence.start_refresh_run,
            instrument_id=profile.instrument_id,
            company_id=profile.fund_id,
            correlation_id=correlation_id,
            mode="LIVE" if self.settings.research_live_enabled else "DEMO",
        )
        documents_before = len(self.documents_for(instrument_id, source_mode=SourceMode.REAL))
        if self.settings.research_live_enabled and self.settings.research_search_enabled:
            await self._refresh_etf_search_discovery(profile)
        self.last_refresh[instrument_id] = datetime.now(timezone.utc)
        documents_after = len(self.documents_for(instrument_id, source_mode=SourceMode.REAL))
        status = "COMPLETED" if documents_after > documents_before else "FAILED"
        await self._run_blocking_persistence(self._persistence.complete_refresh_run,
            run,
            status=status,
            documents_discovered=max(documents_after - documents_before, 0),
            documents_accepted=max(documents_after - documents_before, 0),
            events_extracted=0,
            events_created=0,
            events_updated=0,
            deduplicated_count=0,
            safe_error_code=self.last_live_error.get(instrument_id) if status == "FAILED" else None,
        )
        return profile

    async def _refresh_live(
        self,
        instrument_id: UUID,
        pre_resolved_categories: set[str],
        *,
        force: bool = False,
        requested_categories: set[str] | None = None,
    ) -> None:
        sources = registered_sources_for(instrument_id)
        profile = self.profile(instrument_id)
        now = datetime.now(timezone.utc)
        gate = self._instrument_refresh_gate(profile, pre_resolved_categories, now, force=force)
        due_categories = set(gate.missing_categories)
        if requested_categories is not None:
            requested_categories = {
                _canonical_refresh_category(value) for value in requested_categories
            }
            due_categories.intersection_update(requested_categories)
        logger.info(
            "research_refresh_gate globalInstrumentId=%s outcome=DUE_CATEGORIES dueCategories=%s",
            instrument_id,
            sorted(due_categories),
        )
        if requested_categories is None or "FINANCIAL_RESULTS" in requested_categories:
            await self._reconcile_incomplete_persisted_official_financial_facts(profile)
        shareholding_selected = (
            requested_categories is None or "SHAREHOLDING_PATTERN" in requested_categories
        )
        has_shareholding_reconciliation = shareholding_selected and (
            gate.shareholding_backfill_needed or gate.shareholding_category_enrichment_needed
        )
        if not due_categories and not has_shareholding_reconciliation:
            logger.info("research_refresh_gate globalInstrumentId=%s category=ALL outcome=REUSE_FRESH", instrument_id)
            return
        if not sources:
            self.last_live_error[instrument_id] = "SOURCE_UNAVAILABLE"
        for source in sources:
            if source.priority != 1:
                continue
            if not any(
                _canonical_refresh_category(category) in due_categories
                for category in source.categories
            ):
                continue
            try:
                await self._fetch_registered_source(profile, source)
                self.last_live_error.pop(instrument_id, None)
            except RestrictedFetchError:
                self.last_live_error[instrument_id] = "ACCESS_RESTRICTED"
            except (FetchError, ValueError) as exc:
                self.last_live_error[instrument_id] = f"SOURCE_UNAVAILABLE:{exc}"
        await self._refresh_targeted(
            profile,
            pre_resolved_categories,
            now=now,
            force=force,
            requested_categories=requested_categories,
        )

    def _instrument_refresh_gate(
        self,
        profile: CompanyResearchProfile,
        pre_resolved_categories: set[str],
        now: datetime,
        *,
        force: bool = False,
    ) -> _InstrumentRefreshGate:
        structured_categories = {
            "FINANCIAL_RESULTS", "Ownership", "INSTITUTIONAL_ACTIVITY", "SHAREHOLDING_PATTERN", "VALUATION",
            "ORDERS_BACKLOG", "CONTRACTS", "CAPEX", "NEW_FACILITIES", "ACQUISITIONS",
            "CLIENTS", "GUIDANCE", "ANALYST_OPINION", "ANALYST_TARGETS", "Regulatory",
        }
        missing = self._missing_categories(profile, pre_resolved_categories, now, structured_categories, force=force)
        eligible_missing: set[str] = set()
        for category in missing:
            eligible, next_eligible = (True, None) if force else self._category_is_eligible_to_check(profile.instrument_id, category, now)
            if eligible:
                eligible_missing.add(category)
            else:
                logger.info(
                    "research_refresh_gate globalInstrumentId=%s category=%s outcome=SKIP_NOT_DUE nextEligibleCheckAt=%s",
                    profile.instrument_id,
                    category,
                    next_eligible.isoformat() if next_eligible else "NONE",
                )
        missing = eligible_missing
        valid_shareholding_periods = len(self.shareholding_for(profile.instrument_id, limit=4))
        shareholding_backfill_needed = (
            self._category_is_fresh(profile.instrument_id, "SHAREHOLDING_PATTERN", now)
            and valid_shareholding_periods < 4
            and _eligible_for_nse_shareholding_reconciliation(profile)
        )
        shareholding_category_enrichment_needed = (
            self._category_is_fresh(profile.instrument_id, "SHAREHOLDING_PATTERN", now)
            and _shareholding_xbrl_enrichment_needed(self.shareholding_for(profile.instrument_id, limit=4))
            and _eligible_for_nse_shareholding_reconciliation(profile)
        )
        if not missing and not shareholding_backfill_needed and not shareholding_category_enrichment_needed:
            state = "FRESH_AND_COMPLETE"
        elif not missing and (shareholding_backfill_needed or shareholding_category_enrichment_needed):
            state = "INCOMPLETE"
        elif (
            missing == {"SHAREHOLDING_PATTERN"}
            and valid_shareholding_periods >= 4
            and _eligible_for_nse_shareholding_reconciliation(profile)
        ):
            state = "NEEDS_LIGHTWEIGHT_CHECK"
        elif any(self._category_has_qualifying_evidence(profile.instrument_id, category) for category in missing):
            state = "STALE"
        else:
            state = "INCOMPLETE"
        return _InstrumentRefreshGate(
            missing_categories=missing,
            shareholding_backfill_needed=shareholding_backfill_needed,
            shareholding_category_enrichment_needed=shareholding_category_enrichment_needed,
            state=state,
        )

    async def _refresh_targeted(
        self,
        profile: CompanyResearchProfile,
        pre_resolved_categories: set[str],
        *,
        now: datetime | None = None,
        force: bool = False,
        requested_categories: set[str] | None = None,
    ) -> None:
        now = now or datetime.now(timezone.utc)
        gate = self._instrument_refresh_gate(profile, pre_resolved_categories, now, force=force)
        missing = set(gate.missing_categories)
        if requested_categories is not None:
            requested_categories = {
                _canonical_refresh_category(value) for value in requested_categories
            }
            missing.intersection_update(requested_categories)
        due_categories = set(missing)
        shareholding_selected = (
            requested_categories is None or "SHAREHOLDING_PATTERN" in requested_categories
        )
        shareholding_backfill_needed = gate.shareholding_backfill_needed and shareholding_selected
        shareholding_category_enrichment_needed = (
            gate.shareholding_category_enrichment_needed and shareholding_selected
        )
        valid_shareholding_periods = len(self.shareholding_for(profile.instrument_id, limit=4))
        shareholding_check_succeeded = False
        successful_check_categories: set[str] = set()
        logger.info(
            "research_refresh_gate globalInstrumentId=%s outcome=DUE_CATEGORIES dueCategories=%s",
            profile.instrument_id, sorted(due_categories),
        )
        logger.info("research_missing_categories globalInstrumentId=%s categories=%s", profile.instrument_id, sorted(due_categories))
        if shareholding_backfill_needed:
            logger.info(
                "shareholding_reconciliation provider=NSE globalInstrumentId=%s outcome=START reason=FRESH_BUT_INCOMPLETE validQuarterCount=%s targetQuarterCount=4",
                profile.instrument_id,
                valid_shareholding_periods,
            )
        if shareholding_category_enrichment_needed:
            logger.info(
                "shareholding_category_enrichment provider=NSE globalInstrumentId=%s outcome=START reason=LEGACY_XBRL_PROVENANCE_MISSING validQuarterCount=%s",
                profile.instrument_id,
                valid_shareholding_periods,
            )
        if not missing and not shareholding_backfill_needed and not shareholding_category_enrichment_needed:
            logger.info("official_discovery_gate globalInstrumentId=%s eligible=false reason=NO_MISSING_CATEGORIES", profile.instrument_id)
            return
        seen_urls = {doc.canonical_url for doc in self.documents_for(profile.instrument_id, source_mode=SourceMode.REAL)}
        # Resolve authoritative filings before broad research searches can
        # exhaust public-search engines. This is global-instrument research.
        official_due_categories = {"FINANCIAL_RESULTS", "SHAREHOLDING_PATTERN"} & due_categories
        eligible_official = (
            profile.country.upper() in {"IN", "IND", "INDIA"}
            and profile.exchange.upper() in {"NSE", "XNSE"}
            and bool(official_due_categories)
        )
        logger.info("official_discovery_gate globalInstrumentId=%s eligible=%s reason=%s", profile.instrument_id, eligible_official, "NSE_OFFICIAL_CATEGORY" if eligible_official else "PROFILE_OR_CATEGORY_INELIGIBLE")
        if eligible_official:
            try:
                # Let official discovery return known URLs too: the official fetch
                # loop can then explicitly reuse a durable global document, while
                # failed/scanned historical attempts remain eligible for retry.
                official_filings = await self._official_filing_discovery.discover(profile, official_due_categories, set())
            except Exception as exc:
                official_filings = []
                self.last_live_error[profile.instrument_id] = f"OFFICIAL_FILING_DISCOVERY_UNAVAILABLE:{type(exc).__name__}"
                # OfficialFilingDiscovery emits the terminal provider diagnostic.
                # Keep this boundary diagnostic for injected/legacy implementations.
                logger.warning("official_discovery_handled provider=NSE globalInstrumentId=%s status=FAILED reason=%s", profile.instrument_id, type(exc).__name__)
            if await self._fetch_official_filings(profile, official_filings, seen_urls):
                successful_check_categories.update({"FINANCIAL_RESULTS"} & official_due_categories)
        if (
            "SHAREHOLDING_PATTERN" in due_categories
            or shareholding_backfill_needed
            or shareholding_category_enrichment_needed
        ) and _eligible_for_nse_shareholding_reconciliation(profile):
            try:
                # NSE publishes quarterly Regulation 31 data through its
                # dedicated shareholding feed, not necessarily as a corporate
                # announcement attachment.  The provider returns only real,
                # source-labelled values and durable persistence is keyed by
                # this global instrument and the official filing identity.
                persisted = 0
                discovered_snapshots = await self._official_shareholding_discovery.discover(profile)
                shareholding_check_succeeded = True
                successful_check_categories.add("SHAREHOLDING_PATTERN")
                snapshots_to_process = [
                    snapshot for snapshot in discovered_snapshots
                    if self._shareholding_snapshot_needs_processing(snapshot)
                ]
                if not snapshots_to_process:
                    logger.info(
                        "research_refresh_gate globalInstrumentId=%s category=SHAREHOLDING_PATTERN outcome=LIGHTWEIGHT_CHECK_UNCHANGED latestSourceIdentity=%s",
                        profile.instrument_id,
                        discovered_snapshots[0].source_identity_key if discovered_snapshots else "NONE",
                    )
                for snapshot in snapshots_to_process:
                    enriched_snapshot = await self._enrich_nse_shareholding_snapshot(snapshot)
                    persisted += int(await self._persist_shareholding_snapshot_async(enriched_snapshot))
                if shareholding_backfill_needed or shareholding_category_enrichment_needed:
                    logger.info(
                        "shareholding_reconciliation provider=NSE globalInstrumentId=%s outcome=COMPLETE persistedCount=%s validQuarterCountBefore=%s validQuarterCountAfter=%s reason=%s",
                        profile.instrument_id,
                        persisted,
                        valid_shareholding_periods,
                        len(self.shareholding_for(profile.instrument_id, limit=4)),
                        "FRESH_BUT_INCOMPLETE" if shareholding_backfill_needed else "LEGACY_XBRL_PROVENANCE_MISSING",
                    )
            except SearchProviderError as exc:
                self.last_live_error[profile.instrument_id] = str(exc)
            except (FetchError, ValueError) as exc:
                self.last_live_error[profile.instrument_id] = f"NSE_SHAREHOLDING_UNAVAILABLE:{type(exc).__name__}"
        if due_categories:
            discovery_categories = _search_discovery_categories(due_categories)
            discovered = (
                _profile_source_discovery(profile, discovery_categories, seen_urls)
                if self.settings.research_search_enabled and not registered_sources_for(profile.instrument_id)
                else []
            )
            discovered.extend(self._discovery.discover(profile, discovery_categories, seen_urls | {result.source.url for result in discovered}))
            fetched_source_ids: set[str] = set()
            for result in discovered:
                source = result.source
                if source.source_id in fetched_source_ids:
                    continue
                fetched_source_ids.add(source.source_id)
                try:
                    self._validate_registered_source(profile, source)
                    await self._fetch_registered_source(profile, source)
                except (FetchError, RestrictedFetchError, ValueError) as exc:
                    self.last_live_error[profile.instrument_id] = f"TARGETED_SOURCE_UNAVAILABLE:{source.source_id}:{exc}"
                except Exception:
                    self.last_live_error[profile.instrument_id] = f"TARGETED_SOURCE_UNAVAILABLE:{source.source_id}:HTTP_FETCH_FAILED"
        if self.settings.research_search_enabled:
            self._mark_qualifying_categories_fresh(
                profile.instrument_id,
                _checked_categories(missing, shareholding_check_succeeded),
                now,
            )
            # Keep the pre-discovery due set authoritative.  Some refresh
            # categories (for example REGULATORY) are deliberately not scorer
            # buckets, so recomputing missing only from score coverage would
            # drop them here before their successful no-change check can be
            # recorded.  Evidence that arrived during this refresh still
            # removes its category from fallback.
            search_categories = {
                category
                for category in due_categories
                if not self._category_has_qualifying_evidence(profile.instrument_id, category)
            }
            if search_categories:
                logger.info("search_fallback_start globalInstrumentId=%s missingCategories=%s", profile.instrument_id, sorted(search_categories))
                refreshed_seen_urls = {
                    doc.canonical_url for doc in self.documents_for(profile.instrument_id, source_mode=SourceMode.REAL)
                }
                if await self._refresh_search_discovery(
                    profile, _search_discovery_categories(search_categories), refreshed_seen_urls
                ):
                    successful_check_categories.update(search_categories)
        self._mark_qualifying_categories_fresh(
            profile.instrument_id,
            _checked_categories(missing, shareholding_check_succeeded),
            now,
        )
        self._mark_successful_categories_checked(profile.instrument_id, successful_check_categories, now)

    async def _incomplete_persisted_official_financial_documents(self, profile: CompanyResearchProfile) -> list[ResearchDocument]:
        if (profile.country.upper() not in {"IN", "IND", "INDIA"} or profile.exchange.upper() not in {"NSE", "XNSE"}
                or not profile.provider_instrument_ids.get("NSE")):
            logger.info("official_financial_fact_reconcile provider=NSE globalInstrumentId=%s outcome=SKIPPED reason=INELIGIBLE_PROFILE", profile.instrument_id)
            return []
        documents = [document for document in self.documents_for(profile.instrument_id, source_mode=SourceMode.REAL)
                     if document.company_id == profile.company_id and document.source_classification == SourceClassification.EXCHANGE
                     and document.source_type == SourceType.EXCHANGE_ANNOUNCEMENT and bool(document.normalized_text)
                     and _is_nse_official_document_url(document.canonical_url)]
        if not documents:
            logger.info("official_financial_fact_reconcile provider=NSE globalInstrumentId=%s outcome=SKIPPED reason=NO_QUALIFYING_PERSISTED_DOCUMENT", profile.instrument_id)
            return []
        def incomplete_window_documents():
            # Source freshness and derived-fact completeness are deliberately
            # independent.  Parse only the durable, trusted documents already
            # held for this instrument, then use their explicit periods to
            # define the rolling window.  No calendar-derived target periods
            # are invented here.
            parsed_by_document = [
                (document, parsed_nse_income_statement_periods([document]))
                for document in documents
            ]
            parsed_by_document = [item for item in parsed_by_document if item[1]]
            if not parsed_by_document:
                return None

            available_periods: dict[tuple[str, str | None], set[str]] = {}
            periods_by_document: dict[UUID, set[tuple[str, str | None, str]]] = {}
            for document, periods in parsed_by_document:
                document_periods: set[tuple[str, str | None, str]] = set()
                for period in periods:
                    if period.period_type not in {"QUARTERLY", "ANNUAL"}:
                        continue
                    # A parsed result column is evidence of an explicitly
                    # reported period even when one core field is absent.  It
                    # must not silently make the rolling window complete.
                    if not dict(period.metrics):
                        continue
                    group = (period.period_type, period.reporting_basis)
                    available_periods.setdefault(group, set()).add(period.period_end)
                    document_periods.add((period.period_type, period.reporting_basis, period.period_end))
                if document_periods:
                    periods_by_document[document.document_id] = document_periods

            target_periods = {
                (period_type, reporting_basis, period_end)
                for (period_type, reporting_basis), periods in available_periods.items()
                for period_end in sorted(periods, reverse=True)[:4]
            }
            if not target_periods:
                return None

            facts = self._persistence.load_financial_facts()
            present_by_period: dict[tuple[str, str | None, str], set[str]] = {}
            for fact in facts:
                if (
                    fact.key.instrument_id != profile.instrument_id
                    or fact.key.period_type not in {"QUARTERLY", "ANNUAL"}
                    or fact.source_tier != FactSourceTier.OFFICIAL_NSE
                    or fact.source_mode != SourceMode.REAL
                    or not fact.key.period_end
                ):
                    continue
                key = (fact.key.period_type, fact.key.reporting_basis, fact.key.period_end)
                present_by_period.setdefault(key, set()).add(fact.key.metric)

            incomplete = {
                key for key in target_periods
                if not {"revenue", "pat"} <= present_by_period.get(key, set())
            }
            if not incomplete:
                return [], target_periods, incomplete
            selected = [
                document for document, _periods in parsed_by_document
                if periods_by_document.get(document.document_id, set()) & incomplete
            ]
            return selected, target_periods, incomplete

        window = await self._run_blocking_persistence(incomplete_window_documents)
        if window is None:
            logger.info("official_financial_fact_reconcile provider=NSE globalInstrumentId=%s outcome=SKIPPED reason=NO_PARSEABLE_QUARTERLY_RESULT", profile.instrument_id)
            return []
        selected, target_periods, incomplete = window
        if not selected:
            logger.info(
                "official_financial_fact_reconcile provider=NSE globalInstrumentId=%s outcome=SKIPPED reason=ROLLING_WINDOW_COMPLETE targetPeriodCount=%s",
                profile.instrument_id,
                len(target_periods),
            )
            return []
        logger.info(
            "official_financial_fact_reconcile provider=NSE globalInstrumentId=%s outcome=REQUIRED targetPeriodCount=%s incompletePeriodCount=%s documentCount=%s",
            profile.instrument_id,
            len(target_periods),
            len(incomplete),
            len(selected),
        )
        return selected

    async def _reconcile_incomplete_persisted_official_financial_facts(
        self,
        profile: CompanyResearchProfile,
    ) -> bool:
        documents = await self._incomplete_persisted_official_financial_documents(profile)
        for document in documents:
            logger.info("official_financial_fact_reconcile provider=NSE globalInstrumentId=%s outcome=START documentId=%s", profile.instrument_id, document.document_id)
            parsed, written = await self._run_blocking_persistence(self._persist_official_financial_facts, document)
            logger.info("official_financial_fact_reconcile provider=NSE globalInstrumentId=%s outcome=COMPLETE documentId=%s parserResult=%s factsWritten=%s", profile.instrument_id, document.document_id, "PARSED" if parsed else "NO_RESULT", written)
        return bool(documents)

    def _shareholding_snapshot_needs_processing(self, snapshot: ShareholdingSnapshot) -> bool:
        existing = next(
            (
                item for item in self.shareholding_snapshots.values()
                if item.instrument_id == snapshot.instrument_id
                and item.source_provider == snapshot.source_provider
                and item.source_identity_key == snapshot.source_identity_key
            ),
            None,
        )
        if existing is None:
            return True
        return not any((value.source_locator or "").startswith("nse-xbrl:") for value in existing.values)

    async def _enrich_nse_shareholding_snapshot(self, snapshot: ShareholdingSnapshot) -> ShareholdingSnapshot:
        """Attach source-specific XBRL category facts without changing discovery identity."""
        if snapshot.source_type != "NSE_SHAREHOLDING_XBRL":
            return snapshot
        existing = next((item for item in self.shareholding_snapshots.values()
                         if item.instrument_id == snapshot.instrument_id
                         and item.source_provider == snapshot.source_provider
                         and item.source_identity_key == snapshot.source_identity_key), None)
        if existing and any((value.source_locator or "").startswith("nse-xbrl:") for value in existing.values):
            return snapshot
        try:
            fetch_xbrl = getattr(self._fetcher, "fetch_nse_shareholding_xbrl", None)
            result = await (fetch_xbrl(snapshot.source_url) if callable(fetch_xbrl)
                            else self._fetcher.fetch(snapshot.source_url))
        except (FetchError, RestrictedFetchError, ValueError) as exc:
            logger.warning(
                "shareholding_xbrl_fetch globalInstrumentId=%s host=%s path=%s outcome=UNAVAILABLE reason=%s",
                snapshot.instrument_id,
                (urlparse(snapshot.source_url).hostname or "").lower(),
                _safe_url_path(snapshot.source_url),
                str(exc)[:160],
            )
            return snapshot
        values = await asyncio.to_thread(parse_nse_shareholding_xbrl, result.text)
        if not values:
            logger.info("shareholding_xbrl_parse globalInstrumentId=%s outcome=REJECTED reason=NO_SUPPORTED_EXPLICIT_VALUES", snapshot.instrument_id)
            return snapshot
        logger.info("shareholding_xbrl_parse globalInstrumentId=%s outcome=SUCCESS valueCount=%s", snapshot.instrument_id, len(values))
        return snapshot.model_copy(update={"values": values})

    async def _fetch_official_filings(
        self,
        profile: CompanyResearchProfile,
        filings: list[DiscoveryResult],
        seen_urls: set[str],
    ) -> bool:
        """Fetch a small, newest-first official filing set within an interactive budget."""
        attempted = 0
        completed_without_failure = True
        host_transport_failures: dict[str, int] = {}
        scheduled_filings = _fair_official_filing_order(filings)
        for filing_index, result in enumerate(scheduled_filings):
            source = result.source
            reusable = self._reusable_official_document(profile.instrument_id, source.url)
            if reusable is not None:
                if source.document_subtype and reusable.document_subtype is None:
                    reusable.document_subtype = source.document_subtype
                    await self._run_blocking_persistence(
                        self._persist_ingested_document,
                        reusable,
                        nse_financial_result="FINANCIAL_RESULTS" in source.categories,
                    )
                seen_urls.add(reusable.canonical_url)
                await self._run_blocking_persistence(self._reconcile_reused_official_financial_facts, profile, source, reusable)
                logger.info(
                    "official_document_fetch provider=NSE globalInstrumentId=%s host=%s path=%s outcome=REUSED reason=ALREADY_PERSISTED documentId=%s",
                    profile.instrument_id,
                    (urlparse(source.url).hostname or "").lower(),
                    _safe_url_path(source.url),
                    reusable.document_id,
                )
                continue
            if attempted >= self.settings.research_official_document_max_attempts_per_refresh:
                logger.info(
                    "official_document_fetch provider=NSE globalInstrumentId=%s outcome=SKIPPED reason=ATTEMPT_BUDGET",
                    profile.instrument_id,
                )
                # Continue so a later durable reusable filing can still be
                # recorded without consuming network budget.
                continue
            host = (urlparse(source.url).hostname or "").lower()
            if host_transport_failures.get(host, 0) >= self.settings.research_official_document_max_transport_failures_per_host:
                logger.info(
                    "official_document_fetch provider=NSE globalInstrumentId=%s host=%s outcome=SKIPPED reason=HOST_TRANSPORT_FAILURE_BUDGET",
                    profile.instrument_id,
                    host,
                )
                remaining_hosts = {
                    (urlparse(item.source.url).hostname or "").lower()
                    for item in scheduled_filings[filing_index:]
                }
                if remaining_hosts == {host}:
                    logger.info(
                        "official_document_fetch provider=NSE globalInstrumentId=%s host=%s outcome=STOPPED reason=HOST_TRANSPORT_FAILURE_BUDGET",
                        profile.instrument_id,
                        host,
                    )
                    break
                continue
            attempted += 1
            started = time.monotonic()
            try:
                document, joined_in_flight = await self._single_flight_official_filing(profile, source)
                if document.status != DocumentStatus.DUPLICATE:
                    seen_urls.add(document.canonical_url)
                if document.status != DocumentStatus.DUPLICATE and not _is_usable_durable_document(document):
                    # A completed transport attempt is not a successful
                    # no-change check when extraction/persistence left only a
                    # terminal non-usable document. Keep it retryable.
                    completed_without_failure = False
                if joined_in_flight:
                    logger.info(
                        "official_document_fetch provider=NSE globalInstrumentId=%s host=%s path=%s outcome=REUSED reason=IN_FLIGHT_SINGLE_FLIGHT documentId=%s",
                        profile.instrument_id, host, _safe_url_path(source.url), document.document_id,
                    )
                logger.info(
                    "official_document_fetch provider=NSE globalInstrumentId=%s host=%s path=%s outcome=SUCCESS reason=NONE elapsedMs=%s httpStatus=%s",
                    profile.instrument_id,
                    host,
                    _safe_url_path(source.url),
                    _elapsed_ms(started),
                    200,
                )
                logger.info(
                    "official_document_persist provider=NSE globalInstrumentId=%s documentType=%s outcome=%s",
                    profile.instrument_id,
                    document.document_type,
                    document.status,
                )
            except TimeoutError:
                completed_without_failure = False
                exc = TransportFetchError("NETWORK_TIMEOUT")
                host_transport_failures[host] = host_transport_failures.get(host, 0) + 1
                self.last_live_error[profile.instrument_id] = "OFFICIAL_FILING_FETCH_FAILED:NETWORK_TIMEOUT"
                logger.warning(
                    "official_document_fetch provider=NSE globalInstrumentId=%s host=%s path=%s outcome=FAILED reason=%s elapsedMs=%s httpStatus=%s",
                    profile.instrument_id, host, _safe_url_path(source.url), exc, _elapsed_ms(started), "NONE",
                )
            except TransportFetchError as exc:
                completed_without_failure = False
                host_transport_failures[host] = host_transport_failures.get(host, 0) + 1
                self.last_live_error[profile.instrument_id] = f"OFFICIAL_FILING_FETCH_FAILED:{type(exc).__name__}"
                logger.warning(
                    "official_document_fetch provider=NSE globalInstrumentId=%s host=%s path=%s outcome=FAILED reason=%s elapsedMs=%s httpStatus=%s",
                    profile.instrument_id, host, _safe_url_path(source.url), str(exc), _elapsed_ms(started), "NONE",
                )
            except (FetchError, RestrictedFetchError, ValueError) as exc:
                completed_without_failure = False
                if _is_transport_fetch_failure(exc):
                    host_transport_failures[host] = host_transport_failures.get(host, 0) + 1
                self.last_live_error[profile.instrument_id] = f"OFFICIAL_FILING_FETCH_FAILED:{type(exc).__name__}"
                logger.warning(
                    "official_document_fetch provider=NSE globalInstrumentId=%s host=%s path=%s outcome=FAILED reason=%s elapsedMs=%s httpStatus=%s",
                    profile.instrument_id,
                    host,
                    _safe_url_path(source.url),
                    str(exc),
                    _elapsed_ms(started),
                    getattr(exc, "status_code", "NONE"),
                )
        return completed_without_failure

    def _reconcile_reused_official_financial_facts(
        self,
        profile: CompanyResearchProfile,
        source: RegisteredResearchSource,
        document: ResearchDocument,
    ) -> None:
        reason = self._reusable_financial_fact_reconcile_skip_reason(profile, source, document)
        if reason is not None:
            logger.info("official_financial_fact_reconcile provider=NSE globalInstrumentId=%s documentId=%s outcome=SKIPPED reason=%s",
                        profile.instrument_id, document.document_id, reason)
            return
        parsed, written = self._persist_official_financial_facts(document)
        outcome, reason = ("PROCESSED", "FACTS_UPSERTED" if written else "NO_CHANGES") if parsed else ("SKIPPED", "PARSE_NO_RESULT")
        logger.info("official_financial_fact_reconcile provider=NSE globalInstrumentId=%s documentId=%s outcome=%s reason=%s",
                    profile.instrument_id, document.document_id, outcome, reason)

    def _reusable_financial_fact_reconcile_skip_reason(
        self,
        profile: CompanyResearchProfile,
        source: RegisteredResearchSource,
        document: ResearchDocument,
    ) -> str | None:
        if not self._has_trusted_nse_profile_identity(profile, source, profile):
            return "UNTRUSTED_SOURCE"
        if "FINANCIAL_RESULTS" not in source.categories:
            return "NOT_FINANCIAL_RESULT"
        if (document.source_mode != SourceMode.REAL or document.instrument_id != profile.instrument_id
                or document.company_id != profile.company_id
                or document.source_classification != SourceClassification.EXCHANGE
                or document.source_type != SourceType.EXCHANGE_ANNOUNCEMENT):
            return "UNTRUSTED_SOURCE"
        if not document.normalized_text:
            return "NO_NORMALIZED_TEXT"
        return None

    async def _single_flight_official_filing(
        self,
        profile: CompanyResearchProfile,
        source: RegisteredResearchSource,
    ) -> tuple[ResearchDocument, bool]:
        """Share one official-file operation without retaining completed keys."""
        key = (profile.instrument_id, canonicalize_url(source.url))
        flight = self._official_filing_flights.get(key)
        joined_in_flight = flight is not None
        if flight is None:
            flight = asyncio.create_task(
                self._run_official_filing_flight(key, profile, source)
            )
            self._official_filing_flights[key] = flight
        try:
            # A cancelled follower must not cancel the leader's work.
            return await asyncio.shield(flight), joined_in_flight
        except asyncio.CancelledError:
            # The caller that created the flight owns cancellation.  This also
            # makes its failure visible to followers and guarantees cleanup in
            # the worker's finally block.
            if not joined_in_flight and not flight.done():
                flight.cancel()
            raise

    async def _run_official_filing_flight(
        self,
        key: tuple[UUID, str],
        profile: CompanyResearchProfile,
        source: RegisteredResearchSource,
    ) -> ResearchDocument:
        try:
            self._validate_registered_source(profile, source)
            parsed_url = urlparse(source.url)
            host = (parsed_url.hostname or "").lower()
            logger.info(
                "official_document_fetch_start provider=NSE globalInstrumentId=%s scheme=%s host=%s path=%s timeoutSeconds=%s connectTimeoutSeconds=%s retries=%s",
                profile.instrument_id, parsed_url.scheme, host, _safe_url_path(source.url),
                self.settings.research_official_document_timeout_seconds,
                self.settings.research_connect_timeout_seconds, self.settings.research_max_retries,
            )
            if isinstance(self._fetcher, HttpResearchFetcher):
                async with asyncio.timeout(self.settings.research_official_document_timeout_seconds):
                    network_result = await self._fetcher.fetch_network(
                        source.url,
                        headers={"User-Agent": self.settings.research_official_document_user_agent},
                        max_bytes=self.settings.research_official_document_max_bytes,
                    )
            else:
                network_result = None
            if network_result is not None:
                fetch_result = await self._fetcher.process_network_response_async(
                    network_result,
                    max_bytes=self.settings.research_official_document_max_bytes,
                    extraction_timeout_seconds=self.settings.research_official_document_extraction_timeout_seconds,
                )
                logger.info("fetch_persist_start provider=NSE globalInstrumentId=%s host=%s path=%s", profile.instrument_id, host, _safe_url_path(source.url))
                document = await self._ingest_registered_fetch_result_async(profile, source, fetch_result, expected_profile=profile)
                logger.info("fetch_persist_complete provider=NSE globalInstrumentId=%s host=%s path=%s", profile.instrument_id, host, _safe_url_path(source.url))
                return document
            return await self._fetch_registered_source(profile, source, expected_profile=profile)
        finally:
            # Identity check prevents an old, cancelled flight from removing a
            # retry flight that was installed for the same key.
            if self._official_filing_flights.get(key) is asyncio.current_task():
                self._official_filing_flights.pop(key, None)

    def _reusable_official_document(self, instrument_id: UUID, url: str) -> ResearchDocument | None:
        canonical = canonicalize_url(url)
        return next(
            (
                document
                for document in self.documents_for(instrument_id, source_mode=SourceMode.REAL)
                if document.canonical_url == canonical and _is_usable_durable_document(document)
            ),
            None,
        )

    def _missing_categories(
        self,
        profile: CompanyResearchProfile,
        pre_resolved_categories: set[str],
        now: datetime,
        structured_categories: set[str],
        *,
        force: bool = False,
    ) -> set[str]:
        coverage = self._scorer.score(
            profile.instrument_id,
            self.events_for(profile.instrument_id, source_mode=SourceMode.REAL),
        ).category_evidence
        missing = {
            _canonical_refresh_category(category)
            for category, evidence in coverage.items()
            if evidence.status == "NO_EVIDENCE"
        }
        missing.update(_canonical_refresh_category(category) for category in structured_categories)
        missing.difference_update(_canonical_refresh_category(category) for category in pre_resolved_categories)
        if force:
            return missing
        return {
            category for category in missing
            if not self._category_is_fresh(profile.instrument_id, category, now)
            or self._quarterly_window_open(profile.instrument_id, category, now)
        }

    def _quarterly_window_open(self, instrument_id: UUID, category: str, now: datetime) -> bool:
        if _CATEGORY_STRATEGIES.get(category) is not _QUARTERLY_WINDOW:
            return False
        _evidence_at, period_end = self._category_evidence_timing(instrument_id, category)
        return period_end is not None and now >= _next_quarter_window(period_end)

    def _category_is_eligible_to_check(
        self,
        instrument_id: UUID,
        category: str,
        now: datetime,
    ) -> tuple[bool, datetime | None]:
        """Keep provider polling separate from evidence freshness.

        `_category_refresh` records the last successful provider check in this
        process. Evidence time remains on the durable document/snapshot and is
        never changed when an official listing is unchanged.
        """
        category = _canonical_refresh_category(category)
        strategy = _CATEGORY_STRATEGIES.get(category, _PERIODIC_SLOW)
        evidence_at, period_end = self._category_evidence_timing(instrument_id, category)
        if evidence_at is None:
            checked_at = self._category_successful_no_change_checks.get((instrument_id, category))
            if checked_at is None:
                # Failed provider attempts never write this state and remain
                # immediately retryable.
                return True, None
            next_eligible = checked_at + strategy.lightweight_check_interval
            return now >= next_eligible, next_eligible
        checked_at = self._category_refresh.get((instrument_id, category))
        if strategy is _QUARTERLY_WINDOW and period_end is not None:
            next_window = _next_quarter_window(period_end)
            next_eligible = next_window
            if checked_at is not None and checked_at >= next_window:
                next_eligible = checked_at + strategy.lightweight_check_interval
            return now >= next_eligible, next_eligible
        if strategy is _ANNUAL_WINDOW and period_end is not None:
            next_window = _next_annual_window(period_end)
            next_eligible = next_window
            if checked_at is not None and checked_at >= next_window:
                next_eligible = checked_at + strategy.lightweight_check_interval
            return now >= next_eligible, next_eligible
        next_eligible = (checked_at or evidence_at) + strategy.lightweight_check_interval
        return now >= next_eligible, next_eligible

    def _category_evidence_timing(
        self,
        instrument_id: UUID,
        category: str,
    ) -> tuple[datetime | None, datetime | None]:
        category = _canonical_refresh_category(category)
        if category == "SHAREHOLDING_PATTERN":
            snapshots = self.shareholding_for(instrument_id, limit=1)
            if snapshots:
                return snapshots[0].retrieved_at, snapshots[0].period_end
            return None, None
        if category == "FINANCIAL_RESULTS":
            documents = [
                document for document in self.documents_for(instrument_id, source_mode=SourceMode.REAL)
                if _is_usable_durable_document(document)
                and any(term in f"{document.title or ''} {document.normalized_text or ''}".lower()
                        for term in ("financial result", "quarterly result", "earnings", "annual report"))
            ]
            if documents:
                latest = documents[0]
                return latest.retrieved_at, _explicit_quarter_end(latest.normalized_text or latest.raw_text or "")
        evidence = self._qualifying_category_evidence(instrument_id, category)
        if evidence and isinstance(evidence.get("evidence_at"), str):
            return _parse_iso_datetime(evidence["evidence_at"]), None
        return None, None

    def _mark_qualifying_categories_fresh(self, instrument_id: UUID, categories: set[str], now: datetime) -> None:
        for category in categories:
            category = _canonical_refresh_category(category)
            evidence = self._qualifying_category_evidence(instrument_id, category)
            if evidence is not None:
                self._category_refresh[(instrument_id, category)] = now
                logger.info(
                    "research_refresh_gate globalInstrumentId=%s category=%s outcome=CHECK_COMPLETE reason=%s documentId=%s lastCheckedAt=%s evidenceAt=%s",
                    instrument_id,
                    category,
                    evidence["reason"],
                    evidence.get("document_id", "NONE"),
                    now.isoformat(),
                    evidence.get("evidence_at", "NONE"),
                )

    def _mark_successful_categories_checked(self, instrument_id: UUID, categories: set[str], now: datetime) -> None:
        """Record successful no-change checks without manufacturing evidence."""
        for raw_category in categories:
            category = _canonical_refresh_category(raw_category)
            if self._category_has_qualifying_evidence(instrument_id, category):
                self._category_refresh[(instrument_id, category)] = now
                continue
            self._category_successful_no_change_checks[(instrument_id, category)] = now
            logger.info(
                "research_refresh_gate globalInstrumentId=%s category=%s outcome=LIGHTWEIGHT_CHECK_UNCHANGED lastCheckedAt=%s evidenceAt=NONE",
                instrument_id, category, now.isoformat(),
            )

    def _category_has_qualifying_evidence(self, instrument_id: UUID, category: str) -> bool:
        return self._qualifying_category_evidence(instrument_id, category) is not None

    def _qualifying_category_evidence(self, instrument_id: UUID, category: str) -> dict[str, object] | None:
        category = _canonical_refresh_category(category)
        if category == "SHAREHOLDING_PATTERN":
            snapshots = self.shareholding_for(instrument_id, limit=1)
            if snapshots:
                snapshot = snapshots[0]
                return {
                    "reason": "DURABLE_REAL_SHAREHOLDING_SNAPSHOT",
                    "document_id": snapshot.research_document_id or "NONE",
                    "evidence_at": snapshot.retrieved_at.isoformat(),
                }
        category_evidence = self._scorer.score(
            instrument_id,
            self.events_for(instrument_id, source_mode=SourceMode.REAL),
        ).category_evidence
        for evidence_key in _refresh_evidence_keys(category):
            evidence = category_evidence.get(evidence_key)
            if evidence is not None and evidence.status != "NO_EVIDENCE":
                return {"reason": "REAL_EVENT_EVIDENCE"}
        if category != "FINANCIAL_RESULTS":
            return None
        result_terms = ("financial result", "quarterly result", "earnings", "annual report")
        for document in self.documents_for(instrument_id, source_mode=SourceMode.REAL):
            if _is_usable_durable_document(document) and any(
                term in f"{document.title or ''} {document.normalized_text or ''}".lower() for term in result_terms
            ):
                return {
                    "reason": "DURABLE_FINANCIAL_RESULT_DOCUMENT",
                    "document_id": document.document_id,
                    "evidence_at": document.retrieved_at.isoformat(),
                }
        return None

    def _category_is_fresh(self, instrument_id: UUID, category: str, now: datetime) -> bool:
        category = _canonical_refresh_category(category)
        refreshed_at = self._category_refresh.get((instrument_id, category))
        if category == "SHAREHOLDING_PATTERN" and refreshed_at is None:
            snapshots = self.shareholding_for(instrument_id, limit=1)
            if snapshots:
                refreshed_at = snapshots[0].retrieved_at
        if refreshed_at is None or not self._category_has_qualifying_evidence(instrument_id, category):
            return False
        if category == "FINANCIAL_RESULTS":
            ttl = self.settings.research_quarterly_freshness_seconds
        elif category in {"Ownership", "INSTITUTIONAL_ACTIVITY", "SHAREHOLDING_PATTERN"}:
            ttl = self.settings.research_shareholding_freshness_seconds
        elif category in {"ORDERS_BACKLOG", "CONTRACTS", "CAPEX", "NEW_FACILITIES", "ACQUISITIONS", "CLIENTS", "GUIDANCE", "REGULATORY"}:
            ttl = self.settings.research_catalyst_freshness_seconds
        elif category in {"ANALYST_OPINION", "ANALYST_TARGETS", "VALUATION"}:
            ttl = self.settings.research_analyst_freshness_seconds
        else:
            ttl = self.settings.research_search_refresh_cooldown_seconds
        return (now - refreshed_at).total_seconds() < ttl

    async def _refresh_search_discovery(self, profile: CompanyResearchProfile, missing: set[str], seen_urls: set[str]) -> bool:
        try:
            discovered = await self._search_discovery.discover(profile, missing, seen_urls)
        except SearchProviderError as exc:
            reason = str(exc)
            self._search_discovery.last_stats.reject(reason)
            self.last_live_error[profile.instrument_id] = f"SEARCH_PROVIDER_UNAVAILABLE:{reason}"
            logger.warning("research_discovery_terminal company=%s provider=%s status=SEARCH_PROVIDER_UNAVAILABLE reason=%s",
                profile.company_name, self._search_discovery.provider.provider_name, reason)
            return False
        if not discovered:
            stats = self._search_discovery.last_stats
            if stats.candidate_count == 0:
                terminal = "SEARCH_RETURNED_ZERO_RESULTS"
            else:
                terminal = "RESULTS_REJECTED"
            self.last_live_error[profile.instrument_id] = terminal
            logger.warning("research_discovery_terminal company=%s provider=%s status=%s candidates=%s accepted=%s rejected_reasons=%s",
                profile.company_name, self._search_discovery.provider.provider_name, terminal,
                stats.candidate_count, stats.accepted_count, stats.rejected_reasons)
            # A zero-result response is a successful no-change check only
            # when the provider completed the requested search work.  Partial
            # engine/provider failures remain retryable and must not advance
            # the no-change schedule.
            return stats.provider_failure_count == 0
        fetched_documents = 0
        extracted_before = len(self.events)
        fetch_failures: dict[str, int] = {}
        for result in discovered:
            if fetched_documents >= self.settings.research_search_max_documents_per_refresh:
                break
            source = result.source
            try:
                self._validate_registered_source(profile, source)
                document = await self._fetch_registered_source(profile, source, expected_profile=profile)
                if document.status == DocumentStatus.DUPLICATE:
                    self._search_discovery.last_stats.reject("DUPLICATE")
                    continue
                fetched_documents += 1
                self._search_discovery.last_stats.reject("SEARCH_RESULT_ACCEPTED")
            except RestrictedFetchError as exc:
                self._search_discovery.last_stats.reject("ROBOTS_OR_ACCESS_BLOCKED")
                fetch_failures["ROBOTS_OR_ACCESS_BLOCKED"] = fetch_failures.get("ROBOTS_OR_ACCESS_BLOCKED", 0) + 1
                self.last_live_error[profile.instrument_id] = f"SEARCH_SOURCE_UNAVAILABLE:{source.source_id}:{exc}"
            except FetchError as exc:
                failure = _fetch_rejection_reason(exc)
                self._search_discovery.last_stats.reject(failure)
                fetch_failures[failure] = fetch_failures.get(failure, 0) + 1
                self.last_live_error[profile.instrument_id] = f"SEARCH_SOURCE_UNAVAILABLE:{source.source_id}:{exc}"
            except ValueError as exc:
                self._search_discovery.last_stats.reject("PARSER_FAILED")
                fetch_failures["PARSER_FAILED"] = fetch_failures.get("PARSER_FAILED", 0) + 1
                self.last_live_error[profile.instrument_id] = f"SEARCH_SOURCE_UNAVAILABLE:{source.source_id}:{exc}"
            except Exception as exc:
                self._search_discovery.last_stats.reject("HTTP_FETCH_FAILED")
                fetch_failures["HTTP_FETCH_FAILED"] = fetch_failures.get("HTTP_FETCH_FAILED", 0) + 1
                self.last_live_error[profile.instrument_id] = f"SEARCH_SOURCE_UNAVAILABLE:{source.source_id}:HTTP_FETCH_FAILED"
        if fetched_documents > 0:
            self.last_live_error.pop(profile.instrument_id, None)
        elif discovered:
            self.last_live_error[profile.instrument_id] = "DOCUMENT_FETCH_FAILED"
        self._search_discovery.last_stats.documents_fetched = fetched_documents
        self._search_discovery.last_stats.events_extracted = len(self.events) - extracted_before
        logger.info("research_fetch_complete company=%s provider=%s accepted_results=%s document_fetch_count=%s extraction_count=%s terminal_status=%s failure_reasons=%s",
            profile.company_name, self._search_discovery.provider.provider_name, len(discovered), fetched_documents,
            self._search_discovery.last_stats.events_extracted,
            "RESOLVED_PARTIAL_DATA" if fetched_documents else "DOCUMENT_FETCH_FAILED", fetch_failures)
        return not fetch_failures

    async def _refresh_etf_search_discovery(self, profile: EtfResearchProfile) -> None:
        categories = {"ETF_PROFILE", "ETF_PERFORMANCE", "INDEX_OUTLOOK", "ETF_RISK"}
        seen_urls = {doc.canonical_url for doc in self.documents_for(profile.instrument_id, source_mode=SourceMode.REAL)}
        try:
            discovered = await self._search_discovery.discover(profile, categories, seen_urls)
        except SearchProviderError as exc:
            reason = str(exc)
            self._search_discovery.last_stats.reject(reason)
            self.last_live_error[profile.instrument_id] = reason
            return
        if not discovered:
            self.last_live_error[profile.instrument_id] = "ETF_RESEARCH_SOURCE_UNAVAILABLE:NO_ACCEPTABLE_DOCUMENT"
            return
        fetched_documents = 0
        for result in discovered:
            if fetched_documents >= self.settings.research_search_max_documents_per_refresh:
                break
            try:
                self._validate_registered_source(profile, result.source)
                document = await self._fetch_etf_source(profile, result.source)
                if document.status == DocumentStatus.DUPLICATE:
                    self._search_discovery.last_stats.reject("DUPLICATE")
                    continue
                fetched_documents += 1
                self._search_discovery.last_stats.reject("SEARCH_RESULT_ACCEPTED")
            except RestrictedFetchError:
                self._search_discovery.last_stats.reject("ROBOTS_OR_ACCESS_BLOCKED")
            except FetchError as exc:
                self._search_discovery.last_stats.reject(_fetch_rejection_reason(exc))
            except ValueError:
                self._search_discovery.last_stats.reject("PARSER_FAILED")
            except Exception:
                self._search_discovery.last_stats.reject("HTTP_FETCH_FAILED")
        if fetched_documents > 0:
            self.last_live_error.pop(profile.instrument_id, None)
        else:
            self.last_live_error[profile.instrument_id] = "ETF_RESEARCH_SOURCE_UNAVAILABLE:NO_ACCEPTABLE_DOCUMENT"
        self._search_discovery.last_stats.documents_fetched = fetched_documents

    async def _fetch_registered_source(
        self,
        profile: CompanyResearchProfile,
        source: RegisteredResearchSource,
        *,
        expected_profile: CompanyResearchProfile | None = None,
    ) -> ResearchDocument:
        self._validate_registered_source(profile, source)
        result = await self._fetcher.fetch(source.url)
        return await self._ingest_registered_fetch_result_async(profile, source, result, expected_profile=expected_profile)

    async def _ingest_registered_fetch_result_async(
        self, profile: CompanyResearchProfile, source: RegisteredResearchSource, result, *,
        expected_profile: CompanyResearchProfile | None = None,
    ) -> ResearchDocument:
        if result.status_code >= 400:
            raise FetchError(f"Registered source returned {result.status_code}")
        if source.discovery_method == "NSE_OFFICIAL_API":
            logger.info("official_document_fetch provider=NSE globalInstrumentId=%s documentType=%s httpStatus=%s extractionStatus=%s", profile.instrument_id, result.content_type, result.status_code, result.extraction_status)
        document_status = DocumentStatus.FAILED if result.extraction_status != "EXTRACTED" else DocumentStatus.PARSED
        trusted_identity = _TRUSTED_NSE_PROFILE_IDENTITY if self._has_trusted_nse_profile_identity(profile, source, expected_profile) else None
        started = time.monotonic()
        try:
            document = await asyncio.to_thread(
                self._prepare_ingested_document,
                original_url=result.final_url, source_type=source.source_type, source_classification=source.source_classification,
                source_name=source.source_name, publisher=source.publisher, content_type=result.content_type, body=result.text,
                reliability=source.reliability_level, published_at=None, source_mode=SourceMode.REAL,
                discovered_at=None,
                discovery_provider=(source.discovery_method if source.discovery_method == "SEARCH_DISCOVERY" or trusted_identity else None),
                expected_profile=expected_profile, document_status=document_status,
                allow_empty_content=result.extraction_status != "EXTRACTED", trusted_profile_identity=trusted_identity,
                document_subtype=source.document_subtype,
            )
        except Exception:
            logger.info("document_ingest_stage globalInstrumentId=%s stage=PREPARE elapsedMs=%s outcome=FAILED", profile.instrument_id, _elapsed_ms(started))
            raise
        logger.info("document_ingest_stage globalInstrumentId=%s documentId=%s stage=PREPARE elapsedMs=%s outcome=SUCCESS", profile.instrument_id, document.document_id, _elapsed_ms(started))
        return await self._apply_prepared_ingested_document_async(
            document,
            document_status=document_status,
            metadata_only_nse_financial_result="FINANCIAL_RESULTS" in source.categories,
        )

    def _ingest_registered_fetch_result(
        self,
        profile: CompanyResearchProfile,
        source: RegisteredResearchSource,
        result,
        *,
        expected_profile: CompanyResearchProfile | None = None,
    ) -> ResearchDocument:
        if result.status_code >= 400:
            raise FetchError(f"Registered source returned {result.status_code}")
        if source.discovery_method == "NSE_OFFICIAL_API":
            logger.info("official_document_fetch provider=NSE globalInstrumentId=%s documentType=%s httpStatus=%s extractionStatus=%s", profile.instrument_id, result.content_type, result.status_code, result.extraction_status)
        return self.ingest_fixture(
            original_url=result.final_url,
            source_type=source.source_type,
            source_classification=source.source_classification,
            source_name=source.source_name,
            publisher=source.publisher,
            content_type=result.content_type,
            body=result.text,
            reliability=source.reliability_level,
            source_mode=SourceMode.REAL,
            discovery_provider=(source.discovery_method if source.discovery_method == "SEARCH_DISCOVERY"
                                or self._has_trusted_nse_profile_identity(profile, source, expected_profile) else None),
            expected_profile=expected_profile,
            document_status=DocumentStatus.FAILED if result.extraction_status != "EXTRACTED" else DocumentStatus.PARSED,
            allow_empty_content=result.extraction_status != "EXTRACTED",
            _trusted_profile_identity=(
                _TRUSTED_NSE_PROFILE_IDENTITY
                if self._has_trusted_nse_profile_identity(profile, source, expected_profile) else None
            ),
            document_subtype=source.document_subtype,
            _metadata_only_nse_financial_result="FINANCIAL_RESULTS" in source.categories,
        )

    @staticmethod
    def _has_trusted_nse_profile_identity(
        profile: CompanyResearchProfile,
        source: RegisteredResearchSource,
        expected_profile: CompanyResearchProfile | None,
    ) -> bool:
        """Allow NSE API discovery, not generic retrieval, to bind a filing's identity."""
        return (
            expected_profile is profile
            and source.instrument_id == profile.instrument_id
            and source.company_id == profile.company_id
            and source.source_classification == SourceClassification.EXCHANGE
            and source.discovery_method == "NSE_OFFICIAL_API"
            and source.official_nse_profile_symbol is not None
            and source.official_nse_profile_symbol == profile.provider_instrument_ids.get("NSE")
        )

    async def _fetch_etf_source(self, profile: EtfResearchProfile, source: RegisteredResearchSource) -> ResearchDocument:
        self._validate_registered_source(profile, source)
        result = await self._fetcher.fetch(source.url)
        if result.status_code >= 400:
            raise FetchError(f"Registered source returned {result.status_code}")
        return self.ingest_etf_fixture(
            profile,
            original_url=result.final_url,
            source_type=source.source_type,
            source_classification=source.source_classification,
            source_name=source.source_name,
            publisher=source.publisher,
            content_type=result.content_type,
            body=result.text,
            reliability=source.reliability_level,
            discovery_provider=source.discovery_method if source.discovery_method == "SEARCH_DISCOVERY" else None,
        )

    def ingest_etf_fixture(
        self,
        profile: EtfResearchProfile,
        *,
        original_url: str,
        source_type: SourceType,
        source_name: str,
        publisher: str,
        content_type: str,
        body: str,
        reliability: ReliabilityLevel,
        source_classification: SourceClassification = SourceClassification.OTHER,
        discovery_provider: str | None = None,
    ) -> ResearchDocument:
        canonical = canonicalize_url(original_url)
        title, extracted = extract_text(body, content_type)
        normalized = normalize_text(extracted or "")
        if not normalized:
            raise FetchError("CONTENT_EMPTY")
        if len(normalized) < 40:
            raise FetchError("CONTENT_TOO_SHORT")
        if not _etf_document_relevant(profile, title, normalized):
            raise FetchError("COMPANY_RELEVANCE_FAILED")
        document = ResearchDocument(
            canonical_url=canonical,
            original_url=original_url,
            title=title,
            source_type=source_type,
            source_classification=source_classification,
            source_name=source_name,
            publisher=publisher,
            published_at=extract_published_at(normalized),
            content_type=content_type,
            document_type=DocumentType.PDF_REFERENCE if canonical.lower().endswith(".pdf") else DocumentType.HTML,
            raw_text=body if len(body) < 20_000 else None,
            normalized_text=normalized,
            content_hash=content_hash(normalized or canonical),
            instrument_id=profile.instrument_id,
            company_id=profile.fund_id,
            status=DocumentStatus.PARSED,
            reliability_level=reliability,
            entity_resolution_confidence=1.0,
            source_mode=SourceMode.REAL,
            freshness=SourceMode.REAL.value,
            discovery_provider=discovery_provider,
            source_independence_key=content_hash(normalized or canonical),
        )
        duplicate = self._deduplicator.add(document)
        if duplicate:
            document.status = DocumentStatus.DUPLICATE
            document.duplicate_of_document_id = duplicate.document_id
            return document
        self.documents[document.document_id] = document
        self._persistence.upsert_document(document)
        self._update_etf_facts(profile, document)
        self.last_refresh[profile.instrument_id] = datetime.now(timezone.utc)
        return document

    def _validate_registered_source(self, profile: CompanyResearchProfile | EtfResearchProfile, source: RegisteredResearchSource) -> None:
        if not source.allowed:
            raise FetchError("SOURCE_QUALITY_REJECTED")
        if source.instrument_id != profile.instrument_id:
            raise FetchError("COMPANY_RELEVANCE_FAILED")
        host = (urlparse(source.url).hostname or "").lower()
        if source.host and host != source.host:
            raise FetchError("DOMAIN_VALIDATION_FAILED")
        first_party = source.source_classification == SourceClassification.OFFICIAL_COMPANY
        authoritative_public = source.source_classification in {SourceClassification.EXCHANGE, SourceClassification.REGULATORY}
        known_company_domain = any(host == domain.lower() or host.endswith(f".{domain.lower()}") for domain in profile.known_domains)
        if (first_party or (source.priority <= 2 and not authoritative_public)) and not known_company_domain:
            raise FetchError("DOMAIN_VALIDATION_FAILED")

    def _update_etf_facts(self, profile: EtfResearchProfile, document: ResearchDocument) -> None:
        text = document.normalized_text or ""
        source = ProvenancedValue(value="", source_url=document.canonical_url, source_name=document.source_name, retrieved_at=document.retrieved_at)
        inferred_provider = profile.fund_provider or _infer_fund_provider(profile.fund_name, text)
        inferred_index = profile.underlying_index or _infer_underlying_index(profile.fund_name, text)
        if inferred_provider:
            profile.fund_provider = inferred_provider
            profile.facts["fundProvider"] = source.model_copy(update={"value": inferred_provider})
        if inferred_index:
            profile.underlying_index = inferred_index
            profile.facts["underlyingIndex"] = source.model_copy(update={"value": inferred_index})
        for key, value in _extract_etf_facts(text).items():
            profile.facts[key] = source.model_copy(update={"value": value})

    def _seed_demo_data(self) -> None:
        fixtures = [
            (
                "https://ir.aixtron.example/releases/order-capacity?utm_source=test",
                "AIXTRON SE announced a new order worth €350 million from a leading power electronics customer. The XETR AIXA order supports silicon-carbide equipment demand and increases backlog by +42%.",
                SourceType.INVESTOR_RELATIONS,
                SourceClassification.OFFICIAL_COMPANY,
                ReliabilityLevel.LEVEL_B,
            ),
            (
                "https://exchange.example/xams/besi-capacity",
                "BE Semiconductor Industries BESI XAMS announced capacity expansion of 73.15 MW equivalent production capability and a new facility in the Netherlands. CAPEX is €120 million and the project is under construction.",
                SourceType.EXCHANGE_ANNOUNCEMENT,
                SourceClassification.EXCHANGE,
                ReliabilityLevel.LEVEL_A,
            ),
            (
                "https://nse.example/reliance-filing",
                "RELIANCE XNSE INE002A01018 disclosed an investment of ₹2,000 crore in new energy manufacturing capacity in India. Management maintained revenue guidance.",
                SourceType.REGULATORY_FILING,
                SourceClassification.REGULATORY,
                ReliabilityLevel.LEVEL_A,
            ),
            (
                "https://news.example/nvda-delay",
                "NVIDIA Corporation NVDA XNAS reported that a factory ramp was delayed by one quarter while demand remains strong.",
                SourceType.NEWS,
                SourceClassification.REPUTABLE_NEWS,
                ReliabilityLevel.LEVEL_C,
            ),
        ]
        for url, body, source_type, source_classification, reliability in fixtures:
            self.ingest_fixture(
                original_url=url,
                source_type=source_type,
                source_classification=source_classification,
                source_name="DEMO fixture source",
                publisher="DEMO",
                content_type="text/html",
                body=f"<html><head><title>DEMO research fixture</title></head><body>{body}</body></html>",
                reliability=reliability,
                published_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
                source_mode=SourceMode.DEMO,
            )

    def _load_persisted_research(self) -> None:
        for document in self._persistence.load_documents():
            if document.document_id in self.documents:
                continue
            self.documents[document.document_id] = document
            self._deduplicator.add(document)
        for event in self._persistence.load_events():
            if event.event_id in self.events:
                continue
            self.events[event.event_id] = event
            self._event_keys.add(_event_key(event))
            self.last_refresh[event.instrument_id] = max(
                self.last_refresh.get(event.instrument_id, event.detected_at),
                event.detected_at,
            )
        for snapshot in self._persistence.load_shareholding_snapshots():
            self.shareholding_snapshots.setdefault(snapshot.id, snapshot)


def _demo_profiles() -> list[CompanyResearchProfile]:
    return [
        CompanyResearchProfile(
            instrument_id=UUID("11111111-1111-1111-1111-111111111111"),
            company_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"),
            company_name="AIXTRON SE",
            aliases=["AIXTRON", "AIXA"],
            isin="DE000A0WMPJ6",
            ticker="AIXA",
            exchange="XETR",
            mic="XETR",
            country="DE",
            currency="EUR",
            known_domains=["aixtron.example", "aixtron.com"],
            official_website="https://www.aixtron.com/en",
            investor_relations_url="https://www.aixtron.com/en/investors",
            press_release_url="https://www.aixtron.com/en/press/press-releases",
        ),
        CompanyResearchProfile(
            instrument_id=UUID("22222222-2222-2222-2222-222222222222"),
            company_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2"),
            company_name="BE Semiconductor Industries",
            aliases=["BESI", "Besi", "BE Semiconductor", "BE Semiconductor Industries N.V."],
            isin="NL0012866412",
            ticker="BESI",
            exchange="XAMS",
            mic="XAMS",
            country="NL",
            currency="EUR",
            known_domains=["besi.example", "besi.com"],
            official_website="https://www.besi.com/",
            investor_relations_url="https://www.besi.com/investor-relations/",
            press_release_url="https://www.besi.com/investor-relations/press-releases/",
        ),
        CompanyResearchProfile(
            instrument_id=UUID("33333333-3333-3333-3333-333333333333"),
            company_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa3"),
            company_name="NVIDIA Corporation",
            aliases=["NVIDIA", "NVDA"],
            isin="US67066G1040",
            ticker="NVDA",
            exchange="XNAS",
            mic="XNAS",
            country="US",
            currency="USD",
            known_domains=["nvidia.example"],
        ),
        CompanyResearchProfile(
            instrument_id=UUID("44444444-4444-4444-4444-444444444444"),
            company_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa4"),
            company_name="Reliance Industries Limited",
            aliases=["RELIANCE", "Reliance Industries", "RIL"],
            isin="INE002A01018",
            ticker="RELIANCE",
            exchange="XNSE",
            mic="XNSE",
            country="IN",
            currency="INR",
            known_domains=["ril.example", "ril.com"],
            official_website="https://www.ril.com/",
            investor_relations_url="https://www.ril.com/investors/investor-relations",
        ),
    ]


def _event_key(event: ResearchEvent) -> tuple[UUID, ResearchEventType, str, str | None, str | None]:
    return (
        event.instrument_id,
        event.event_type,
        content_hash(event.raw_evidence_reference),
        event.monetary_original,
        event.customer,
    )


def _search_provider_from_settings(settings: Settings) -> SearchDiscoveryProvider:
    if not settings.research_search_enabled:
        return DisabledSearchDiscoveryProvider()
    if settings.research_search_provider == "google-compatible":
        missing = []
        if not settings.research_search_endpoint:
            missing.append("endpoint")
        if not settings.research_search_api_key:
            missing.append("api_key")
        if not settings.research_search_engine_id:
            missing.append("engine_id")
        if missing:
            raise SearchProviderConfigurationError(f"SEARCH_PROVIDER_NOT_CONFIGURED:{','.join(missing)}")
        return GoogleCompatibleSearchDiscoveryProvider(
            settings.research_search_endpoint,
            settings.research_search_api_key,
            settings.research_search_engine_id,
            max_results_per_query=settings.research_search_max_results_per_query,
        )
    if settings.research_search_provider == "brave-compatible":
        missing = []
        if not settings.research_search_endpoint:
            missing.append("endpoint")
        if not settings.research_search_api_key:
            missing.append("api_key")
        if missing:
            raise SearchProviderConfigurationError(f"SEARCH_PROVIDER_NOT_CONFIGURED:{','.join(missing)}")
        return BraveCompatibleSearchDiscoveryProvider(
            settings.research_search_endpoint,
            settings.research_search_api_key,
            max_results_per_query=settings.research_search_max_results_per_query,
        )
    if settings.research_search_provider == "searxng":
        if not settings.research_search_endpoint:
            raise SearchProviderConfigurationError("SEARCH_PROVIDER_NOT_CONFIGURED:endpoint")
        return SearxngSearchDiscoveryProvider(
            settings.research_search_endpoint,
            max_results_per_query=settings.research_search_max_results_per_query,
        )
    raise SearchProviderConfigurationError(f"SEARCH_PROVIDER_NOT_CONFIGURED:unsupported_provider:{settings.research_search_provider}")


def _profile_source_discovery(
    profile: CompanyResearchProfile,
    missing_categories: set[str],
    seen_urls: set[str],
) -> list[DiscoveryResult]:
    if not missing_categories:
        return []
    source_urls = [
        ("official-website", profile.official_website, SourceType.COMPANY_WEBSITE),
        ("investor-relations", profile.investor_relations_url, SourceType.INVESTOR_RELATIONS),
        ("press-releases", profile.press_release_url, SourceType.INVESTOR_RELATIONS),
        ("annual-reports", profile.annual_reports_url, SourceType.INVESTOR_RELATIONS),
        ("exchange-announcements", profile.exchange_announcements_url, SourceType.EXCHANGE_ANNOUNCEMENT),
        ("regulatory-filings", profile.regulatory_filings_url, SourceType.REGULATORY_FILING),
    ]
    results: list[DiscoveryResult] = []
    for source_key, url, source_type in source_urls:
        if url is None:
            continue
        canonical = canonicalize_url(str(url))
        if canonical in seen_urls:
            continue
        host = (urlparse(canonical).hostname or "").lower()
        classification = SourceClassification.OFFICIAL_COMPANY
        reliability = ReliabilityLevel.LEVEL_B
        if source_type == SourceType.EXCHANGE_ANNOUNCEMENT:
            classification = SourceClassification.EXCHANGE
            reliability = ReliabilityLevel.LEVEL_A
        elif source_type == SourceType.REGULATORY_FILING:
            classification = SourceClassification.REGULATORY
            reliability = ReliabilityLevel.LEVEL_A
        source = RegisteredResearchSource(
            source_id=f"profile:{profile.instrument_id}:{source_key}",
            instrument_id=profile.instrument_id,
            url=canonical,
            source_type=source_type,
            source_classification=classification,
            source_name=f"{profile.company_name} {source_key.replace('-', ' ')}",
            publisher=profile.company_name,
            reliability_level=reliability,
            domain=host,
            company_id=profile.company_id,
            allowed=True,
            discovery_method="PROFILE_OFFICIAL",
            priority=2,
            categories=tuple(sorted(missing_categories)),
        )
        for category in sorted(missing_categories):
            results.append(DiscoveryResult(category=category, source=source))
    return results


def _fetch_rejection_reason(exc: FetchError) -> str:
    message = str(exc)
    if message in {
        "CONTENT_EMPTY",
        "CONTENT_TOO_SHORT",
        "COMPANY_RELEVANCE_FAILED",
        "DOCUMENT_PERSIST_FAILED",
        "DOMAIN_VALIDATION_FAILED",
        "SOURCE_QUALITY_REJECTED",
    }:
        return message
    if "Unsupported content type" in message:
        return "PARSER_FAILED"
    if "not approved" in message or "not permitted" in message:
        return "DOMAIN_VALIDATION_FAILED"
    if "HTTP status" in message or "returned" in message or "timed out" in message:
        return "HTTP_FETCH_FAILED"
    return "PARSER_FAILED"


def _is_transport_fetch_failure(exc: FetchError) -> bool:
    """Only transport failures trip the short-lived per-host official budget."""
    return isinstance(exc, TransportFetchError)


def _is_quarter_end(value: datetime) -> bool:
    return (value.month, value.day) in {(3, 31), (6, 30), (9, 30), (12, 31)}


def _next_quarter_window(period_end: datetime) -> datetime:
    """First day of the next reporting quarter, in UTC, without instrument rules."""
    month = period_end.month + 3
    year = period_end.year + (1 if month > 12 else 0)
    month = month - 12 if month > 12 else month
    return datetime(year, month, 1, tzinfo=timezone.utc)


def _next_annual_window(period_end: datetime) -> datetime:
    return datetime(period_end.year + 1, period_end.month, 1, tzinfo=timezone.utc)


def _canonical_financial_period_end(period: str | None) -> str | None:
    if not period:
        return None
    if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", period):
        try:
            return datetime.fromisoformat(period).date().isoformat()
        except ValueError:
            return None
    match = re.fullmatch(r"Q([1-4])\s*FY\s*(\d{2,4})", period.strip(), re.I)
    if not match:
        return None
    financial_year = int(match.group(2))
    if financial_year < 100:
        financial_year += 2000
    month, day = {"1": (6, 30), "2": (9, 30), "3": (12, 31), "4": (3, 31)}[match.group(1)]
    year = financial_year - 1 if match.group(1) != "4" else financial_year
    return datetime(year, month, day).date().isoformat()


def _parse_iso_datetime(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _explicit_quarter_end(text: str) -> datetime | None:
    """Use an explicitly stated reporting end date; never round an arbitrary date."""
    match = re.search(
        r"(?:quarter|three\s+months)\s+ended\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    for pattern in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"):
        try:
            parsed = datetime.strptime(match.group(1), pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        return parsed if _is_quarter_end(parsed) else None
    return None


def _eligible_for_nse_shareholding_reconciliation(profile: CompanyResearchProfile) -> bool:
    return (
        profile.country.upper() in {"IN", "IND", "INDIA"}
        and profile.exchange.upper() in {"NSE", "XNSE"}
        and bool(profile.provider_instrument_ids.get("NSE"))
    )


def _shareholding_xbrl_enrichment_needed(snapshots: list[ShareholdingSnapshot]) -> bool:
    """Detect legacy structured filings without treating optional categories as required.

    Successful XBRL parsing persists at least one value with a stable
    ``nse-xbrl:`` locator.  That durable provenance marker, rather than a
    complete category set, prevents repeat fetches when an official filing
    legitimately omits an optional ownership class.
    """
    return any(
        snapshot.source_provider.upper() == "NSE"
        and snapshot.source_type == "NSE_SHAREHOLDING_XBRL"
        and not any((value.source_locator or "").startswith("nse-xbrl:") for value in snapshot.values)
        for snapshot in snapshots
    )


def _checked_categories(categories: set[str], shareholding_check_succeeded: bool) -> set[str]:
    """A provider outage must not advance the shareholding check schedule."""
    if shareholding_check_succeeded or "SHAREHOLDING_PATTERN" not in categories:
        return categories
    return categories - {"SHAREHOLDING_PATTERN"}


def _fair_official_filing_order(filings: list[DiscoveryResult]) -> list[DiscoveryResult]:
    """Interleave categories while preserving discovery order within each one."""
    buckets: dict[str, list[DiscoveryResult]] = {}
    category_order: list[str] = []
    for filing in filings:
        if filing.category not in buckets:
            buckets[filing.category] = []
            category_order.append(filing.category)
        buckets[filing.category].append(filing)
    ordered: list[DiscoveryResult] = []
    offsets = {category: 0 for category in category_order}
    while True:
        emitted = False
        for category in category_order:
            index = offsets[category]
            bucket = buckets[category]
            if index >= len(bucket):
                continue
            ordered.append(bucket[index])
            offsets[category] = index + 1
            emitted = True
        if not emitted:
            return ordered


def _is_usable_durable_document(document: ResearchDocument) -> bool:
    """A durable official result suitable for reuse and category evidence."""
    if document.source_mode != SourceMode.REAL:
        return False
    if document.status == DocumentStatus.PROCESSED:
        return True
    # Historical rows can legitimately be PARSED.  Require persisted extracted
    # text so an empty/scanned/incomplete row remains retryable.
    return document.status == DocumentStatus.PARSED and bool((document.normalized_text or "").strip())


def _canonical_refresh_category(value: str) -> str:
    normalized = re.sub(r"[_&]+", " ", str(value or "").strip()).upper()
    normalized = re.sub(r"\s+", " ", normalized)
    return _REFRESH_CATEGORY_ALIASES.get(normalized, normalized)


def _refresh_evidence_keys(category: str) -> tuple[str, ...]:
    return {
        "GROWTH": ("Growth", "GROWTH"),
        "CLIENTS": ("CLIENTS", "Customers", "New Customers"),
        "ORDERS_BACKLOG": ("ORDERS_BACKLOG", "Orders & Backlog", "New Orders"),
        "CAPEX": ("CAPEX", "CAPEX & Capacity"),
        "GUIDANCE": ("GUIDANCE", "Guidance"),
        "INSTITUTIONAL_ACTIVITY": ("INSTITUTIONAL_ACTIVITY", "Ownership"),
        "REGULATORY": ("REGULATORY", "Regulatory"),
        "MANAGEMENT": ("MANAGEMENT", "Management"),
    }.get(category, (category,))


def _search_discovery_categories(categories: set[str]) -> set[str]:
    """Translate canonical scheduling keys to the existing search vocabulary."""
    display = {
        "GROWTH": "Growth",
        "CLIENTS": "Customers",
        "ORDERS_BACKLOG": "Orders & Backlog",
        "CAPEX": "CAPEX & Capacity",
        "GUIDANCE": "Guidance",
        "INSTITUTIONAL_ACTIVITY": "Ownership",
        "REGULATORY": "Regulatory",
        "MANAGEMENT": "Management",
    }
    return {display.get(_canonical_refresh_category(category), _canonical_refresh_category(category)) for category in categories}


def _safe_url_path(url: str) -> str:
    try:
        return urlparse(url).path or "/"
    except ValueError:
        return "/"


def _is_nse_official_document_url(url: str) -> bool:
    """Persisted-document repair may only reuse a known NSE archive host."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host == "nseindia.com" or host.endswith(".nseindia.com")


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _etf_document_relevant(profile: EtfResearchProfile, title: str | None, text: str) -> bool:
    haystack = f"{title or ''} {text}".lower()
    name_tokens = [token for token in re.findall(r"[a-z0-9]+", profile.fund_name.lower()) if len(token) >= 4]
    ticker_match = profile.ticker and re.search(rf"(?<![a-z0-9]){re.escape(profile.ticker.lower())}(?![a-z0-9])", haystack)
    index = profile.underlying_index or _infer_underlying_index(profile.fund_name, text)
    index_match = bool(index and index.lower() in haystack)
    token_matches = sum(1 for token in set(name_tokens) if token in haystack)
    fund_context = any(term in haystack for term in ["etf", "fund", "ucits", "factsheet", "holdings", "expense ratio", "aum"])
    return fund_context and (token_matches >= 2 or bool(ticker_match) or index_match)


def _infer_fund_provider(name: str, text: str) -> str | None:
    haystack = f"{name} {text}".lower()
    if "ishares" in haystack or "blackrock" in haystack:
        return "iShares"
    if "vanguard" in haystack:
        return "Vanguard"
    if "xtrackers" in haystack:
        return "Xtrackers"
    return None


def _infer_underlying_index(name: str, text: str = "") -> str | None:
    haystack = f"{name} {text}".upper()
    if "S&P 500" in haystack or "SP 500" in haystack:
        return "S&P 500"
    if "NASDAQ 100" in haystack or "NASDAQ-100" in haystack:
        return "NASDAQ 100"
    return None


def _extract_etf_facts(text: str) -> dict[str, object]:
    facts: dict[str, object] = {}
    lower = text.lower()
    expense = re.search(r"(?:expense ratio|ter|total expense ratio)\D{0,30}(\d+(?:\.\d+)?)\s?%", lower)
    if expense:
        facts["expenseRatio"] = f"{expense.group(1)}%"
    holdings = re.search(r"(?:holdings count|number of holdings|holdings)\D{0,30}(\d{2,5})", lower)
    if holdings:
        facts["holdingsCount"] = int(holdings.group(1))
    dividend = re.search(r"(?:dividend yield|distribution yield)\D{0,30}(\d+(?:\.\d+)?)\s?%", lower)
    if dividend:
        facts["dividendYield"] = f"{dividend.group(1)}%"
    if "accumulating" in lower or "acc" in lower:
        facts["distributionPolicy"] = "Accumulating"
    elif "distributing" in lower or "dist" in lower:
        facts["distributionPolicy"] = "Distributing"
    aum = re.search(r"(?:aum|assets under management|fund size)\D{0,30}((?:EUR|USD|GBP)?\s?\d+(?:\.\d+)?\s?(?:bn|billion|mn|million))", text, re.IGNORECASE)
    if aum:
        facts["AUM"] = aum.group(1).strip()
    nav = re.search(r"\bNAV\D{0,20}((?:EUR|USD|GBP)?\s?\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if nav:
        facts["NAV"] = nav.group(1).strip()
    if any(term in lower for term in ["apple", "microsoft", "nvidia", "amazon", "meta"]):
        top = [name for name in ["Apple", "Microsoft", "NVIDIA", "Amazon", "Meta"] if name.lower() in lower]
        facts["topHoldings"] = top[:10]
    return facts


def _evidence_source(document: ResearchDocument) -> ResearchEvidenceSource:
    return ResearchEvidenceSource(
        publisher=document.publisher,
        url=document.canonical_url,
        source_type=document.source_classification,
        published_at=document.published_at,
        retrieved_at=document.retrieved_at,
        reliability=document.reliability_level,
        source_mode=document.source_mode,
        document_id=document.document_id,
        source_name=document.source_name,
        canonical_url=document.canonical_url,
        independent=document.duplicate_of_document_id is None,
    )
