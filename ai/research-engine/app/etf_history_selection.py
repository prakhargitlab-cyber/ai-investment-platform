"""Select one source of persisted ETF closes; never merge venues/providers.

This is an evidence boundary around the existing pure mathematical functions.
It does not fetch, fill candles, infer corporate actions, or change their weights.
"""
from collections import Counter, defaultdict
from datetime import date
from urllib.parse import urlsplit

from app.market_sessions import session_bounds
from app.models import DailyMarketBar


HISTORY_PROVIDERS = ("NSE", "YAHOO_FINANCE", "YAHOO_FINANCE_MCP")
SOURCE_HOSTS = {
    "NSE": {"www.nseindia.com", "nseindia.com", "nsearchives.nseindia.com", "archives.nseindia.com"},
    "YAHOO_FINANCE": {"finance.yahoo.com", "www.finance.yahoo.com", "query1.finance.yahoo.com", "query2.finance.yahoo.com"},
    "YAHOO_FINANCE_MCP": {"finance.yahoo.com", "www.finance.yahoo.com", "query1.finance.yahoo.com", "query2.finance.yahoo.com"},
}


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
