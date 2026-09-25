"""FOMC meeting-calendar acquisition from the official Federal Reserve
meeting-calendar page:
https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm

Verified directly (fetched during this iteration's discovery pass): the
page is plain HTML, publicly accessible with no CAPTCHA or login, and lists
each year's eight regularly-scheduled meetings as month/day-range text
(e.g. "January 27-28", "March 17-18*" -- the asterisk marks a meeting with
a Summary of Economic Projections). No clock time is ever published for a
meeting; only a calendar date (or a start/end date pair for the two-day
meeting).

Parsing approach and its limitation: this sandbox has no live network
reachability to federalreserve.gov and no way to inspect the page's raw
HTML/DOM (fetch tooling available here returns processed/summarized
content, not byte-for-byte markup), so no CSS selector could be verified.
Rather than guess one, `parse_fomc_calendar_text` works on the same
plain-text extraction already used elsewhere in this codebase for document
parsing (app.normalization.extract_text, BeautifulSoup-based) and locates
meetings with a text-pattern scan: a bare 4-digit year followed by
"<Month> <day>[-<day>][*]" tokens is treated as that year's meetings, which
does not depend on any specific heading wording or tag structure. This is
built and tested against the actual meeting dates confirmed during
discovery (2026 and 2027), not fabricated placeholders -- but the
text-pattern parser itself has NOT been run against the page's real live
HTML from this environment, so it must be validated there before
production use (see the report's Limitations section).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone

import httpx

from app.macro_event import CENTRAL_BANK_MEETING, MacroEvent, macro_event_id
from app.macro_observation import FED_POLICY_RATE, MacroProviderError

FOMC_CALENDAR_ENDPOINT = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
_YEAR_TOKEN = re.compile(r"\b(20\d{2})\b")
_MONTH_DAY_RANGE = re.compile(
    r"\b(" + "|".join(m.capitalize() for m in _MONTHS) + r")\s+(\d{1,2})(?:\s*[-–]\s*(\d{1,2}))?\*?"
)


class FomcMeetingCandidate:
    __slots__ = ("start_date", "end_date")

    def __init__(self, start_date: date, end_date: date) -> None:
        self.start_date = start_date
        self.end_date = end_date


def parse_fomc_calendar_text(text: str) -> list[FomcMeetingCandidate]:
    """Text-pattern scan (see module docstring): a bare year token sets the
    year context for every "<Month> <day>[-<day>]" token that follows it,
    until the next year token. Returns meetings in document order; the
    caller is responsible for de-duplication (a meeting can legitimately be
    idempotently re-parsed on refresh)."""
    tokens: list[tuple[int, str, object]] = []
    for m in _YEAR_TOKEN.finditer(text):
        tokens.append((m.start(), "YEAR", int(m.group(1))))
    for m in _MONTH_DAY_RANGE.finditer(text):
        tokens.append((m.start(), "DATE", m))
    tokens.sort(key=lambda t: t[0])

    candidates: list[FomcMeetingCandidate] = []
    current_year: int | None = None
    for _, kind, value in tokens:
        if kind == "YEAR":
            current_year = value
            continue
        if current_year is None:
            continue
        match = value
        month = _MONTHS[match.group(1).lower()]
        start_day = int(match.group(2))
        end_day = int(match.group(3)) if match.group(3) else start_day
        try:
            start_date = date(current_year, month, start_day)
            end_date = date(current_year, month, end_day)
        except ValueError:
            continue  # an impossible calendar date is never fabricated into a record
        candidates.append(FomcMeetingCandidate(start_date, end_date))
    return candidates


class FomcCalendarProvider:
    provider_name = "FOMC_CALENDAR_FEDERAL_RESERVE"
    source_name = "Board of Governors of the Federal Reserve System (official FOMC meeting calendar)"

    def __init__(self, *, endpoint: str = FOMC_CALENDAR_ENDPOINT, client: httpx.AsyncClient | None = None,
                 timeout_seconds: float = 10.0) -> None:
        self.endpoint = endpoint
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds, connect=4.0))

    async def fetch(self, now: datetime | None = None) -> list[MacroEvent]:
        try:
            response = await self._client.get(self.endpoint)
        except httpx.TimeoutException as exc:
            raise MacroProviderError("MACRO_PROVIDER_TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise MacroProviderError("MACRO_PROVIDER_UNAVAILABLE:transport") from exc
        if response.status_code in (401, 403):
            raise MacroProviderError("MACRO_PROVIDER_FORBIDDEN")
        if response.status_code == 429:
            raise MacroProviderError("MACRO_PROVIDER_RATE_LIMITED")
        if response.status_code >= 500:
            raise MacroProviderError("MACRO_PROVIDER_UNAVAILABLE")
        if response.status_code >= 400:
            raise MacroProviderError(f"MACRO_PROVIDER_UNAVAILABLE:http_status_{response.status_code}")

        from app.normalization import extract_text
        _, body = extract_text(response.text, "text/html")
        if not body:
            raise MacroProviderError("MACRO_PROVIDER_UNAVAILABLE:empty_page")
        return self.parse(body, now=now)

    def parse(self, body_text: str, *, now: datetime | None = None) -> list[MacroEvent]:
        candidates = parse_fomc_calendar_text(body_text)
        if not candidates:
            raise MacroProviderError("MACRO_PROVIDER_NO_RECORDS")
        observed_at = now or datetime.now(timezone.utc)
        events = []
        for candidate in candidates:
            period = f"{candidate.start_date.isoformat()}/{candidate.end_date.isoformat()}"
            events.append(MacroEvent(
                id=macro_event_id(CENTRAL_BANK_MEETING, FED_POLICY_RATE, "US", candidate.start_date),
                event_type=CENTRAL_BANK_MEETING, indicator=FED_POLICY_RATE, region="US",
                scheduled_at=candidate.start_date, period=period, status="SCHEDULED",
                source=self.source_name, source_url=self.endpoint, provider=self.provider_name,
                provenance="OFFICIAL_GOVERNMENT", observed_at=observed_at,
            ))
        return events
