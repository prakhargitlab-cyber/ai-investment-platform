import json
import logging
import re
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from uuid import UUID

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
_MAX_OFFICIAL_DOCUMENT_BYTES = 50 * 1024 * 1024
_REQUEST_ID: ContextVar[str | None] = ContextVar("aip_request_id", default=None)
_SENSITIVE_LOG_VALUE = re.compile(
    r"(?i)\b(authorization|cookie|password|token|api[_-]?(?:key|token)|"
    r"access[_-]?token|refresh[_-]?token|request[_-]?token|client[_-]?secret)"
    r"\s*[:=]\s*(?:bearer\s+)?([^\s,;&]+)"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIP_", env_file=".env", extra="ignore")

    service_name: str = "research-engine"
    environment: str = "LOCAL"
    llm_provider: str = "ollama"
    ollama_base_url: str = "http://ollama:11434"
    research_live_enabled: bool = False
    research_demo_enabled: bool = True
    research_log_level: str = "WARNING"
    research_user_agent: str = "AIInvestmentResearchBot/0.1 contact=research-compliance@example.invalid"
    research_official_document_user_agent: str = "Mozilla/5.0"
    research_official_document_max_bytes: int = 25 * 1024 * 1024
    research_request_timeout_seconds: float = 10.0
    research_connect_timeout_seconds: float = 3.0
    research_max_content_bytes: int = 1_500_000
    # Bounded retention of full ResearchDocument bodies (raw/normalized text,
    # PDF structure) after durable persistence; see app/document_cache.py.
    # Sized for active work (Stage2 concurrency x per-candidate document
    # budget), not for total documents processed. Evicted bodies reload from
    # research_documents.
    research_document_cache_max_documents: int = 64
    research_document_cache_max_bytes: int = 64 * 1024 * 1024
    # Resident instruments whose research events are kept in memory (LRU);
    # evicted instruments reload from research_events on demand.
    research_event_cache_max_instruments: int = 256
    # DB lease for resumable production opportunity cycles (app/cycle_checkpoint.py).
    # A crashed owner's cycle is taken over after this many seconds.
    research_opportunity_cycle_lease_seconds: int = 300
    # Bounded in-cycle repair passes over technically failed deep candidates
    # (Slice 4). Total attempts per candidate per cycle, across restarts, are
    # capped by app/cycle_checkpoint.DEFAULT_MAX_ATTEMPTS.
    research_opportunity_repair_passes: int = 1
    # Backoff before re-attempting the NSE financial-authority upgrade after a
    # TECHNICAL failure (a successful/empty check keeps the 3-day window).
    research_authority_retry_backoff_seconds: int = 900
    # Bounded Stage-2 (deep) acquisition look-ahead (Slice 7). Benchmarked 1..4
    # (tests/test_slice7_stage2_concurrency.py): 2 is the smallest value with a
    # meaningful gain (-43% latency-bound, -28% with real ingestion); 3 adds ~5%,
    # 4 regresses (CPU-bound ingestion). Memory is bounded by the document cache
    # at every value; results are identical. 1 = sequential.
    research_stage2_concurrency: int = 2
    # NSE corporate-announcement structured `category`/`subCategory` values that
    # denote governance disclosures. Empty by default: no real NSE governance
    # category values are captured in the repository, so none are invented.
    # Populate from a captured NSE payload; titles remain the fallback.
    research_nse_governance_categories: list[str] = []
    research_max_redirects: int = 5
    research_max_retries: int = 2
    # Official filings are fetched as part of an interactive refresh.  Keep a
    # single unhealthy archive host from consuming the whole refresh budget.
    research_official_document_max_attempts_per_refresh: int = 3
    research_official_document_timeout_seconds: float = 8.0
    research_official_document_extraction_timeout_seconds: float = 12.0
    # Distinct from the extraction timeout above (Guardian Review Slice 4):
    # this bounds only how long a PDF may wait for an extraction-worker
    # permit before being rejected as PDF_EXTRACTION_QUEUE_TIMEOUT: queue
    # admission time is not the same failure mode as a stuck/slow parse, and
    # conflating them under one value made queue saturation and slow-parse
    # timeouts indistinguishable. Defaults to the same value as the
    # extraction timeout above; tune independently only when there is
    # comparable evidence for queue admission itself.
    research_pdf_extraction_queue_timeout_seconds: float = 12.0
    research_pdf_extraction_concurrency: int = 1
    research_official_document_max_transport_failures_per_host: int = 1
    # Guardian Review (Issue 3, STAGE2_LIVE_RUN_DEFECTS_20260930.md): the
    # per-host transport-failure count above is tracked in
    # _OfficialFilingBatchState, which is created fresh on every
    # _fetch_official_filings() call. A single readiness cycle calls it
    # once per capability group needing official filings for the SAME
    # instrument, so a host that just tripped the failure budget in one
    # group's batch was, before this setting existed, immediately
    # retried from zero in the next group's batch moments later. This
    # cooldown is consulted/updated in the repository-instance-level
    # (not per-batch) `_official_host_cooldowns` map so the budget
    # survives across batches for the same (instrument, host) pair, while
    # still allowing a legitimate retry once it elapses.
    research_official_document_host_cooldown_seconds: float = 60.0
    # Bounded, conservative overlap for independent official-filing fetches
    # WITHIN a single instrument (never across requirement groups, never
    # across instruments): the network fetch + PDF extraction inside
    # _single_flight_official_filing is the expensive part of
    # _fetch_official_filings; everything that decides WHETHER to fetch
    # (budget/reuse/content-type/attempt-budget/host-failure checks) stays
    # fully serialized under a dispatch lock regardless of this value.
    # Default 2 is deliberately conservative -- see the concurrency-bound
    # report for this change for the rationale.
    research_official_document_fetch_concurrency: int = 2
    research_playwright_enabled: bool = False
    research_playwright_concurrency: int = 1
    research_search_enabled: bool = False
    research_search_provider: str = "disabled"
    research_search_endpoint: str | None = None
    research_search_api_key: str | None = None
    research_search_engine_id: str | None = None
    research_search_window_months: int = 12
    research_search_max_queries_per_category: int = 6
    research_search_max_results_per_query: int = 5
    research_search_max_documents_per_refresh: int = 24
    research_search_refresh_cooldown_seconds: int = 3600
    research_search_cache_ttl_seconds: int = 86400
    research_search_allowed_domains: list[str] = []
    research_ownership_change_threshold_percentage_points: float = 0.10
    research_quarterly_freshness_seconds: int = 2_592_000
    research_shareholding_freshness_seconds: int = 2_592_000
    research_catalyst_freshness_seconds: int = 86_400
    research_annual_report_freshness_seconds: int = 15_552_000
    research_analyst_freshness_seconds: int = 604_800
    structured_provider_enabled: bool = True
    structured_provider_timeout_seconds: float = 10.0
    yahoo_search_url: str = "https://query1.finance.yahoo.com/v1/finance/search"
    yahoo_quote_url: str = "https://query1.finance.yahoo.com/v7/finance/quote"
    yahoo_summary_url: str = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
    nse_announcements_url: str = "https://www.nseindia.com/api/corporate-announcements"
    nse_shareholdings_url: str = "https://www.nseindia.com/api/corporate-share-holdings-master"
    structured_resolution_min_confidence: float = 0.62
    structured_resolution_ambiguity_margin: float = 0.08
    structured_market_price_freshness_seconds: int = 300
    structured_fundamentals_freshness_seconds: int = 86_400
    sec_edgar_ticker_endpoint: str = "https://www.sec.gov/files/company_tickers.json"
    sec_edgar_companyfacts_endpoint: str = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    sec_edgar_user_agent: str = "AIInvestmentResearchBot/0.1 contact=research-compliance@example.invalid"
    sec_edgar_timeout_seconds: float = 10.0
    # India macro foundation (RBI repo rate, India CPI): acquisition +
    # persistence + read model only in this iteration -- not consumed by
    # the Rule Engine, readiness, or ranking. Unset by default: this sandbox
    # could not verify a live data.gov.in resource, so nothing is guessed;
    # each endpoint must be an operator-verified data.gov.in OGD resource URL.
    india_macro_enabled: bool = False
    india_macro_rbi_repo_rate_endpoint: str | None = None
    india_macro_rbi_repo_rate_api_key: str | None = None
    india_macro_cpi_endpoint: str | None = None
    india_macro_cpi_api_key: str | None = None
    india_macro_rbi_freshness_seconds: int = 5_184_000
    india_macro_cpi_freshness_seconds: int = 3_024_000
    # US macro foundation (Fed policy rate, US CPI): acquisition + persistence
    # + read model only, extending the same macro framework above. The FRED
    # endpoint/response schema were directly verified against official docs
    # (see app/us_macro_provider.py); only the API key is credential-gated
    # and unset by default.
    us_macro_enabled: bool = False
    us_macro_fred_api_key: str | None = None
    us_macro_fed_freshness_seconds: int = 4_838_400
    us_macro_cpi_freshness_seconds: int = 3_024_000
    # Macro event calendar foundation (FOMC, RBI MPC meeting dates):
    # acquisition + persistence + read model only, no consensus/expected
    # values, no scoring. FOMC's endpoint is the verified official page;
    # RBI has no configured endpoint because no parser is implemented yet
    # (see app/rbi_mpc_calendar_provider.py).
    macro_event_calendar_enabled: bool = False
    fomc_calendar_freshness_seconds: int = 604_800
    rbi_mpc_calendar_freshness_seconds: int = 604_800
    eodhd_api_key: str | None = None
    eodhd_base_url: str = "https://eodhd.com/api"
    eodhd_timeout_seconds: float = 10.0
    structured_valuation_freshness_seconds: int = 86_400
    structured_analyst_freshness_seconds: int = 604_800
    portfolio_refresh_instrument_concurrency: int = 4
    research_persistence_enabled: bool = False
    research_database_backend: str = "postgres"
    research_database_host: str = "localhost"
    research_database_port: int = 5432
    research_database_name: str = "investment"
    research_database_user: str = "investment"
    research_database_password: str | None = None
    research_database_schema: str = "research"
    research_database_ssl_mode: str = "disable"
    research_database_connect_timeout_seconds: int = 10
    research_database_statement_timeout_seconds: int = 30
    research_distributed_lock_backend: str = "process"
    research_distributed_lock_acquire_timeout_seconds: int = 30
    # Narrow, research-engine-only switch for the in-process Global
    # Opportunity scheduler (GlobalOpportunityScheduler). Defaults True so
    # existing environments keep today's behavior (the scheduler already
    # starts whenever persistence is enabled) unless explicitly opted out.
    # Deliberately separate from the broad, chart-wide
    # AIP_FEATURE_SCHEDULED_JOBS_ENABLED flag, which no service in this
    # codebase currently reads -- flipping that flag would not affect this
    # scheduler and could unintentionally gate unrelated future jobs.
    research_opportunity_scheduler_enabled: bool = True
    # Operator-controlled recovery pause (two-phase cancellation contract,
    # section 2 of STOCK_RADAR_CONTROLLED_VALIDATION_RUNBOOK). Default False
    # (off): all recovery, takeover, scheduling, and manual submission
    # behavior is unchanged. When True: the worker refuses to resume an
    # active cycle at startup, the scheduler refuses new submissions, and
    # manual POST /opportunities/cycles is rejected. The cancellation
    # REQUEST/STATUS API (GET /status, DELETE /cancel) remains available.
    # Cancellation finalization of an unowned CANCEL_REQUESTED cycle is
    # still possible via a dedicated fenced operator endpoint while paused.
    research_opportunity_recovery_paused: bool = False
    research_mcp_first_enabled: bool = False
    research_mcp_gateway_base_url: str = "http://mcp-gateway"
    research_mcp_gateway_timeout_seconds: float = 10.0
    research_mcp_service_identity: str = "research-engine"
    research_readiness_ensure_timeout_seconds: float = 25.0
    portfolio_service_base_url: str = "http://portfolio-service"
    market_data_nifty_refresh_timeout_seconds: float = 30.0
    market_data_population_batch_size: int = 50
    # Operational single-request bound, not a claimed NSE maximum.
    nse_historical_request_window_days: int = Field(default=30, ge=1)
    nse_historical_max_retries: int = Field(default=2, ge=0, le=3)
    market_data_population_request_interval_seconds: float = 0.20
    market_data_population_initial_lookback_days: int = 400
    market_data_nifty_freshness_hours: int = 12
    market_data_historical_freshness_hours: int = 72
    market_data_ensure_retry_cooldown_minutes: int = 15
    market_data_population_retry_cooldown_hours: int = 12
    market_data_internal_user_id: str = "00000000-0000-0000-0000-000000000101"
    market_data_internal_issuer: str = "aip-internal"
    market_data_internal_subject: str = "research-engine-market-data"

    @field_validator("portfolio_refresh_instrument_concurrency")
    @classmethod
    def validate_portfolio_refresh_instrument_concurrency(cls, value: int) -> int:
        if not 1 <= value <= 8:
            raise ValueError("portfolio_refresh_instrument_concurrency must be between 1 and 8")
        return value

    @field_validator("market_data_population_batch_size")
    @classmethod
    def validate_market_data_population_batch_size(cls, value: int) -> int:
        if not 1 <= value <= 100:
            raise ValueError("market_data_population_batch_size must be between 1 and 100")
        return value

    @field_validator("market_data_nifty_refresh_timeout_seconds")
    @classmethod
    def validate_market_data_nifty_refresh_timeout_seconds(cls, value: float) -> float:
        if not 10 <= value <= 120:
            raise ValueError("market_data_nifty_refresh_timeout_seconds must be between 10 and 120")
        return value

    @field_validator("market_data_population_request_interval_seconds")
    @classmethod
    def validate_market_data_population_request_interval_seconds(cls, value: float) -> float:
        if not 0 <= value <= 10:
            raise ValueError("market_data_population_request_interval_seconds must be between 0 and 10")
        return value

    @field_validator("market_data_population_initial_lookback_days")
    @classmethod
    def validate_market_data_population_initial_lookback_days(cls, value: int) -> int:
        if not 365 <= value <= 450:
            raise ValueError("market_data_population_initial_lookback_days must be between 365 and 450")
        return value

    @field_validator("market_data_nifty_freshness_hours", "market_data_historical_freshness_hours")
    @classmethod
    def validate_market_data_freshness_hours(cls, value: int) -> int:
        if not 1 <= value <= 168:
            raise ValueError("market-data freshness hours must be between 1 and 168")
        return value

    @field_validator("market_data_ensure_retry_cooldown_minutes")
    @classmethod
    def validate_market_data_ensure_retry_cooldown_minutes(cls, value: int) -> int:
        if not 1 <= value <= 1440:
            raise ValueError("market_data_ensure_retry_cooldown_minutes must be between 1 and 1440")
        return value

    @field_validator("market_data_population_retry_cooldown_hours")
    @classmethod
    def validate_market_data_population_retry_cooldown_hours(cls, value: int) -> int:
        if not 1 <= value <= 168:
            raise ValueError("market_data_population_retry_cooldown_hours must be between 1 and 168")
        return value

    @field_validator("market_data_internal_user_id")
    @classmethod
    def validate_market_data_internal_user_id(cls, value: str) -> str:
        UUID(value)
        return value

    @field_validator("research_pdf_extraction_concurrency")
    @classmethod
    def validate_research_pdf_extraction_concurrency(cls, value: int) -> int:
        if not 1 <= value <= 4:
            raise ValueError("research_pdf_extraction_concurrency must be between 1 and 4")
        return value

    @field_validator("research_log_level")
    @classmethod
    def validate_research_log_level(cls, value: str) -> str:
        normalized = str(value).upper()
        if normalized not in _LOG_LEVELS:
            raise ValueError(f"Unsupported research log level: {value}")
        return normalized

    @field_validator("environment")
    @classmethod
    def normalize_environment(cls, value: str) -> str:
        normalized = str(value).upper()
        if normalized not in {"LOCAL", "TEST", "AZURE", "DEV", "PRD"}:
            raise ValueError(f"Unsupported environment profile: {value}")
        return normalized

    @field_validator("research_database_ssl_mode")
    @classmethod
    def validate_database_ssl_mode(cls, value: str) -> str:
        normalized = str(value).lower()
        if normalized not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
            raise ValueError(f"Unsupported PostgreSQL sslmode: {value}")
        return normalized

    @field_validator("research_database_connect_timeout_seconds", "research_database_statement_timeout_seconds",
                     "research_distributed_lock_acquire_timeout_seconds")
    @classmethod
    def validate_positive_timeout(cls, value: int) -> int:
        if not 1 <= value <= 300:
            raise ValueError("database and lock timeouts must be between 1 and 300 seconds")
        return value

    @field_validator("research_distributed_lock_backend")
    @classmethod
    def validate_distributed_lock_backend(cls, value: str) -> str:
        normalized = str(value).lower()
        if normalized not in {"process", "postgres", "redis", "disabled"}:
            raise ValueError(f"Unsupported distributed lock backend: {value}")
        return normalized

    @field_validator("research_mcp_gateway_base_url")
    @classmethod
    def validate_mcp_gateway_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("research MCP gateway base URL must be HTTP(S)")
        return normalized

    @field_validator("research_mcp_gateway_timeout_seconds")
    @classmethod
    def validate_mcp_gateway_timeout(cls, value: float) -> float:
        if not 0 < value <= 120:
            raise ValueError("research MCP gateway timeout must be between 0 and 120 seconds")
        return value

    @field_validator("research_readiness_ensure_timeout_seconds")
    @classmethod
    def validate_readiness_ensure_timeout(cls, value: float) -> float:
        if not 1 <= value <= 120:
            raise ValueError("research readiness ensure timeout must be between 1 and 120 seconds")
        return value

    @field_validator("research_mcp_service_identity")
    @classmethod
    def validate_mcp_service_identity(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("research MCP service identity is required")
        return normalized

    @field_validator("research_stage2_concurrency")
    @classmethod
    def validate_stage2_concurrency(cls, value: int) -> int:
        if not 1 <= value <= 8:
            raise ValueError("research_stage2_concurrency must be between 1 and 8")
        return value

    @field_validator("research_document_cache_max_documents", "research_document_cache_max_bytes")
    @classmethod
    def validate_document_cache_bounds(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("Document cache bounds must be positive")
        return value

    @field_validator("research_official_document_max_bytes")
    @classmethod
    def validate_official_document_max_bytes(cls, value: int) -> int:
        if value <= 0 or value > _MAX_OFFICIAL_DOCUMENT_BYTES:
            raise ValueError(f"Official document max bytes must be between 1 and {_MAX_OFFICIAL_DOCUMENT_BYTES}")
        return value


def configure_application_logging(settings: Settings) -> None:
    """Configure application logging without modifying Uvicorn loggers."""
    level = getattr(logging, settings.research_log_level)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    app_logger = logging.getLogger("app")
    app_logger.setLevel(level)
    app_logger.propagate = False

    if not any(getattr(handler, "_aip_application_handler", False) for handler in app_logger.handlers):
        handler = logging.StreamHandler()
        handler.setLevel(level)
        handler.setFormatter(_StructuredLogFormatter(settings.service_name, settings.environment))
        handler._aip_application_handler = True
        app_logger.addHandler(handler)
    else:
        for handler in app_logger.handlers:
            if getattr(handler, "_aip_application_handler", False):
                handler.setLevel(level)


def set_request_id(value: str) -> Token:
    return _REQUEST_ID.set(value)


def reset_request_id(token: Token) -> None:
    _REQUEST_ID.reset(token)


class _StructuredLogFormatter(logging.Formatter):
    def __init__(self, service: str, environment: str) -> None:
        super().__init__()
        self.service = service
        self.environment = environment

    def format(self, record: logging.LogRecord) -> str:
        message = _SENSITIVE_LOG_VALUE.sub(r"\1=<redacted>", record.getMessage()).replace("\r", " ").replace("\n", " ")
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "service": self.service,
            "environment": self.environment,
            "level": record.levelname,
            "event": record.name,
            "requestId": _REQUEST_ID.get(),
            "message": message,
        }
        return json.dumps(payload, separators=(",", ":"), default=str)
