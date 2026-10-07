"""A single, small, deterministic reporting-period parsing/normalization
utility -- reusable by any evidence interpreter that needs to recognize a
financial reporting period from text (Shareholding today; Balance Sheet,
Cash Flow, Quarterly Financials/P&L tomorrow), rather than each
interpreter re-implementing its own date guessing.

This module is intentionally domain-agnostic: it has no notion of
shareholding, balance sheets, or any other evidence type, and no opinion
about what a CALLER should do when a complete calendar date is technically
required for canonical persistence but the source only gave month+year --
that decision belongs to the calling interpreter (which may have its own
pre-existing domain contract about what a given month+year period means;
see app.evidence_interpretation for the SHAREHOLDING-specific example).

Precedence (least guessing first):
    1. An explicit, source-stated DAY-precision date ("30 Jun 2026") is
       preserved exactly -- never renormalized.
    2. MONTH + YEAR ("Jun 2026", "June 2026", "Jun-26") is recognized at
       MONTH precision. When a canonical persistence model technically
       requires a complete date, the GENERIC normalization this module
       performs is the first day of that month (YYYY-MM-01) -- a
       technical placeholder, never represented as though the source
       stated that exact day. This module never invents a month-end or
       quarter-end date on its own.
    3. YEAR alone ("2026") is recognized at YEAR precision with no
       normalized_date at all -- nothing here invents a month or day.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

Precision = Literal["DAY", "MONTH", "YEAR"]

_MONTHS: dict[str, int] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
_MONTH_NAME_PATTERN = "|".join(sorted(_MONTHS.keys(), key=len, reverse=True))

# Matches an optional leading day, a month name, and a 2-or-4-digit year,
# e.g. "30 Jun 2026", "Jun 2026", "June 2026", "Jun-26", "Jun 26".
_MONTH_YEAR_TOKEN = re.compile(
    rf"\b(?:(?P<day>\d{{1,2}})\s+)?(?P<month>{_MONTH_NAME_PATTERN})\.?[\s\-]+(?P<year>\d{{2}}|\d{{4}})\b",
    re.I,
)
# A bare 4-digit year, used only as a last-resort fallback when no month
# name is present at all in the given text.
_YEAR_ONLY_TOKEN = re.compile(r"\b(\d{4})\b")


def _resolve_year(raw_year: str) -> int:
    """2-digit years are assumed to be 2000s -- this platform's financial
    evidence never concerns a pre-2000 reporting period, so this is an
    unambiguous, documented assumption rather than a guess."""
    if len(raw_year) == 2:
        return 2000 + int(raw_year)
    return int(raw_year)


@dataclass(frozen=True)
class ReportingPeriod:
    """A recognized reporting period at one of three precisions.

    ``normalized_date`` is a TECHNICAL placeholder for callers that
    require a complete date, never a claim that the source stated that
    exact day:
      - DAY precision: the exact source-stated date.
      - MONTH precision: the first day of the stated month (YYYY-MM-01).
      - YEAR precision: always None -- nothing here invents a month/day.
    """
    year: int
    month: int | None
    day: int | None
    precision: Precision
    original_text: str
    normalized_date: date | None

    @property
    def sort_key(self) -> tuple[int, int, int]:
        """Comparable key for ordering periods of the SAME precision (the
        normal case -- comparing a table's own period columns against
        each other). Never relies on normalized_date, so the synthetic
        first-of-month day used for MONTH precision has no effect on
        ordering between distinct months/years."""
        return (self.year, self.month or 0, self.day or 0)

    def __lt__(self, other: "ReportingPeriod") -> bool:
        return self.sort_key < other.sort_key

    def __le__(self, other: "ReportingPeriod") -> bool:
        return self.sort_key <= other.sort_key

    def __gt__(self, other: "ReportingPeriod") -> bool:
        return self.sort_key > other.sort_key

    def __ge__(self, other: "ReportingPeriod") -> bool:
        return self.sort_key >= other.sort_key


def parse_reporting_period(text: str) -> ReportingPeriod | None:
    """Parse a single reporting-period token (e.g. a table header cell,
    or an isolated phrase) into a ReportingPeriod. Returns None when
    nothing recognizable is present -- never guesses a period that is
    not genuinely supported by the text."""
    stripped = text.strip()
    if not stripped:
        return None

    match = _MONTH_YEAR_TOKEN.search(stripped)
    if match:
        month = _MONTHS[match.group("month").lower().rstrip(".")]
        year = _resolve_year(match.group("year"))
        day_raw = match.group("day")
        if day_raw:
            day = int(day_raw)
            try:
                normalized = date(year, month, day)
            except ValueError:
                return None  # e.g. "31 Feb 2026" -- not a real calendar date
            return ReportingPeriod(
                year=year, month=month, day=day, precision="DAY",
                original_text=stripped, normalized_date=normalized,
            )
        return ReportingPeriod(
            year=year, month=month, day=None, precision="MONTH",
            original_text=stripped, normalized_date=date(year, month, 1),
        )

    year_match = _YEAR_ONLY_TOKEN.fullmatch(stripped)
    if year_match:
        year = int(year_match.group(1))
        return ReportingPeriod(
            year=year, month=None, day=None, precision="YEAR",
            original_text=stripped, normalized_date=None,
        )

    return None


def find_period_tokens(line: str) -> list[ReportingPeriod]:
    """Scan a line (typically a table header row) for ALL recognizable
    month/year or day/month/year period tokens, in left-to-right order of
    appearance. Used for multi-column table period-column detection.
    Does NOT fall back to bare-year matching (a header row full of plain
    4-digit numbers is far more likely to be data than a list of bare
    years), so this is intentionally narrower than parse_reporting_period
    on its own."""
    periods: list[ReportingPeriod] = []
    for match in _MONTH_YEAR_TOKEN.finditer(line):
        month = _MONTHS[match.group("month").lower().rstrip(".")]
        year = _resolve_year(match.group("year"))
        day_raw = match.group("day")
        if day_raw:
            day = int(day_raw)
            try:
                normalized = date(year, month, day)
            except ValueError:
                continue
            periods.append(ReportingPeriod(
                year=year, month=month, day=day, precision="DAY",
                original_text=match.group(0).strip(), normalized_date=normalized,
            ))
        else:
            periods.append(ReportingPeriod(
                year=year, month=month, day=None, precision="MONTH",
                original_text=match.group(0).strip(), normalized_date=date(year, month, 1),
            ))
    return periods


def month_end_date(year: int, month: int) -> date:
    """The actual last calendar day of the given month -- a deterministic
    calendar computation, not a guess. Exposed for callers whose OWN
    pre-existing domain contract defines a month-precision period as that
    month's end date (see app.evidence_interpretation's shareholding
    quarter-end handling for the documented example); this module itself
    never applies this on its own, and parse_reporting_period/
    find_period_tokens never return a month-end normalized_date."""
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, last_day)
