from datetime import date, datetime, time, timezone

from app.market_sessions import MarketCalendarException, MarketTradingSchedule, class_due, market_session_status, price_session_valid_until, price_sync_eligible
from app.persistence import SqliteResearchPersistence


NSE = [MarketTradingSchedule("NSE", "XNSE", "IN", "Asia/Kolkata", day, time(9, 15), time(15, 30)) for day in range(5)]


def test_nse_session_holiday_and_exception_rules_are_data_driven():
    assert market_session_status("NSE", NSE, [], datetime(2026, 9, 7, 3, 30, tzinfo=timezone.utc)) == "CLOSED"
    assert market_session_status("NSE", NSE, [], datetime(2026, 9, 7, 5, 0, tzinfo=timezone.utc)) == "OPEN"
    assert market_session_status("NSE", NSE, [], datetime(2026, 9, 7, 11, 0, tzinfo=timezone.utc)) == "CLOSED"
    assert market_session_status("NSE", NSE, [], datetime(2026, 9, 6, 5, 0, tzinfo=timezone.utc)) == "CLOSED"
    holiday = [MarketCalendarException("NSE", date(2026, 9, 7), "CLOSED")]
    assert market_session_status("NSE", NSE, holiday, datetime(2026, 9, 7, 5, 0, tzinfo=timezone.utc)) == "HOLIDAY"
    early = [MarketCalendarException("NSE", date(2026, 9, 7), "EARLY_CLOSE", close_time=time(12, 0))]
    assert market_session_status("NSE", NSE, early, datetime(2026, 9, 7, 6, 0, tzinfo=timezone.utc)) == "OPEN"
    assert market_session_status("NSE", NSE, early, datetime(2026, 9, 7, 7, 0, tzinfo=timezone.utc)) == "CLOSED"


def test_unknown_fails_closed_and_dst_iana_zone_is_used():
    assert market_session_status("MISSING", NSE, [], datetime.now(timezone.utc)) == "UNKNOWN"
    nyse = [MarketTradingSchedule("NYSE", "XNYS", "US", "America/New_York", day, time(9, 30), time(16)) for day in range(5)]
    assert market_session_status("NYSE", nyse, [], datetime(2026, 7, 6, 14, 0, tzinfo=timezone.utc)) == "OPEN"
    assert market_session_status("NYSE", nyse, [], datetime(2026, 1, 6, 14, 0, tzinfo=timezone.utc)) == "CLOSED"


def test_price_eligibility_is_open_only_but_other_class_due_is_independent():
    now = datetime(2026, 9, 7, 5, tzinfo=timezone.utc)
    assert price_sync_eligible("OPEN", None, 300, now)
    assert not price_sync_eligible("OPEN", now, 300, now)
    assert not price_sync_eligible("CLOSED", now.replace(hour=0), 300, now)
    assert class_due(now.replace(day=6), 3600, now)


def test_market_schedule_and_exception_time_deserialization_accepts_postgres_time_and_sqlite_strings():
    class Result:
        def __init__(self, rows): self.rows = rows
        def fetchall(self): return self.rows
    class Connection:
        # DI-16B: read persistence methods now wrap SELECTs in
        # ``with self._connection:``, so any stubbed connection must support
        # the context-manager protocol (real psycopg3/sqlite3 connections do).
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
        def execute(self, sql):
            if "market_trading_schedules" in sql:
                return Result([{
                    "market_code": "NSE", "mic": "XNSE", "country_code": "IN", "timezone": "Asia/Kolkata", "trading_day": 0,
                    "regular_open_time": time(9, 15), "regular_close_time": time(15, 30), "enabled": True,
                }])
            return Result([{
                "market_code": "NSE", "trading_date": "2026-09-07", "exception_type": "EARLY_CLOSE",
                "open_time": "09:15:00", "close_time": time(12, 0), "reason": "fixture",
            }])

    persistence = SqliteResearchPersistence()
    persistence._connection = Connection()
    schedule = persistence.load_market_schedules({"NSE"})[0]
    exception = persistence.load_market_calendar_exceptions({"NSE"})[0]
    assert schedule.regular_open_time == time(9, 15)
    assert schedule.regular_close_time == time(15, 30)
    assert exception.open_time == time(9, 15)
    assert exception.close_time == time(12, 0)


def test_price_session_valid_until_extends_close_through_next_session_open():
    friday_close = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)  # 15:30 IST
    monday_open = datetime(2026, 9, 14, 3, 45, tzinfo=timezone.utc)    # 09:15 IST
    assert price_session_valid_until("NSE", NSE, [], friday_close) == monday_open


def test_price_session_valid_until_persists_across_holiday():
    friday_close = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)
    tuesday_open = datetime(2026, 9, 15, 3, 45, tzinfo=timezone.utc)   # Mon Sep 14 holiday
    holidays = [MarketCalendarException("NSE", date(2026, 9, 14), "CLOSED")]
    assert price_session_valid_until("NSE", NSE, holidays, friday_close) == tuesday_open


def test_price_session_valid_until_rejects_non_close_observation():
    # A mid-session observation is not a genuine close -> no session validity.
    mid_session = datetime(2026, 9, 11, 7, 0, tzinfo=timezone.utc)  # 12:30 IST
    assert price_session_valid_until("NSE", NSE, [], mid_session) is None


def test_price_session_valid_until_is_none_without_calendar_context():
    friday_close = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)
    assert price_session_valid_until("NSE", [], [], friday_close) is None
