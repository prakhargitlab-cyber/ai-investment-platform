"""Select one source of persisted ETF closes; never merge venues/providers.

This is an evidence boundary around the existing pure mathematical functions.
It does not fetch, fill candles, infer corporate actions, or change their weights.
"""
from collections import Counter, defaultdict
from datetime import date
from urllib.parse import parse_qsl, unquote, urlsplit

from app.market_sessions import session_bounds
from app.models import DailyMarketBar


HISTORY_PROVIDERS = ("NSE", "YAHOO_FINANCE", "YAHOO_FINANCE_MCP")
SOURCE_HOSTS = {
    "NSE": {"www.nseindia.com", "nseindia.com", "nsearchives.nseindia.com", "archives.nseindia.com"},
    "YAHOO_FINANCE": {"finance.yahoo.com", "www.finance.yahoo.com", "query1.finance.yahoo.com", "query2.finance.yahoo.com"},
    "YAHOO_FINANCE_MCP": {"finance.yahoo.com", "www.finance.yahoo.com", "query1.finance.yahoo.com", "query2.finance.yahoo.com"},
}


# Explicit "not adjusted" spellings. This is the same vocabulary as
# session_history.history_symbol, which cannot be reused directly: it also
# enforces a single host and the symbol shape (stricter than this selector's
# accepted hosts), lets a fragment silently override the query string, and
# ignores blank or valueless flags. Provenance here must fail closed.
_UNADJUSTED_FLAG_VALUES = frozenset({"false", "0", "unadjusted"})


def yahoo_price_provenance_is_unadjusted(source_url):
    """True unless the URL carries adjusted, malformed or ambiguous adjustment evidence.

    Any query/fragment/path parameter that mentions an adjustment (``auto_adjust``,
    ``adjusted``, ``adjclose`` ...) must appear exactly once with an explicit false
    value. A URL with no adjustment indicator keeps the legacy Yahoo /history
    contract (``auto_adjust=False``); this check never upgrades a source to verified.
    """
    try:
        parts = urlsplit(source_url)
        segments = [unquote(part).lower() for part in parts.path.split("/") if part]
        # A ticker such as ADJUSTCO.NS legitimately contains "adj": skip the symbol segment.
        symbol_at = segments.index("quote") + 1 if "quote" in segments else -1
        if (";" in parts.path or ";" in parts.query or ";" in parts.fragment
                or any("adj" in part for i, part in enumerate(segments) if i != symbol_at)):
            return False
        seen = set()
        for text in (parts.query, parts.fragment):
            for key, value in parse_qsl(text, keep_blank_values=True, strict_parsing=False):
                name = key.strip().lower().replace("-", "_")
                if "adj" not in name:
                    continue
                if name in seen or value.strip().lower() not in _UNADJUSTED_FLAG_VALUES:
                    return False
                seen.add(name)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def select_etf_history_bars(bars, instrument_id, *, now=None, provider=None,
                           canonical_symbol=None, schedules=None, exceptions=None,
                           require_calendar=False):
    if provider is not None and provider not in HISTORY_PROVIDERS:
        raise ValueError("UNSUPPORTED_ETF_HISTORY_PROVIDER")
    if now is not None and (now.tzinfo is None or now.utcoffset() is None):
        raise ValueError("AWARE_HISTORY_CLOCK_REQUIRED")
    if require_calendar and now is None:
        raise ValueError("HISTORY_SESSION_CLOCK_REQUIRED")
    calendar_known = bool(schedules)
    rejected = Counter()
    grouped = defaultdict(list)
    for raw in bars:
        try:
            bar = DailyMarketBar.model_validate(raw.model_dump())
        except (ValueError, TypeError, AttributeError):
            rejected["INVALID_PERSISTED_BAR"] += 1
            continue
        reason = None
        if str(bar.global_instrument_id) != str(instrument_id):
            reason = "INSTRUMENT_ID_MISMATCH"
        elif bar.source_mode != "REAL":
            reason = "NON_REAL_HISTORY"
        elif bar.currency != "INR":
            reason = "CURRENCY_MISMATCH"
        elif bar.provider not in HISTORY_PROVIDERS:
            reason = "UNSUPPORTED_HISTORY_PROVIDER"
        elif bar.close is None:
            reason = "CLOSE_NOT_RECORDED"
        elif now is not None and (bar.trading_date > now.date() or bar.retrieved_at > now):
            reason = "FUTURE_HISTORY"
        if reason is None:
            try:
                source = urlsplit(bar.source_url)
                if (source.scheme != "https" or source.hostname not in SOURCE_HOSTS[bar.provider]
                        or source.username is not None or source.password is not None
                        or source.port not in (None, 443)):
                    reason = "UNVERIFIED_HISTORY_SOURCE"
                elif (bar.provider in ("YAHOO_FINANCE", "YAHOO_FINANCE_MCP")
                        and not yahoo_price_provenance_is_unadjusted(bar.source_url)):
                    reason = "ADJUSTED_OR_AMBIGUOUS_PRICE_PROVENANCE"
            except ValueError:
                reason = "UNVERIFIED_HISTORY_SOURCE"
        if reason is None and canonical_symbol is not None:
            expected = {canonical_symbol} if bar.provider == "NSE" else {canonical_symbol + ".NS"}
            if bar.provider_symbol not in expected:
                reason = "PROVIDER_SYMBOL_MISMATCH"
        if reason is None and require_calendar:
            bounds = session_bounds("NSE", schedules or [], exceptions or [], bar.trading_date)
            if not calendar_known:
                reason = "HISTORY_SESSION_CALENDAR_UNAVAILABLE"
            elif bounds is None:
                reason = "NOT_A_CONFIGURED_TRADING_SESSION"
            elif bounds[1] > now or bar.retrieved_at < bounds[1]:
                reason = "UNCOMPLETED_SESSION_OBSERVATION"
        if reason:
            rejected[reason] += 1
        else:
            grouped[bar.provider].append(bar)

    # Selection is explicit and deterministic, independent of row ordering or
    # which provider happens to have the longest series. A caller can request a
    # different source; a chart never silently switches source between pages.
    selected_provider = provider or next((p for p in HISTORY_PROVIDERS if grouped[p]), None)
    selected = grouped.get(selected_provider, [])
    dates = defaultdict(list)
    for bar in selected:
        dates[bar.trading_date].append(bar)
    clean = []
    conflicts = []
    for trading_date, values in sorted(dates.items()):
        signatures = {(b.close, b.open, b.high, b.low, b.volume, b.provider_symbol) for b in values}
        if len(signatures) != 1:
            conflicts.append(trading_date.isoformat())
            rejected["CONFLICTING_PROVIDER_SESSION"] += len(values)
            continue
        clean.append(max(values, key=lambda b: b.retrieved_at))
    warnings = ["CORPORATE_ACTION_ADJUSTMENT_UNVERIFIED", "UNADJUSTED_PRICE_NOT_TOTAL_RETURN"]
    if len([p for p in HISTORY_PROVIDERS if grouped[p]]) > 1:
        warnings.append("MULTIPLE_PROVIDERS_KEPT_SEPARATE")
    if require_calendar and not calendar_known:
        warnings.append("HISTORY_SESSION_CALENDAR_UNAVAILABLE")
    if conflicts:
        warnings.append("CONFLICTING_PROVIDER_SESSIONS_EXCLUDED")
    return clean, {
        "provider": selected_provider,
        "price_basis": "UNADJUSTED_CLOSE",
        "price_basis_source": "EXISTING_ADAPTER_CONTRACT",
        "calendar_state": "PERSISTED_SESSION_SCHEDULE" if require_calendar and calendar_known else "NOT_VERIFIED",
        "accepted_rows": len(clean), "rejected_rows": sum(rejected.values()),
        "rejection_reasons": dict(sorted(rejected.items())),
        "provider_rows": {p: len(grouped[p]) for p in HISTORY_PROVIDERS if grouped[p]},
        "conflicting_dates": conflicts, "warnings": warnings,
    }
