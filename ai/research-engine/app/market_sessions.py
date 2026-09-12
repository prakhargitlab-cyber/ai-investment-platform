from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class MarketTradingSchedule:
    market_code: str
    mic: str | None
    country_code: str | None
    timezone: str
    trading_day: int
    regular_open_time: time
    regular_close_time: time
    enabled: bool = True


@dataclass(frozen=True)
class MarketCalendarException:
    market_code: str
    trading_date: date
    exception_type: str
    open_time: time | None = None
    close_time: time | None = None
    reason: str | None = None


def market_session_status(market: str | None, schedules: list[MarketTradingSchedule],
                          exceptions: list[MarketCalendarException], now_utc: datetime) -> str:
    """Data-driven, DST-safe regular-session state. Unknown configuration fails closed."""
    key = (market or "").upper()
    applicable = [s for s in schedules if s.enabled and (s.market_code.upper() == key or (s.mic or "").upper() == key)]
    if not applicable:
        return "UNKNOWN"
    try:
        local = now_utc.astimezone(ZoneInfo(applicable[0].timezone))
    except (ValueError, ZoneInfoNotFoundError):
        return "UNKNOWN"
    exception = next((e for e in exceptions if e.market_code.upper() == applicable[0].market_code.upper() and e.trading_date == local.date()), None)
    if exception and exception.exception_type == "CLOSED":
        return "HOLIDAY"
    schedule = next((s for s in applicable if s.trading_day == local.weekday()), None)
    if not schedule and not (exception and exception.exception_type == "SPECIAL_SESSION"):
        return "CLOSED"
    open_time = schedule.regular_open_time if schedule else None
    close_time = schedule.regular_close_time if schedule else None
    if exception:
        if exception.exception_type in {"LATE_OPEN", "SPECIAL_SESSION"}:
            open_time = exception.open_time
        if exception.exception_type in {"EARLY_CLOSE", "SPECIAL_SESSION"}:
            close_time = exception.close_time
    if open_time is None or close_time is None:
        return "UNKNOWN"
    return "OPEN" if open_time <= local.timetz().replace(tzinfo=None) < close_time else "CLOSED"


def class_due(last_at: datetime | None, ttl_seconds: int, now_utc: datetime) -> bool:
    return last_at is None or (now_utc - last_at).total_seconds() >= ttl_seconds


def price_sync_eligible(market_status: str, last_price_at: datetime | None, price_ttl_seconds: int,
                        now_utc: datetime) -> bool:
    return market_status == "OPEN" and class_due(last_price_at, price_ttl_seconds, now_utc)


def _session_bounds(market, schedules, exceptions, day):
    matches = [s for s in schedules if s.enabled and market.upper() in {s.market_code.upper(), (s.mic or "").upper()}]
    if not matches:
        return None
    first = matches[0]
    exception = next((e for e in exceptions if e.market_code.upper() == first.market_code.upper() and e.trading_date == day), None)
    if exception and exception.exception_type == "CLOSED":
        return None
    schedule = next((s for s in matches if s.trading_day == day.weekday()), None)
    if schedule is None and not (exception and exception.exception_type == "SPECIAL_SESSION"):
        return None
    opening = exception.open_time if exception and exception.open_time else schedule.regular_open_time if schedule else None
    closing = exception.close_time if exception and exception.close_time else schedule.regular_close_time if schedule else None
    if opening is None or closing is None:
        return None
    try:
        zone = ZoneInfo(first.timezone)
    except (ValueError, ZoneInfoNotFoundError):
        return None
    return (datetime.combine(day, opening, zone).astimezone(timezone.utc), datetime.combine(day, closing, zone).astimezone(timezone.utc))


def latest_completed_session(market, schedules, exceptions, at):
    # Bounded lookback and unknown-calendar fail closed; holidays come from DB.
    for offset in range(15):
        bounds = _session_bounds(market, schedules, exceptions, at.date() - timedelta(days=offset))
        if bounds and bounds[1] <= at:
            return bounds[1]
    return None


def next_session_open(market, schedules, exceptions, after):
    for offset in range(15):
        bounds = _session_bounds(market, schedules, exceptions, after.date() + timedelta(days=offset))
        if bounds and bounds[0] > after:
            return bounds[0]
    return None
