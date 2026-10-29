"""Validated, unadjusted daily closes over the existing observation store.

Yahoo's /history provenance is the legacy contract for auto_adjust=False.
Quote URLs never imply a daily candle. Unknown/adjusted sources fail closed.
Stored timestamps/provenance are untouched; normalized copies use session close.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.market_sessions import completed_sessions, next_session_close, session_bounds, unresolved_session_dates


@dataclass(frozen=True)
class SessionHistory:
    observations: tuple = ()
    conflicting_dates: tuple = ()
    current_conflict: bool = False
    rejected_count: int = 0
    duplicate_count: int = 0
    session_dates: tuple = ()
    missing_dates: tuple = ()
    valid_until: datetime | None = None


def history_symbol(row):
    parsed = urlparse(row.source_url)
    parts = [unquote(part) for part in parsed.path.split('/') if part]
    if (row.provider not in {'YAHOO_FINANCE', 'YAHOO_FINANCE_MCP'}
            or parsed.scheme != 'https' or parsed.hostname != 'finance.yahoo.com'
            or len(parts) != 3 or parts[0] != 'quote' or parts[2] != 'history'):
        return None
    flags = {**parse_qs(parsed.query), **parse_qs(parsed.fragment)}
    for key in ('auto_adjust', 'adjusted', 'adjustment'):
        if key in flags and any(value.lower() not in {'false', '0', 'unadjusted'} for value in flags[key]):
            return None
    if 'interval' in flags and flags['interval'] != ['1d']:
        return None
    return parts[1].upper()


def is_daily_history(row):
    # Includes explicitly adjusted history, which must not become a quote.
    return '/history' in urlparse(row.source_url).path


def aware(value):
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def validate_daily_closes(instrument_id, observations, *, as_of, currency, market,
                          schedules, exceptions, provider_symbol=None, trusted_providers=None):
    matches = [s for s in schedules if s.enabled and market.upper() in {s.market_code.upper(), (s.mic or '').upper()}]
    if not matches or not aware(as_of):
        return SessionHistory(rejected_count=len(observations))
    try:
        zone = ZoneInfo(matches[0].timezone)
    except (ValueError, ZoneInfoNotFoundError):
        return SessionHistory(rejected_count=len(observations))
    grouped = defaultdict(list)
    rejected = 0
    for row in observations:
        symbol = history_symbol(row)
        if (row.instrument_id != instrument_id or not symbol or not currency or row.currency != currency
                or (trusted_providers is not None and row.provider not in trusted_providers)
                or (provider_symbol and symbol != provider_symbol.upper())
                or (market.upper() in {'NSE', 'XNSE'} and not symbol.endswith('.NS'))
                or row.provider not in {'YAHOO_FINANCE', 'NSE', 'YAHOO_FINANCE_MCP'}
                or not aware(row.observed_at) or not aware(row.retrieved_at)
                or row.observed_at > as_of or row.retrieved_at > as_of
                or not row.price.is_finite() or row.price <= 0):
            rejected += 1
            continue
        stamp = row.observed_at.astimezone(timezone.utc)
        local = row.observed_at.astimezone(zone)
        # Old direct rows use UTC midnight; Yahoo exchange-local dates are
        # often persisted as 18:30 UTC on the previous date for NSE.
        day = stamp.date() if stamp.time() == time.min else local.date()
        bounds = session_bounds(market, schedules, exceptions, day)
        date_label = stamp.time() == time.min or local.time() == time.min
        if (not bounds or bounds[1] > as_of or row.retrieved_at < bounds[1]
                or (not date_label and abs((row.observed_at - bounds[1]).total_seconds()) > 60)):
            rejected += 1
            continue
        grouped[day].append(row)
    output, dates, conflicts = [], [], []
    duplicates = 0
    for day, rows in sorted(grouped.items()):
        duplicates += len(rows) - 1
        # A newer retrieval may revise the SAME provider's candle. Different
        # providers must agree; a later quote/clock convention cannot win.
        by_provider = defaultdict(list)
        for row in rows:
            by_provider[row.provider].append(row)
        current = []
        for values in by_provider.values():
            newest = max(row.retrieved_at for row in values)
            current.extend(row for row in values if row.retrieved_at == newest)
        if len({row.price for row in current}) != 1:
            conflicts.append(day)
            continue
        chosen = min(current, key=lambda row: (row.provider, row.source_url))
        close = session_bounds(market, schedules, exceptions, day)[1]
        output.append(chosen.model_copy(update={'observed_at': close}))
        dates.append(day)
    expected = dict(completed_sessions(market, schedules, exceptions, min(grouped), max(grouped), as_of)) if grouped else {}
    unresolved = unresolved_session_dates(market, schedules, exceptions, min(grouped), as_of) if grouped else ()
    missing = tuple(sorted((set(expected) - set(dates)) | set(unresolved)))
    return SessionHistory(tuple(output), tuple(conflicts), bool(grouped and max(grouped) in conflicts),
                          rejected, duplicates, tuple(dates), missing,
                          next_session_close(market, schedules, exceptions, output[-1].observed_at) if output else None)


def missing_history_ranges(existing_dates, *, market, schedules, exceptions, start, as_of, listing_date=None):
    """Missing completed sessions grouped into supported [start,end) dates."""
    if listing_date is not None:
        start = max(start, listing_date)
    present = set(existing_dates)
    ranges, first, last = [], None, None
    for day, _ in completed_sessions(market, schedules, exceptions, start, as_of.date(), as_of):
        if day in present:
            if first is not None:
                ranges.append((first, last + timedelta(days=1)))
                first = last = None
        else:
            first = day if first is None else first
            last = day
    if first is not None:
        ranges.append((first, last + timedelta(days=1)))
    return ranges
