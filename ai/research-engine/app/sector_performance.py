"""Pure market-performance projection for the global sector dashboard."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Iterable


_PERIOD_DAYS = {"WEEK": 7, "MONTH": 30, "YEAR": 365}
_EUROPE_COUNTRIES = {"AT", "BE", "CH", "DE", "DK", "ES", "FI", "FR", "GB", "IE", "IT", "NL", "NO", "PT", "SE"}


def belongs_to_region(item: dict, region: str) -> bool:
    region = region.upper()
    country = str(item.get("country") or "").upper()
    exchange = str(item.get("exchange") or item.get("mic") or "").upper()
    if region == "INDIA":
        return country in {"IN", "IND", "INDIA"} or exchange in {"XNSE", "NSE", "XBOM", "BSE"}
    if region == "USA":
        return country in {"US", "USA"} or exchange in {"XNAS", "XNYS", "ARCX", "BATS"}
    if region == "EUROPE":
        return country in _EUROPE_COUNTRIES or exchange in {"XETR", "XAMS", "AEB", "XLON", "XPAR", "XSWX", "XMIL", "XMAD", "XSTO", "XHEL", "XCSE", "XOSL"}
    return False


def performance_window(observations: Iterable, period: str):
    """Return latest and the nearest valid prior observation for a period."""
    period = period.upper()
    if period not in {"DAY", *_PERIOD_DAYS}:
        raise ValueError("UNSUPPORTED_PERFORMANCE_PERIOD")
    usable = sorted((value for value in observations if value.price is not None and value.price > 0), key=lambda value: value.observed_at)
    if len(usable) < 2:
        return None
    latest = usable[-1]
    # A day means the preceding *observed trading close*, rather than a
    # calendar-day subtraction.  This makes a Monday correctly compare with
    # Friday (or the preceding holiday-adjusted observation).
    if period == "DAY":
        reference = usable[-2]
    else:
        target = latest.observed_at - timedelta(days=_PERIOD_DAYS[period])
        reference = next((value for value in reversed(usable[:-1]) if value.observed_at <= target), None)
    if reference is None or reference.price <= 0:
        return None
    return latest, reference, (latest.price - reference.price) / reference.price * Decimal("100")


def deduplicated_performance_candidates(candidates: Iterable[dict]) -> list[dict]:
    """Resolve duplicate global identities before either performance ranking."""
    winners: dict[str, dict] = {}
    for candidate in candidates:
        identity = str(candidate["globalInstrumentId"])
        previous = winners.get(identity)
        key = (-candidate["performancePct"], str(candidate.get("ticker") or "").upper(), identity)
        if previous is None or key < (-previous["performancePct"], str(previous.get("ticker") or "").upper(), identity):
            winners[identity] = candidate
    return list(winners.values())


def rank_performers(candidates: Iterable[dict], limit: int = 5) -> tuple[list[dict], list[dict]]:
    """Return deterministic top and worst performers from one read-only input."""
    rows = deduplicated_performance_candidates(candidates)
    bounded = max(1, min(5, limit))
    ticker_key = lambda value: (str(value.get("ticker") or "").upper(), str(value["globalInstrumentId"]))
    best = sorted(rows, key=lambda value: (-value["performancePct"], *ticker_key(value)))[:bounded]
    worst = sorted(rows, key=lambda value: (value["performancePct"], *ticker_key(value)))[:bounded]
    return best, worst


def rank_top_gainers(candidates: Iterable[dict], limit: int = 5) -> list[dict]:
    """Compatibility helper retained for callers of the earlier endpoint."""
    return rank_performers(candidates, limit)[0]
