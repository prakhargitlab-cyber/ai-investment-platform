from __future__ import annotations

import logging
import re
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Any, Protocol
from urllib.parse import quote
from uuid import UUID

import httpx
import yfinance as yf

from app.models import ProvenancedValue, StructuredInstrumentResolution, StructuredMarketSnapshot
from app.settings import Settings

logger = logging.getLogger(__name__)


class StructuredProviderError(RuntimeError):
    pass


class StructuredResearchProvider(Protocol):
    provider_name: str

    async def collect(self, instrument: dict[str, Any]) -> StructuredMarketSnapshot:
        ...


class YahooFinanceProvider:
    """Provider-neutral adapter over Yahoo's public structured market responses."""

    provider_name = "YAHOO_FINANCE"
    SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
    QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
    SUMMARY_URL = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None,
                 ticker_factory: Any | None = None) -> None:
        self.settings = settings
        self.search_url = settings.yahoo_search_url
        self.quote_url = settings.yahoo_quote_url
        self.summary_url = settings.yahoo_summary_url
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.structured_provider_timeout_seconds, connect=3.0),
            headers={"User-Agent": settings.research_user_agent, "Accept": "application/json"},
            follow_redirects=True,
        )
        self._cache: dict[str, tuple[datetime, StructuredMarketSnapshot]] = {}
        self.ticker_factory = ticker_factory or yf.Ticker
        self.use_yfinance = client is None or ticker_factory is not None

    async def collect(self, instrument: dict[str, Any]) -> StructuredMarketSnapshot:
        identity = strongest_company_identity(instrument)
        cache_key = _identity_key(instrument, identity)
        cached = self._cache.get(cache_key)
        now = datetime.now(timezone.utc)
        if cached and cached[0] > now:
            return cached[1]
        durable_ticker = _clean_identity(instrument.get("structuredProviderTicker"))
        durable_status = str(instrument.get("structuredProviderStatus") or "").upper()
        if durable_ticker and durable_status in {"VERIFIED", "RESOLVED"}:
            resolution = StructuredInstrumentResolution(
                instrument_id=_uuid(instrument.get("instrumentId")), provider=self.provider_name,
                provider_ticker=durable_ticker, company_name=_comparison_company_name(instrument, identity),
                exchange=_clean_identity(instrument.get("structuredProviderExchange")) or None,
                currency=_clean_identity(instrument.get("structuredProviderCurrency")) or None,
                quote_type=str(instrument.get("assetType") or instrument.get("securityType") or "EQUITY").upper(),
                confidence=0.95, resolved_at=now, status="VERIFIED_REUSED",
            )
            logger.info("structured_mapping_reused instrument=%s provider=%s ticker=%s status=%s resolution_attempted=false",
                instrument.get("instrumentId"), self.provider_name, durable_ticker, durable_status)
        else:
            resolution = await self.resolve_instrument(instrument, identity)
        snapshot = await self._collect_resolved(resolution)
        # A combined snapshot may contain a quote, so its reuse window follows the shorter market-price TTL.
        self._cache[cache_key] = (now + timedelta(seconds=self.settings.structured_market_price_freshness_seconds), snapshot)
        return snapshot

    async def resolve_instrument(self, instrument: dict[str, Any], identity: str | None = None) -> StructuredInstrumentResolution:
        identity = identity or strongest_company_identity(instrument)
        if not identity:
            raise StructuredProviderError("COMPANY_NOT_RESOLVED")
        trusted_nse_candidate = _trusted_nse_candidate(instrument)
        if trusted_nse_candidate:
            return await self._resolve_verified_nse_candidate(instrument, identity, trusted_nse_candidate)
        raw_overview = _clean_identity(instrument.get("overview") or instrument.get("displayIdentity"))
        overview_company = _overview_company(raw_overview)
        broker_symbol = _clean_identity(instrument.get("brokerSymbol") or instrument.get("ticker"))
        queries = _resolution_queries(instrument, identity)
        logger.info(
            "yahoo_discovery_identity raw_overview=%r extracted_company=%r normalized_search=%r broker_symbol=%r",
            raw_overview, overview_company, queries[0] if queries else "", broker_symbol,
        )
        candidates_by_symbol: dict[str, dict[str, Any]] = {}
        provider_errors: list[str] = []
        for query_identity in queries:
            try:
                response = await self.client.get(self.search_url, params={"q": query_identity, "quotesCount": 10, "newsCount": 0})
                if response.status_code >= 400:
                    provider_errors.append(f"HTTP_{response.status_code}")
                    continue
                payload = response.json()
                candidates = payload.get("quotes") if isinstance(payload, dict) else None
                if not isinstance(candidates, list):
                    provider_errors.append("INVALID_RESPONSE")
                    continue
                for candidate in candidates:
                    if isinstance(candidate, dict) and candidate.get("symbol"):
                        candidates_by_symbol.setdefault(str(candidate["symbol"]), candidate)
            except Exception as exc:
                provider_errors.append(type(exc).__name__)
        candidates = list(candidates_by_symbol.values())
        if not candidates and provider_errors:
            raise StructuredProviderError(f"STRUCTURED_PROVIDER_UNAVAILABLE:{provider_errors[-1]}")
        comparison_identity = _comparison_company_name(instrument, identity)
        scored = sorted(
            ((_candidate_score(instrument, comparison_identity, candidate), candidate) for candidate in candidates),
            key=lambda item: item[0], reverse=True,
        )
        for score, candidate in scored:
            logger.info(
                "yahoo_candidate symbol=%r exchange=%r currency=%r quote_type=%r score=%.4f validation=%s",
                candidate.get("symbol"), candidate.get("exchange") or candidate.get("exchDisp"),
                candidate.get("currency"), candidate.get("quoteType"), score,
                _candidate_validation_reason(instrument, candidate, score,
                                             self.settings.structured_resolution_min_confidence),
            )
        if not scored or scored[0][0] < self.settings.structured_resolution_min_confidence:
            logger.info("yahoo_resolution_rejected reason=LOW_CONFIDENCE")
            raise StructuredProviderError("COMPANY_NOT_RESOLVED")
        if len(scored) > 1 and scored[0][0] - scored[1][0] < self.settings.structured_resolution_ambiguity_margin:
            logger.info("yahoo_resolution_rejected reason=AMBIGUOUS top_score=%.4f runner_up_score=%.4f",
                        scored[0][0], scored[1][0])
            raise StructuredProviderError("RESOLUTION_AMBIGUOUS")
        confidence, candidate = scored[0]
        quote_type = str(candidate.get("quoteType") or "").upper()
        if quote_type not in {"EQUITY", "STOCK", "ETF", "MUTUALFUND"}:
            raise StructuredProviderError("RESOLUTION_UNSUPPORTED_QUOTE_TYPE")
        return StructuredInstrumentResolution(
            instrument_id=_uuid(instrument.get("instrumentId")), provider=self.provider_name,
            provider_ticker=str(candidate.get("symbol")),
            company_name=str(candidate.get("longname") or candidate.get("shortname") or identity),
            exchange=str(candidate.get("exchange") or candidate.get("exchDisp") or "") or None,
            currency=str(candidate.get("currency") or "") or None, quote_type=quote_type,
            confidence=confidence, resolved_at=datetime.now(timezone.utc),
        )

    async def _resolve_verified_nse_candidate(self, instrument: dict[str, Any], identity: str,
                                              candidate_symbol: str) -> StructuredInstrumentResolution:
        try:
            response = await self.client.get(self.search_url, params={"q": candidate_symbol, "quotesCount": 10, "newsCount": 0})
            response.raise_for_status()
            quotes = response.json().get("quotes", [])
        except Exception as exc:
            logger.info("yahoo_mapping_resolution globalInstrumentId=%s candidateSource=VERIFIED_NSE candidate=%s outcome=PROVIDER_UNAVAILABLE reason=%s",
                        instrument.get("instrumentId"), candidate_symbol, type(exc).__name__)
            raise StructuredProviderError("STRUCTURED_PROVIDER_UNAVAILABLE") from exc
        matches = [item for item in quotes if isinstance(item, dict)
                   and str(item.get("symbol") or "").upper() == candidate_symbol]
        if len(matches) != 1:
            logger.info("yahoo_mapping_resolution globalInstrumentId=%s candidateSource=VERIFIED_NSE candidate=%s outcome=REJECTED reason=EXACT_SYMBOL_NOT_FOUND",
                        instrument.get("instrumentId"), candidate_symbol)
            raise StructuredProviderError("COMPANY_NOT_RESOLVED")
        candidate = matches[0]
        reason = _trusted_nse_candidate_reason(instrument, identity, candidate, candidate_symbol)
        if reason is not None:
            logger.info("yahoo_mapping_resolution globalInstrumentId=%s candidateSource=VERIFIED_NSE candidate=%s outcome=REJECTED reason=%s",
                        instrument.get("instrumentId"), candidate_symbol, reason)
            raise StructuredProviderError("COMPANY_NOT_RESOLVED")
        logger.info("yahoo_mapping_resolution globalInstrumentId=%s candidateSource=VERIFIED_NSE candidate=%s outcome=VALIDATED reason=NONE",
                    instrument.get("instrumentId"), candidate_symbol)
        return StructuredInstrumentResolution(
            instrument_id=_uuid(instrument.get("instrumentId")), provider=self.provider_name,
            provider_ticker=candidate_symbol, company_name=str(candidate.get("longname") or candidate.get("shortname") or identity),
            exchange=str(candidate.get("exchange") or candidate.get("exchDisp") or "") or None,
            currency=str(candidate.get("currency") or "") or None,
            quote_type=str(candidate.get("quoteType") or "").upper(), confidence=0.95,
            resolved_at=datetime.now(timezone.utc), status="VERIFIED_NSE_CANDIDATE",
        )

    async def _collect_resolved(self, resolution: StructuredInstrumentResolution) -> StructuredMarketSnapshot:
        if self.use_yfinance:
            return await asyncio.to_thread(self._collect_resolved_yfinance, resolution)
        return await self._collect_resolved_http(resolution)

    async def collect_verified(
        self, resolution: StructuredInstrumentResolution
    ) -> StructuredMarketSnapshot:
        """Collect through a caller-owned, verified provider mapping.

        This deliberately bypasses Yahoo search and never creates or updates a
        provider mapping. It is the narrow acquisition seam used by the
        first-party Yahoo MCP service.
        """
        if (
            resolution.provider.strip().upper() != self.provider_name
            or not resolution.provider_ticker.strip()
        ):
            raise StructuredProviderError("COMPANY_NOT_RESOLVED")
        return await self._collect_resolved(resolution)

    def _collect_resolved_yfinance(self, resolution: StructuredInstrumentResolution) -> StructuredMarketSnapshot:
        ticker = resolution.provider_ticker
        retrieved = datetime.now(timezone.utc)
        try:
            provider = self.ticker_factory(ticker)
            info = provider.info or {}
            raw_news = provider.news or []
        except Exception as exc:
            raise StructuredProviderError(f"STRUCTURED_PROVIDER_UNAVAILABLE:{type(exc).__name__}") from exc
        if not isinstance(info, dict):
            raise StructuredProviderError("STRUCTURED_PROVIDER_UNAVAILABLE:INVALID_INFO")
        _validate_returned_identity(
            resolution,
            returned_symbol=info.get("symbol"),
            returned_exchange=info.get("exchange") or info.get("fullExchangeName"),
        )
        returned_currency = str(info.get("currency") or "").upper()
        returned_type = _normalized_quote_type(
            info.get("quoteType") or resolution.quote_type,
            info.get("longName") or info.get("shortName") or resolution.company_name,
        )
        if resolution.currency and returned_currency and resolution.currency.upper() != returned_currency:
            raise StructuredProviderError("PERSISTED_MAPPING_CONFLICT:CURRENCY")
        if returned_type not in {"EQUITY", "STOCK", "ETF", "MUTUALFUND"}:
            raise StructuredProviderError("PERSISTED_MAPPING_CONFLICT:QUOTE_TYPE")
        income_statement = balance_sheet = quarterly_income = quarterly_balance = cashflow = quarterly_cashflow = None
        if returned_type in {"EQUITY", "STOCK"}:
            try:
                income_statement = getattr(provider, "income_stmt", None)
                balance_sheet = getattr(provider, "balance_sheet", None)
                quarterly_income = getattr(provider, "quarterly_income_stmt", None)
                quarterly_balance = getattr(provider, "quarterly_balance_sheet", None)
                cashflow = getattr(provider, "cashflow", None)
                quarterly_cashflow = getattr(provider, "quarterly_cashflow", None)
            except Exception:
                # Statement-derived ROCE is optional; quote retrieval remains usable.
                pass
        market_as_of = _timestamp(info.get("regularMarketTime"))
        source_url = f"https://finance.yahoo.com/quote/{quote(ticker, safe='')}"
        facts = _normalize_facts(info, ticker, source_url, retrieved, market_as_of)
        # Financial issuers still have reported statements; only industrial ROCE is inapplicable.
        roce = None if _is_financial_identity(info) else _normalize_roce(income_statement, balance_sheet, source_url, retrieved)
        if roce is not None:
            facts["roce"] = roce
        news = _normalize_yfinance_news(raw_news, retrieved)
        if "latestPrice" not in facts:
            raise StructuredProviderError("STRUCTURED_PRICE_UNAVAILABLE")
        normalized_resolution = resolution.model_copy(update={
            "company_name": str(info.get("longName") or resolution.company_name),
            "exchange": str(info.get("exchange") or resolution.exchange or "") or None,
            "currency": returned_currency or resolution.currency,
            "quote_type": returned_type,
        })
        return StructuredMarketSnapshot(
            resolution=normalized_resolution, status="STRUCTURED_PROVIDER_AVAILABLE", retrieved_at=retrieved,
            market_as_of=market_as_of, source_url=source_url, facts=facts,
            statement_facts=_normalize_statement_facts(income_statement, balance_sheet, cashflow, "ANNUAL", source_url, retrieved)
                + _normalize_statement_facts(quarterly_income, quarterly_balance, quarterly_cashflow, "QUARTERLY", source_url, retrieved), news=news,
            accepted_fields_count=len(facts) + len(news),
        )

    async def _collect_resolved_http(self, resolution: StructuredInstrumentResolution) -> StructuredMarketSnapshot:
        ticker = resolution.provider_ticker
        retrieved = datetime.now(timezone.utc)
        quote_payload: dict[str, Any] = {}
        summary_payload: dict[str, Any] = {}
        errors: list[str] = []
        news: list[dict[str, Any]] = []
        try:
            response = await self.client.get(self.quote_url, params={"symbols": ticker})
            response.raise_for_status()
            values = response.json().get("quoteResponse", {}).get("result", [])
            if values:
                quote_payload = values[0]
        except Exception as exc:
            errors.append(type(exc).__name__)
        try:
            modules = "price,summaryDetail,defaultKeyStatistics,financialData,assetProfile,calendarEvents,earnings,earningsHistory,earningsTrend,recommendationTrend,institutionOwnership,majorHoldersBreakdown"
            response = await self.client.get(self.summary_url.format(ticker=quote(ticker, safe="")), params={"modules": modules})
            response.raise_for_status()
            values = response.json().get("quoteSummary", {}).get("result", [])
            if values:
                summary_payload = values[0]
        except Exception as exc:
            errors.append(type(exc).__name__)
        try:
            response = await self.client.get(self.search_url, params={"q": ticker, "quotesCount": 0, "newsCount": 10})
            response.raise_for_status()
            for item in response.json().get("news", []):
                if not isinstance(item, dict) or not item.get("title") or not item.get("link"):
                    continue
                news.append({
                    "headline": str(item["title"]), "publisher": str(item.get("publisher") or "Yahoo Finance"),
                    "url": str(item["link"]), "publishedAt": _timestamp(item.get("providerPublishTime")),
                    "retrievedAt": retrieved, "sourceType": "STRUCTURED_MARKET_PROVIDER", "confidence": 0.72,
                })
        except Exception as exc:
            errors.append(type(exc).__name__)
        combined = _flatten_provider_payload(quote_payload, summary_payload)
        _validate_returned_identity(
            resolution,
            returned_symbol=combined.get("symbol"),
            returned_exchange=combined.get("exchange") or combined.get("fullExchangeName"),
        )
        returned_currency = str(combined.get("currency") or "").upper()
        returned_type = str(combined.get("quoteType") or "EQUITY").upper()
        if resolution.currency and returned_currency and resolution.currency.upper() != returned_currency:
            raise StructuredProviderError("PERSISTED_MAPPING_CONFLICT:CURRENCY")
        if returned_type not in {"EQUITY", "STOCK", "ETF", "MUTUALFUND"}:
            raise StructuredProviderError("PERSISTED_MAPPING_CONFLICT:QUOTE_TYPE")
        market_as_of = _timestamp(combined.get("regularMarketTime"))
        source_url = f"https://finance.yahoo.com/quote/{quote(ticker, safe='')}"
        facts = _normalize_facts(combined, ticker, source_url, retrieved, market_as_of)
        if not facts:
            raise StructuredProviderError("STRUCTURED_PROVIDER_UNAVAILABLE:NO_ACCEPTED_FIELDS")
        status = "STRUCTURED_PROVIDER_PARTIAL" if errors or len(facts) < 8 else "STRUCTURED_PROVIDER_AVAILABLE"
        return StructuredMarketSnapshot(
            resolution=resolution, status=status, retrieved_at=retrieved, market_as_of=market_as_of,
            source_url=source_url, facts=facts, news=news, accepted_fields_count=len(facts) + len(news),
            safe_error_code=errors[-1] if errors else None,
        )


def strongest_company_identity(instrument: dict[str, Any]) -> str:
    values = [
        _overview_company(instrument.get("overview") or instrument.get("displayIdentity")),
        instrument.get("canonicalName"), instrument.get("companyName"), instrument.get("isin"),
        instrument.get("brokerDescription"), instrument.get("displayName"),
        instrument.get("brokerSymbol"),
        instrument.get("canonicalSymbol"), instrument.get("ticker"),
    ]
    return next((_clean_identity(value) for value in values if _clean_identity(value)), "")


def _resolution_queries(instrument: dict[str, Any], primary: str) -> list[str]:
    overview_company = _overview_company(instrument.get("overview") or instrument.get("displayIdentity"))
    symbols = {_clean_identity(instrument.get(key)).casefold() for key in
               ("canonicalSymbol", "brokerSymbol", "ticker") if _clean_identity(instrument.get(key))}
    company_values = [overview_company, instrument.get("canonicalName"), instrument.get("companyName"),
                      instrument.get("brokerDescription"), instrument.get("displayName")]
    result: list[str] = []
    for value in company_values:
        cleaned = _clean_identity(value)
        if cleaned and cleaned.casefold() not in symbols and cleaned.casefold() not in {item.casefold() for item in result}:
            result.append(cleaned)
    if result:
        return result
    # Short symbols are discovery fallback only when no richer company identity exists.
    for value in (instrument.get("canonicalSymbol"), instrument.get("brokerSymbol"), instrument.get("ticker"), primary):
        cleaned = _clean_identity(value)
        if cleaned and cleaned.casefold() not in {item.casefold() for item in result}:
            result.append(cleaned)
    return result


def _comparison_company_name(instrument: dict[str, Any], fallback: str) -> str:
    for key in ("canonicalName", "companyName", "brokerDescription", "displayName"):
        value = _clean_identity(instrument.get(key))
        if value:
            return value
    overview = _clean_identity(_overview_company(instrument.get("overview")))
    return overview or fallback


def _legacy_overview_company(value: Any) -> str:
    cleaned = str(value or "").replace("\u00a0", " ")
    return re.split(r"(?:·|\u00c2\u00b7)", cleaned, maxsplit=1)[0]


def _clean_identity(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\u00a0", " ")).strip()


def _overview_company(value: Any) -> str:
    cleaned = str(value or "").replace("\u00a0", " ")
    # Handle the actual middle dot and its common UTF-8-as-Latin-1 mojibake form.
    return _clean_identity(re.split("(?:\\u00b7|\\u00c2\\u00b7)", cleaned, maxsplit=1)[0])


def _candidate_validation_reason(instrument: dict[str, Any], candidate: dict[str, Any], score: float,
                                 minimum_confidence: float) -> str:
    quote_type = str(candidate.get("quoteType") or "").upper()
    if quote_type not in {"EQUITY", "STOCK"}:
        return "REJECT_QUOTE_TYPE"
    expected_currency = str(instrument.get("tradingCurrency") or "").upper()
    candidate_currency = str(candidate.get("currency") or "").upper()
    if expected_currency and candidate_currency and expected_currency != candidate_currency:
        return "REJECT_CURRENCY"
    expected_listing = _exchange_family(_held_listing_exchange(instrument), "")
    candidate_listing = _exchange_family(candidate.get("exchange") or candidate.get("exchDisp"), candidate.get("symbol"))
    if expected_listing and candidate_listing and expected_listing != candidate_listing:
        return "REJECT_EXCHANGE"
    if score < 0.01:
        return "REJECT_IDENTITY_CONFLICT"
    return "IDENTITY_SCORE_ACCEPTABLE" if score >= minimum_confidence else "REJECT_LOW_CONFIDENCE"


def _trusted_nse_candidate(instrument: dict[str, Any]) -> str | None:
    candidate = _clean_identity(instrument.get("structuredNseCandidateTicker")).upper()
    if str(instrument.get("structuredNseCandidateSource") or "").upper() != "VERIFIED_NSE":
        return None
    return candidate if candidate.endswith(".NS") and len(candidate) > 3 else None


def _trusted_nse_candidate_reason(instrument: dict[str, Any], identity: str, candidate: dict[str, Any], expected_symbol: str) -> str | None:
    if str(candidate.get("symbol") or "").upper() != expected_symbol:
        return "SYMBOL_MISMATCH"
    if str(candidate.get("quoteType") or "").upper() not in {"EQUITY", "STOCK"}:
        return "QUOTE_TYPE_MISMATCH"
    candidate_listing = _exchange_family(candidate.get("exchange") or candidate.get("exchDisp"), expected_symbol)
    if candidate_listing != "XNSE":
        return "EXCHANGE_MISMATCH"
    expected_currency = str(instrument.get("tradingCurrency") or instrument.get("currency") or "").upper()
    candidate_currency = str(candidate.get("currency") or "").upper()
    if expected_currency and candidate_currency and expected_currency != candidate_currency:
        return "CURRENCY_MISMATCH"
    expected_isin = str(instrument.get("isin") or "").upper()
    candidate_isin = str(candidate.get("isin") or "").upper()
    if expected_isin and candidate_isin and expected_isin != candidate_isin:
        return "ISIN_MISMATCH"
    candidate_name = str(candidate.get("longname") or candidate.get("shortname") or "")
    if not candidate_name or _name_similarity(identity, candidate_name) < 0.55:
        return "COMPANY_NAME_MISMATCH"
    return None


def _candidate_score(instrument: dict[str, Any], identity: str, candidate: dict[str, Any]) -> float:
    if str(candidate.get("quoteType") or "").upper() not in {"EQUITY", "STOCK"}:
        return 0.0
    symbol = str(candidate.get("symbol") or "").upper()
    expected_symbols = {str(instrument.get(key) or "").upper() for key in ("canonicalSymbol", "brokerSymbol", "ticker")}
    expected_symbols.discard("")
    base_symbol = symbol.split(".", 1)[0]
    score = 0.15
    expected_isin = str(instrument.get("isin") or "").upper()
    candidate_isin = str(candidate.get("isin") or "").upper()
    if expected_isin and candidate_isin:
        if expected_isin != candidate_isin:
            return 0.0
        score += 0.60
    if symbol in expected_symbols or base_symbol in expected_symbols:
        score += 0.20
    candidate_name = str(candidate.get("longname") or candidate.get("shortname") or "")
    # Name establishes company identity, but cannot outweigh a conflicting known listing.
    score += 0.35 * _name_similarity(identity, candidate_name)
    expected_currency = str(instrument.get("tradingCurrency") or "").upper()
    candidate_currency = str(candidate.get("currency") or "").upper()
    if expected_currency and candidate_currency:
        if expected_currency != candidate_currency:
            return 0.0
        score += 0.10
    expected_country = str(instrument.get("country") or "").upper()
    expected_listing = _exchange_family(_held_listing_exchange(instrument), "")
    candidate_listing = _exchange_family(candidate.get("exchange") or candidate.get("exchDisp"), symbol)
    if expected_listing and candidate_listing and expected_listing != candidate_listing:
        return 0.0
    if expected_listing and candidate_listing == expected_listing:
        score += 0.40
    candidate_country = str(candidate.get("country") or candidate.get("region") or "").upper()
    if expected_country and candidate_country:
        if expected_country != candidate_country:
            return 0.0
        score += 0.05
    return min(score, 1.0)


def _held_listing_exchange(instrument: dict[str, Any]) -> str:
    # Broker listing exchange outranks SMART or other routing venues.
    routing = {"", "SMART", "BEST", "AUTO"}
    for key in ("brokerExchange", "primaryExchange", "listingExchange", "canonicalExchange", "exchange"):
        value = str(instrument.get(key) or "").upper()
        if value not in routing:
            return value
    return ""


def _exchange_family(exchange: Any, symbol: Any) -> str:
    value = str(exchange or "").upper().replace(" ", "")
    aliases = {
        "AEB": "XAMS", "AMS": "XAMS", "XAMS": "XAMS", "EURONEXTAMSTERDAM": "XAMS",
        "IBIS": "XETR", "IBIS2": "XETR", "XETR": "XETR", "GER": "XETR", "XETRA": "XETR",
        "FRA": "XFRA", "XFRA": "XFRA", "FRANKFURT": "XFRA",
        "NSE": "XNSE", "NSI": "XNSE", "XNSE": "XNSE",
        "BSE": "XBOM", "BOM": "XBOM", "XBOM": "XBOM",
        "NASDAQ": "XNAS", "NMS": "XNAS", "NGM": "XNAS", "NCM": "XNAS", "XNAS": "XNAS",
        "NYSE": "XNYS", "NYQ": "XNYS", "XNYS": "XNYS",
        "LSE": "XLON", "LONDON": "XLON", "XLON": "XLON",
    }
    return aliases.get(value, "")


def _validate_returned_identity(
    resolution: StructuredInstrumentResolution,
    *,
    returned_symbol: Any,
    returned_exchange: Any,
) -> None:
    symbol = _clean_identity(returned_symbol)
    if symbol and symbol.upper() != resolution.provider_ticker.upper():
        raise StructuredProviderError("PERSISTED_MAPPING_CONFLICT:SYMBOL")
    expected_family = _exchange_family(resolution.exchange, resolution.provider_ticker)
    returned_family = _exchange_family(returned_exchange, symbol or resolution.provider_ticker)
    if expected_family and returned_family and expected_family != returned_family:
        raise StructuredProviderError("PERSISTED_MAPPING_CONFLICT:EXCHANGE")


def _name_similarity(left: str, right: str) -> float:
    def normalized(value: str) -> str:
        value = re.sub(r"\b(limited|ltd|n\.?v\.?|ag|se|plc|inc|corp(?:oration)?)\b", " ", value.lower())
        return re.sub(r"[^a-z0-9]+", " ", value).strip()
    return SequenceMatcher(None, normalized(left), normalized(right)).ratio()


def _flatten_provider_payload(quote_payload: dict[str, Any], summary_payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(quote_payload)
    for module in summary_payload.values():
        if isinstance(module, dict):
            for key, value in module.items():
                if key not in result or result[key] is None:
                    result[key] = value
    return result


FIELD_MAP = {
    "regularMarketPreviousClose": ("previousClose", "currency"),
    "bid": ("bid", "currency"), "bidSize": ("bidSize", "shares"),
    "ask": ("ask", "currency"), "askSize": ("askSize", "shares"),
    "regularMarketVolume": ("volume", "shares"), "fiftyTwoWeekHigh": ("fiftyTwoWeekHigh", "currency"),
    "fiftyTwoWeekLow": ("fiftyTwoWeekLow", "currency"), "marketCap": ("marketCap", "currency"),
    "enterpriseValue": ("enterpriseValue", "currency"), "trailingPE": ("trailingPE", "ratio"),
    "forwardPE": ("forwardPE", "ratio"), "priceToBook": ("priceToBook", "ratio"),
    "pegRatio": ("pegRatio", "ratio"), "bookValue": ("bookValue", "currency"),
    "priceToSalesTrailing12Months": ("priceToSales", "ratio"),
    "enterpriseToRevenue": ("evToRevenue", "ratio"), "enterpriseToEbitda": ("evToEbitda", "ratio"),
    "trailingEps": ("trailingEps", "currency"), "forwardEps": ("forwardEps", "currency"),
    "averageDailyVolume10Day": ("averageVolume10Day", "shares"), "averageVolume": ("averageVolume", "shares"),
    "targetLowPrice": ("publicAnalystTargetLowPrice", "currency"),
    "targetMedianPrice": ("publicAnalystTargetMedianPrice", "currency"),
    "targetHighPrice": ("publicAnalystTargetHighPrice", "currency"),
    "recommendationMean": ("publicAnalystRecommendationMean", "ratio"),
    "navPrice": ("navPrice", "currency"), "returnOnEquity": ("roe", "percent"),
    "returnOnAssets": ("roa", "percent"), "profitMargins": ("profitMargin", "percent"),
    "operatingMargins": ("operatingMargin", "percent"), "revenueGrowth": ("revenueGrowth", "percent"),
    "earningsGrowth": ("earningsGrowth", "percent"), "totalCash": ("totalCash", "currency"),
    "totalDebt": ("totalDebt", "currency"), "debtToEquity": ("debtToEquity", "ratio"),
    "freeCashflow": ("freeCashFlow", "currency"), "operatingCashflow": ("operatingCashFlow", "currency"),
    "targetMeanPrice": ("publicAnalystTargetMeanPrice", "currency"), "numberOfAnalystOpinions": ("publicAnalystCount", "count"),
    "heldPercentInsiders": ("insidersPercent", "percent"), "heldPercentInstitutions": ("institutionsPercent", "percent"),
}


def _normalize_facts(payload: dict[str, Any], ticker: str, source_url: str, retrieved: datetime, market_as_of: datetime | None) -> dict[str, ProvenancedValue]:
    facts: dict[str, ProvenancedValue] = {}
    currency = str(payload.get("currency") or "") or None
    price = _decimal(_raw(payload.get("currentPrice")))
    if price is None:
        price = _decimal(_raw(payload.get("regularMarketPrice")))
    if price is not None and price > 0:
        facts["latestPrice"] = ProvenancedValue(value=price, unit=currency, as_of_date=market_as_of,
            source_url=source_url, source_name="Yahoo Finance", source_type="STRUCTURED_MARKET_PROVIDER",
            retrieved_at=retrieved, confidence=0.90)
    for provider_key, (metric, unit_kind) in FIELD_MAP.items():
        value = _raw(payload.get(provider_key))
        number = _decimal(value)
        if number is None:
            continue
        if metric in {"bid", "ask", "bidSize", "askSize"} and number <= 0:
            continue
        if unit_kind == "percent" and abs(number) <= 1:
            number *= Decimal("100")
        unit = currency if unit_kind == "currency" else unit_kind
        facts[metric] = ProvenancedValue(value=number, unit=unit, as_of_date=market_as_of,
            source_url=source_url, source_name="Yahoo Finance", source_type="STRUCTURED_MARKET_PROVIDER",
            retrieved_at=retrieved, confidence=0.82)
    text_fields = {
        "longName": "providerCompanyName", "sector": "sector", "industry": "industry",
        "longBusinessSummary": "businessSummary", "recommendationKey": "publicAnalystConsensus",
    }
    for provider_key, metric in text_fields.items():
        value = _raw(payload.get(provider_key))
        if value not in (None, ""):
            facts[metric] = ProvenancedValue(value=str(value), as_of_date=market_as_of, source_url=source_url,
                source_name="Yahoo Finance", source_type="STRUCTURED_MARKET_PROVIDER", retrieved_at=retrieved, confidence=0.78)
    earnings = payload.get("quarterly") or payload.get("quarterlyChart")
    if isinstance(earnings, list) and earnings:
        normalized_earnings = []
        for item in earnings:
            if not isinstance(item, dict):
                continue
            normalized_earnings.append({
                "period": item.get("date") or item.get("period"),
                "earnings": _raw(item.get("earnings")), "revenue": _raw(item.get("revenue")),
                "epsActual": _raw(item.get("epsActual")), "epsEstimate": _raw(item.get("epsEstimate")),
                "epsDifference": _raw(item.get("epsDifference")), "surprisePercent": _raw(item.get("surprisePercent")),
            })
        if normalized_earnings:
            facts["earningsHistory"] = ProvenancedValue(value=normalized_earnings, source_url=source_url,
                source_name="Yahoo Finance", source_type="STRUCTURED_MARKET_PROVIDER", retrieved_at=retrieved, confidence=0.74)
    facts["providerTicker"] = ProvenancedValue(value=ticker, source_url=source_url, source_name="Yahoo Finance",
        source_type="STRUCTURED_MARKET_PROVIDER", retrieved_at=retrieved, confidence=0.95)
    return facts


def _normalize_yfinance_news(raw_news: Any, retrieved: datetime) -> list[dict[str, Any]]:
    normalized = []
    for item in raw_news if isinstance(raw_news, list) else []:
        if not isinstance(item, dict):
            continue
        content = item.get("content") if isinstance(item.get("content"), dict) else item
        title = content.get("title")
        provider = content.get("provider") if isinstance(content.get("provider"), dict) else {}
        canonical = content.get("canonicalUrl") if isinstance(content.get("canonicalUrl"), dict) else {}
        url = content.get("link") or canonical.get("url")
        if not title or not url:
            continue
        normalized.append({"headline": str(title), "publisher": str(content.get("publisher") or provider.get("displayName") or "Yahoo Finance"),
            "url": str(url), "publishedAt": _timestamp(content.get("providerPublishTime") or content.get("pubDate")),
            "retrievedAt": retrieved, "sourceType": "STRUCTURED_MARKET_PROVIDER", "confidence": 0.72})
    return sorted(normalized, key=lambda article: article.get("publishedAt") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)


def _normalize_roce(income_statement: Any, balance_sheet: Any, source_url: str,
                    retrieved: datetime) -> ProvenancedValue | None:
    """Calculate annual ROCE as EBIT / (total assets - current liabilities).

    A value is returned only when all three statement inputs exist for the same
    annual period and capital employed is positive. No proxy ratios are used.
    """
    if income_statement is None or balance_sheet is None:
        return None
    try:
        income_columns = list(income_statement.columns)
        balance_columns = set(balance_sheet.columns)
        period = next((column for column in income_columns if column in balance_columns), None)
        if period is None:
            return None
        ebit = _statement_value(income_statement, period, ("EBIT", "Operating Income"))
        assets = _statement_value(balance_sheet, period, ("Total Assets",))
        current_liabilities = _statement_value(
            balance_sheet, period, ("Current Liabilities", "Total Current Liabilities"))
        if ebit is None or assets is None or current_liabilities is None:
            return None
        capital_employed = assets - current_liabilities
        if capital_employed <= 0:
            return None
        value = (ebit / capital_employed) * Decimal("100")
        period_text = period.isoformat() if hasattr(period, "isoformat") else str(period)
        return ProvenancedValue(
            value=value, unit="percent", as_of_date=_coerce_period_datetime(period), period=period_text,
            source_url=source_url, source_name="Yahoo Finance", source_type="STRUCTURED_FINANCIAL_STATEMENTS",
            retrieved_at=retrieved, confidence=0.78,
            calculation_basis="EBIT / (total assets - current liabilities), latest common annual period",
        )
    except (AttributeError, KeyError, TypeError, InvalidOperation, ValueError):
        return None


def _normalize_statement_facts(income: Any, balance: Any, cashflow: Any, period_type: str, source_url: str, retrieved: datetime) -> list[dict[str, Any]]:
    """Normalize only explicit yfinance statement rows and dated columns.

    Yahoo does not expose standalone/consolidated basis here, so UNKNOWN is a
    deliberate canonical isolation value rather than an implied equivalence.
    """
    mappings = (
        (income, {"Total Revenue": "revenue", "Net Income": "pat", "EBITDA": "ebitda", "Pretax Income": "pbt", "Tax Provision": "tax", "Interest Expense": "finance_cost", "Diluted EPS": "eps"}),
        (balance, {"Cash Cash Equivalents And Short Term Investments": "cash_and_equivalents", "Cash And Cash Equivalents": "cash_and_equivalents", "Total Debt": "debt_or_borrowings", "Total Assets": "total_assets", "Total Liabilities Net Minority Interest": "total_liabilities", "Stockholders Equity": "equity", "Accounts Receivable": "receivables", "Inventory": "inventory"}),
        (cashflow, {"Operating Cash Flow": "operating_cash_flow", "Investing Cash Flow": "investing_cash_flow", "Financing Cash Flow": "financing_cash_flow", "Capital Expenditure": "capex"}),
    )
    normalized: list[dict[str, Any]] = []
    for frame, labels in mappings:
        if frame is None or not hasattr(frame, "columns") or not hasattr(frame, "index"):
            continue
        for period in frame.columns:
            period_end = period.isoformat() if hasattr(period, "isoformat") else str(period)
            if not period_end:
                continue
            for label, metric in labels.items():
                if label not in frame.index:
                    continue
                value = _decimal(frame.loc[label, period])
                if value is None:
                    continue
                normalized.append({"metric": metric, "value": value, "periodEnd": period_end, "periodType": period_type,
                                   "reportingBasis": "UNKNOWN", "sourceUrl": source_url, "sourceName": "Yahoo Finance",
                                   "sourceType": "STRUCTURED_FINANCIAL_STATEMENTS", "retrievedAt": retrieved,
                                   "confidence": 0.78, "rawFieldOrigin": label})
    return normalized


def _is_financial_identity(info: dict[str, Any]) -> bool:
    """Banks and financials do not receive industrial EBIT/ROCE semantics."""
    identity = " ".join(str(info.get(key) or "") for key in ("sector", "industry", "longName", "shortName")).lower()
    return bool(re.search(r"\b(bank|financial|insurance|credit|lending|asset management)\b", identity))


def _statement_value(frame: Any, period: Any, labels: tuple[str, ...]) -> Decimal | None:
    for label in labels:
        try:
            value = frame.loc[label, period]
        except (KeyError, TypeError):
            continue
        number = _decimal(value)
        if number is not None:
            return number
    return None


def _coerce_period_datetime(period: Any) -> datetime | None:
    try:
        value = period.to_pydatetime() if hasattr(period, "to_pydatetime") else period
        if isinstance(value, datetime):
            return value.replace(tzinfo=value.tzinfo or timezone.utc)
    except (TypeError, ValueError):
        pass
    return None


def _raw(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("raw", value.get("fmt"))
    return value


def _normalized_quote_type(provider_type: Any, validated_name: Any) -> str:
    quote_type = str(provider_type or "").upper()
    name_tokens = {token.strip(".,()[]-").upper() for token in str(validated_name or "").split()}
    # Yahoo labels some exchange-traded funds as EQUITY. The validated provider
    # identity is stronger evidence when its actual security name explicitly says ETF.
    if quote_type in {"EQUITY", "STOCK"} and "ETF" in name_tokens:
        return "ETF"
    return quote_type


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value)) if value is not None and not isinstance(value, bool) else None
        return number if number is not None and number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _timestamp(value: Any) -> datetime | None:
    value = _raw(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc) if value is not None else None
    except (ValueError, TypeError, OSError):
        return None


def _uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except ValueError:
        return None


def _identity_key(instrument: dict[str, Any], identity: str) -> str:
    return "|".join(str(instrument.get(key) or "").upper() for key in
                    ("instrumentId", "isin", "provider", "providerInstrumentId", "structuredProviderTicker")) + "|" + identity.upper()
