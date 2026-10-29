"""NSE ETF snapshot contract verified against read-only responses on 2026-10-11.

Market Watch has a session timestamp, not per-instrument last-trade timestamps.
getYearwiseData contains periodic percentage returns and is NOT a candle source.
No cookies, credentials, provider URL overrides, or automatic provider fallback.
"""
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import httpx

from app.etf_evidence import EtfAuthority, EtfFact, EtfMetric, EtfNavObservation, EtfProvenance

logger = logging.getLogger(__name__)

MARKET_URL = "https://www.nseindia.com/api/NextApi/apiClient/marketWatchApi?functionName=getETFData&parent=all&child=all"
META_URL = "https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi"
HEADERS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0",
           "Referer": "https://www.nseindia.com/"}
IST = timezone(timedelta(hours=5, minutes=30))
MONTHS = {name: i for i, name in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}


class NseContractError(ValueError):
    """Safe, static provider validation code suitable for durable attempts."""


def failure_code(exc):
    if isinstance(exc, httpx.HTTPStatusError):
        return f"NSE_HTTP_{exc.response.status_code}"
    return str(exc) if isinstance(exc, NseContractError) else type(exc).__name__


def nse_date(value):
    day, month, year = value.split("-")
    return datetime(int(year), MONTHS[month], int(day), tzinfo=IST)


def numeric(value, *, positive=False, integer=False):
    if value is None or value == "-" or value == "":
        return None
    if isinstance(value, bool):
        raise NseContractError("INVALID_NSE_NUMBER")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise NseContractError("INVALID_NSE_NUMBER") from None
    if (not number.is_finite() or number < 0 or (positive and number == 0)
            or (integer and number != number.to_integral_value())):
        raise NseContractError("INVALID_NSE_NUMBER")
    return number


async def get_json(client, url, *, params=None):
    # One attempt, fixed official host, no redirect; outer API serializes batches.
    response = await client.get(url, params=params, headers=HEADERS, timeout=15, follow_redirects=False)
    response.raise_for_status()
    if len(response.content) > 3_000_000:
        raise NseContractError("NSE_RESPONSE_TOO_LARGE")
    return response.json()


def market_snapshot(payload, listings, now, *, requested_symbols=None):
    """``requested_symbols``, when given, is the set of normalized official
    symbols for the specific bounded instruments being ingested (the only
    real caller, app.etf_ingestion.ingest_quotes, is always limited to 1-5
    instrument_ids -- see MAX_ETF_INGESTION_CANDIDATES). getETFData's
    parent=all&child=all live feed and the official ETF security list both
    cover the ENTIRE NSE ETF universe; prior to this scoping, one unrelated
    symbol's listing/duplicate/format problem anywhere in that shared
    universe aborted this call for every bounded request regardless of
    which instrument it was actually about (confirmed: GOLDBEES's own
    discovery/identity checks succeeded independently, yet MARKET_PRICE/
    TRADING_VOLUME/NAV all failed identically via this one shared call).
    Rows/listings outside ``requested_symbols`` are skipped, never fatal;
    every requested instrument still gets the exact same strict, fail-closed
    uniqueness and format checks as before -- no ambiguous match is ever
    chosen. When ``requested_symbols`` is None (omitted), behavior is
    unchanged from before this parameter existed: every row is validated.
    """
    envelope = payload["data"]
    rows = envelope["data"]
    if not isinstance(rows, list) or not rows:
        raise NseContractError("EMPTY_NSE_MARKET_SNAPSHOT")
    day, time = envelope["timestamp"].split(" ")
    stamp = nse_date(day).replace(**dict(zip(("hour", "minute", "second"), map(int, time.split(":")))))
    if stamp > now:
        raise NseContractError("FUTURE_NSE_SESSION")
    by_symbol = {}
    for listing in listings:
        if requested_symbols is not None and listing.symbol not in requested_symbols:
            continue
        if listing.symbol in by_symbol:
            logger.warning("etf_market_snapshot_rejected symbol=%s category=%s", listing.symbol, "AMBIGUOUS_OFFICIAL_SYMBOL")
            raise NseContractError("AMBIGUOUS_OFFICIAL_SYMBOL")
        by_symbol[listing.symbol] = listing
    selected = {}
    for row in rows:
        # The official ETF security-list CSV (parse_nse_etf_list, above) is
        # normalized to .strip().upper() before becoming a by_symbol key.
        # The live market-watch feed's own "symbol" field was compared to
        # that normalized key WITHOUT equivalent normalization -- any
        # incidental case or whitespace difference between NSE's two
        # separately-maintained feeds for the same instrument would
        # therefore raise UNVERIFIED_OR_DUPLICATE_MARKET_SYMBOL even though
        # the instrument is genuinely verified and unambiguous. Normalizing
        # both sides the same way closes that gap without weakening the
        # check itself: an actually-unknown or actually-duplicate symbol
        # is still rejected exactly as before.
        symbol = str(row["symbol"]).strip().upper()
        if requested_symbols is not None and symbol not in requested_symbols:
            continue
        if symbol in selected or symbol not in by_symbol:
            logger.warning("etf_market_snapshot_rejected symbol=%s category=%s", symbol, "UNVERIFIED_OR_DUPLICATE_MARKET_SYMBOL")
            raise NseContractError("UNVERIFIED_OR_DUPLICATE_MARKET_SYMBOL")
        if row["series"] != "EQ" or row["identifier"] != symbol + "EQN":
            logger.warning("etf_market_snapshot_rejected symbol=%s category=%s", symbol, "UNSUPPORTED_NSE_LISTING")
            raise NseContractError("UNSUPPORTED_NSE_LISTING")
        selected[symbol] = row
    # A missing watch row is an explicit per-instrument failure, never a fake quote.
    return stamp, selected, {k: v for k, v in envelope.items() if k != "data"}


def verify_metadata(meta, row):
    if (meta.get("symbol") != row["primarySymbol"] or meta.get("isin") != row["isin"]
            or meta.get("isETFSec") != "true" or meta.get("isDelisted") != "false"
            or meta.get("isSuspended") != "false" or "EQ" not in meta.get("activeSeries", [])
            or "EQ" in meta.get("tempSuspendedSeries", []) or meta.get("marketType") != "N"
            or row.get("currency") != "INR"):
        raise NseContractError("NSE_METADATA_IDENTITY_CONFLICT")


def evidence(profile, metric, row, stamp, envelope, metadata, now):
    field = {EtfMetric.MARKET_PRICE: "lastPrice", EtfMetric.TRADING_VOLUME: "totalTradedVolume",
             EtfMetric.NAV: "nav"}[metric]
    value = numeric(row.get(field), positive=metric != EtfMetric.TRADING_VOLUME,
                    integer=metric == EtfMetric.TRADING_VOLUME)
    if value is None:
        return None
    if metric == EtfMetric.MARKET_PRICE and not numeric(row.get("totalTradedVolume"), integer=True):
        # Zero-trade session can carry forward an older lastPrice with unknown date.
        return None
    effective = nse_date(envelope["navDate"]) if metric == EtfMetric.NAV else stamp
    if effective > now or effective.date() > stamp.date():
        raise NseContractError("FUTURE_NSE_EVIDENCE")
    provenance = EtfProvenance(provider="NSE", source_type="NSE_ETF_MARKET_WATCH",
        source_identity=f"{row['identifier']}:{field}:{effective.isoformat()}", source_url=MARKET_URL,
        source_locator=f"data.data[symbol={profile.ticker}].{field}",
        retrieved_at=now, authority=EtfAuthority.OFFICIAL_EXCHANGE, reliability_level="LEVEL_A",
        source_metadata={"session": envelope, "record": row, "metadata": metadata,
                         "metadata_source": {"url": META_URL, "functionName": "getMetaData", "symbol": profile.ticker},
                         "timestamp_basis": "NSE_NAV_DATE" if metric == EtfMetric.NAV else "NSE_MARKET_SESSION_IST"})
    if metric == EtfMetric.NAV:
        return EtfNavObservation(instrument_id=profile.instrument_id, nav=value, currency="INR",
                                 nav_date=effective.date(), provenance=provenance)
    return EtfFact(instrument_id=profile.instrument_id, metric=metric, value=value,
        unit="shares" if metric == EtfMetric.TRADING_VOLUME else "INR",
        as_of_date=effective.date(), provenance=provenance)
