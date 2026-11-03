"""Read-only candidate resolver for a reviewed issuer's monthly Nippon factsheets.

Resolving a URL NEVER approves it for network access or ingestion. The
ApprovedSourceRegistry remains the only authorizer of an actual fetch.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import urlsplit

BASE = "https://mf.nipponindiaim.com/InvestorServices/FactSheetsDocuments/"
MONTHS = ("January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December")


@dataclass(frozen=True)
class MonthlyFactsheetCandidate:
    publication_month: date  # first day of the URL month, NOT the metrics date
    expected_reporting_date: date  # last day of the preceding month, not verified
    url: str


def month_candidate(year: int, month: int) -> MonthlyFactsheetCandidate:
    if not isinstance(year, int) or isinstance(year, bool) or not 2020 <= year <= 2100:
        raise ValueError("FACTSHEET_YEAR_INVALID")
    if not isinstance(month, int) or isinstance(month, bool) or not 1 <= month <= 12:
        raise ValueError("FACTSHEET_MONTH_INVALID")
    publication = date(year, month, 1)
    prior_end = publication - timedelta(days=1)
    return MonthlyFactsheetCandidate(
        publication_month=publication,
        expected_reporting_date=prior_end,
        url=f"{BASE}Nippon-FS-{MONTHS[month - 1]}-{year}.pdf",
    )


def bounded_candidates(today: date, *, count: int = 2) -> tuple[MonthlyFactsheetCandidate, ...]:
    """Recent publication months only. No HTTP or date acceptance is implied."""
    if type(count) is not int or not 1 <= count <= 2:
        raise ValueError("FACTSHEET_CANDIDATE_LIMIT_EXCEEDED")
    return tuple(month_candidate(
        (today.year * 12 + today.month - 1 - back) // 12,
        (today.year * 12 + today.month - 1 - back) % 12 + 1,
    ) for back in range(count))


def is_reviewed_path_shape(url: str) -> bool:
    """Structural check only, NOT approval of a new monthly document."""
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "mf.nipponindiaim.com"
            or parsed.port is not None or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        return False
    for year in range(2020, 2101):
        for month in range(1, 13):
            if url == month_candidate(year, month).url:
                return True
    return False


def reporting_period_matches(candidate: MonthlyFactsheetCandidate, reported_as_of: date) -> bool:
    """Do not trust URL month to establish financial metric freshness."""
    return reported_as_of == candidate.expected_reporting_date
