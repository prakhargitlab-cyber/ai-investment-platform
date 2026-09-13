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
    research_max_redirects: int = 5
    research_max_retries: int = 2
    # Official filings are fetched as part of an interactive refresh.  Keep a
    # single unhealthy archive host from consuming the whole refresh budget.
    research_official_document_max_attempts_per_refresh: int = 3
    research_official_document_timeout_seconds: float = 8.0
    research_official_document_extraction_timeout_seconds: float = 12.0
    research_pdf_extraction_concurrency: int = 1
    research_official_document_max_transport_failures_per_host: int = 1
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
