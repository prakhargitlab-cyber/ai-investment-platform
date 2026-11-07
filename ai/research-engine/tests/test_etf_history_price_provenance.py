"""Yahoo price provenance must fail closed before a bar is labelled UNADJUSTED_CLOSE.

Offline: in-memory bars only; no provider, database or clock beyond a fixed value.
"""
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from app.etf_history_selection import select_etf_history_bars, yahoo_price_provenance_is_unadjusted
from app.models import DailyMarketBar
from app.session_history import history_symbol

NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)
BASE = "https://finance.yahoo.com/quote/TESTETF.NS/history"
LEGIT = BASE + "?interval=1d&auto_adjust=false"
REASON = "ADJUSTED_OR_AMBIGUOUS_PRICE_PROVENANCE"

ACCEPTED = [
    LEGIT,
    BASE,  # legacy flagless /history contract is preserved, not upgraded
    BASE + "?interval=1d&auto_adjust=False",
    BASE + "?auto_adjust=0",
    BASE + "?adjusted=unadjusted",
    BASE + "?interval=1d#auto_adjust=false",
    "https://query1.finance.yahoo.com/quote/TESTETF.NS/history?auto_adjust=false",
    "https://finance.yahoo.com/quote/ADJUSTCO.NS/history?auto_adjust=false",  # ticker contains "adj"
]
REJECTED = [
    BASE + "?interval=1d&auto_adjust=true",
    BASE + "?auto_adjust=1",
    BASE + "?adjusted=true",
    BASE + "?adjustment=split",
    BASE + "?interval=1d#adjusted=true",
    BASE + "?interval=1d#auto_adjust=true",
    BASE + "?auto_adjust=false#auto_adjust=true",   # fragment must not be masked by query
    BASE + "?auto_adjust=true#auto_adjust=false",   # nor query by fragment (history_symbol accepts this)
    BASE + "?auto_adjust=false&auto_adjust=true",   # duplicate / conflicting
    BASE + "?auto_adjust=false&auto_adjust=false",  # duplicate is still ambiguous
    BASE + "?auto_adjust",                           # valueless flag
    BASE + "?auto_adjust=",                          # blank flag (history_symbol accepts this)
    BASE + "?auto_adjust=maybe",
    BASE + "?auto_adjust=false;adjusted=true",
    BASE + "?Auto_Adjust=TRUE",
    BASE + "?auto%5Fadjust=true",
    BASE + "?adjclose=true",
    BASE + "?adj_close=1",
    BASE + "#adjusted",
    BASE + "?auto_adjust=false&back-adjust=true",
    "https://finance.yahoo.com/quote/TESTETF.NS/history;auto_adjust=true",
    "https://finance.yahoo.com/quote/TESTETF.NS/adjusted/history",
    "https://finance.yahoo.com/quote/TESTETF.NS/adjclose",
]


def _bar(iid, url, *, provider="YAHOO_FINANCE", close="100", day=date(2026, 10, 1)):
    return DailyMarketBar(
        global_instrument_id=iid, trading_date=day, close=Decimal(close), currency="INR",
        provider=provider, provider_symbol="TESTETF.NS" if provider != "NSE" else "TESTETF",
        source_mode="REAL", source_url=url, retrieved_at=datetime(2026, 10, 2, tzinfo=timezone.utc))


@pytest.mark.parametrize("url", ACCEPTED)
def test_unadjusted_or_unflagged_provenance_is_accepted_unchanged(url):
    iid = uuid4()
    clean, meta = select_etf_history_bars([_bar(iid, url)], iid, now=NOW)
    assert len(clean) == 1
    assert clean[0].close == Decimal("100") and clean[0].trading_date == date(2026, 10, 1)
    assert clean[0].source_url == url
    assert meta["price_basis"] == "UNADJUSTED_CLOSE" and meta["accepted_rows"] == 1
    assert REASON not in meta["rejection_reasons"]


@pytest.mark.parametrize("url", REJECTED)
@pytest.mark.parametrize("provider", ["YAHOO_FINANCE", "YAHOO_FINANCE_MCP"])
def test_adjusted_or_ambiguous_provenance_is_rejected_never_verified(url, provider):
    iid = uuid4()
    clean, meta = select_etf_history_bars([_bar(iid, url, provider=provider)], iid, now=NOW)
    assert clean == []
    assert meta["accepted_rows"] == 0
    assert meta["rejection_reasons"] == {REASON: 1}
    assert meta["rejected_rows"] == 1
    # The existing honest-limits warnings are untouched.
    assert "CORPORATE_ACTION_ADJUSTMENT_UNVERIFIED" in meta["warnings"]
    assert "UNADJUSTED_PRICE_NOT_TOTAL_RETURN" in meta["warnings"]


def test_mixed_rows_keep_valid_candles_and_count_only_the_rejected_one():
    iid = uuid4()
    good = _bar(iid, LEGIT, close="101", day=date(2026, 10, 1))
    bad = _bar(iid, BASE + "?auto_adjust=true", close="93.7", day=date(2026, 10, 2))
    clean, meta = select_etf_history_bars([good, bad], iid, now=NOW)
    assert [(b.trading_date, b.close) for b in clean] == [(date(2026, 10, 1), Decimal("101"))]
    assert meta["rejection_reasons"] == {REASON: 1}
    assert meta["conflicting_dates"] == []


def test_nse_provider_is_not_subject_to_the_yahoo_flag_check():
    iid = uuid4()
    url = "https://www.nseindia.com/api/historical?adjusted=true"
    clean, meta = select_etf_history_bars([_bar(iid, url, provider="NSE")], iid, now=NOW)
    assert len(clean) == 1 and meta["provider"] == "NSE" and REASON not in meta["rejection_reasons"]


def test_host_failure_keeps_its_existing_reason_and_precedence():
    iid = uuid4()
    clean, meta = select_etf_history_bars(
        [_bar(iid, "https://evil.example/quote/X.NS/history?auto_adjust=true")], iid, now=NOW)
    assert clean == [] and meta["rejection_reasons"] == {"UNVERIFIED_HISTORY_SOURCE": 1}


def test_serialized_fields_and_basis_source_are_unchanged():
    iid = uuid4()
    _, meta = select_etf_history_bars([_bar(iid, LEGIT)], iid, now=NOW)
    assert set(meta) == {"provider", "price_basis", "price_basis_source", "calendar_state", "accepted_rows",
                         "rejected_rows", "rejection_reasons", "provider_rows", "conflicting_dates", "warnings"}
    assert meta["price_basis_source"] == "EXISTING_ADAPTER_CONTRACT"


def test_helper_never_raises_on_garbage():
    for bad in ("", "not a url", "http://[::1", None, 5):
        assert isinstance(yahoo_price_provenance_is_unadjusted(bad), bool)


def test_stricter_than_session_history_where_that_gate_is_lenient():
    """session_history.history_symbol accepts these (fragment overrides query; blank flags dropped)."""
    class Row:
        provider = "YAHOO_FINANCE"

        def __init__(self, url):
            self.source_url = url

    lenient = [BASE + "?auto_adjust=true#auto_adjust=false", BASE + "?auto_adjust="]
    for url in lenient:
        assert history_symbol(Row(url)) == "TESTETF.NS"
        assert not yahoo_price_provenance_is_unadjusted(url)
    # Both gates agree on the legitimate provider URL and on a plain adjusted flag.
    assert history_symbol(Row(LEGIT)) == "TESTETF.NS" and yahoo_price_provenance_is_unadjusted(LEGIT)
    assert history_symbol(Row(BASE + "?auto_adjust=true")) is None
    assert not yahoo_price_provenance_is_unadjusted(BASE + "?auto_adjust=true")
