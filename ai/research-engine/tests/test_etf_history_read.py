"""Synthetic persisted observations only. No market/provider/production DB calls."""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from threading import RLock
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.etf_history_read import history_range, read_etf_history
from app.etf_history_selection import select_etf_history_bars
from app.market_sessions import MarketCalendarException, MarketTradingSchedule
from app.models import DailyMarketBar
from app.persistence import SqliteResearchPersistence


KEY = UUID(int=71)
NOW = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)
META = dict(globalInstrumentId=str(KEY), status="ACTIVE", assetType="ETF", country="IN",
    primaryExchange="NSE", primarySymbol="SYNTHETF", canonicalName="Synthetic ETF", isin="IN0000000071", currency="INR",
    providerMappings=[dict(provider="NSE", providerSymbol="SYNTHETF", status="VERIFIED",
                          resolutionSource="NSE_OFFICIAL_ETF_BOOTSTRAP", currency="INR")])
SCHEDULES = [MarketTradingSchedule("NSE", "XNSE", "IN", "Asia/Kolkata", d,
    time(9, 15), time(15, 30)) for d in range(5)]
EXCEPTIONS = [MarketCalendarException("NSE", date(2026, 9, 9), "CLOSED", reason="SYNTHETIC_TEST_CLOSURE")]


def bar(day=date(2026, 9, 7), **changes):
    return DailyMarketBar(**(dict(global_instrument_id=KEY, trading_date=day, close=Decimal("100.25"),
        volume=None, currency="INR", provider="NSE", provider_symbol="SYNTHETF", source_mode="REAL",
        source_url="https://nsearchives.nseindia.com/SYNTHETIC_UNIT_TEST_ONLY.csv", retrieved_at=NOW) | changes))


@pytest.fixture
def store():
    value = SqliteResearchPersistence()
    value.load_market_schedules = lambda markets=None: SCHEDULES
    value.load_market_calendar_exceptions = lambda markets=None: EXCEPTIONS
    yield value
    value._connection.close()


def selection(bars, **changes):
    return select_etf_history_bars(bars, KEY, now=NOW, canonical_symbol="SYNTHETF",
        schedules=SCHEDULES, exceptions=EXCEPTIONS, require_calendar=True, **changes)


@pytest.mark.parametrize("change,reason", [
    ({"global_instrument_id": UUID(int=72)}, "INSTRUMENT_ID_MISMATCH"),
    ({"source_mode": "DEMO"}, "NON_REAL_HISTORY"),
    ({"currency": "USD"}, "CURRENCY_MISMATCH"),
    ({"provider": "UNKNOWN"}, "UNSUPPORTED_HISTORY_PROVIDER"),
    ({"source_url": "https://nsearchives.nseindia.com.evil.test/record"}, "UNVERIFIED_HISTORY_SOURCE"),
    ({"source_url": "http://nsearchives.nseindia.com/record"}, "UNVERIFIED_HISTORY_SOURCE"),
    ({"source_url": "https://user:pass@nsearchives.nseindia.com/record"}, "UNVERIFIED_HISTORY_SOURCE"),
    ({"provider_symbol": "OTHER"}, "PROVIDER_SYMBOL_MISMATCH"),
    ({"retrieved_at": NOW+timedelta(seconds=1)}, "FUTURE_HISTORY"),
    ({"trading_date": NOW.date()+timedelta(days=1)}, "FUTURE_HISTORY"),
    ({"close": None}, "CLOSE_NOT_RECORDED"),
    ({"trading_date": date(2026, 9, 6)}, "NOT_A_CONFIGURED_TRADING_SESSION"),
    ({"trading_date": date(2026, 9, 9)}, "NOT_A_CONFIGURED_TRADING_SESSION"),
    ({"retrieved_at": datetime(2026, 9, 7, 9, tzinfo=timezone.utc)}, "UNCOMPLETED_SESSION_OBSERVATION"),
])
def test_history_evidence_rejection_is_explicit(change, reason):
    selected, diagnostics = selection([bar(**change)])
    assert selected == []
    assert diagnostics["rejection_reasons"] == {reason: 1}


def test_provider_selection_does_not_overwrite_or_mix_same_date():
    official = bar()
    secondary = bar(provider="YAHOO_FINANCE", provider_symbol="SYNTHETF.NS", close=Decimal("999"),
        source_url="https://finance.yahoo.com/quote/SYNTHETF.NS/history")
    selected, diagnostics = selection([secondary, official])
    assert selected == [official]
    assert diagnostics["provider"] == "NSE"
    selected, diagnostics = selection([official, secondary], provider="YAHOO_FINANCE")
    assert selected == [secondary]
    assert "MULTIPLE_PROVIDERS_KEPT_SEPARATE" in diagnostics["warnings"]


def test_conflicting_duplicate_source_session_does_not_last_win():
    selected, diagnostics = selection([bar(), bar(close=Decimal("200"))])
    assert selected == []
    assert diagnostics["conflicting_dates"] == ["2026-09-07"]


def test_missing_calendar_never_certifies_sessions():
    selected, diagnostics = select_etf_history_bars([bar()], KEY, now=NOW,
        schedules=[], exceptions=[], require_calendar=True)
    assert selected == []
    assert diagnostics["rejection_reasons"] == {"HISTORY_SESSION_CALENDAR_UNAVAILABLE": 1}


def test_bounded_keyset_page_is_read_only_and_zero_is_not_missing(store):
    store.upsert_daily_market_bars([bar(date(2026, 9, d), volume=0 if d==7 else None) for d in (7, 8, 10, 11, 14)])
    changes = store._connection.total_changes
    queries = []
    store._connection.set_trace_callback(queries.append)
    first = read_etf_history(store, KEY, META, range_name="MAX", limit=2, now=NOW)
    assert len(first["bars"]) == 2 and first["next_cursor"] == "2026-09-08"
    assert first["bars"][0]["volume"] == "0" and first["bars"][1]["volume"] is None
    second = read_etf_history(store, KEY, META, range_name="MAX", limit=2, after=date(2026, 9, 8), provider="NSE", now=NOW)
    assert [b["date"] for b in second["bars"]] == ["2026-09-10", "2026-09-11"]
    assert all(not b["gap_before"] for b in second["bars"])
    assert second["warmup"] == []
    assert store._connection.total_changes == changes
    assert any("LIMIT 3" in q and "trading_date>'2026-09-08'" in q.replace(" ", "") for q in queries)
    assert not any(q.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries)


def test_missing_session_resets_window_but_weekend_and_closure_do_not(store):
    store.upsert_daily_market_bars([bar(date(2026, 9, d)) for d in (7, 10, 11, 14)])
    data = read_etf_history(store, KEY, META, range_name="MAX", now=NOW)
    assert data["bars"][1]["gap_before"] is True  # Sept8 is missing; Sept9 is a configured closure.
    assert data["bars"][3]["gap_before"] is False  # Friday to Monday.
    assert "2026-09-08" in data["diagnostics"]["missing_sessions"]
    assert "2026-09-09" not in data["diagnostics"]["missing_sessions"]
    assert all(b["date"] != "2026-09-08" for b in data["bars"])


def test_empty_history_and_unavailable_benchmark_have_reasons(store):
    data = read_etf_history(store, KEY, META, now=NOW)
    assert data["bars"] == [] and data["next_cursor"] is None
    assert data["available"]["observations"] is None
    assert data["benchmark"]["status"] == "UNAVAILABLE"
    assert "NO_PERSISTED_HISTORY_IN_RANGE" in data["diagnostics"]["warnings"]


@pytest.mark.parametrize("name,days", [("1Y", 365), ("3Y", 1096), ("5Y", 1826)])
def test_real_calendar_year_ranges(name, days):
    start, end = history_range(name, now=NOW)
    assert (end-start).days == days


def test_leap_day_range_is_clamped_explicitly():
    start, _ = history_range("1Y", now=datetime(2024,2,29,tzinfo=timezone.utc))
    assert start == date(2023,2,28)


@pytest.mark.parametrize("kwargs", [
    {"range_name":"CUSTOM"}, {"range_name":"CUSTOM","start":date(2026,9,10),"end":date(2026,9,7)},
    {"range_name":"CUSTOM","start":date(2026,9,1),"end":date(2027,1,1)},
    {"start":date(2026,9,1)}, {"after":date(2027,1,1)}, {"limit":501}, {"limit":0}, {"provider":"BSE"},
])
def test_invalid_ranges_and_bounds_fail_without_query(store, kwargs):
    with pytest.raises(ValueError):
        read_etf_history(store, KEY, META, now=NOW, **kwargs)


def test_unknown_source_and_wrong_currency_do_not_become_available_series(store):
    store.upsert_daily_market_bars([bar(provider="UNKNOWN"),bar(provider="YAHOO_FINANCE",currency="USD")])
    data=read_etf_history(store,KEY,META,now=NOW)
    assert data["available_providers"]==[] and data["bars"]==[]


@pytest.fixture
def history_client(monkeypatch, store):
    import app.main as main
    lock = RLock()
    async def dispatch(operation, *args, **kwargs):
        with lock:
            return operation(*args, **kwargs)
    repository = SimpleNamespace(persistence=store, _persistence_worker_lock=lock,
        _run_blocking_persistence=dispatch)
    source = SimpleNamespace(global_instrument_metadata=AsyncMock(return_value=META))
    monkeypatch.setattr(main, "repository", repository)
    monkeypatch.setattr(main, "portfolio_orchestrator", source)
    # A synchronous dispatcher is used above; disable SQLite thread affinity in
    # the same way as the existing API suite's in-memory test adapter.
    import sqlite3
    replacement = sqlite3.connect(":memory:", check_same_thread=False)
    replacement.row_factory = sqlite3.Row
    store._connection.close()
    store._connection = replacement
    store.migrate()
    return TestClient(main.app), source


HEADERS = {"X-AIP-User-Id":"synthetic-user", "X-AIP-User-Issuer":"synthetic-issuer", "X-AIP-User-Subject":"synthetic-subject"}


def test_history_api_requires_existing_authenticated_identity(history_client):
    client, source=history_client
    response=client.get(f"/api/v1/etf-radar/instruments/{KEY}/history")
    assert response.status_code==401 and source.global_instrument_metadata.await_count==0


def test_history_api_contract_and_existing_authorization_forwarding(history_client, store):
    client, source=history_client
    store.upsert_daily_market_bar(bar(volume=0))
    response=client.get(f"/api/v1/etf-radar/instruments/{KEY}/history?range=MAX",headers=HEADERS)
    assert response.status_code==200
    data=response.json()
    assert data["instrument_id"]==str(KEY) and data["isin"]==META["isin"]
    assert data["price_basis"]=="UNADJUSTED_CLOSE" and data["bars"][0]["volume"]=="0"
    assert source.global_instrument_metadata.call_args.kwargs["identity_headers"]["X-AIP-User-Id"]=="synthetic-user"


@pytest.mark.parametrize("change", [
    {"assetType":"EQUITY"}, {"status":"INACTIVE"}, {"globalInstrumentId":str(UUID(int=72))},
    {"providerMappings":[]}, {"isin":None}, {"currency":"USD"},
])
def test_history_api_does_not_authorize_from_ticker_only(history_client, change):
    client, source=history_client
    source.global_instrument_metadata.return_value=META|change
    response=client.get(f"/api/v1/etf-radar/instruments/{KEY}/history",headers=HEADERS)
    assert response.status_code==422


def test_history_api_returns_503_when_canonical_source_is_unavailable(history_client):
    from app.portfolio_orchestration import PortfolioServiceUnavailableError
    client, source=history_client
    source.global_instrument_metadata.side_effect=PortfolioServiceUnavailableError("synthetic source failure")
    response=client.get(f"/api/v1/etf-radar/instruments/{KEY}/history",headers=HEADERS)
    assert response.status_code==503
