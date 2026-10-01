from __future__ import annotations

import asyncio
import hashlib
import json
import re
import logging
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, quote, urlparse
from uuid import UUID

from app.deduplication import DocumentDeduplicator
from app.financial_projection import nse_authority_rejection
from app.document_cache import BoundedDocumentCache, DocumentRef, _ref as _document_ref
from app.event_store import BoundedEventStore
from app.failure_taxonomy import TECHNICAL_RETRYABLE, classify_reason as classify_failure_reason
from app.entity_resolution import EntityResolver
from app.events import company_updated, document_event, research_event_extracted
from app.extraction import RuleBasedEventExtractor
from app.models import (
    CompanyResearchProfile,
    DocumentSubtype,
    DocumentStatus,
    DocumentType,
    EntityResolution,
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
    DailyMarketBar,
)
from app.normalization import canonicalize_url, content_hash, detect_document_type, extract_published_at, extract_text, normalize_text
from app import cycle_timing
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
from app.structured_research import _BALANCE_SHEET_HEADING, _CASH_FLOW_HEADING, financial_result_history_from_facts, financial_statement_history_from_facts, latest_quarterly_result_from_facts, latest_quarterly_result, parsed_nse_balance_sheet_periods, parsed_nse_cash_flow_periods, parsed_nse_income_statement_periods
from app.fact_precedence import FinancialFact, FinancialFactKey, FactSourceTier, newer_official_disclosure
from app.financial_authority import authoritative_financial_upgrade_required

logger = logging.getLogger(__name__)
_TRUSTED_NSE_PROFILE_IDENTITY = object()

# Bump when the financial parser/projection contract changes. Receipts certify
# unchanged parsing work only; they never satisfy a readiness requirement.
FINANCIAL_PARSER_VERSION = "2"


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


@dataclass
class _OfficialFilingBatchState:
    """Shared, mutable accounting for one _fetch_official_filings() batch.

    Every read/write of these fields happens while the batch's
    ``dispatch_lock`` is held (see _fetch_official_filings), so this is
    exactly as race-safe as the single local-variable set the original
    fully-serial loop used -- concurrency is only ever introduced around the
    network fetch itself, never around this state.
    """
    cursor: int = 0
    attempted: int = 0
    completed_without_failure: bool = True
    stop: bool = False
    host_transport_failures: dict = field(default_factory=dict)


_SHORT_TTL = _CategoryRefreshStrategy("SHORT_TTL", timedelta(minutes=5))
_DAILY_LIGHTWEIGHT = _CategoryRefreshStrategy("DAILY_LIGHTWEIGHT", timedelta(days=1))
_PERIODIC_SLOW = _CategoryRefreshStrategy("PERIODIC_SLOW", timedelta(days=7))
_QUARTERLY_WINDOW = _CategoryRefreshStrategy("QUARTERLY_WINDOW", timedelta(days=3))
_ANNUAL_WINDOW = _CategoryRefreshStrategy("ANNUAL_WINDOW", timedelta(days=14))

_CATEGORY_STRATEGIES: dict[str, _CategoryRefreshStrategy] = {
    # Each category has exactly one strategy (no duplicate-key shadowing).
    #
    # Announcement / catalyst categories carry time-sensitive, frequent
    # corporate disclosures; their no-change lightweight check cadence is one
    # day, matching the canonical announcement block (CONTRACTS,
    # ACQUISITIONS, GUIDANCE, MANAGEMENT, REGULATORY, CATALYSTS). A prior
    # second dict block re-declared CAPEX, NEW_FACILITIES, CLIENTS and
    # ORDERS_BACKLOG as _PERIODIC_SLOW (7d), which shadowed this daily cadence
    # and is removed here. (No audit/test asserts the prior 7-day shadow for
    # these categories.)
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
    # Financial-cycle / structural categories checked on a longer cadence.
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


_PLATFORM_EVENT_HISTORY = 1024


def _authority_rank(fact: FinancialFact) -> int:
    """Deterministic tie-break rank: higher OFFICIAL tier wins."""
    try:
        return int(fact.source_tier.value)
    except (AttributeError, ValueError):
        return 0


def _pick_better(existing: FinancialFact | None, candidate: FinancialFact | None) -> FinancialFact | None:
    if existing is None:
        return candidate
    if candidate is None:
        return existing
    if _authority_rank(candidate) > _authority_rank(existing):
        return candidate
    if _authority_rank(candidate) < _authority_rank(existing):
        return existing
    if candidate.key.period_type == "ANNUAL" and existing.key.period_type != "ANNUAL":
        return candidate
    return existing


def _to_decimal(value):
    from decimal import Decimal, InvalidOperation
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _period_datetime(period_end, fallback):
    candidate = None
    if period_end:
        try:
            candidate = datetime.fromisoformat(str(period_end).replace("Z", "+00:00"))
        except ValueError:
            candidate = None
    if candidate is None:
        candidate = fallback
    if candidate is None:
        return None
    # Research domain convention (see persistence._parse_dt): datetimes are
    # UTC-aware.  Production PostgreSQL / DATE-only period_end values hydrate
    # as naive (e.g. "2026-03-31"); attach UTC at this durable read boundary
    # -- never strip tz from an already-aware timestamp.  This guarantees
    # derived ROE/ROCE facts carry a tz-aware as_of_date so Stage-2
    # (ResearchEvidence._require_aware) does not raise
    # "as_of must be timezone-aware".
    if candidate.tzinfo is None or candidate.utcoffset() is None:
        return candidate.replace(tzinfo=timezone.utc)
    return candidate.astimezone(timezone.utc)


def _ratio_fact(instrument_id: UUID, metric: str, numerator: FinancialFact, denominator, basis_desc: str, now) -> FinancialFact | None:
    """Build a derived PERCENT ratio fact (ROE/ROCE); never persisted directly."""
    from decimal import Decimal, InvalidOperation
    num = _to_decimal(numerator.value.value)
    if num is None or denominator <= 0:
        return None
    try:
        value = num / denominator * Decimal("100")
    except (InvalidOperation, ZeroDivisionError, TypeError):
        return None
    period_end = numerator.key.period_end
    as_of = _period_datetime(period_end, numerator.value.as_of_date or now)
    return FinancialFact(
        FinancialFactKey(instrument_id, metric, period_end, numerator.key.period_type, numerator.key.reporting_basis),
        ProvenancedValue(
            value=value, unit="PERCENT", as_of_date=as_of,
            period=f"derived:{metric}",
            source_url=numerator.value.source_url,
            source_name=numerator.value.source_name or "NSE",
            source_type="DERIVED",
            published_at=None,
            retrieved_at=now,
            confidence=Decimal("0.9"),
            calculation_basis=f"{metric.upper()}={basis_desc}",
        ),
        FactSourceTier.OFFICIAL_NSE, "NSE",
        f"{numerator.source_identity}:derived:{metric}", SourceMode.REAL,
    )


def _derive_ratio_facts(instrument_id: UUID, facts) -> list[FinancialFact]:
    """Read-only, matching-period derivation of official ratio facts.

    ROE  = PAT / Equity                          * 100
    ROCE = EBIT / (TotalAssets - CurrentLiabilities) * 100

    Emitted only when numerator and denominator facts share the same
    period_end and reporting_basis (matching-period) and the denominator is
    strictly positive.  Derived facts are NEVER persisted: they are appended at
    the durable read boundary (``financial_facts_for`` /
    ``financial_facts_for_instruments``) so readiness and rule-engine scoring
    observe an OFFICIAL_NSE (authority 4) ratio that outranks a Yahoo summary
    ratio (authority 2).  ROCE suppression for financial issuers lives one layer
    up, in the rule engine (``stock_rule_engine._is_financial``); financial
    applicability is reconciled between ``research_applicability`` and the rule
    engine, so neither special-cases IRFC.
    """
    from app.models import SourceMode
    if not facts:
        return []
    groups: dict[tuple, dict[str, FinancialFact]] = {}
    for fact in facts:
        if fact.source_mode != SourceMode.REAL or not fact.key.period_end:
            continue
        bucket = groups.setdefault((fact.key.period_end, fact.key.reporting_basis), {})
        bucket[fact.key.metric] = _pick_better(bucket.get(fact.key.metric), fact)

    now = datetime.now(timezone.utc)
    derived: list[FinancialFact] = []
    for (period_end, basis), bucket in groups.items():
        pat = bucket.get("pat")
        equity = bucket.get("total_equity") or bucket.get("equity")
        if pat is not None and equity is not None:
            eq = _to_decimal(equity.value.value)
            if eq is not None:
                roe = _ratio_fact(instrument_id, "roe", pat, eq, "PAT/Equity", now)
                if roe is not None:
                    derived.append(roe)

        ebit = bucket.get("ebit") or bucket.get("operating_profit") or bucket.get("operating_income")
        assets = bucket.get("total_assets")
        liab = bucket.get("current_liabilities")
        if ebit is not None and assets is not None and liab is not None:
            cap_employed = _to_decimal(assets.value.value)
            cur_liab = _to_decimal(liab.value.value)
            if cap_employed is not None and cur_liab is not None:
                roce = _ratio_fact(instrument_id, "roce", ebit, cap_employed - cur_liab,
                                   "EBIT/(TotalAssets-CurrentLiabilities)", now)
                if roce is not None:
                    derived.append(roce)
    return derived


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
        # Full bodies are bounded (count + bytes); compact refs are kept for
        # enumeration. Durable research_documents is the source of truth --
        # see app/document_cache.py for the retention contract.
        self.documents = BoundedDocumentCache(
            max_documents=self.settings.research_document_cache_max_documents,
            max_bytes=self.settings.research_document_cache_max_bytes,
        )
        # Bounded per-instrument event retention; durable research_events is the
        # source of truth (see app/event_store.py).
        self.events = BoundedEventStore(
            lambda instrument_id: self._persistence.load_events({instrument_id}),
            _event_key, durable=self._documents_are_durable,
            max_instruments=self.settings.research_event_cache_max_instruments)
        self.shareholding_snapshots: dict[UUID, ShareholdingSnapshot] = {}
        # Never read by production code; bounded so it cannot grow with the
        # number of documents/events processed over the process lifetime.
        self.platform_events: deque[PlatformEvent] = deque(maxlen=_PLATFORM_EVENT_HISTORY)
        self.last_refresh: dict[UUID, datetime] = {}
        self.last_live_error: dict[UUID, str] = {}
        self._category_refresh: dict[tuple[UUID, str], datetime] = {}
        # A successful provider response with no qualifying evidence is not
        # freshness. Keep its check cadence separate from durable evidence so
        # normal refreshes do not rediscover the same missing category.
        self._category_successful_no_change_checks: dict[tuple[UUID, str], datetime] = {}
        self._financial_authority_attempts: dict[UUID, datetime] = {}
        self._deduplicator = DocumentDeduplicator()
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
            announcements_url=self.settings.nse_announcements_url,
            governance_categories=self.settings.research_nse_governance_categories,
        )
        self._official_shareholding_discovery = official_shareholding_discovery or OfficialNseShareholdingDiscovery(
            shareholdings_url=self.settings.nse_shareholdings_url
        )
        # This is deliberately process-local.  Persistence remains the durable
        # cross-process deduplication boundary.
        self._official_filing_flights: dict[tuple[UUID, str], asyncio.Task[ResearchDocument]] = {}
        # Guardian Review (Issue 3, STAGE2_LIVE_RUN_DEFECTS_20260930.md):
        # repository-instance-level (survives across separate
        # _fetch_official_filings() batch calls for the same instrument, e.g.
        # one call per capability group), keyed by (instrument_id, host),
        # value = monotonic time.monotonic() timestamp until which that host
        # is in cooldown for that instrument. A fresh per-batch
        # _OfficialFilingBatchState.host_transport_failures counter is still
        # used WITHIN one batch (unchanged); this map is the ACROSS-batches
        # memory that counter never had.
        self._official_host_cooldowns: dict[tuple[UUID, str], float] = {}
        self._instrument_refresh_flights: dict[UUID, asyncio.Task[ResearchSummary]] = {}
        # The production persistence adapter owns one synchronous database
        # connection.  Worker operations are serialized per repository so two
        # refreshes do not interleave transactions on that connection.
        self._persistence_worker_lock = threading.RLock()
        self._news_worker_lock = asyncio.Lock()
        # Bounded per-repository cache of market-session metadata (trading
        # schedules + calendar exceptions) keyed by the market set. Schedules
        # are refreshed by a separate, dedicated schedule-refresh flow (never by
        # the readiness acquisition path), so within a single investigate flow
        # the same market set is requested on every read(). Caching collapses
        # those repeated locked loads. The cache is invalidated conservatively:
        # any persistence *write* (upsert_/record_/insert_/complete_/start_/_persist/_reconcile/_apply/_create_)
        # clears it, so schedule data is never served stale across a mutation
        # boundary. Read operations never clear it. See
        # `_invalidate_market_session_cache` + the write-prefix check in
        # `_run_blocking_persistence`.
        self._market_session_cache: dict[frozenset[str], tuple] = {}
        # Monotonic counter bumped on every persistence *write*. The readiness
        # runtime reads this to decide whether its cached evidence-only
        # ResearchReadinessResult is still durable-current (i.e. no mutation has
        # persisted new evidence/observations since the snapshot was taken).
        # This is a bounded, process-local staleness sentinel -- it does NOT
        # replace durable reads; it only lets an evidence_only read skip a
        # reload when the caller already holds an equivalent snapshot and no
        # intervening persistence mutation occurred.
        self._readiness_mutation_generation: int = 0
        self._seed_demo_data()
        self._load_persisted_research()

    def news_records_for(self, instrument_id, model, *, as_of):
        loader = getattr(self._persistence, 'load_news_records', None)
        return loader(model, instrument_id, as_of=as_of) if callable(loader) else []

    async def append_news_record(self, record):
        return await self._run_blocking_persistence(self._persistence.append_news_record, record)

    async def refresh_news_intelligence(self, instrument_id, *, industry=None):
        from app.news_acquisition import acquire_news
        async with self._news_worker_lock:
            return await acquire_news(self,self.profile(instrument_id),providers=[self._search_discovery.provider],industry=industry,
                max_queries=min(20,max(1,self.settings.research_search_max_queries_per_category)),
                max_documents=min(20,max(1,self.settings.research_search_max_documents_per_refresh)))

    def list_profiles(self) -> list[CompanyResearchProfile]:
        return self.profiles

    @property
    def persistence(self):
        return self._persistence

    def list_etf_profiles(self) -> list[EtfResearchProfile]:
        return self.etf_profiles

    def financial_facts_for(self, instrument_id: UUID):
        persisted = list(self._persistence.load_financial_facts({instrument_id}))
        persisted.extend(_derive_ratio_facts(instrument_id, persisted))
        return persisted

    async def financial_facts_for_instruments(self, instrument_ids: set[UUID]) -> dict[UUID, list[FinancialFact]]:
        started = time.perf_counter()
        facts = await self._run_blocking_persistence(self._persistence.load_financial_facts, instrument_ids)
        grouped: dict[UUID, list[FinancialFact]] = {instrument_id: [] for instrument_id in instrument_ids}
        for fact in facts:
            if fact.key.instrument_id in grouped:
                grouped[fact.key.instrument_id].append(fact)
        for instrument_id, instrument_facts in grouped.items():
            instrument_facts.extend(_derive_ratio_facts(instrument_id, instrument_facts))
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

    def daily_market_bars_for(self, instrument_ids: set[UUID], *, start_date: date | None = None,
                             end_date: date | None = None, provider: str | None = None) -> dict[UUID, list[DailyMarketBar]]:
        grouped = {instrument_id: [] for instrument_id in instrument_ids}
        for bar in self._persistence.load_daily_market_bars(
                instrument_ids, start_date=start_date, end_date=end_date, provider=provider):
            grouped[bar.global_instrument_id].append(bar)
        return grouped

    async def daily_market_bars_for_instruments(self, instrument_ids: set[UUID], *, start_date: date | None = None,
                                               end_date: date | None = None, provider: str | None = None) -> dict[UUID, list[DailyMarketBar]]:
        return await self._run_blocking_persistence(self.daily_market_bars_for, instrument_ids,
            start_date=start_date, end_date=end_date, provider=provider)

    async def upsert_daily_market_bar_async(self, bar: DailyMarketBar) -> None:
        await self._run_blocking_persistence(self._persistence.upsert_daily_market_bar, bar)

    async def upsert_daily_market_bars_async(self, bars: list[DailyMarketBar]) -> int:
        return await self._run_blocking_persistence(self._persistence.upsert_daily_market_bars, bars)

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
        key = frozenset(markets)
        cached = self._market_session_cache.get(key)
        if cached is not None:
            return cached
        schedules, exceptions = await asyncio.gather(
            self._run_blocking_persistence(self._persistence.load_market_schedules, markets),
            self._run_blocking_persistence(self._persistence.load_market_calendar_exceptions, markets),
        )
        result = schedules, exceptions
        # Only cache complete (non-empty) schedule data; an empty/partial load
        # is still authoritative for "no schedules persisted yet", so we cache
        # it too to avoid re-running the locked loads within the same flow.
        self._market_session_cache[key] = result
        return result

    def _invalidate_market_session_cache(self) -> None:
        """Clear the market-session metadata cache.

        Called from the persistence chokepoint on any write operation so stale
        schedule data is never served across a mutation boundary. Safe to call
        when the cache is empty (no-op).
        """
        self._market_session_cache.clear()

    _PERSISTENCE_WRITE_PREFIXES = (
        "upsert_",
        "record_",
        "insert_",
        "complete_",
        "start_",
        "create_",
        "_persist",
        "_reconcile",
        "_apply",
        "_store",
    )

    def _is_persistence_write(self, operation) -> bool:
        """Return True if ``operation`` is a persistence write that may mutate
        durable state and therefore must invalidate derived caches.

        Uses the operation's method ``__name__`` (e.g. ``upsert_acquisition_observation``)
        or, for repository-level wrapper methods, ``__qualname__`` to classify.
        Read-only loads (``load_*``, ``profile``, ``summary``, ``financial_facts_for``,
        ``acquisition_observations_for``, ``news_records_for``,
        ``market_session_data``, ``assess`` and similar) are not writes, so they
        never clear the market-session cache. This is a conservative classification:
        clearing on a write that does not touch schedule tables is safe (just a
        cache miss on the next read); never clearing on a schedule-relevant write
        would be the unsafe case, which this guard prevents.
        """
        qn = getattr(operation, "__qualname__", None) or ""
        name = getattr(operation, "__name__", "") or ""
        # Bound persistence-adapter methods expose the bare method name (e.g.
        # "upsert_acquisition_observation"); repository-level wrappers expose a
        # qualname like "ResearchRepository.record_acquisition_observation".
        # Check the bare method name first (most precise), then the qualname.
        label = name or qn
        return any(label.startswith(prefix) for prefix in self._PERSISTENCE_WRITE_PREFIXES)

    async def _run_blocking_persistence(self, operation, *args, **kwargs):
        """Keep production database work out of the request event loop.

        The in-memory SQLite adapter is deliberately single-thread-affine and
        is used only by the local/test persistence configuration.  Production
        uses the psycopg adapter, whose synchronous operations are safe to run
        in a worker thread.  Keeping this compatibility branch here avoids
        moving repository-owned state or changing the SQLite adapter contract.

        Any persistence *write* invalidates the bounded market-session cache so
        stale schedule data is never served across a mutation boundary. Read
        operations are unaffected.
        """
        if self._is_persistence_write(operation):
            self._invalidate_market_session_cache()
            self._readiness_mutation_generation += 1
        if isinstance(self._persistence, SqliteResearchPersistence) and self._persistence.__class__.__module__ != "app.postgres_persistence":
            return operation(*args, **kwargs)
        dispatched_at = time.monotonic()
        return await asyncio.to_thread(self._run_serialized_persistence_operation, operation, args, kwargs, dispatched_at)

    def _run_serialized_persistence_operation(self, operation, args, kwargs, dispatched_at=None):
        with self._persistence_worker_lock:
            op_name = (getattr(operation, "__qualname__", None)
                       or getattr(operation, "__name__", None)
                       or type(operation).__name__)
            if dispatched_at is not None:
                # Wait = time from dispatch (event loop handing this off to
                # asyncio.to_thread) to actually holding the serialization
                # lock -- covers both thread-pool scheduling delay and any
                # time another persistence operation held the lock first.
                wait_ms = (time.monotonic() - dispatched_at) * 1000
                cycle_timing.record_persistence_wait(wait_ms)
                # Guardian Review Slice 7 (measure first, no blind pool
                # change): count this dispatched call by its operation name,
                # so the cycle report can identify the highest-frequency
                # persistence calls without any architecture change.
                cycle_timing.record_persistence_operation(op_name)
            op_started = time.monotonic()
            try:
                return operation(*args, **kwargs)
            finally:
                if dispatched_at is not None:
                    exec_ms = (time.monotonic() - op_started) * 1000
                    cycle_timing.record_persistence_elapsed(exec_ms)
                    # Guardian Review Slice 7 (measure-first): per-operation-
                    # NAME timing (wait + exec), so a flat persistence_wait/
                    # persistence_elapsed aggregate can be attributed to a
                    # specific load_*/upsert_*/commit method. Observation-only:
                    # no behavior/decision change.
                    cycle_timing.record_persistence_operation_timing(op_name, wait_ms, exec_ms)

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
        refs = self.documents.refs_for_instrument(instrument_id, source_mode)
        resident: dict[UUID, ResearchDocument] = {}
        missing: list[UUID] = []
        for ref in refs:
            document = self.documents.get(ref.document_id)
            if document is None:
                missing.append(ref.document_id)
            else:
                resident[ref.document_id] = document
        if missing:
            # Evicted bodies: durable storage is the source of truth. One
            # batched read, then re-cache as durable (evictable) entries.
            for document in self._load_documents_by_ids(missing):
                self.documents.reloads += 1
                resident[document.document_id] = document
                self.documents.put(document, durable=True)
        docs = [resident[ref.document_id] for ref in refs if ref.document_id in resident]
        if source_mode is not None:
            docs = [doc for doc in docs if doc.source_mode == source_mode]
        return sorted(docs, key=lambda doc: doc.published_at or doc.retrieved_at, reverse=True)

    @property
    def _event_keys(self) -> set:
        """Read-only view of resident dedup keys (compatibility)."""
        return self.events.resident_keys()

    def document_count_for(self, instrument_id: UUID, source_mode: SourceMode | None = None) -> int:
        """Compact count from the identity index -- never loads bodies."""
        return len(self.documents.refs_for_instrument(instrument_id, source_mode))

    def document_urls_for(self, instrument_id: UUID, source_mode: SourceMode | None = None) -> set[str]:
        """Canonical URLs already known for an instrument -- never loads bodies."""
        return {ref.canonical_url for ref in self.documents.refs_for_instrument(instrument_id, source_mode)}

    def _load_documents_by_ids(self, document_ids: list[UUID]) -> list[ResearchDocument]:
        loader = getattr(self._persistence, "load_documents_by_ids", None)
        if loader is not None:
            return list(loader(list(document_ids)))
        single = getattr(self._persistence, "load_document", None)
        if single is None:
            return []
        return [document for document in (single(document_id) for document_id in document_ids) if document is not None]

    def remember_persisted_document(self, document: ResearchDocument) -> None:
        """Cache a document its caller has already persisted; the body is
        evictable only when persistence is actually durable."""
        self.documents.put(document, durable=document.source_mode == SourceMode.REAL and self._documents_are_durable())

    def _sync_persisted_document_identities(self, instrument_id: UUID) -> None:
        # Another worker may have committed evidence since this process started.
        # Refresh only this instrument's compact index before acquisition.
        loader = getattr(self._persistence, "load_document_identities_for_instrument", None)
        if callable(loader):
            known = {ref.document_id: ref for ref in self.documents.refs_for_instrument(instrument_id)}
            for identity in loader(instrument_id):
                document_id, owner_id, url, digest, source_mode, sort_key = identity
                prior = known.get(document_id)
                if prior and (prior.canonical_url != url or prior.content_hash != digest):
                    self._deduplicator.forget(prior.canonical_url, prior.content_hash, document_id)
                resident = self.documents.get(document_id)
                if resident is not None and (resident.content_hash != digest
                        or resident.published_at != sort_key and resident.retrieved_at != sort_key
                        or resident.status == DocumentStatus.FAILED or not resident.normalized_text):
                    self.documents.pop(document_id, None)
                    self._deduplicator.forget(resident.canonical_url, resident.content_hash, document_id)
                self.documents.register_ref(DocumentRef(document_id, owner_id, source_mode, url, digest, sort_key))
                self._deduplicator.add_identity(document_id, url, digest)

    def _documents_are_durable(self) -> bool:
        return bool(getattr(self._persistence, "durable_documents", True))

    def _forget_unpersisted_document(self, document: ResearchDocument) -> None:
        """Undo in-memory registration of a document whose persistence
        failed or was cancelled: body, compact ref and dedup identity."""
        self.documents.pop(document.document_id, None)
        self._deduplicator.forget(document.canonical_url, document.content_hash, document.document_id)

    def _adopt_existing_durable_document(self, document: ResearchDocument) -> ResearchDocument:
        """Durable storage already holds this URL/hash (UNIQUE) under another
        id that this process did not know -- e.g. a row committed by a
        persistence thread whose caller was cancelled, or written by another
        replica. Adopt the durable row instead of keeping an orphan id."""
        durable_id = document.duplicate_of_document_id
        self._forget_unpersisted_document(document)
        document.status = DocumentStatus.DUPLICATE
        for durable in self._load_documents_by_ids([durable_id] if durable_id else []):
            self.documents.register_ref(_document_ref(durable))
            self._deduplicator.add_identity(durable.document_id, durable.canonical_url, durable.content_hash)
        return document

    def _mark_document_durable(self, document: ResearchDocument) -> None:
        if document.source_mode == SourceMode.REAL and self._documents_are_durable():
            self.documents.mark_durable(document.document_id)

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
        values = self.events.for_instrument(instrument_id)
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
        from app.research_applicability import shareholding_input_coverage

        def selection_key(snapshot):
            qualifying_official = (
                snapshot.source_provider.upper() == "NSE"
                and snapshot.reliability_level == ReliabilityLevel.LEVEL_A
                and "PROMOTER_INSTITUTIONAL_PUBLIC_CATEGORIES" in shareholding_input_coverage(snapshot)
            )
            official_xbrl = qualifying_official and (
                snapshot.source_type == "NSE_SHAREHOLDING_XBRL"
                or any((value.source_locator or "").startswith("nse-xbrl:") for value in snapshot.values)
            )
            # Choose an intact source snapshot; never relabel/merge another
            # filing's values (including pledge) under the selected provenance.
            return (snapshot.period_end, qualifying_official, official_xbrl,
                    snapshot.published_at or datetime.min.replace(tzinfo=timezone.utc),
                    snapshot.retrieved_at, snapshot.source_identity_key, str(snapshot.id))

        ordered = sorted(
            [snapshot for snapshot in self.shareholding_snapshots.values()
             if snapshot.instrument_id == instrument_id
             and snapshot.source_mode == SourceMode.REAL
             and _is_quarter_end(snapshot.period_end)],
            key=selection_key, reverse=True,
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

    def _latest_acquisition_observation(self, instrument_id, requirement_id, provider):
        rows = [row for row in self.acquisition_observations_for(instrument_id)
                if row.get("requirement_id") == requirement_id and row.get("provider") == provider]
        return rows[-1] if rows else None

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
        _official_title: str | None = None,
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
        if _trusted_profile_identity is _TRUSTED_NSE_PROFILE_IDENTITY and _official_title:
            document.title = _official_title
            document.published_at = published_at
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
        if duplicate and document_status != DocumentStatus.FAILED:
            repaired = await self._run_blocking_persistence(self._persist_official_document_repair, document)
            if repaired:
                self._deduplicator.forget(duplicate.canonical_url, duplicate.content_hash, duplicate.document_id)
                self._deduplicator.add(document)
                self.remember_persisted_document(document)
                return await self._continue_repaired_document_async(document)
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
                stored = await self._run_blocking_persistence(
                    self._persist_ingested_document,
                    document,
                    nse_financial_result=metadata_only_nse_financial_result,
                )
            except BaseException as exc:
                # Includes cancellation (timeouts): never leave a document that
                # is not durably stored pinned in memory or registered as a
                # dedup identity -- a retry must reprocess it, not skip it.
                self._forget_unpersisted_document(document)
                logger.info("document_ingest_stage globalInstrumentId=%s documentId=%s stage=DOCUMENT_PERSIST elapsedMs=%s outcome=%s errorType=%s", document.instrument_id, document.document_id, _elapsed_ms(started), "FAILED" if isinstance(exc, Exception) else "CANCELLED", type(exc).__name__)
                if isinstance(exc, Exception):
                    raise FetchError("DOCUMENT_PERSIST_FAILED") from exc
                raise
            if stored is False:
                logger.info("document_ingest_stage globalInstrumentId=%s documentId=%s stage=DOCUMENT_PERSIST elapsedMs=%s outcome=ALREADY_DURABLE durableDocumentId=%s", document.instrument_id, document.document_id, _elapsed_ms(started), document.duplicate_of_document_id)
                return self._adopt_existing_durable_document(document)
            logger.info("document_ingest_stage globalInstrumentId=%s documentId=%s stage=DOCUMENT_PERSIST elapsedMs=%s outcome=SUCCESS", document.instrument_id, document.document_id, _elapsed_ms(started))
            self._mark_document_durable(document)
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
        if duplicate and document_status != DocumentStatus.FAILED and self._persist_official_document_repair(document):
            self._deduplicator.forget(duplicate.canonical_url, duplicate.content_hash, duplicate.document_id)
            self._deduplicator.add(document)
            self.remember_persisted_document(document)
            self._reconcile_persisted_official_financial_document(document)
            return self._continue_ingested_document_after_persistence(document, document_status=DocumentStatus.PROCESSED, financial_processed=True)
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
                stored = self._persist_ingested_document(
                    document,
                    nse_financial_result=metadata_only_nse_financial_result,
                )
            except BaseException as exc:
                self._forget_unpersisted_document(document)
                if isinstance(exc, Exception):
                    raise FetchError("DOCUMENT_PERSIST_FAILED") from exc
                raise
            if stored is False:
                return self._adopt_existing_durable_document(document)
            self._mark_document_durable(document)
        return self._continue_ingested_document_after_persistence(document, document_status=document_status)

    def _continue_ingested_document_after_persistence(self, document: ResearchDocument, *, document_status: DocumentStatus, financial_processed: bool = False, event_candidates: list[ResearchEvent] | None = None) -> ResearchDocument:
        if (not financial_processed and document.instrument_id and document.source_mode == SourceMode.REAL and document_status != DocumentStatus.FAILED
                and document.source_classification in {SourceClassification.EXCHANGE, SourceClassification.REGULATORY, SourceClassification.OFFICIAL_COMPANY}):
            self._persist_official_financial_facts(document)
        self.platform_events.append(document_event("research.document.processed", document))
        for event in (event_candidates if event_candidates is not None else self._extract_ingested_event_candidates(document, document_status=document_status)):
            self._apply_ingested_event(event, source_mode=document.source_mode)
        return self._continue_ingested_document_after_events(document)

    def _persist_official_document_repair(self, document: ResearchDocument) -> bool:
        repair = getattr(self._persistence, "repair_official_document", None)
        return bool(callable(repair) and repair(document))

    async def _continue_repaired_document_async(self, document: ResearchDocument) -> ResearchDocument:
        await self._run_blocking_persistence(self._reconcile_persisted_official_financial_document, document)
        candidates = await asyncio.to_thread(self._extract_ingested_event_candidates, document, document_status=DocumentStatus.PROCESSED)
        self.platform_events.append(document_event("research.document.processed", document))
        for event in candidates:
            await self._apply_ingested_event_async(event, source_mode=document.source_mode)
        return await self._continue_ingested_document_after_events_async(document)

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
        if not self.events.add(event, durable=source_mode == SourceMode.REAL and self._documents_are_durable()):
            return
        if source_mode == SourceMode.REAL:
            self._persist_ingested_event(event)
        self.platform_events.append(research_event_extracted(event))

    async def _apply_ingested_event_async(self, event: ResearchEvent, *, source_mode: SourceMode) -> None:
        if not self.events.add(event, durable=source_mode == SourceMode.REAL and self._documents_are_durable()):
            return
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
        persisted = self._durable_nse_financial_document(
            document,
            nse_financial_result=nse_financial_result,
        )
        stored = self._persistence.upsert_document(persisted)
        if stored is False and persisted is not document:
            document.duplicate_of_document_id = persisted.duplicate_of_document_id
        return stored

    def _durable_nse_financial_document(
        self,
        document: ResearchDocument,
        *,
        nse_financial_result: bool = False,
    ) -> ResearchDocument:
        """Retain bounded normalized parser input, never raw PDF content.

        Dropping normalized text makes a persisted document impossible to
        reconcile after restart or a parser correction. The existing fetch
        size limit bounds this input; PDF bytes remain transient.
        """
        if (
            document.content_type != "application/pdf"
            or document.discovery_provider != "NSE_OFFICIAL_API"
            or document.source_classification != SourceClassification.EXCHANGE
            or not _is_nse_official_document_url(document.canonical_url)
        ):
            return document
        has_quarterly_facts = nse_financial_result or any(
            fact.key.period_type == "QUARTERLY"
            for fact in self._official_financial_fact_candidates(document)
        )
        if not nse_financial_result and not has_quarterly_facts:
            return document
        logger.info(
            "official_document_persist provider=NSE globalInstrumentId=%s documentId=%s "
            "storage=URL_METADATA_NORMALIZED_TEXT_AND_FACTS",
            document.instrument_id,
            document.document_id,
        )
        return document.model_copy(update={"raw_text": None, "pdf_structure": None})

    def _prepare_ingested_document(self, *, original_url: str, source_type: SourceType, source_name: str,
                                   publisher: str, content_type: str, body: str, reliability: ReliabilityLevel,
                                   published_at: datetime | None, source_mode: SourceMode,
                                   source_classification: SourceClassification, discovered_at: datetime | None,
                                   discovery_provider: str | None, expected_profile: CompanyResearchProfile | None,
                                   document_status: DocumentStatus, allow_empty_content: bool,
                                   trusted_profile_identity: object | None,
                                   document_subtype: DocumentSubtype | None = None,
                                   precomputed_pdf_structure: PdfTextStructure | None = None) -> ResearchDocument:
        canonical = canonicalize_url(original_url)
        title, extracted = extract_text(body, content_type)
        normalized = normalize_text(extracted or "")
        # Transient structural sidecar only; the durable flattened-text contract is unchanged.
        # When a caller already built this from the identical extracted text
        # (e.g. FetchResult.pdf_structure, computed once in
        # research_fetching.py from the very same string that becomes
        # ``body`` here), reuse it BY IDENTITY instead of rebuilding the
        # complete PdfTextStructure a second time -- preserve_pdf_structure is
        # a pure function of that text, so this changes nothing about the
        # result, only avoids recomputing it. Non-PDF content_type is
        # unaffected either way: pdf_structure stays None regardless of what
        # a caller passes.
        if content_type == "application/pdf":
            if precomputed_pdf_structure is not None:
                pdf_structure = precomputed_pdf_structure
            else:
                from app.pdf_structure import preserve_pdf_structure
                pdf_structure = preserve_pdf_structure(body)
        else:
            pdf_structure = None
        if source_mode == SourceMode.REAL and not allow_empty_content:
            if not normalized:
                raise FetchError("CONTENT_EMPTY")
            if len(normalized) < 40:
                raise FetchError("CONTENT_TOO_SHORT")
        parsed_published_at = published_at or extract_published_at(normalized)
        if expected_profile is not None and trusted_profile_identity is _TRUSTED_NSE_PROFILE_IDENTITY:
            # The caller already verified exact NSE symbol/instrument/company
            # ownership. A universe-wide text scan was discarded by this same
            # binding anyway; avoid doing it for every official filing.
            resolution = EntityResolution(instrument_id=expected_profile.instrument_id,
                                          company_id=expected_profile.company_id,
                                          confidence=0.99, matched_on=[])
        else:
            resolution = self._resolver.resolve(title, normalized, canonical)
            if expected_profile is not None and not allow_empty_content:
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
            pdf_structure=pdf_structure,
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
        """Persist only source-qualified, validated semantic NSE projections."""
        if not document.instrument_id:
            return False, 0
        receipt = self._financial_document_parse_unchanged(document)
        if receipt is not None:
            return receipt == "PARSED", 0
        facts = self._official_financial_fact_candidates(document)
        if not facts:
            self._record_financial_document_parse(document, "UNSUPPORTED")
            return False, 0
        existing = {fact.key: fact for fact in self._persistence.load_financial_facts({document.instrument_id})}
        written = 0
        explicit_revision = self._is_explicit_financial_revision(document)
        for fact in facts:
            prior = existing.get(fact.key)
            same_document_correction = (
                prior is not None
                and prior.source_tier == FactSourceTier.OFFICIAL_NSE
                and prior.source_identity == str(document.document_id)
            )
            revised = explicit_revision and newer_official_disclosure(prior, fact)
            if self._persistence.upsert_financial_fact(fact, allow_same_tier_correction=same_document_correction or revised):
                written += 1
        self._record_financial_document_parse(document, "PARSED", facts)
        return True, written

    def _reconcile_persisted_official_financial_document(self, document: ResearchDocument) -> tuple[bool, int]:
        """Atomically reconcile a known trusted document without retrieval.

        Source-owned corrections and explicitly published newer revisions may
        replace comparable facts. Obsolete-key removal stays source-owned.
        """
        receipt = self._financial_document_parse_unchanged(document)
        if receipt is not None:
            return receipt == "PARSED", 0
        from app.financial_projection import project_semantic_financial_facts
        from app.structured_research import official_financial_sector_ratio_facts
        projection = project_semantic_financial_facts(document)
        facts = list(projection.facts)
        # Mirror _official_financial_fact_candidates: persist financial-sector
        # regulatory ratios (capital adequacy / gross NPA / net NPA) extracted
        # from the same official document text so the reconcile path keeps the
        # durable fact set consistent with the upsert path.
        facts.extend(official_financial_sector_ratio_facts(document))
        if not facts:
            self._record_financial_document_parse(document, "UNSUPPORTED")
            return False, 0
        existing = {fact.key: fact for fact in self._persistence.load_financial_facts({document.instrument_id})}
        owned = {key for key, fact in existing.items() if fact.source_identity == str(document.document_id)}
        if owned == {fact.key for fact in facts} and all(
            _same_persisted_financial_fact(existing[fact.key], fact) for fact in facts
        ):
            self._record_financial_document_parse(document, "PARSED", facts)
            return True, 0
        written = self._persistence.reconcile_financial_facts_for_source(
            document.instrument_id,
            str(document.document_id),
            facts,
            complete_scopes=projection.scopes,
            **({"allow_official_revision": True} if self._is_explicit_financial_revision(document) else {}),
        )
        self._record_financial_document_parse(document, "PARSED", facts)
        return True, written

    def _financial_document_parse_identity(self, document: ResearchDocument, keys=()) -> str:
        expected = {tuple(key) for key in keys}
        # Include projected keys owned by other sources too. If that source's
        # comparable fact disappears, this document must be able to repair it.
        owned = [fact for fact in self._persistence.load_financial_facts({document.instrument_id})
                 if fact.source_identity == str(document.document_id)
                 or (fact.key.metric, fact.key.period_end, fact.key.period_type, fact.key.reporting_basis) in expected]
        payload = [document.model_dump(mode="json", exclude={"raw_text", "pdf_structure", "publisher", "language", "country", "exchange", "duplicate_of_document_id"}),
                   sorted([[str(fact.key), int(fact.source_tier), fact.source_provider, fact.source_identity, str(fact.source_mode),
                            fact.value.model_dump(mode="json")] for fact in owned], key=str)]
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return f"urn:research:financial-parse:{FINANCIAL_PARSER_VERSION}:{digest}"

    def _financial_document_parse_unchanged(self, document: ResearchDocument) -> str | None:
        if not document.instrument_id or nse_authority_rejection(document) or not document.normalized_text:
            return None
        receipt = self._latest_acquisition_observation(document.instrument_id, f"FINANCIAL_PARSE:{document.document_id}", "NSE")
        if receipt and receipt.get("outcome") in {"PARSED", "UNSUPPORTED"}:
            locator = receipt.get("source_url") or ""
            try:
                keys = json.loads(parse_qs(urlparse(locator).query)["keys"][0])
                if not isinstance(keys, list) or any(not isinstance(key, list) or len(key) != 4 for key in keys):
                    return None
                identity = self._financial_document_parse_identity(document, keys)
            except (ValueError, KeyError, TypeError):
                return None
            if locator.partition("?")[0] == identity:
                return receipt["outcome"]
        return None

    def _record_financial_document_parse(self, document: ResearchDocument, outcome: str, facts=()) -> None:
        writer = getattr(self._persistence, "upsert_acquisition_observation", None)
        if (not callable(writer) or nse_authority_rejection(document)
                or not (document.normalized_text or "").strip()):
            return
        keys = sorted([[fact.key.metric, fact.key.period_end, fact.key.period_type, fact.key.reporting_basis] for fact in facts], key=str)
        identity = self._financial_document_parse_identity(document, keys)
        writer(document.instrument_id, f"FINANCIAL_PARSE:{document.document_id}", "NSE", outcome,
               datetime.now(timezone.utc), identity + "?keys=" + quote(json.dumps(keys, separators=(",", ":"))),
               "PARSER_FAILED:NO_SUPPORTED_FINANCIAL_FACTS" if outcome == "UNSUPPORTED" else None)

    def _is_explicit_financial_revision(self, document: ResearchDocument) -> bool:
        if (not document.instrument_id or document.source_mode != SourceMode.REAL
                or document.source_type != SourceType.EXCHANGE_ANNOUNCEMENT
                or document.source_classification != SourceClassification.EXCHANGE
                or not _is_nse_official_document_url(document.canonical_url)):
            return False
        title = (document.title or "").lower()
        if re.search(r"\b(?:no|not)\s+(?:revised|restated|corrected)\b", title):
            return False
        if not re.search(r"\b(?:revised|restated|corrected)\s+(?:(?:annual|quarterly|consolidated|standalone|audited|unaudited)\s+)*(?:financial results|financial statements|results)\b", title):
            return False
        try:
            profile = self.profile(document.instrument_id)
        except KeyError:
            return False
        return (document.company_id == profile.company_id and bool(profile.provider_instrument_ids.get("NSE"))
            and profile.country.upper() in {"IN", "IND", "INDIA"} and profile.exchange.upper() in {"NSE", "XNSE"})

    @staticmethod
    def _official_financial_fact_candidates(document: ResearchDocument, *, parsed_periods=None) -> list[FinancialFact]:
        # Legacy parsed_periods is deliberately not a trust/projection bypass.
        # Retain the keyword for callers performing rolling-window discovery.
        from app.financial_projection import project_semantic_financial_facts
        from app.structured_research import official_financial_sector_ratio_facts
        _facts = list(project_semantic_financial_facts(document).facts)
        # Surface official financial-sector regulatory ratios (capital adequacy,
        # gross NPA, net NPA) so the StockRuleEngine financial-branch balance
        # sheet scorer sees them as durable FinancialFact rows -- the ratio
        # metrics are extracted from the same official document text the
        # statement-table parser consumes, so they share its authority tier.
        _facts.extend(official_financial_sector_ratio_facts(document))
        return _facts

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
        authority_upgrade_categories: set[str] | None = None,
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
                authority_upgrade_categories=authority_upgrade_categories,
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
        authority_upgrade_categories: set[str] | None = None,
    ) -> ResearchSummary:
        profile = self.profile(instrument_id)
        run = await self._run_blocking_persistence(
            self._persistence.start_refresh_run,
            instrument_id=profile.instrument_id,
            company_id=profile.company_id,
            correlation_id=correlation_id,
            mode="TARGETED_LIVE" if self.settings.research_live_enabled else "TARGETED_DEMO",
        )
        documents_before = self.document_count_for(instrument_id, source_mode=SourceMode.REAL)
        events_before = len(self.events_for(instrument_id, source_mode=SourceMode.REAL))
        try:
            if self.settings.research_live_enabled:
                # Deep investigation runs under a per-requirement acquisition
                # budget (set by deep_investigation.investigate); when present,
                # the targeted categories are re-acquired as a bounded repair
                # that bypasses ONLY the successful-no-change cooldown (see
                # _instrument_refresh_gate/targeted_repair), never fresh
                # evidence and never unrelated categories. Without an active
                # budget this remains the ordinary public/compat path and keeps
                # force=True. targeted_repair is only forwarded when active so
                # existing strict _refresh_live test doubles stay compatible.
                from app.deep_investigation import acquisition_budget
                _targeted_repair = acquisition_budget(instrument_id) is not None
                _live_kwargs: dict = dict(
                    force=not _targeted_repair,
                    requested_categories=categories,
                )
                if _targeted_repair:
                    _live_kwargs["targeted_repair"] = True
                if authority_upgrade_categories:
                    _live_kwargs["authority_upgrade_categories"] = authority_upgrade_categories
                await self._refresh_live(instrument_id, set(), **_live_kwargs)
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
        documents_after = self.document_count_for(instrument_id, source_mode=SourceMode.REAL)
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
        documents_before = self.document_count_for(instrument_id, source_mode=SourceMode.REAL)
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
        documents_after = self.document_count_for(instrument_id, source_mode=SourceMode.REAL)
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
        documents_before = self.document_count_for(instrument_id, source_mode=SourceMode.REAL)
        if self.settings.research_live_enabled and self.settings.research_search_enabled:
            await self._refresh_etf_search_discovery(profile)
        self.last_refresh[instrument_id] = datetime.now(timezone.utc)
        documents_after = self.document_count_for(instrument_id, source_mode=SourceMode.REAL)
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
        targeted_repair: bool = False,
        requested_categories: set[str] | None = None,
        authority_upgrade_categories: set[str] | None = None,
    ) -> None:
        from app.deep_investigation import acquisition_budget
        budget = acquisition_budget(instrument_id)
        sources = registered_sources_for(instrument_id) if budget is None else ()
        profile = self.profile(instrument_id)
        await self._run_blocking_persistence(self._sync_persisted_document_identities, instrument_id)
        if budget is not None and await budget.sufficient():
            budget.stopped = True
            return
        if requested_categories is None or "FINANCIAL_RESULTS" in requested_categories:
            await self._reconcile_incomplete_persisted_official_financial_facts(profile)
        now = datetime.now(timezone.utc)
        gate = self._instrument_refresh_gate(
            profile, pre_resolved_categories, now, force=force, targeted_repair=targeted_repair,
            requested_categories=requested_categories,
        )
        due_categories = set(gate.missing_categories)
        if requested_categories is not None:
            requested_categories = {
                _canonical_refresh_category(value) for value in requested_categories
            }
            due_categories.intersection_update(requested_categories)
        due_categories.difference_update(authority_upgrade_categories or set())
        logger.info(
            "research_refresh_gate globalInstrumentId=%s outcome=DUE_CATEGORIES dueCategories=%s",
            instrument_id,
            sorted(due_categories),
        )
        authority_upgrade = (
            (requested_categories is None or "FINANCIAL_RESULTS" in requested_categories)
            and await self._run_blocking_persistence(self.financial_authority_upgrade_due, profile, now)
        )
        shareholding_selected = (
            requested_categories is None or "SHAREHOLDING_PATTERN" in requested_categories
        )
        has_shareholding_reconciliation = shareholding_selected and (
            gate.shareholding_backfill_needed or gate.shareholding_category_enrichment_needed
        )
        if not due_categories and not has_shareholding_reconciliation and not authority_upgrade:
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
                if budget is not None and not await budget.allow_document():
                    break
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
            targeted_repair=targeted_repair,
            requested_categories=requested_categories,
            authority_upgrade_categories=authority_upgrade_categories,
        )

    def _instrument_refresh_gate(
        self,
        profile: CompanyResearchProfile,
        pre_resolved_categories: set[str],
        now: datetime,
        *,
        force: bool = False,
        targeted_repair: bool = False,
        requested_categories: set[str] | None = None,
    ) -> _InstrumentRefreshGate:
        structured_categories = {
            "FINANCIAL_RESULTS", "Ownership", "INSTITUTIONAL_ACTIVITY", "SHAREHOLDING_PATTERN", "VALUATION",
            "ORDERS_BACKLOG", "CONTRACTS", "CAPEX", "NEW_FACILITIES", "ACQUISITIONS",
            "CLIENTS", "GUIDANCE", "ANALYST_OPINION", "ANALYST_TARGETS", "Regulatory",
        }
        # The narrow set of categories deep-investigation targeted repair
        # explicitly asked for (e.g. {"FINANCIAL_RESULTS"} for a
        # BUSINESS_QUALITY_FACTS repair). Readiness (requirement-level) has
        # already determined the specific mandatory input backed by this
        # category is still missing/partial/failed before this repair budget
        # was ever opened -- so, for exactly these explicitly-requested
        # categories, a category-level "qualifying evidence exists"/TTL
        # freshness stamp must not by itself suppress a real acquisition
        # attempt (the stamp is blind to which specific fact was extracted).
        # Never applied to a category the caller did not request.
        repair_target_categories: frozenset[str] = frozenset()
        if targeted_repair and requested_categories:
            repair_target_categories = frozenset(
                _canonical_refresh_category(category) for category in requested_categories
            )
        missing = self._missing_categories(
            profile, pre_resolved_categories, now, structured_categories, force=force,
            repair_target_categories=repair_target_categories,
        )
        eligible_missing: set[str] = set()
        for category in missing:
            if force:
                eligible, next_eligible = True, None
            else:
                # targeted_repair is the narrow override used only by deep
                # investigation/repair of an applicable mandatory requirement
                # that is still MISSING/PARTIAL/FAILED. It bypasses the
                # successful-no-change lightweight cooldown (a prior check that
                # found nothing) so a real acquisition attempt can run for the
                # targeted requirement. For genuinely fresh evidence
                # (evidence_at is set), the TTL cooldown is bypassed ONLY for
                # the specific category(ies) explicitly requested by this
                # repair (repair_target_categories) -- never for a category
                # that was not requested. force remains the broad
                # global/backfill escape.
                eligible, next_eligible = self._category_is_eligible_to_check(
                    profile.instrument_id, category, now,
                    bypass_no_change_cooldown=targeted_repair,
                    bypass_evidence_cooldown=category in repair_target_categories,
                )
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
        targeted_repair: bool = False,
        requested_categories: set[str] | None = None,
        authority_upgrade_categories: set[str] | None = None,
    ) -> None:
        from app.deep_investigation import acquisition_budget
        budget = acquisition_budget(profile.instrument_id)
        if budget is not None and await budget.sufficient():
            budget.stopped = True
            return
        now = now or datetime.now(timezone.utc)
        gate = self._instrument_refresh_gate(
            profile, pre_resolved_categories, now, force=force, targeted_repair=targeted_repair,
            requested_categories=requested_categories,
        )
        missing = set(gate.missing_categories)
        if requested_categories is not None:
            requested_categories = {
                _canonical_refresh_category(value) for value in requested_categories
            }
            missing.intersection_update(requested_categories)
        missing.difference_update(authority_upgrade_categories or set())
        if missing & {"CAPEX", "NEW_FACILITIES", "ORDERS_BACKLOG", "CONTRACTS", "GUIDANCE"}:
            missing.difference_update(await self._run_blocking_persistence(self._inapplicable_business_categories, profile))
        due_categories = set(missing)
        authority_upgrade = (
            (requested_categories is None or "FINANCIAL_RESULTS" in requested_categories)
            and await self._run_blocking_persistence(self.financial_authority_upgrade_due, profile, now)
        )
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
        if not missing and not shareholding_backfill_needed and not shareholding_category_enrichment_needed and not authority_upgrade:
            logger.info("official_discovery_gate globalInstrumentId=%s eligible=false reason=NO_MISSING_CATEGORIES", profile.instrument_id)
            return
        # The dedicated ownership feed must get its bounded opportunity before
        # generic announcement PDFs can exhaust the shared capability budget.
        if (
            "SHAREHOLDING_PATTERN" in due_categories
            or shareholding_backfill_needed
            or shareholding_category_enrichment_needed
        ) and _eligible_for_nse_shareholding_reconciliation(profile):
            shareholding_failure = None
            try:
                # NSE publishes quarterly Regulation 31 data through its
                # dedicated shareholding feed, not necessarily as a corporate
                # announcement attachment.  The provider returns only real,
                # source-labelled values and durable persistence is keyed by
                # this global instrument and the official filing identity.
                persisted = 0
                discovered_snapshots = await self._official_shareholding_discovery.discover(profile)
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
                    if budget is not None and not await budget.allow_document():
                        if not self._category_has_qualifying_evidence(profile.instrument_id, "SHAREHOLDING_PATTERN"):
                            raise FetchError("DOCUMENT_BUDGET_EXHAUSTED:NSE_SHAREHOLDING")
                        break
                    enriched_snapshot = await self._enrich_nse_shareholding_snapshot(snapshot)
                    persisted += int(await self._persist_shareholding_snapshot_async(enriched_snapshot))
                shareholding_check_succeeded = True
                successful_check_categories.add("SHAREHOLDING_PATTERN")
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
                shareholding_failure = str(exc)
            except (FetchError, ValueError) as exc:
                shareholding_failure = f"NSE_SHAREHOLDING_UNAVAILABLE:{exc}"
            if shareholding_failure:
                self.last_live_error[profile.instrument_id] = shareholding_failure
                if budget is not None:
                    budget.requirement_failures["SHAREHOLDING"] = shareholding_failure
                if callable(getattr(self._persistence, "upsert_acquisition_observation", None)):
                    await self.record_acquisition_observation(
                        profile.instrument_id, "SHAREHOLDING", "NSE", "FAILED",
                        datetime.now(timezone.utc), failure_reason=shareholding_failure,
                    )
        seen_urls = self.document_urls_for(profile.instrument_id, source_mode=SourceMode.REAL)
        # Resolve authoritative filings before broad research searches can
        # exhaust public-search engines. This is global-instrument research.
        official_due_categories = {"FINANCIAL_RESULTS", "SHAREHOLDING_PATTERN", "CAPEX", "NEW_FACILITIES", "ORDERS_BACKLOG", "CONTRACTS", "GUIDANCE", "RISKS", "REGULATORY", "MANAGEMENT"} & due_categories
        if authority_upgrade:
            official_due_categories.add("FINANCIAL_RESULTS")
        eligible_official = (
            profile.country.upper() in {"IN", "IND", "INDIA"}
            and profile.exchange.upper() in {"NSE", "XNSE"}
            and bool(official_due_categories)
        )
        logger.info("official_discovery_gate globalInstrumentId=%s eligible=%s reason=%s", profile.instrument_id, eligible_official, "NSE_OFFICIAL_CATEGORY" if eligible_official else "PROFILE_OR_CATEGORY_INELIGIBLE")
        if eligible_official:
            financial_attempt = (
                "FINANCIAL_RESULTS" in official_due_categories
                and bool(profile.provider_instrument_ids.get("NSE"))
            )
            discovery_failed = False
            if financial_attempt:
                # Bound unsuccessful and empty authority upgrades too. This
                # does not mark evidence fresh or change fallback readiness.
                self._financial_authority_attempts[profile.instrument_id] = now
            try:
                # Let official discovery return known URLs too: the official fetch
                # loop can then explicitly reuse a durable global document, while
                # failed/scanned historical attempts remain eligible for retry.
                official_filings = await self._official_filing_discovery.discover(profile, official_due_categories, set())
                if budget is not None:
                    # DI-20H.4 accounting: the default is an empty dict, so the
                    # previous `is None` guard never populated it and every
                    # no-failure shortfall was misreported as DISCOVERY_NO_*.
                    budget.discovery_attempted = True
                    for _result in official_filings:
                        _cat = _result.category or "UNKNOWN"
                        budget.discovery_outcome[_cat] = budget.discovery_outcome.get(_cat, 0) + 1
            except Exception as exc:
                discovery_failed = True
                if budget is not None:
                    budget.failures.append(f"SOURCE_UNAVAILABLE:NSE:{type(exc).__name__}")
                official_filings = []
                self.last_live_error[profile.instrument_id] = f"OFFICIAL_FILING_DISCOVERY_UNAVAILABLE:{type(exc).__name__}"
                # OfficialFilingDiscovery emits the terminal provider diagnostic.
                # Keep this boundary diagnostic for injected/legacy implementations.
                logger.warning("official_discovery_handled provider=NSE globalInstrumentId=%s status=FAILED reason=%s", profile.instrument_id, type(exc).__name__)
            fetched = await self._fetch_official_filings(profile, official_filings, seen_urls)
            if fetched and not discovery_failed:
                successful_check_categories.update({"FINANCIAL_RESULTS"} & official_due_categories)
            if financial_attempt and callable(getattr(self._persistence, "upsert_acquisition_observation", None)):
                facts = await self._run_blocking_persistence(
                    self._persistence.load_financial_facts, {profile.instrument_id},
                )
                official_count = sum(fact.source_tier == FactSourceTier.OFFICIAL_NSE
                                     and fact.source_mode == SourceMode.REAL for fact in facts)
                failed = discovery_failed or not fetched
                # Downloaded result text with zero supported facts is an
                # unresolved parser capability, not an authoritative empty
                # result. Reuse persisted text; do not download/parse it again.
                filing_urls = {item.source.url for item in official_filings
                               if item.category == "FINANCIAL_RESULTS"}
                unparsed_results = not official_count and any(
                    document.canonical_url in filing_urls and _is_usable_durable_document(document)
                    for document in self.documents_for(profile.instrument_id, source_mode=SourceMode.REAL)
                )
                if unparsed_results and not failed:
                    failed = True
                    self.last_live_error[profile.instrument_id] = "PARSER_FAILED:NO_SUPPORTED_FINANCIAL_FACTS"
                    successful_check_categories.discard("FINANCIAL_RESULTS")
                    if budget is not None:
                        budget.failures.append("PARSER_FAILED:NO_SUPPORTED_FINANCIAL_FACTS")
                await self.record_acquisition_observation(
                    profile.instrument_id, "QUARTERLY_FINANCIALS", "NSE",
                    "FAILED" if failed else "SUCCESS" if official_filings and official_count else "SUCCESS_EMPTY",
                    datetime.now(timezone.utc), evidence_count=official_count,
                    failure_reason=self.last_live_error.get(profile.instrument_id) if failed else None,
                )
            if profile.provider_instrument_ids.get("NSE") and callable(getattr(self._persistence, "upsert_acquisition_observation", None)):
                from app.research_applicability import event_concept, CONCEPT_CATEGORIES
                from app.extraction import governance_disclosure
                business = set().union(*CONCEPT_CATEGORIES.values())
                for requirement_id, categories in (
                    ("ORDER_BOOK_CAPEX_GUIDANCE", business),
                    ("GOVERNANCE_HISTORY", {"RISKS", "REGULATORY", "MANAGEMENT"}),
                ):
                    selected_categories = official_due_categories & categories
                    if not selected_categories:
                        continue
                    selected_concepts = {key for key, values in CONCEPT_CATEGORIES.items() if values & selected_categories}
                    count = sum(event.source_classification == SourceClassification.EXCHANGE and (
                        event_concept(str(event.event_type)) in selected_concepts if requirement_id == "ORDER_BOOK_CAPEX_GUIDANCE"
                        else governance_disclosure(event.title + " " + event.summary))
                        for event in self.events_for(profile.instrument_id, source_mode=SourceMode.REAL))
                    if requirement_id == "GOVERNANCE_HISTORY":
                        count += sum(document.source_classification == SourceClassification.EXCHANGE
                            and document.status in {DocumentStatus.PARSED, DocumentStatus.PROCESSED}
                            and bool(document.normalized_text)
                            and governance_disclosure((document.title or "") + " " + document.normalized_text)
                            for document in self.documents_for(profile.instrument_id, source_mode=SourceMode.REAL))
                    failed = discovery_failed or not fetched
                    # STEP-8A: a shared group budget (financial + governance +
                    # shareholding/catalyst -- see acquisition_budget()) that
                    # was already exhausted by the time we get here means the
                    # discovery/fetch pass did not necessarily examine every
                    # candidate in scope. Zero found governance evidence under
                    # that condition is genuinely PARTIAL coverage, not a
                    # verified-clean check -- it must never be recorded as
                    # SUCCESS_EMPTY (which downstream readiness treats as an
                    # authoritative "nothing found" signal). Found evidence
                    # (count>0) is unaffected: it is real regardless of
                    # whether the budget later ran out. This does not change
                    # the budget's own size/consumption, only which outcome
                    # label a zero-result governance check is given.
                    partial_budget_exhausted = (
                        requirement_id == "GOVERNANCE_HISTORY" and not failed and not count
                        and budget is not None and (
                            budget.documents_attempted >= budget.max_documents
                            or "DOCUMENT_BUDGET_EXHAUSTED" in budget.failures
                        )
                    )
                    await self.record_acquisition_observation(profile.instrument_id, requirement_id, "NSE",
                        "FAILED" if (failed or partial_budget_exhausted) else "SUCCESS" if count else "SUCCESS_EMPTY",
                        datetime.now(timezone.utc), evidence_count=count,
                        failure_reason=(self.last_live_error.get(profile.instrument_id) if failed
                            else "DOCUMENT_BUDGET_EXHAUSTED_PARTIAL_COVERAGE" if partial_budget_exhausted else None))
        # An authoritative NSE acquisition failure recorded above (official
        # filing discovery or the NSE shareholding feed) must survive every
        # non-authoritative acquisition attempt that follows in this same
        # refresh -- both the profile-known-source loop immediately below
        # and the broader search fallback further down -- whether that
        # later attempt succeeds for an unrelated category or merely fails
        # with a less specific reason.  Search success (or an unrelated
        # failure) must never mask an authoritative NSE failure.
        current_live_error = self.last_live_error.get(profile.instrument_id)
        protect_authoritative_error = (
            current_live_error
            if current_live_error is not None
            and current_live_error.startswith(("OFFICIAL_FILING_", "NSE_SHAREHOLDING_"))
            else None
        )
        if due_categories:
            if budget is not None:
                # Financial and ownership requirements use their authoritative
                # routes above. Generic search must not substitute presentations.
                due_categories.difference_update({"FINANCIAL_RESULTS", "SHAREHOLDING_PATTERN"})
            elif shareholding_check_succeeded:
                # No targeted-repair budget on this call path (e.g. the plain
                # /ensure route calls refresh_targeted_categories directly,
                # never through deep_investigation.investigate() -- so
                # acquisition_budget() is None here even though the dedicated
                # NSE shareholding feed above WAS actually queried this
                # refresh). shareholding_check_succeeded is True whenever
                # that feed call completed without raising -- ZERO_RESULTS
                # and LIGHTWEIGHT_CHECK_UNCHANGED both still mean "NSE was
                # checked", not "never attempted". NSE is authoritative for
                # SHAREHOLDING_PATTERN: once its authoritative state has
                # genuinely been checked this refresh and found
                # unchanged/empty, a generic web search must not run merely
                # to try to manufacture shareholding evidence. A genuine NSE
                # outage (shareholding_failure set, leaving
                # shareholding_check_succeeded False) is NOT covered by this
                # branch, so it still falls through to whatever fallback
                # behavior already applies to a real authoritative failure.
                due_categories.discard("SHAREHOLDING_PATTERN")
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
                    if budget is not None and not await budget.allow_document():
                        break
                    self._validate_registered_source(profile, source)
                    await self._fetch_registered_source(profile, source)
                except (FetchError, RestrictedFetchError, ValueError) as exc:
                    if protect_authoritative_error is None:
                        self.last_live_error[profile.instrument_id] = f"TARGETED_SOURCE_UNAVAILABLE:{source.source_id}:{exc}"
                except Exception:
                    if protect_authoritative_error is None:
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
                refreshed_seen_urls = self.document_urls_for(profile.instrument_id, source_mode=SourceMode.REAL)
                if await self._refresh_search_discovery(
                    profile, _search_discovery_categories(search_categories), refreshed_seen_urls,
                    protect_error=protect_authoritative_error,
                ):
                    successful_check_categories.update(search_categories)
        self._mark_qualifying_categories_fresh(
            profile.instrument_id,
            _checked_categories(missing, shareholding_check_succeeded),
            now,
        )
        self._mark_successful_categories_checked(profile.instrument_id, successful_check_categories, now)

    def financial_authority_upgrade_due(
        self, profile: CompanyResearchProfile, now: datetime, readiness=None,
    ) -> bool:
        # The last NSE authority attempt is durable (research_acquisition_
        # observations), so the cooldown survives restarts instead of being
        # repeated by every new process. A successful/empty check keeps the
        # lightweight interval; a TECHNICAL failure only backs off briefly so a
        # later deep/repair attempt can retry instead of being locked out for
        # the whole window. An attempt still in flight (in-memory only) keeps
        # the conservative interval.
        attempted = self._financial_authority_attempts.get(profile.instrument_id)
        observation = self._latest_acquisition_observation(profile.instrument_id, "QUARTERLY_FINANCIALS", "NSE")
        observed_at = _aware_observation_time(observation)
        interval = _QUARTERLY_WINDOW.lightweight_check_interval
        last = attempted
        if observed_at is not None and (attempted is None or observed_at >= attempted - timedelta(seconds=1)):
            last = observed_at
            if observation.get("outcome") == "FAILED" and classify_failure_reason(
                    observation.get("failure_reason") or "NSE_AUTHORITY_ATTEMPT_FAILED") == TECHNICAL_RETRYABLE:
                interval = timedelta(seconds=self.settings.research_authority_retry_backoff_seconds)
        if last is not None and now < last + interval:
            return False
        return authoritative_financial_upgrade_required(
            profile, self._persistence.load_financial_facts({profile.instrument_id}), readiness,
        )

    async def _incomplete_persisted_official_financial_documents(self, profile: CompanyResearchProfile) -> list[ResearchDocument]:
        if (profile.country.upper() not in {"IN", "IND", "INDIA"} or profile.exchange.upper() not in {"NSE", "XNSE"}
                or not profile.provider_instrument_ids.get("NSE")):
            logger.info("official_financial_fact_reconcile provider=NSE globalInstrumentId=%s outcome=SKIPPED reason=INELIGIBLE_PROFILE", profile.instrument_id)
            return []
        documents = [document for document in self.documents_for(profile.instrument_id, source_mode=SourceMode.REAL)
                     if document.company_id == profile.company_id and document.source_classification == SourceClassification.EXCHANGE
                     and document.source_type == SourceType.EXCHANGE_ANNOUNCEMENT and bool(document.normalized_text)
                     and _is_usable_durable_document(document)
                     and not _official_filing_content_type_unsupported(document.canonical_url)
                     and _is_nse_official_document_url(document.canonical_url)]
        if not documents:
            logger.info("official_financial_fact_reconcile provider=NSE globalInstrumentId=%s outcome=SKIPPED reason=NO_QUALIFYING_PERSISTED_DOCUMENT", profile.instrument_id)
            return []
        checked_identity = await self._run_blocking_persistence(self._financial_reconciliation_identity, profile)
        # A parsed partial/unsupported result is still incomplete evidence, but
        # repeating the same parser cannot fill it. Changed facts, inputs or
        # parser version invalidate the per-document receipt independently.
        documents = await self._run_blocking_persistence(
            lambda: [document for document in documents if self._financial_document_parse_unchanged(document) is None])
        if not documents:
            await self._run_blocking_persistence(self._record_financial_reconciliation, profile, checked_identity)
            return []
        def incomplete_window_documents():
            from app.structured_research import official_financial_sector_ratio_facts
            ratio_facts_by_document = {
                document.document_id: official_financial_sector_ratio_facts(document)
                for document in documents
            }
            # Source freshness and derived-fact completeness are deliberately
            # independent.  Parse only the durable, trusted documents already
            # held for this instrument, then use their explicit periods to
            # define the rolling window.  No calendar-derived target periods
            # are invented here.
            #
            # DI-7C Steps 3/5/6: income-statement completeness keeps its
            # existing fixed {"revenue", "pat"} bar (unchanged).  Balance-sheet
            # (AS_AT) and cash-flow periods have no universal required-field
            # list -- NSE does not always publish every field -- so their
            # "expected" metrics are whatever this same document's own parse
            # actually found for that period.  A document that never mentions
            # a balance sheet at all contributes nothing here and is never
            # required to have one (Step 3's "do not require fields NSE
            # legitimately does not publish").
            parsed_by_document = [
                (
                    document,
                    parsed_nse_income_statement_periods([document]),
                    parsed_nse_balance_sheet_periods([document]),
                    parsed_nse_cash_flow_periods([document]),
                )
                for document in documents
            ]
            parsed_by_document = [
                item for item in parsed_by_document
                if item[1] or item[2] or item[3] or ratio_facts_by_document[item[0].document_id]
            ]
            if not parsed_by_document:
                return None

            available_periods: dict[tuple[str, str | None], set[str]] = {}
            periods_by_document: dict[UUID, set[tuple[str, str | None, str]]] = {}
            # Balance-sheet/cash-flow metrics a period is expected to have,
            # derived from this document's own parse rather than a hard-coded
            # universal set (Step 3).  AS_AT keys never collide with
            # QUARTERLY/ANNUAL income-statement keys (different period_type),
            # but an ANNUAL cash-flow period shares its key with that same
            # period's ANNUAL income-statement entry, so cash-flow metrics
            # found there are unioned onto the existing revenue/pat bar
            # (Step 6: cash flow is only ever expected where the existing
            # parser's own ANNUAL-only semantics already say it applies).
            expected_statement_metrics_by_key: dict[tuple[str, str | None, str], set[str]] = {}
            statement_periods: set[tuple[str, str | None, str]] = set()

            for document, income_periods, balance_periods, cash_periods in parsed_by_document:
                document_periods: set[tuple[str, str | None, str]] = set()
                document_text = document.normalized_text or ""
                for period in income_periods:
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
                for period in (*balance_periods, *cash_periods):
                    metrics = dict(period.metrics)
                    if not metrics:
                        continue
                    group = (period.period_type, period.reporting_basis)
                    available_periods.setdefault(group, set()).add(period.period_end)
                    key = (period.period_type, period.reporting_basis, period.period_end)
                    document_periods.add(key)
                    expected_statement_metrics_by_key.setdefault(key, set()).update(metrics.keys())
                annual_income_periods = [
                    period for period in income_periods
                    if period.period_type == "ANNUAL" and dict(period.metrics)
                ]
                if annual_income_periods and not balance_periods and _BALANCE_SHEET_HEADING.search(document_text):
                    for period in annual_income_periods:
                        key = ("AS_AT", period.reporting_basis, period.period_end)
                        available_periods.setdefault(("AS_AT", period.reporting_basis), set()).add(period.period_end)
                        document_periods.add(key)
                        expected_statement_metrics_by_key.setdefault(key, set()).add(_BALANCE_SHEET_EXTRACTION_PENDING_SENTINEL)
                if annual_income_periods and not cash_periods and _CASH_FLOW_HEADING.search(document_text):
                    for period in annual_income_periods:
                        key = ("ANNUAL", period.reporting_basis, period.period_end)
                        available_periods.setdefault(("ANNUAL", period.reporting_basis), set()).add(period.period_end)
                        document_periods.add(key)
                        expected_statement_metrics_by_key.setdefault(key, set()).add(_CASH_FLOW_EXTRACTION_PENDING_SENTINEL)
                statement_periods.update(document_periods)
                for fact in ratio_facts_by_document[document.document_id]:
                    key = (fact.key.period_type, fact.key.reporting_basis, fact.key.period_end)
                    available_periods.setdefault(key[:2], set()).add(key[2])
                    document_periods.add(key)
                    expected_statement_metrics_by_key.setdefault(key, set()).add(fact.key.metric)
                if document_periods:
                    periods_by_document[document.document_id] = document_periods

            target_periods = {
                (period_type, reporting_basis, period_end)
                for (period_type, reporting_basis), periods in available_periods.items()
                for period_end in sorted(periods, reverse=True)[:4]
            }
            if not target_periods:
                return None

            facts = self._persistence.load_financial_facts({profile.instrument_id})
            present_by_period: dict[tuple[str, str | None, str], set[str]] = {}
            for fact in facts:
                if (
                    fact.key.instrument_id != profile.instrument_id
                    or fact.key.period_type not in {"QUARTERLY", "ANNUAL", "AS_AT"}
                    or fact.source_tier != FactSourceTier.OFFICIAL_NSE
                    or fact.source_mode != SourceMode.REAL
                    or not fact.key.period_end
                ):
                    continue
                key = (fact.key.period_type, fact.key.reporting_basis, fact.key.period_end)
                present_by_period.setdefault(key, set()).add(fact.key.metric)

            def _is_incomplete(key: tuple[str, str | None, str]) -> bool:
                period_type = key[0]
                present = present_by_period.get(key, set())
                if period_type == "AS_AT":
                    expected = expected_statement_metrics_by_key.get(key, set())
                    return not expected or not (expected <= present)
                required = expected_statement_metrics_by_key.get(key, set())
                if key in statement_periods:
                    required = {"revenue", "pat"} | required
                return not (required <= present)

            incomplete = {key for key in target_periods if _is_incomplete(key)}
            existing = {fact.key: fact for fact in facts}
            obsolete_documents = set()
            for document, income, balance, cash in parsed_by_document:
                from app.financial_projection import project_semantic_financial_facts
                projection = project_semantic_financial_facts(document)
                candidates = (*projection.facts, *ratio_facts_by_document[document.document_id])
                explicit_revision = self._is_explicit_financial_revision(document)
                candidate_keys = {candidate.key for candidate in candidates}
                rolling_dates = {key[2] for key in target_periods & periods_by_document.get(document.document_id, set())}
                if any(fact.source_tier == FactSourceTier.OFFICIAL_NSE
                       and fact.source_identity == str(document.document_id)
                       and fact.key.period_end in rolling_dates and fact.key not in candidate_keys
                       and any(scope.contains(fact.key) for scope in projection.scopes)
                       for fact in facts):
                    obsolete_documents.add(document.document_id)
                for candidate in candidates:
                    prior = existing.get(candidate.key)
                    period = (candidate.key.period_type, candidate.key.reporting_basis, candidate.key.period_end)
                    if (period in target_periods and prior is not None
                            and prior.source_tier == FactSourceTier.OFFICIAL_NSE
                            and (prior.source_identity == str(document.document_id)
                                 or (explicit_revision and newer_official_disclosure(prior, candidate)))
                            and (prior.value.value != candidate.value.value or prior.value.unit != candidate.value.unit)):
                        incomplete.add(period)
            if not incomplete and not obsolete_documents:
                return [], target_periods, incomplete
            selected = [
                document for document, *_parsed in parsed_by_document
                if periods_by_document.get(document.document_id, set()) & incomplete
                or document.document_id in obsolete_documents
            ]
            return selected, target_periods, incomplete

        window = await self._run_blocking_persistence(incomplete_window_documents)
        if window is None:
            for document in documents:
                await self._run_blocking_persistence(self._reconcile_persisted_official_financial_document, document)
            await self._run_blocking_persistence(self._record_financial_reconciliation, profile, checked_identity)
            logger.info("official_financial_fact_reconcile provider=NSE globalInstrumentId=%s outcome=SKIPPED reason=NO_PARSEABLE_QUARTERLY_RESULT", profile.instrument_id)
            return []
        selected, target_periods, incomplete = window
        if not selected:
            for document in documents:
                await self._run_blocking_persistence(self._reconcile_persisted_official_financial_document, document)
            await self._run_blocking_persistence(self._record_financial_reconciliation, profile, checked_identity)
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
        if await self._run_blocking_persistence(self._financial_reconciliation_unchanged, profile):
            return False
        documents = await self._incomplete_persisted_official_financial_documents(profile)
        for document in documents:
            logger.info("official_financial_fact_reconcile provider=NSE globalInstrumentId=%s outcome=START documentId=%s", profile.instrument_id, document.document_id)
            parsed, written = await self._run_blocking_persistence(self._reconcile_persisted_official_financial_document, document)
            logger.info("official_financial_fact_reconcile provider=NSE globalInstrumentId=%s outcome=COMPLETE documentId=%s parserResult=%s factsWritten=%s", profile.instrument_id, document.document_id, "PARSED" if parsed else "NO_RESULT", written)
        if documents:
            # Certify only a complete, unchanged post-repair fact set. A failed
            # or partial parse remains eligible for reconciliation/acquisition.
            await self._incomplete_persisted_official_financial_documents(profile)
        return bool(documents)

    def _financial_reconciliation_identity(self, profile: CompanyResearchProfile) -> str | None:
        documents = self.documents_for(profile.instrument_id, source_mode=SourceMode.REAL)
        if not documents:
            return None
        # This receipt certifies an unchanged local reconciliation, NOT evidence
        # freshness or completeness. Include text, provenance and every fact so
        # missing/corrected/contradictory inputs invalidate it. Bump v1 whenever
        # the financial parser/projection contract changes.
        payload = {
            "profile": profile.model_dump(mode="json"),
            "documents": [[document.model_dump(mode="json", exclude={"raw_text", "pdf_structure", "publisher", "language", "country", "exchange"}),
                           content_hash(document.normalized_text or "")]
                          for document in sorted(documents, key=lambda item: str(item.document_id))],
            "facts": sorted([
                [str(fact.key.instrument_id), fact.key.metric, fact.key.period_end,
                 fact.key.period_type, fact.key.reporting_basis, int(fact.source_tier),
                 fact.source_provider, fact.source_identity, str(fact.source_mode),
                 fact.value.model_dump(mode="json")]
                for fact in self._persistence.load_financial_facts({profile.instrument_id})
            ], key=lambda item: json.dumps(item, sort_keys=True)),
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return f"urn:research:financial-reconciliation:{FINANCIAL_PARSER_VERSION}:{digest}"

    def _financial_reconciliation_unchanged(self, profile: CompanyResearchProfile) -> bool:
        observation = self._latest_acquisition_observation(profile.instrument_id, "FINANCIAL_RECONCILIATION", "NSE")
        return bool(observation and observation.get("outcome") == "CHECKED"
                    and observation.get("source_url") == self._financial_reconciliation_identity(profile))

    def _record_financial_reconciliation(self, profile: CompanyResearchProfile, checked_identity: str | None) -> None:
        writer = getattr(self._persistence, "upsert_acquisition_observation", None)
        if not callable(writer):
            return
        identity = self._financial_reconciliation_identity(profile)
        if identity is not None and identity == checked_identity:
            writer(profile.instrument_id, "FINANCIAL_RECONCILIATION", "NSE", "CHECKED",
                   datetime.now(timezone.utc), identity)

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
            from app.research_applicability import shareholding_input_coverage
            if "PROMOTER_INSTITUTIONAL_PUBLIC_CATEGORIES" not in shareholding_input_coverage(snapshot):
                raise FetchError(f"NSE_SHAREHOLDING_XBRL_FAILED:{exc}") from exc
            return snapshot
        values = await asyncio.to_thread(parse_nse_shareholding_xbrl, result.text)
        if not values:
            logger.info("shareholding_xbrl_parse globalInstrumentId=%s outcome=REJECTED reason=NO_SUPPORTED_EXPLICIT_VALUES", snapshot.instrument_id)
            from app.research_applicability import shareholding_input_coverage
            if "PROMOTER_INSTITUTIONAL_PUBLIC_CATEGORIES" not in shareholding_input_coverage(snapshot):
                raise FetchError("PARSER_FAILED:NSE_SHAREHOLDING_XBRL_NO_SUPPORTED_VALUES")
            return snapshot
        logger.info("shareholding_xbrl_parse globalInstrumentId=%s outcome=SUCCESS valueCount=%s", snapshot.instrument_id, len(values))
        return snapshot.model_copy(update={"values": values})

    async def _fetch_official_filings(
        self,
        profile: CompanyResearchProfile,
        filings: list[DiscoveryResult],
        seen_urls: set[str],
    ) -> bool:
        """Fetch a small, newest-first official filing set within an interactive
        budget, overlapping up to ``research_official_document_fetch_concurrency``
        independent network fetches for THIS instrument only.

        Concurrency design (bounded official-filing fetch concurrency):
          - A fixed pool of ``concurrency`` worker coroutines pulls filings, in
            the same newest-first ``_fair_official_filing_order``, from a
            single shared cursor (``_OfficialFilingBatchState.cursor``).
          - ALL decision-making that reads or mutates shared accounting --
            budget checks/increments, reuse lookup + reconciliation,
            content-type skip, the per-refresh attempt-budget check, the
            per-host transport-failure check, and recording a completed
            fetch's outcome back into shared state -- happens while holding
            ``dispatch_lock`` (an ``asyncio.Lock``), so it is exactly as
            serialized -- and therefore exactly as race-safe -- as the
            original one-filing-at-a-time loop it replaces. Nothing here
            runs on a separate OS thread; asyncio's single event loop only
            switches coroutines at an ``await``, so every block between two
            ``await`` points inside the lock still runs to completion
            without interleaving.
          - ONLY the actual network fetch + PDF extraction
            (``_single_flight_official_filing``, which already owns its own
            per-URL single-flight de-duplication) runs OUTSIDE the lock,
            which is what lets up to ``concurrency`` of them overlap.
          - Because the shared cursor is only ever advanced under the lock,
            no filing is ever handed to two workers, and dispatch order
            (newest-first, core-category-first) is identical to the serial
            loop; only completion order can now vary.
          - With ``concurrency == 1`` there is exactly one worker, so this
            degenerates to the original fully-serial behavior.
        """
        from app.deep_investigation import acquisition_budget
        budget = acquisition_budget(profile.instrument_id)
        scheduled_filings = _fair_official_filing_order(filings)
        await self._run_blocking_persistence(self._sync_persisted_document_identities, profile.instrument_id)
        concurrency = max(1, self.settings.research_official_document_fetch_concurrency)

        dispatch_lock = asyncio.Lock()
        state = _OfficialFilingBatchState()
        batch_started = time.monotonic()
        logger.info(
            "official_filing_fetch_batch_start provider=NSE globalInstrumentId=%s filingCount=%s concurrency=%s",
            profile.instrument_id, len(scheduled_filings), concurrency,
        )

        async def _next_dispatch() -> RegisteredResearchSource | None:
            """Advance the shared cursor and run every pre-fetch decision that
            must stay serialized. Must be called only while holding
            ``dispatch_lock``. Returns the source to fetch next, or None when
            this worker should stop entirely (exhausted / budget-stopped /
            host-failure-halted)."""
            while True:
                if state.stop or state.cursor >= len(scheduled_filings):
                    return None
                filing_index = state.cursor
                result = scheduled_filings[filing_index]
                state.cursor += 1
                source = result.source

                if budget is not None:
                    if budget.stopped or await budget.sufficient():
                        budget.stopped = True
                        state.stop = True
                        return None
                    if not budget.accepts_date(source.official_published_at):
                        continue
                host = (urlparse(source.url).hostname or "").lower()
                # Check reuse and content-type support BEFORE consuming a budget
                # document slot. Both paths require no network fetch and must
                # never exhaust the per-candidate document budget (DI-20H Fix 2:
                # reusable documents and unsupported archives were consuming
                # allow_document() slots, starving genuinely-needed fetches).
                reusable = self._reusable_official_document(profile.instrument_id, source.url, source=source)
                if reusable is not None:
                    if source.document_subtype and reusable.document_subtype is None:
                        reusable.document_subtype = source.document_subtype
                        await self._run_blocking_persistence(
                            self._persist_ingested_document,
                            reusable,
                            nse_financial_result="FINANCIAL_RESULTS" in source.categories,
                        )
                    seen_urls.add(reusable.canonical_url)
                    # Isolate a reconciliation failure so a single problematic
                    # reusable document cannot crash the entire official-filing
                    # fetch loop and prevent subsequent valid filings from being
                    # processed (DI-20H Fix 3).
                    try:
                        await self._run_blocking_persistence(self._reconcile_reused_official_financial_facts, profile, source, reusable)
                    except Exception as exc:
                        state.completed_without_failure = False
                        logger.warning(
                            "official_document_fetch provider=NSE globalInstrumentId=%s host=%s path=%s outcome=SKIPPED reason=RECONCILE_FAILED documentId=%s error=%s",
                            profile.instrument_id, host, _safe_url_path(source.url),
                            reusable.document_id, type(exc).__name__,
                        )
                        if budget is not None:
                            budget.failures.append("RECONCILE_FAILED")
                        continue
                    logger.info(
                        "official_document_fetch provider=NSE globalInstrumentId=%s host=%s path=%s outcome=REUSED reason=ALREADY_PERSISTED documentId=%s",
                        profile.instrument_id,
                        host,
                        _safe_url_path(source.url),
                        reusable.document_id,
                    )
                    continue
                if _official_filing_content_type_unsupported(source.url):
                    # An unsupported archive/office attachment can never be parsed
                    # by the official-document fetch path
                    # (HttpResearchFetcher.process_network_response rejects it
                    # with FetchError "Unsupported content type",
                    # research_fetching.py:304). Skipping it here -- before
                    # `state.attempted += 1` and before any network fetch -- keeps
                    # a provably-unusable file from exhausting the per-refresh
                    # attempt budget (DI-11C Fix 1). Extensionless/ambiguous URLs
                    # are never skipped here.
                    logger.info(
                        "official_document_fetch provider=NSE globalInstrumentId=%s host=%s path=%s outcome=SKIPPED reason=UNSUPPORTED_CONTENT_TYPE",
                        profile.instrument_id,
                        host,
                        _safe_url_path(source.url),
                    )
                    continue
                if state.attempted >= self.settings.research_official_document_max_attempts_per_refresh:
                    logger.info(
                        "official_document_fetch provider=NSE globalInstrumentId=%s outcome=SKIPPED reason=ATTEMPT_BUDGET",
                        profile.instrument_id,
                    )
                    # Continue so a later durable reusable filing can still be
                    # recorded without consuming network budget.
                    if budget is not None and "DOCUMENT_BUDGET_EXHAUSTED" not in budget.failures:
                        budget.failures.append("DOCUMENT_BUDGET_EXHAUSTED")
                    continue
                host_cooldown_until = self._official_host_cooldowns.get((profile.instrument_id, host))
                cross_batch_cooldown_active = (
                    host_cooldown_until is not None and time.monotonic() < host_cooldown_until
                )
                if (
                    state.host_transport_failures.get(host, 0) >= self.settings.research_official_document_max_transport_failures_per_host
                    or cross_batch_cooldown_active
                ):
                    # Guardian Review (Issue 3): state.host_transport_failures
                    # alone only remembers failures within THIS batch call.
                    # cross_batch_cooldown_active is what makes the budget
                    # survive across the separate _fetch_official_filings()
                    # calls one readiness cycle makes for the same instrument
                    # (e.g. one per capability group) -- without it, a host
                    # that just failed out in the previous group's batch was
                    # retried again immediately here with a fresh counter.
                    logger.info(
                        "official_document_fetch provider=NSE globalInstrumentId=%s host=%s outcome=SKIPPED reason=HOST_TRANSPORT_FAILURE_BUDGET crossBatchCooldown=%s",
                        profile.instrument_id,
                        host,
                        cross_batch_cooldown_active,
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
                        state.stop = True
                        return None
                    continue
                # Only an actual dispatch consumes the shared document budget;
                # local attempt/host-limit skips must not starve other feeds.
                if budget is not None:
                    if not await budget.allow_document():
                        state.stop = True
                        return None
                state.attempted += 1
                return source

        async def _worker() -> None:
            while True:
                async with dispatch_lock:
                    source = await _next_dispatch()
                if source is None:
                    return
                host = (urlparse(source.url).hostname or "").lower()
                started = time.monotonic()
                try:
                    document, joined_in_flight = await self._single_flight_official_filing(profile, source)
                    async with dispatch_lock:
                        if document.status != DocumentStatus.DUPLICATE:
                            seen_urls.add(document.canonical_url)
                        if document.status != DocumentStatus.DUPLICATE and not _is_usable_durable_document(document):
                            # A completed transport attempt is not a successful
                            # no-change check when extraction/persistence left
                            # only a terminal non-usable document. Keep it
                            # retryable.
                            state.completed_without_failure = False
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
                    async with dispatch_lock:
                        state.completed_without_failure = False
                        if budget is not None:
                            budget.failures.append("NETWORK_TIMEOUT")
                        exc = TransportFetchError("NETWORK_TIMEOUT")
                        state.host_transport_failures[host] = state.host_transport_failures.get(host, 0) + 1
                        self._official_host_cooldowns[(profile.instrument_id, host)] = (
                            time.monotonic() + self.settings.research_official_document_host_cooldown_seconds
                        )
                        self.last_live_error[profile.instrument_id] = "OFFICIAL_FILING_FETCH_FAILED:NETWORK_TIMEOUT"
                        logger.warning(
                            "official_document_fetch provider=NSE globalInstrumentId=%s host=%s path=%s outcome=FAILED reason=%s elapsedMs=%s httpStatus=%s",
                            profile.instrument_id, host, _safe_url_path(source.url), exc, _elapsed_ms(started), "NONE",
                        )
                except TransportFetchError as exc:
                    async with dispatch_lock:
                        state.completed_without_failure = False
                        if budget is not None:
                            budget.failures.append(_fetch_rejection_reason(exc))
                        state.host_transport_failures[host] = state.host_transport_failures.get(host, 0) + 1
                        self._official_host_cooldowns[(profile.instrument_id, host)] = (
                            time.monotonic() + self.settings.research_official_document_host_cooldown_seconds
                        )
                        self.last_live_error[profile.instrument_id] = f"OFFICIAL_FILING_FETCH_FAILED:{_fetch_rejection_reason(exc)}"
                        logger.warning(
                            "official_document_fetch provider=NSE globalInstrumentId=%s host=%s path=%s outcome=FAILED reason=%s elapsedMs=%s httpStatus=%s",
                            profile.instrument_id, host, _safe_url_path(source.url), str(exc), _elapsed_ms(started), "NONE",
                        )
                except (FetchError, RestrictedFetchError, ValueError) as exc:
                    async with dispatch_lock:
                        state.completed_without_failure = False
                        if budget is not None:
                            budget.failures.append(_fetch_rejection_reason(exc) if isinstance(exc, FetchError) else "PARSER_FAILED")
                        if _is_transport_fetch_failure(exc):
                            state.host_transport_failures[host] = state.host_transport_failures.get(host, 0) + 1
                            self._official_host_cooldowns[(profile.instrument_id, host)] = (
                                time.monotonic() + self.settings.research_official_document_host_cooldown_seconds
                            )
                        reason = _fetch_rejection_reason(exc) if isinstance(exc, FetchError) else "PARSER_FAILED"
                        self.last_live_error[profile.instrument_id] = f"OFFICIAL_FILING_FETCH_FAILED:{reason}"
                        logger.warning(
                            "official_document_fetch provider=NSE globalInstrumentId=%s host=%s path=%s outcome=FAILED reason=%s elapsedMs=%s httpStatus=%s",
                            profile.instrument_id,
                            host,
                            _safe_url_path(source.url),
                            str(exc),
                            _elapsed_ms(started),
                            getattr(exc, "status_code", "NONE"),
                        )

        workers = [asyncio.create_task(_worker()) for _ in range(concurrency)]
        try:
            await asyncio.gather(*workers)
        finally:
            for task in workers:
                if not task.done():
                    task.cancel()
        logger.info(
            "official_filing_fetch_batch_complete provider=NSE globalInstrumentId=%s filingCount=%s concurrency=%s elapsedMs=%s",
            profile.instrument_id, len(scheduled_filings), concurrency, _elapsed_ms(batch_started),
        )
        return state.completed_without_failure

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
        if self._financial_reconciliation_unchanged(profile):
            return
        parsed, written = self._reconcile_persisted_official_financial_document(document)
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
        leader_task = asyncio.current_task()
        # Guardian Review (late-result reconciliation): a caller-facing
        # PdfExtractionTimeoutError does NOT mean the underlying worker
        # thread has stopped -- it is shielded and keeps running. If this
        # flight's `finally` popped `key` unconditionally on that timeout (as
        # it used to), a follower arriving in that window would start a
        # brand-new duplicate download+extraction for the same document
        # while the first one is still silently in flight, and a late
        # successful parse would have nowhere to go but the logs. So on a
        # PdfExtractionTimeoutError specifically, `defer_key_cleanup` is set
        # and the `finally` below leaves `key` in place; `_on_late_result`
        # (passed into process_network_response_async) is what eventually
        # pops it, once the worker's real outcome -- success or failure -- is
        # known, persisting a late success first so it is not lost.
        defer_key_cleanup = False

        async def _on_late_result(outcome: "FetchResult | BaseException") -> None:
            late_host = (urlparse(source.url).hostname or "").lower()
            try:
                if isinstance(outcome, BaseException):
                    logger.info(
                        "official_document_late_result provider=NSE globalInstrumentId=%s host=%s path=%s "
                        "exception=%s outcome=LATE_FAILURE_RETRYABLE",
                        profile.instrument_id, late_host, _safe_url_path(source.url), type(outcome).__name__,
                    )
                    return
                logger.info(
                    "official_document_late_result provider=NSE globalInstrumentId=%s host=%s path=%s "
                    "outcome=LATE_SUCCESS_PERSISTING",
                    profile.instrument_id, late_host, _safe_url_path(source.url),
                )
                await self._ingest_registered_fetch_result_async(
                    profile, source, outcome, expected_profile=profile,
                )
                logger.info(
                    "official_document_late_result provider=NSE globalInstrumentId=%s host=%s path=%s "
                    "outcome=LATE_SUCCESS_PERSISTED",
                    profile.instrument_id, late_host, _safe_url_path(source.url),
                )
            finally:
                # Identity check: an old, already-superseded flight must not
                # remove a newer retry flight installed for the same key.
                if self._official_filing_flights.get(key) is leader_task:
                    self._official_filing_flights.pop(key, None)

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
                try:
                    fetch_result = await self._fetcher.process_network_response_async(
                        network_result,
                        max_bytes=self.settings.research_official_document_max_bytes,
                        extraction_timeout_seconds=self.settings.research_official_document_extraction_timeout_seconds,
                        on_late_result=_on_late_result,
                    )
                except PdfExtractionTimeoutError:
                    defer_key_cleanup = True
                    raise
                finally:
                    # Release the large raw PDF bytes regardless of whether
                    # extraction succeeded or timed out (DI-20H Fix 4). The
                    # worker thread holds its own reference to response.content
                    # and releases it when done; this drops the caller's
                    # reference promptly so it cannot be retained across the
                    # persist tail of a cycle.
                    del network_result
                # process_network_response_async already extracted the text we
                # need to ingest; the raw downloaded document bytes are no
                # longer required. Drop them now (before the blocking persist
                # below) so large NSE PDFs cannot be retained across the persist
                # tail of a cycle that may itself await I/O.
                logger.info("fetch_persist_start provider=NSE globalInstrumentId=%s host=%s path=%s", profile.instrument_id, host, _safe_url_path(source.url))
                document = await self._ingest_registered_fetch_result_async(profile, source, fetch_result, expected_profile=profile)
                logger.info("fetch_persist_complete provider=NSE globalInstrumentId=%s host=%s path=%s", profile.instrument_id, host, _safe_url_path(source.url))
                return document
            return await self._fetch_registered_source(profile, source, expected_profile=profile)
        finally:
            # Identity check prevents an old, cancelled flight from removing a
            # retry flight that was installed for the same key. A
            # PdfExtractionTimeoutError leaves the key in place on purpose
            # (see defer_key_cleanup above): _on_late_result pops it once the
            # worker's true outcome is known, rather than here.
            if not defer_key_cleanup and self._official_filing_flights.get(key) is leader_task:
                self._official_filing_flights.pop(key, None)

    def _inapplicable_business_categories(self, profile: CompanyResearchProfile) -> set[str]:
        """Apply the same positive rules to refresh callers outside readiness."""
        from app.research_applicability import classify_requirements, CONCEPT_CATEGORIES
        records = self.structured_market_snapshots_for({profile.instrument_id}).get(profile.instrument_id, [])
        for record in sorted(records, key=lambda row: (row.provider != "NSE", -row.retrieved_at.timestamp())):
            if record.instrument_id != profile.instrument_id:
                continue
            industry = record.snapshot.facts.get("industry")
            if not industry or not industry.value:
                continue
            decision = classify_requirements(None, str(industry.value), industry.source_url)["ORDER_BOOK_CAPEX_GUIDANCE"]
            return set().union(*(CONCEPT_CATEGORIES[concept] for concept, state in decision.concepts.items()
                if state.state == "NOT_APPLICABLE"))
        return set()

    def _reusable_official_document(self, instrument_id: UUID, url: str, *, source: RegisteredResearchSource | None = None) -> ResearchDocument | None:
        canonical = canonicalize_url(url)
        financial = source is not None and "FINANCIAL_RESULTS" in source.categories
        candidates = [document for document in self.documents_for(instrument_id, source_mode=SourceMode.REAL)
                      if canonicalize_url(document.canonical_url) == canonical]
        fact_documents = (
            {item.document_id for item in self._financial_result_documents_with_extracted_facts(instrument_id)}
            if financial and any(not item.normalized_text for item in candidates) else set()
        )
        return next(
            (
                document
                for document in candidates
                if (_is_usable_durable_document(document) or document.document_id in fact_documents)
                and (not financial or (nse_authority_rejection(document) is None
                     and document.company_id == source.company_id
                     and (bool(document.normalized_text) or document.document_id in fact_documents)))
                and not (source and source.official_published_at and document.published_at
                         and source.official_published_at > document.published_at)
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
        repair_target_categories: frozenset[str] = frozenset(),
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
            if category in repair_target_categories
            or not self._category_is_fresh(profile.instrument_id, category, now)
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
        *,
        bypass_no_change_cooldown: bool = False,
        bypass_evidence_cooldown: bool = False,
    ) -> tuple[bool, datetime | None]:
        """Keep provider polling separate from evidence freshness.

        `_category_refresh` records the last successful provider check in this
        process. Evidence time remains on the durable document/snapshot and is
        never changed when an official listing is unchanged.

        ``bypass_no_change_cooldown`` is the deep-investigation repair escape:
        when an applicable mandatory requirement is still MISSING/PARTIAL/FAILED,
        a prior *successful-no-change* lightweight check must not keep the
        targeted requirement stuck behind a category cooldown. It bypasses ONLY
        the no-evidence no-change cooldown below; by itself it does not touch
        the evidence_at-is-set (genuinely fresh evidence) branch.

        ``bypass_evidence_cooldown`` is the narrower, requirement-aware repair
        escape: category-level "qualifying evidence exists" freshness (e.g. a
        FINANCIAL_RESULTS document with at least one extracted fact) is blind
        to which *specific* fact was extracted. Readiness (requirement-level)
        has already determined, before this repair budget was opened, that the
        specific mandatory input backed by this category is still missing --
        so that stale-but-present evidence must not suppress a real
        acquisition attempt. The caller passes this only for the exact
        category(ies) the current repair explicitly requested; it is never
        applied to a category that was not requested, so genuinely fresh and
        already-complete evidence for any other category remains untouched
        and is not refetched.
        """
        category = _canonical_refresh_category(category)
        strategy = _CATEGORY_STRATEGIES.get(category, _PERIODIC_SLOW)
        evidence_at, period_end = self._category_evidence_timing(instrument_id, category)
        if evidence_at is None:
            if bypass_no_change_cooldown:
                # Deep-investigation repair: the targeted requirement's backing
                # category had a no-change check with no qualifying evidence; a
                # bounded re-acquisition is permitted. Fresh evidence (below)
                # is never bypassed.
                return True, None
            checked_at = self._category_successful_no_change_checks.get((instrument_id, category))
            if checked_at is None:
                # Failed provider attempts never write this state and remain
                # immediately retryable.
                return True, None
            next_eligible = checked_at + strategy.lightweight_check_interval
            return now >= next_eligible, next_eligible
        if bypass_evidence_cooldown:
            return True, None
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
            documents = self._financial_result_documents_with_extracted_facts(instrument_id)
            if documents:
                latest = documents[0]
                identities = {str(document.document_id) for document in documents}
                periods = [datetime.fromisoformat(_canonical_financial_period_end(fact.key.period_end)).replace(tzinfo=timezone.utc)
                           for fact in self._persistence.load_financial_facts({instrument_id})
                           if fact.source_identity in identities and fact.key.period_type == "QUARTERLY"
                           and _canonical_financial_period_end(fact.key.period_end)
                           # A future period cannot establish eligibility for
                           # an older retrieval (e.g. a malformed OCR year).
                           and _canonical_financial_period_end(fact.key.period_end) <= fact.value.retrieved_at.date().isoformat()]
                return latest.retrieved_at, max(periods) if periods else _explicit_quarter_end(latest.normalized_text or latest.raw_text or "")
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

    def _financial_result_documents_with_extracted_facts(self, instrument_id: UUID) -> list[ResearchDocument]:
        """Durable documents linked to authoritative extracted financial facts.

        DI-7C Step 4: DOCUMENT_FETCHED is not equivalent to
        FINANCIAL_FACTS_EXTRACTED.  A document whose title/text looks like a
        result announcement but whose parse produced zero persisted
        FinancialFact rows must not count as qualifying evidence -- doing so
        would incorrectly open the multi-day quarterly freshness window and
        block retry even though nothing was actually extracted.
        """
        documents = [
            document for document in self.documents_for(instrument_id, source_mode=SourceMode.REAL)
            if document.status in {DocumentStatus.PROCESSED, DocumentStatus.PARSED}
            and nse_authority_rejection(document) is None
            and document.company_id == self.profile(instrument_id).company_id
        ]
        if not documents:
            return []
        extracted_document_ids = {
            fact.source_identity
            for fact in self._persistence.load_financial_facts({instrument_id})
            if fact.key.instrument_id == instrument_id
            and fact.source_tier == FactSourceTier.OFFICIAL_NSE
            and fact.source_mode == SourceMode.REAL
            and fact.source_provider == "NSE"
            and _canonical_financial_period_end(fact.key.period_end)
            and any(str(document.document_id) == fact.source_identity
                    and canonicalize_url(fact.value.source_url or "") == canonicalize_url(document.canonical_url)
                    for document in documents)
        }
        # The durable fact's source identity proves financial content. Legacy
        # metadata-only rows or differently worded filing titles must not lose
        # that evidence merely because the title lacks a financial keyword.
        return [document for document in documents if str(document.document_id) in extracted_document_ids]

    def _qualifying_category_evidence(self, instrument_id: UUID, category: str) -> dict[str, object] | None:
        category = _canonical_refresh_category(category)
        if category == "SHAREHOLDING_PATTERN":
            snapshots = self.shareholding_for(instrument_id, limit=1)
            if snapshots:
                snapshot = snapshots[0]
                from app.research_applicability import shareholding_input_coverage
                if "PROMOTER_INSTITUTIONAL_PUBLIC_CATEGORIES" not in shareholding_input_coverage(snapshot):
                    return None
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
        documents = self._financial_result_documents_with_extracted_facts(instrument_id)
        if documents:
            document = documents[0]
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
        if category == "FINANCIAL_RESULTS" and refreshed_at is None:
            refreshed_at, _period_end = self._category_evidence_timing(instrument_id, category)
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

    async def _refresh_search_discovery(
        self, profile: CompanyResearchProfile, missing: set[str], seen_urls: set[str],
        *, protect_error: str | None = None,
    ) -> bool:
        from app.deep_investigation import acquisition_budget
        budget = acquisition_budget(profile.instrument_id)
        # When protect_error is set, an authoritative NSE acquisition failure
        # was already recorded earlier in this same refresh for a different
        # category. This search-fallback attempt still runs normally (its
        # stats, logging, and return value are unaffected) but it must not
        # touch last_live_error: neither a fallback success nor a fallback
        # failure may overwrite or erase that authoritative failure.
        def _set_live_error(value: str) -> None:
            if protect_error is None:
                self.last_live_error[profile.instrument_id] = value

        def _clear_live_error() -> None:
            if protect_error is None:
                self.last_live_error.pop(profile.instrument_id, None)

        if budget is not None and await budget.sufficient():
            # Authoritative evidence persisted earlier in this refresh (e.g. an
            # NSE governance filing) already satisfies the requirement: spend
            # no search queries on a fallback that can add nothing.
            logger.info("search_fallback_skipped globalInstrumentId=%s requirement=%s reason=REQUIREMENT_SATISFIED",
                        profile.instrument_id, budget.requirement_id)
            budget.stopped = True
            return True

        # Category-level authoritative-evidence filter for the group budget.
        # A single generic-search pass services every member of a capability
        # group (e.g. ORDER_BOOK_CAPEX_GUIDANCE + SHAREHOLDING +
        # GOVERNANCE_HISTORY), so a degraded search for one category must not
        # be attributed to an unrelated member whose authoritative evidence is
        # already durable.  Drop any category that already has qualifying
        # durable evidence before invoking the search provider, so the
        # resulting failure (if any) is attributable only to the categories
        # that genuinely lack evidence.
        #
        # SHAREHOLDING_PATTERN is the authoritative case: a durable real NSE
        # shareholding snapshot (DURABLE_REAL_SHAREHOLDING_SNAPSHOT) is
        # authoritative evidence and must win over a non-authoritative
        # SEARCH_PROVIDER_DEGRADED transport failure -- never the reverse.
        search_categories = set(missing)
        for category in sorted(search_categories):
            if self._category_has_qualifying_evidence(profile.instrument_id, category):
                logger.info(
                    "search_fallback_skipped_category globalInstrumentId=%s category=%s reason=QUALIFYING_DURABLE_EVIDENCE",
                    profile.instrument_id, category,
                )
                search_categories.discard(category)
        if not search_categories:
            # Every pending category already has qualifying durable evidence:
            # run no generic search and append no failure, but do NOT mark the
            # whole group budget stopped -- other members of the capability
            # group are resolved by their own evidence and this call simply has
            # nothing left to search.
            logger.info("search_fallback_skipped globalInstrumentId=%s requirement=%s reason=NO_CATEGORIES_NEED_SEARCH",
                        profile.instrument_id, budget.requirement_id if budget is not None else "NONE")
            return True

        try:
            discovered = await self._search_discovery.discover(profile, search_categories, seen_urls)
        except SearchProviderError as exc:
            if budget is not None:
                budget.failures.append("SOURCE_UNAVAILABLE:" + str(exc)[:160])
            reason = str(exc)
            self._search_discovery.last_stats.reject(reason)
            _set_live_error(f"SEARCH_PROVIDER_UNAVAILABLE:{reason}")
            logger.warning("research_discovery_terminal company=%s provider=%s status=SEARCH_PROVIDER_UNAVAILABLE reason=%s",
                profile.company_name, self._search_discovery.provider.provider_name, reason)
            return False
        # Keep this acquisition's outcome across awaited document fetches;
        # another Stage-2 candidate can replace the service's last_stats.
        stats = self._search_discovery.last_stats
        if not discovered:
            if stats.provider_failure_count:
                reason = "SEARCH_PROVIDER_DEGRADED"
                if budget is not None and reason not in budget.failures:
                    budget.failures.append(reason)
                _set_live_error(reason)
                return False
            if stats.candidate_count == 0:
                terminal = "SEARCH_RETURNED_ZERO_RESULTS"
            else:
                terminal = "RESULTS_REJECTED"
            _set_live_error(terminal)
            logger.warning("research_discovery_terminal company=%s provider=%s status=%s candidates=%s accepted=%s rejected_reasons=%s",
                profile.company_name, self._search_discovery.provider.provider_name, terminal,
                stats.candidate_count, stats.accepted_count, stats.rejected_reasons)
            # A zero-result response is a successful no-change check only
            # when the provider completed the requested search work.  Partial
            # engine/provider failures remain retryable and must not advance
            # the no-change schedule.
            return stats.provider_failure_count == 0
        fetched_documents = 0
        extracted_before = self.events.applied
        fetch_failures: dict[str, int] = {}
        for result in discovered:
            if budget is not None and not await budget.allow_document():
                break
            if fetched_documents >= self.settings.research_search_max_documents_per_refresh:
                break
            source = result.source
            try:
                self._validate_registered_source(profile, source)
                document = await self._fetch_registered_source(profile, source, expected_profile=profile)
                if document.status == DocumentStatus.DUPLICATE:
                    stats.reject("DUPLICATE")
                    continue
                fetched_documents += 1
                stats.reject("SEARCH_RESULT_ACCEPTED")
            except RestrictedFetchError as exc:
                stats.reject("ROBOTS_OR_ACCESS_BLOCKED")
                fetch_failures["ROBOTS_OR_ACCESS_BLOCKED"] = fetch_failures.get("ROBOTS_OR_ACCESS_BLOCKED", 0) + 1
                _set_live_error(f"SEARCH_SOURCE_UNAVAILABLE:{source.source_id}:{exc}")
            except FetchError as exc:
                failure = _fetch_rejection_reason(exc)
                stats.reject(failure)
                fetch_failures[failure] = fetch_failures.get(failure, 0) + 1
                _set_live_error(f"SEARCH_SOURCE_UNAVAILABLE:{source.source_id}:{exc}")
            except ValueError as exc:
                stats.reject("PARSER_FAILED")
                fetch_failures["PARSER_FAILED"] = fetch_failures.get("PARSER_FAILED", 0) + 1
                _set_live_error(f"SEARCH_SOURCE_UNAVAILABLE:{source.source_id}:{exc}")
            except Exception as exc:
                stats.reject("HTTP_FETCH_FAILED")
                fetch_failures["HTTP_FETCH_FAILED"] = fetch_failures.get("HTTP_FETCH_FAILED", 0) + 1
                _set_live_error(f"SEARCH_SOURCE_UNAVAILABLE:{source.source_id}:HTTP_FETCH_FAILED")
        if fetched_documents > 0:
            if stats.provider_failure_count:
                _set_live_error("SEARCH_PROVIDER_DEGRADED")
            else:
                _clear_live_error()
        elif discovered:
            _set_live_error("DOCUMENT_FETCH_FAILED")
        stats.documents_fetched = fetched_documents
        stats.events_extracted = self.events.applied - extracted_before
        logger.info("research_fetch_complete company=%s provider=%s accepted_results=%s document_fetch_count=%s extraction_count=%s terminal_status=%s failure_reasons=%s",
            profile.company_name, self._search_discovery.provider.provider_name, len(discovered), fetched_documents,
            stats.events_extracted,
            "RESOLVED_PARTIAL_DATA" if fetched_documents else "DOCUMENT_FETCH_FAILED", fetch_failures)
        return not fetch_failures and stats.provider_failure_count == 0

    async def _refresh_etf_search_discovery(self, profile: EtfResearchProfile) -> None:
        categories = {"ETF_PROFILE", "ETF_PERFORMANCE", "INDEX_OUTLOOK", "ETF_RISK"}
        seen_urls = self.document_urls_for(profile.instrument_id, source_mode=SourceMode.REAL)
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
        if self._has_trusted_nse_profile_identity(profile, source, profile):
            await self._run_blocking_persistence(self._sync_persisted_document_identities, profile.instrument_id)
            reusable = self._reusable_official_document(profile.instrument_id, source.url, source=source)
            if reusable is not None:
                await self._run_blocking_persistence(self._reconcile_reused_official_financial_facts, profile, source, reusable)
                return reusable
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
                reliability=source.reliability_level, published_at=source.official_published_at if trusted_identity else None, source_mode=SourceMode.REAL,
                discovered_at=None,
                discovery_provider=(source.discovery_method if source.discovery_method == "SEARCH_DISCOVERY" or trusted_identity else None),
                expected_profile=expected_profile, document_status=document_status,
                allow_empty_content=result.extraction_status != "EXTRACTED", trusted_profile_identity=trusted_identity,
                document_subtype=source.document_subtype,
                # ResearchFetcher already built this from the identical
                # extracted text when content_type is application/pdf
                # (FetchResult.pdf_structure); reuse it instead of having
                # _prepare_ingested_document rebuild the complete
                # PdfTextStructure a second time. getattr guards non-FetchResult
                # test doubles that predate this field.
                precomputed_pdf_structure=getattr(result, "pdf_structure", None),
            )
        except Exception:
            logger.info("document_ingest_stage globalInstrumentId=%s stage=PREPARE elapsedMs=%s outcome=FAILED", profile.instrument_id, _elapsed_ms(started))
            raise
        logger.info("document_ingest_stage globalInstrumentId=%s documentId=%s stage=PREPARE elapsedMs=%s outcome=SUCCESS", profile.instrument_id, document.document_id, _elapsed_ms(started))
        if trusted_identity and source.official_title:
            document.title = source.official_title
            document.published_at = source.official_published_at
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
            **({"published_at": source.official_published_at, "_official_title": source.official_title}
               if self._has_trusted_nse_profile_identity(profile, source, expected_profile) else {}),
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
        try:
            stored = self._persistence.upsert_document(document)
        except BaseException:
            self._forget_unpersisted_document(document)
            raise
        if stored is False:
            return self._adopt_existing_durable_document(document)
        self._mark_document_durable(document)
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
        # Rehydrate identity/index/dedup state cheaply: identity columns
        # only, never full document bodies. Full ResearchDocument objects
        # are loaded lazily and kept in a bounded cache (self.documents /
        # documents_for()), so process memory at startup no longer scales
        # with the total historical document count.
        for identity in self._persistence.load_document_identities():
            document_id, instrument_id, canonical_url, content_hash = identity[:4]
            if document_id is None:
                continue
            source_mode, sort_key = (identity[4], identity[5]) if len(identity) >= 6 else (SourceMode.REAL, None)
            self.documents.register_ref(DocumentRef(document_id, instrument_id, source_mode, canonical_url, content_hash, sort_key))
            self._deduplicator.add_identity(document_id, canonical_url, content_hash)
        # Events are loaded per instrument on first use; startup only needs the
        # latest detection time per instrument (compact aggregate).
        latest = getattr(self._persistence, "load_event_refresh_times", None)
        for instrument_id, detected_at in (latest() if callable(latest) else []):
            if instrument_id is not None and detected_at is not None:
                self.last_refresh[instrument_id] = max(self.last_refresh.get(instrument_id, detected_at), detected_at)
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


def _aware_observation_time(observation) -> datetime | None:
    if not observation or not observation.get("observed_at"):
        return None
    value = observation["observed_at"]
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _fetch_rejection_reason(exc: FetchError) -> str:
    message = str(exc)
    if message in {
        "NETWORK_TIMEOUT",
        "PDF_EXTRACTION_TIMEOUT",
        "PDF_EXTRACTION_QUEUE_TIMEOUT",
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


def _same_persisted_financial_fact(prior: FinancialFact, incoming: FinancialFact) -> bool:
    # as_of_date/period are parser projection fields, not persisted columns.
    excluded = {"as_of_date", "period", "calculation_basis"}
    return (
        prior.key == incoming.key and prior.source_tier == incoming.source_tier
        and prior.source_provider == incoming.source_provider
        and prior.source_identity == incoming.source_identity
        and prior.source_mode == incoming.source_mode
        and prior.value.model_dump(exclude=excluded) == incoming.value.model_dump(exclude=excluded)
    )


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


# Core authoritative financial-statement categories that must never be
# starved out of the bounded per-refresh attempt budget by a batch of
# opportunistic NSE announcement categories (DI-7C Step 10).  Only
# FINANCIAL_RESULTS is actually routed through this function today --
# SHAREHOLDING_PATTERN uses a separate official XBRL discovery path -- but
# both are listed for robustness if that ever changes.
_CORE_FINANCIAL_FILING_CATEGORIES = frozenset({"FINANCIAL_RESULTS", "SHAREHOLDING_PATTERN"})


# The official-document fetch path (HttpResearchFetcher.process_network_response,
# research_fetching.py:295) can only ingest these content types. Every other
# content type is rejected at runtime with FetchError("Unsupported content type")
# (research_fetching.py:304). This set is the single source of truth for whether
# a candidate is *processable* by the official fetch path.
_OFFICIAL_DOCUMENT_PROCESSABLE_CONTENT_TYPES = frozenset({
    "text/html",
    "text/plain",
    "application/xml",
    "text/xml",
    "application/rss+xml",
    "application/pdf",
})


# Binary/archive/office extensions NSE occasionally attaches. Each maps to the
# content type the fetcher would actually receive; all of them are, by
# construction, absent from _OFFICIAL_DOCUMENT_PROCESSABLE_CONTENT_TYPES, so a
# file with one of these extensions can provably never be parsed by the official
# fetch path. Skipping such a candidate BEFORE a network fetch and BEFORE
# `attempted += 1` prevents a useless file from exhausting the per-refresh
# attempt budget (DI-11C Fix 1).
_OFFICIAL_UNSUPPORTED_CONTENT_TYPES_BY_EXTENSION: dict[str, str] = {
    ".zip": "application/zip",
    ".zipx": "application/zip",
    ".7z": "application/x-7z-compressed",
    ".rar": "application/vnd.rar",
    ".tar": "application/x-tar",
    ".gz": "application/gzip",
    ".tgz": "application/gzip",
    ".bz2": "application/x-bzip2",
    ".xz": "application/x-xz",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".exe": "application/x-msdownload",
    ".dll": "application/x-msdownload",
    ".bin": "application/octet-stream",
    ".iso": "application/octet-stream",
    ".img": "application/octet-stream",
    ".eml": "message/rfc822",
    ".msg": "application/vnd.ms-outlook",
}


def _official_filing_url_extension(url: str) -> str:
    """Lowercased file extension of an announcement attachment URL.

    Returns "" for extensionless/ambiguous URLs (e.g. ``...?id=5`` or a path
    with no dot in its final segment) so callers retain the existing run-time
    content-type handling instead of falsely pre-skipping such a candidate.
    """
    path = urlparse(url).path
    name = path.rsplit("/", 1)[-1]
    dot = name.rfind(".")
    if dot <= 0:  # no extension, or a leading-dot filename like ".gitignore"
        return ""
    return name[dot:].lower()


def _official_filing_content_type_unsupported(url: str) -> bool:
    """True iff the URL's file extension provably resolves to a content type the
    official-document fetch path cannot process.

    Only decisive, processable-unsupported extensions are matched. Unknown and
    extensionless/ambiguous URLs return False so existing discovery/fetch
    behavior is preserved (DI-11C Fix 1).
    """
    extension = _official_filing_url_extension(url)
    if not extension:
        return False
    content_type = _OFFICIAL_UNSUPPORTED_CONTENT_TYPES_BY_EXTENSION.get(extension)
    if content_type is None:
        # Unknown extension: cannot prove it is unsupported. Defer to run-time
        # content-type handling rather than risk a false pre-skip.
        return False
    return content_type not in _OFFICIAL_DOCUMENT_PROCESSABLE_CONTENT_TYPES


def _derivative_newspaper_publication(source) -> bool:
    text = f"{getattr(source, 'official_title', None) or ''} {source.url}".casefold()
    return "newspaper" in text or "clipping" in text


def _fair_official_filing_order(filings: list[DiscoveryResult]) -> list[DiscoveryResult]:
    """Two-phase ordering: prioritize core financial categories over non-core.

    Phase 1: round-robin across CORE categories only (FINANCIAL_RESULTS,
    SHAREHOLDING_PATTERN).  Phase 2: round-robin across remaining non-core
    categories.  This guarantees that a bounded document budget of ``N`` useful
    acquisition attempts always exhausts core financial-result candidates before
    any opportunistic non-core announcement (newspaper, board meeting, investor
    presentation) consumes a slot.

    Within FINANCIAL_RESULTS, results are sorted newest-first with unsupported
    content-type URLs deferred (the fetch loop skips them before consuming a
    budget slot, but defusing them from first place keeps the most recently
    published parseable result prioritized).
    """
    buckets: dict[str, list[DiscoveryResult]] = {}
    category_order: list[str] = []
    for filing in filings:
        if filing.category not in buckets:
            buckets[filing.category] = []
            category_order.append(filing.category)
        buckets[filing.category].append(filing)
    if "FINANCIAL_RESULTS" in buckets:
        def financial_priority(filing: DiscoveryResult):
            published = filing.source.official_published_at
            dated = published.astimezone(timezone.utc) if published and published.tzinfo else published
            age_key = (dated - datetime.min.replace(tzinfo=dated.tzinfo)).total_seconds() if dated else 0
            # A newspaper publication/clipping of results is a derivative
            # (usually an image scan filed after the results): it must not
            # consume the bounded document budget before primary result
            # filings of any recent quarter. It stays eligible afterwards.
            return (_official_filing_content_type_unsupported(filing.source.url),
                    _derivative_newspaper_publication(filing.source), -age_key, filing.source.url)
        buckets["FINANCIAL_RESULTS"].sort(key=financial_priority)
    core_categories = [c for c in category_order if c in _CORE_FINANCIAL_FILING_CATEGORIES]
    non_core_categories = [c for c in category_order if c not in _CORE_FINANCIAL_FILING_CATEGORIES]

    def _round_robin(categories: list[str]) -> list[DiscoveryResult]:
        ordered: list[DiscoveryResult] = []
        offsets = {category: 0 for category in categories}
        while True:
            emitted = False
            for category in categories:
                index = offsets[category]
                bucket = buckets[category]
                if index >= len(bucket):
                    continue
                ordered.append(bucket[index])
                offsets[category] = index + 1
                emitted = True
            if not emitted:
                return ordered

    return _round_robin(core_categories) + _round_robin(non_core_categories)


# Shared with _financial_result_documents_with_extracted_facts and
# _category_evidence_timing so both use one definition of "looks like a
# financial result announcement" (DI-7C Step 4/9).
_FINANCIAL_RESULT_EVIDENCE_TERMS = ("financial result", "quarterly result", "earnings", "annual report")

# Internal-only completeness sentinels (DI-7C Step 4/5/6): a document whose
# text carries a recognized balance-sheet/cash-flow heading but whose parse
# nonetheless yielded zero periods for that statement is a genuine
# extraction gap, not "nothing was expected".  These sentinel metric names
# are namespaced so they can never collide with a real parsed metric key and
# are never satisfied by any persisted fact, so such a period stays
# incomplete (and thus retryable) until extraction actually succeeds.
_BALANCE_SHEET_EXTRACTION_PENDING_SENTINEL = "__nse_balance_sheet_extraction_pending__"
_CASH_FLOW_EXTRACTION_PENDING_SENTINEL = "__nse_cash_flow_extraction_pending__"


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
