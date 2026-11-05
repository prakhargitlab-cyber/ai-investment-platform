"""Five-year bounded ETF history onboarding: windows, listing-date clamp, no repeated leading-gap requests."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from test_radar_history_remediation import META, NOW, checkpoint, etf_bar, etf_repo, compatible_etf_dispatch  # noqa: F401
from app.etf_full_universe import _ensure_etf_history
from app.etf_history import ETF_HISTORY_TARGET_DAYS, ETF_HISTORY_WINDOW_DAYS
from app.market_sessions import completed_sessions


def calls(provider):
    return [(c.kwargs["start"].date(), c.kwargs["end"].date()) for c in provider.closes.await_args_list]


@pytest.mark.asyncio
async def test_empty_store_requests_five_years_in_bounded_oldest_first_windows(etf_repo):
    cp = await checkpoint(etf_repo)
    provider = SimpleNamespace(closes=AsyncMock(return_value=[]))
    await _ensure_etf_history(etf_repo, META, now=NOW, checkpoint=cp, provider=provider)
    windows = calls(provider)
    assert len(windows) >= 3
    assert windows == sorted(windows) and windows[0][0] >= (NOW - timedelta(days=ETF_HISTORY_TARGET_DAYS)).date()
    assert all((end - start).days <= ETF_HISTORY_WINDOW_DAYS for start, end in windows)
    assert all(a_end == b_start for (_, a_end), (b_start, _) in zip(windows, windows[1:]))
    assert (NOW.date() - windows[0][0]).days > 4 * 365


@pytest.mark.asyncio
async def test_listing_date_is_never_requested_before(etf_repo):
    cp = await checkpoint(etf_repo)
    listed = NOW.date() - timedelta(days=500)
    provider = SimpleNamespace(closes=AsyncMock(return_value=[]))
    await _ensure_etf_history(etf_repo, META | {"listingDate": listed.isoformat()}, now=NOW, checkpoint=cp, provider=provider)
    windows = calls(provider)
    assert windows and windows[0][0] >= listed and len(windows) == 1


@pytest.mark.asyncio
async def test_unavailable_prefix_is_not_requested_again_once_earliest_data_is_stored(etf_repo):
    cp = await checkpoint(etf_repo)
    schedules = etf_repo.persistence.load_market_schedules({"NSE"})
    days = [d for d, _ in completed_sessions("NSE", schedules, [], NOW.date() - timedelta(days=ETF_HISTORY_TARGET_DAYS), NOW.date(), NOW)]
    present = days[-700:]          # provider had nothing older (e.g. a ~2.8 year old ETF), no listing date known
    etf_repo.persistence.upsert_daily_market_bars([etf_bar(d, NOW) for d in present])
    provider = SimpleNamespace(closes=AsyncMock(return_value=[]))
    result = await _ensure_etf_history(etf_repo, META, now=NOW, checkpoint=cp, provider=provider)
    assert result["status"] == "SUFFICIENT_EXISTING_HISTORY" and provider.closes.await_count == 0
