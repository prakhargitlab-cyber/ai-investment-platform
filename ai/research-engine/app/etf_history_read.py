"""Bounded DB-only chart projection; no acquisition or evidence persistence."""
from datetime import date, datetime, timedelta, timezone

from app.etf_history_selection import HISTORY_PROVIDERS, select_etf_history_bars
from app.market_sessions import completed_sessions


def history_range(name, *, now, start=None, end=None):
    if name not in {"1Y", "3Y", "5Y", "MAX", "CUSTOM"}:
        raise ValueError("INVALID_HISTORY_RANGE")
    if name == "CUSTOM":
        if start is None or end is None:
            raise ValueError("CUSTOM_HISTORY_DATES_REQUIRED")
    elif start is not None or end is not None:
        raise ValueError("CUSTOM_HISTORY_RANGE_REQUIRED")
    else:
        end = now.date()
        if name == "MAX":
            start = None
        else:
            years = int(name[0])
            # Calendar years, with an explicit leap-day clamp; never turn a
            # nominal year into a fabricated number of observed sessions.
            try:
                start = end.replace(year=end.year - years)
            except ValueError:
                start = end.replace(year=end.year - years, day=28)
    if (end > now.date() or start is not None and (start > end or start < date(1970, 1, 1))):
        raise ValueError("INVALID_HISTORY_DATE_RANGE")
    return start, end


def serialize_bar(bar, *, gap_before=False):
    return {
        "date": bar.trading_date.isoformat(),
        **{key: str(getattr(bar, key)) if getattr(bar, key) is not None else None
           for key in ("open", "high", "low", "close")},
        "volume": str(bar.volume) if bar.volume is not None else None, "gap_before": gap_before,
        "provider": bar.provider, "provider_symbol": bar.provider_symbol,
        "currency": bar.currency, "source_url": bar.source_url,
        "retrieved_at": bar.retrieved_at.isoformat(),
    }


def read_etf_history(store, instrument_id, canonical, *, range_name="1Y", start=None, end=None,
                     after=None, limit=500, provider=None, now=None):
    now = now or datetime.now(timezone.utc)
    start, end = history_range(range_name, now=now, start=start, end=end)
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("INVALID_HISTORY_PAGE_SIZE")
    if provider is not None and provider not in HISTORY_PROVIDERS:
        raise ValueError("UNSUPPORTED_ETF_HISTORY_PROVIDER")
    if after is not None and (type(after) is not date or after > end or start is not None and after < start):
        raise ValueError("INVALID_HISTORY_CURSOR")
    sources = store.daily_market_history_sources(instrument_id, end_date=now.date())
    available_providers = {s["provider"] for s in sources}
    selected_provider = provider or next((p for p in HISTORY_PROVIDERS if p in available_providers), None)
    schedules = store.load_market_schedules({"NSE", "XNSE"})
    exceptions = store.load_market_calendar_exceptions({"NSE", "XNSE"})
    selection = dict(now=now, provider=selected_provider, canonical_symbol=canonical["primarySymbol"],
                     schedules=schedules, exceptions=exceptions, require_calendar=True)
    rows = store.daily_market_history_page(instrument_id, provider=selected_provider,
        start_date=start, end_date=end, after=after, limit=limit + 1) if selected_provider else []
    # Cursor advances over SCANNED rows, including excluded rows, so a bad row
    # cannot make a page loop forever. It is bound to explicit provider/range.
    has_more = len(rows) > limit
    scanned = rows[:limit]
    bars, diagnostics = select_etf_history_bars(scanned, instrument_id, **selection)
    warmup = []
    context = []
    context_end = after if after is not None else start-timedelta(days=1) if start else None
    if context_end is not None and selected_provider:
        previous = store.daily_market_history_page(instrument_id, provider=selected_provider,
            start_date=None, end_date=context_end, limit=199, descending=True)
        context, warmup_diagnostics = select_etf_history_bars(previous, instrument_id, **selection)
        warmup = context if after is None else []
        if warmup_diagnostics["rejected_rows"]:
            diagnostics["warnings"].append("WARMUP_OBSERVATIONS_EXCLUDED")
    gaps = {}
    previous_date = None
    for bar in context + bars:
        gap = bool(after is not None and previous_date is None and not context)
        if previous_date is not None and schedules:
            expected = sum(1 for _ in completed_sessions("NSE", schedules, exceptions,
                previous_date+timedelta(days=1), bar.trading_date, now))
            gap = expected != 1
        gaps[bar.trading_date] = gap
        previous_date = bar.trading_date
    selected_source = next((s for s in sources if s["provider"] == selected_provider), None)
    available = {"first_date": None, "last_date": None, "stored_rows": 0,
                 "validation_scope": "EACH_RETURNED_PAGE", "observations": None}
    if selected_source:
        available.update({k: selected_source[k] for k in ("first_date", "last_date", "stored_rows")})
    diagnostics["scanned_rows"] = len(scanned)
    diagnostics["source_policy"] = "EXPLICIT_SINGLE_PROVIDER_NO_FORWARD_FILL"
    diagnostics["missing_sessions"] = []
    # Diagnose only the page interval; a missing page must not be labelled a
    # market-data gap, and stored range bounds are not a certified full audit.
    if schedules and scanned:
        page_start = start if after is None and start is not None else scanned[0].trading_date
        page_end = scanned[-1].trading_date if has_more else end
        found = {b.trading_date for b in bars}
        missing = [d.isoformat() for d, _ in
            completed_sessions("NSE", schedules, exceptions, page_start, page_end, now) if d not in found]
        diagnostics["missing_sessions"] = missing[:100]
        diagnostics["missing_session_count"] = len(missing)
        diagnostics["missing_session_list_truncated"] = len(missing) > 100
        if missing:
            diagnostics["warnings"].append("MISSING_COMPLETED_SESSIONS_NO_CANDLES_FILLED")
    if not bars:
        diagnostics["warnings"].append("NO_VALIDATED_CLOSES_IN_RETURNED_PAGE" if scanned else "NO_PERSISTED_HISTORY_IN_RANGE")
    return {
        "instrument_id": str(instrument_id), "symbol": canonical["primarySymbol"],
        "isin": canonical["isin"], "provider": selected_provider, "currency": "INR",
        "exchange": "NSE", "mic": "XNSE", "price_basis": "UNADJUSTED_CLOSE",
        "range": {"name": range_name, "start": start.isoformat() if start else None, "end": end.isoformat()},
        "available": available, "available_providers": sources,
        "bars": [serialize_bar(b, gap_before=gaps[b.trading_date]) for b in bars],
        "warmup": [serialize_bar(b, gap_before=gaps[b.trading_date]) for b in warmup],
        "next_cursor": scanned[-1].trading_date.isoformat() if has_more else None,
        "diagnostics": diagnostics,
        "benchmark": {"status": "UNAVAILABLE", "reason": "NO_VERIFIED_COMPARABLE_BENCHMARK_SERIES"},
        "as_of": now.isoformat(),
    }
