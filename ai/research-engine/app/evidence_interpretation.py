"""Evidence-type-aware interpretation of an ``ExtractedDocument``.

OCR/file extraction (``app.document_extraction``) is TEXT ACQUISITION.
Its output alone is not canonical evidence -- turning raw extracted text
into CANDIDATE structured values for a SPECIFIC Research Readiness
requirement is a separate, evidence-type-aware step, implemented here.

Interpreters in this module are deterministic today (reusing the existing
canonical parser in ``app.shareholding`` wherever one already exists,
rather than inventing a parallel domain model -- see
``interpret_shareholding``). A future LLM-based interpreter would consume
the SAME ``ExtractedDocument`` input and is expected to produce candidate
data in the SAME shape; nothing in this module is specific to a
particular interpretation backend, and nothing here imports
``ResearchReadinessRuntime``, a specific evidence-type's readiness status,
or any LLM provider.

Every interpreter in this module:
  - never invents a missing value -- a field stays ``None``/empty when
    nothing recognizable was found, for the user to fill in during Review
  - never assigns a fabricated confidence score
  - produces CANDIDATES only -- the caller (``app.manual_evidence``) still
    owns Review/Accept and canonical persistence; nothing here is ever
    treated as accepted evidence.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from app.document_extraction import ExtractedDocument

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SHAREHOLDING
# ---------------------------------------------------------------------------


@dataclass
class ShareholdingInterpretation:
    """Candidate shareholding values recognized in an ``ExtractedDocument``.

    Two interpretation paths feed this, tried in order:
      1. The EXISTING canonical deterministic prose parser in
         ``app.shareholding`` (``parse_official_shareholding``) -- reused
         here, not duplicated. Handles an explicit "as on"/"ended" date
         phrase.
      2. A multi-column TABLE parser (see ``_parse_shareholding_table``)
         for the real-world screenshot shape: a header row of reporting
         periods (e.g. "Sep 2025  Dec 2025  Mar 2026  Jun 2026") followed
         by category rows, selecting the LATEST column.

    Nothing here is persisted directly; the caller still owns Review/
    Accept and canonical persistence (``app.manual_evidence``).
    """
    values: list[Any]  # list[app.models.ShareholdingSnapshotValue]
    period_end: datetime | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # The reporting period's precision ("DAY"/"MONTH"/"YEAR"/None) --
    # None when period_end itself is None (e.g. parsed via the prose
    # path, or a table period that could not be mapped to a canonical
    # date -- see _shareholding_quarter_end).
    period_precision: str | None = None
    # Non-canonical reference metadata recognized alongside the
    # canonical category values but with NO field in the existing
    # ShareholdingCategory domain model (e.g. "No. of Shareholders").
    # Never persisted as a canonical fact; preserved only so the source
    # information is not silently discarded (see module docstring note
    # on not inventing a parallel domain model). Keys are descriptive
    # strings, e.g. {"shareholderCount": "50407", "sourcePeriodText": "Jun 2026"}.
    supplementary: dict[str, str] | None = None


def interpret_shareholding(
    extracted: "ExtractedDocument",
    *,
    instrument_id: UUID | None,
    content_digest: str,
    filename: str | None,
    mime: str,
) -> ShareholdingInterpretation:
    """``ExtractedDocument`` -> candidate shareholding values.

    Reuses ``app.shareholding.parse_official_shareholding`` (the existing
    canonical deterministic parser) by constructing a synthetic
    ``ResearchDocument`` that satisfies its prerequisites -- the same
    approach ``app.manual_evidence`` used before this refactor, just
    relocated so it is independently testable and reusable without a
    ``ManualEvidenceIngestor`` instance.

    Any unexpected exception from the underlying parser degrades to an
    empty, errors-populated interpretation rather than propagating: real-
    world OCR text from a photographed/screenshotted table is far noisier
    (misread characters, partial table fragments) than the clean official
    filing text that parser was built against, and a runtime defect here
    must never become an unhandled 500.
    """
    from app.models import (
        DocumentStatus, DocumentType, ReliabilityLevel, ResearchDocument,
        SourceClassification, SourceMode, SourceType,
    )
    from app.normalization import normalize_text
    from app.shareholding import parse_official_shareholding

    document = ResearchDocument(
        canonical_url=f"manual-evidence:{content_digest}",
        original_url=f"manual-evidence:{content_digest}",
        title=filename,
        source_type=SourceType.REGULATORY_FILING,
        source_classification=SourceClassification.REGULATORY,
        source_name="USER_UPLOAD",
        published_at=None,
        retrieved_at=datetime.now(timezone.utc),
        language="en",
        content_type=mime,
        document_type=DocumentType.TEXT,
        status=DocumentStatus.PROCESSED,
        reliability_level=ReliabilityLevel.LEVEL_D,
        instrument_id=instrument_id,
        source_mode=SourceMode.REAL,
        freshness="REAL",
        content_hash=content_digest,
    )
    no_values_error = [
        "NO_SHAREHOLDING_VALUES_EXTRACTED: could not find recognizable "
        "shareholding category percentages in the document"
    ]
    try:
        document.normalized_text = normalize_text(extracted.text)
        document.raw_text = extracted.text
        snapshot = parse_official_shareholding(document)
    except Exception as exc:  # noqa: BLE001 -- see docstring
        logger.warning("shareholding_interpretation_failed reason=%s", exc, exc_info=True)
        snapshot = None
    if snapshot is not None and snapshot.values:
        return ShareholdingInterpretation(
            values=snapshot.values, period_end=snapshot.period_end,
            period_precision="DAY" if snapshot.period_end is not None else None,
        )

    # The prose "as on <date>" parser found nothing -- this is the real-
    # world shape your actual runtime screenshot has: a multi-column
    # table (reporting periods across the top, category rows below) with
    # no "as on"/"ended" narrative phrase at all. Try the table parser
    # before giving up.
    table_result = _parse_shareholding_table(extracted.text)
    if table_result is not None and (table_result.values or table_result.errors):
        return ShareholdingInterpretation(
            values=table_result.values,
            period_end=table_result.period_end,
            errors=table_result.errors,
            warnings=table_result.warnings,
            period_precision=table_result.period_precision,
            supplementary=table_result.supplementary,
        )

    return ShareholdingInterpretation(values=[], period_end=None, errors=no_values_error)


# ---------------------------------------------------------------------------
# SHAREHOLDING -- multi-column table interpretation
# ---------------------------------------------------------------------------
#
# The real runtime screenshot shape is a multi-column financial table:
#
#                     Sep 2025   Dec 2025   Mar 2026   Jun 2026
#     Promoters        73.67%     74.24%     74.24%     74.24%
#     FIIs              1.87%      1.58%      1.51%      1.87%
#     DIIs              0.01%      0.01%      0.09%      0.11%
#     Public           24.45%     24.16%     24.18%     23.78%
#     No. Shareholders 49,673     52,198     52,418     50,407
#
# This is NOT the "Shareholding as on <date>" narrative prose the
# existing app.shareholding.parse_official_shareholding parser was built
# for (see interpret_shareholding above, tried FIRST and unchanged). This
# section is additive: it only runs when that prose parser finds nothing.

# Deliberately narrow, explicit label variants -- NOT fuzzy/substring
# matching against arbitrary text (see module docstring; the task this
# was built against explicitly warns against "dangerously broad fuzzy
# matching"). Longest variants first so e.g. "promoter and promoter
# group" is preferred over a bare "promoter" prefix match.
_SHAREHOLDING_TABLE_LABELS: tuple[tuple["ShareholdingCategory", tuple[str, ...]], ...] = ()
_SHAREHOLDER_COUNT_LABELS: tuple[str, ...] = (
    "no. of shareholders", "no. shareholders", "number of shareholders", "no of shareholders",
)

_PERCENT_TOKEN = re.compile(r"(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*%?")
# Percentage-sum plausibility tolerance for a 4-category ownership
# breakdown (Promoter/FII/DII/Public): real-world OCR/rounding routinely
# lands a point or two off 100. This is a WARNING threshold surfaced for
# Review -- never a silent correction, never a hard rejection.
_OWNERSHIP_SUM_TOLERANCE = Decimal("3.0")


def _init_shareholding_table_labels() -> None:
    """Deferred so this module never needs ShareholdingCategory at
    import time in a way that could create an import cycle; called once
    at first use."""
    global _SHAREHOLDING_TABLE_LABELS
    if _SHAREHOLDING_TABLE_LABELS:
        return
    from app.models import ShareholdingCategory

    _SHAREHOLDING_TABLE_LABELS = (
        (ShareholdingCategory.PROMOTER, ("promoter and promoter group", "promoter holding", "promoters", "promoter")),
        (ShareholdingCategory.FII_FPI, ("foreign institutional investors", "foreign portfolio investors", "fii/fpi", "fiis", "fii", "fpis", "fpi")),
        (ShareholdingCategory.DII, ("domestic institutional investors", "diis", "dii")),
        (ShareholdingCategory.PUBLIC_RETAIL, ("public shareholders", "public/retail", "public")),
    )


def _match_label_prefix(line: str, variants: tuple[str, ...]) -> str | None:
    """Returns the remainder of ``line`` after a case-insensitive exact
    prefix match of one of ``variants``, or None if no variant matches
    the start of the line. Never a substring/fuzzy match."""
    stripped = line.strip()
    lowered = stripped.lower()
    for variant in variants:
        if lowered.startswith(variant):
            return stripped[len(variant):]
    return None


def _extract_row_numbers(remainder: str) -> list[Decimal]:
    numbers: list[Decimal] = []
    for match in _PERCENT_TOKEN.finditer(remainder):
        raw = match.group(1).replace(",", "")
        try:
            numbers.append(Decimal(raw))
        except InvalidOperation:
            continue
    return numbers


@dataclass
class _ShareholdingTableResult:
    values: list[Any]
    period_end: datetime | None
    period_precision: str | None
    supplementary: dict[str, str] | None
    warnings: list[str]
    errors: list[str]


def _shareholding_canonical_period_end(period: Any) -> date | None:
    """Map a recognized reporting period to the canonical
    ShareholdingSnapshot.period_end -- following the SAME common
    reporting-period normalization rule as every other period-based
    financial table (Balance Sheet, Cash Flow, P&L, Quarterly
    Financials): the application must never manufacture a day that was
    absent from the source.

    A DAY-precision period (an explicit source date, e.g. "30 Jun 2026")
    is used exactly as stated -- never touched.
    A MONTH-precision period (e.g. "Jun 2026") normalizes to the first
    day of that month (``period.normalized_date``, YYYY-MM-01) -- a
    technical placeholder only, never a claim that the source stated
    that day. This is NOT further rounded to a quarter-end/month-end
    date here (an earlier version of this function did that; it was
    corrected because inventing a day the source never stated conflicts
    with the platform's authoritative period-normalization rule).
    A MONTH-precision period whose month is NOT a quarter month (3/6/9/
    12) is not a plausible shareholding reporting period (SEBI
    shareholding filings are always quarterly) and returns None so the
    caller flags the candidate for Review rather than guessing. A
    YEAR-only period likewise returns None -- not enough precision to
    identify a shareholding quarter.

    The repository's shareholding grouping/selection logic (see
    app.repository._is_shareholding_quarter_period /
    shareholding_period_groups) recognizes BOTH this MONTH-normalized
    first-of-month date AND an explicit DAY-precision quarter-end date
    as the SAME semantic (year, month) reporting period -- the synthetic
    day is never required to identify the quarter.
    """
    if period is None:
        return None
    if period.precision == "DAY":
        return period.normalized_date
    if period.precision == "MONTH" and period.month in (3, 6, 9, 12):
        return period.normalized_date
    return None


def _parse_shareholding_table(text: str) -> "_ShareholdingTableResult | None":
    """Conservatively interpret a multi-column shareholding table:
    reporting-period columns across a header row, category rows below,
    mapping values from the LATEST column only. Returns None when no
    header row with at least two recognizable period columns is found
    at all -- the caller falls back to treating this as unrecognized
    text rather than guessing a table structure that is not genuinely
    there.
    """
    from app.reporting_period import find_period_tokens

    _init_shareholding_table_labels()

    lines = [line for line in text.splitlines() if line.strip()]
    header_periods: list[Any] = []
    header_index = -1
    for index, line in enumerate(lines):
        periods = find_period_tokens(line)
        if len(periods) >= 2:
            header_periods = periods
            header_index = index
            break
    if not header_periods:
        return None

    latest_period = max(header_periods, key=lambda p: p.sort_key)
    latest_column = header_periods.index(latest_period)
    if sum(1 for p in header_periods if p.sort_key == latest_period.sort_key) > 1:
        # A genuinely ambiguous/malformed header (the same period
        # appears twice) -- never guess which column is truly latest.
        return _ShareholdingTableResult(
            values=[], period_end=None, period_precision=None, supplementary=None,
            warnings=[],
            errors=["AMBIGUOUS_PERIOD_COLUMNS: multiple header columns share the same latest reporting period"],
        )

    collected: dict[Any, Decimal] = {}
    supplementary: dict[str, str] = {}
    warnings: list[str] = []
    errors: list[str] = []

    for line in lines[header_index + 1:]:
        matched_category = None
        remainder = None
        for category, variants in _SHAREHOLDING_TABLE_LABELS:
            remainder = _match_label_prefix(line, variants)
            if remainder is not None:
                matched_category = category
                break
        if matched_category is None:
            shareholder_remainder = _match_label_prefix(line, _SHAREHOLDER_COUNT_LABELS)
            if shareholder_remainder is not None:
                numbers = _extract_row_numbers(shareholder_remainder)
                if len(numbers) > latest_column:
                    supplementary["shareholderCount"] = str(int(numbers[latest_column]))
                else:
                    warnings.append(
                        "SHAREHOLDER_COUNT_COLUMN_COUNT_MISMATCH: row had fewer "
                        "values than header periods; shareholder count left unset"
                    )
            continue

        numbers = _extract_row_numbers(remainder)
        if len(numbers) <= latest_column:
            warnings.append(
                f"ROW_COLUMN_COUNT_MISMATCH: '{matched_category.value}' row had "
                f"{len(numbers)} value(s) but the header has {len(header_periods)} "
                "period columns -- left unset rather than guessed"
            )
            continue

        value = numbers[latest_column]
        if value < 0 or value > 100:
            errors.append(f"INVALID_PERCENTAGE_IN_TABLE: {matched_category.value} = {value}")
            continue
        if matched_category in collected:
            errors.append(f"DUPLICATE_CATEGORY_ROW: {matched_category.value} appears more than once")
            continue
        collected[matched_category] = value

    supplementary["sourcePeriodText"] = latest_period.original_text

    if not collected and not errors:
        return _ShareholdingTableResult(
            values=[], period_end=None, period_precision=None, supplementary=None,
            warnings=warnings,
            errors=["NO_SHAREHOLDING_VALUES_EXTRACTED: could not find recognizable "
                    "shareholding category percentages in the document"],
        )

    from app.models import ShareholdingCategory

    mandatory = {ShareholdingCategory.PROMOTER, ShareholdingCategory.FII_FPI,
                 ShareholdingCategory.DII, ShareholdingCategory.PUBLIC_RETAIL}
    if mandatory <= collected.keys():
        total = sum((collected[c] for c in mandatory), Decimal("0"))
        if abs(total - Decimal("100")) > _OWNERSHIP_SUM_TOLERANCE:
            warnings.append(
                f"OWNERSHIP_SUM_IMPLAUSIBLE: Promoter+FII+DII+Public = {total}%, "
                f"expected close to 100% (tolerance {_OWNERSHIP_SUM_TOLERANCE}%) -- "
                "review the extracted values before accepting"
            )

    period_end = _shareholding_canonical_period_end(latest_period)
    if period_end is None and not errors:
        warnings.append(
            f"PERIOD_NOT_RECOGNIZED_AS_SHAREHOLDING_QUARTER: '{latest_period.original_text}' "
            "is not a standard quarter-end reporting period for shareholding -- the period "
            "could not be mapped to a canonical date; review before accepting"
        )

    values = [
        _shareholding_table_value(category, percentage)
        for category, percentage in collected.items()
    ]

    return _ShareholdingTableResult(
        values=values,
        period_end=datetime(period_end.year, period_end.month, period_end.day, tzinfo=timezone.utc) if period_end else None,
        period_precision=latest_period.precision,
        supplementary=supplementary,
        warnings=warnings,
        errors=errors,
    )


def _shareholding_table_value(category: Any, percentage: Decimal) -> Any:
    """Construct an app.models.ShareholdingSnapshotValue for a table-
    derived candidate."""
    from app.models import ShareholdingSnapshotValue

    return ShareholdingSnapshotValue(
        category=category,
        percentage=percentage,
        metric_basis=None,
        raw_source_label=None,
        source_locator="manual-evidence:table-column",
        evidence_text=f"{category.value} value read from the latest reporting-period column of the uploaded table",
    )


# ---------------------------------------------------------------------------
# CURRENT_NEWS
# ---------------------------------------------------------------------------


@dataclass
class CurrentNewsCandidate:
    """Conservative, NON-BINDING field suggestions extracted from a
    CURRENT_NEWS screenshot/document -- never auto-accepted facts, never
    persisted, never used to compute readiness. A field left ``None``
    means "nothing recognizable was found", never a guess. The platform
    still requires the user to explicitly review and submit every
    CURRENT_NEWS manual field at accept() time (see
    app.manual_evidence.MANUAL_FIELD_SCHEMA /
    _validate_current_news_fields) -- these suggestions exist only to
    reduce retyping, exactly like a browser form autofill the user can
    freely overwrite or ignore. Provenance remains USER_UPLOAD regardless;
    a suggestion recognized from the screenshot does not make the
    underlying news independently verified."""
    title: str | None = None
    source_url: str | None = None
    event_date: str | None = None


_URL_PATTERN = re.compile(r"https?://[^\s)\]}\"'<>]+")
_SOURCE_LABEL_PATTERN = re.compile(r"(?:source|via)\s*[:\-]\s*(\S+)", re.I)
_DATE_PATTERNS = (
    re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),
    re.compile(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{4})\b"),
    re.compile(r"\b(\d{1,2}\s+[A-Za-z]+\s+\d{4})\b"),
    re.compile(r"\b([A-Za-z]+\s+\d{1,2},?\s+\d{4})\b"),
)
_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %B %Y", "%d %b %Y", "%B %d %Y", "%B %d, %Y", "%b %d %Y", "%b %d, %Y")
# Lines that are pure UI/navigation noise rather than a headline -- a
# conservative denylist, never a guess at what IS a headline.
_NOISE_LINE_PATTERN = re.compile(
    r"^(home|menu|search|login|sign in|subscribe|share|back|next|previous|"
    r"page \d+|\d+ of \d+|advertisement)$",
    re.I,
)


def _looks_like_headline(stripped: str) -> bool:
    """A conservative sentence-shape check, not a semantic one.

    UI/navigation chrome in a screenshot ("Home Menu Search", "Recent
    Partnerships & Announcements", "Next Earnings Date") is almost always
    Title-Cased with every word capitalized and no connecting lowercase
    words. A genuine headline/sentence ("Infosys announces strategic
    partnership with Columbia University") reliably contains several
    lowercase connector/verb words. Requiring at least two such words is a
    cheap, explainable filter -- never a claim that the line IS a
    headline, only that it is not obviously a UI label."""
    words = re.findall(r"[A-Za-z]+", stripped)
    lowercase_leading = sum(1 for w in words if w[:1].islower())
    return lowercase_leading >= 2


def _candidate_title(lines: list[str]) -> str | None:
    for line in lines:
        stripped = line.strip()
        if not (5 <= len(stripped) <= 140):
            continue
        if _NOISE_LINE_PATTERN.match(stripped):
            continue
        if not any(ch.isalpha() for ch in stripped):
            continue
        if not _looks_like_headline(stripped):
            continue
        return stripped
    return None


def _candidate_source_url(text: str) -> str | None:
    label_match = _SOURCE_LABEL_PATTERN.search(text)
    if label_match:
        candidate = label_match.group(1).strip().rstrip(".,;")
        if candidate:
            return candidate
    url_match = _URL_PATTERN.search(text)
    if url_match:
        return url_match.group(0).rstrip(".,;")
    return None


def _candidate_event_date(text: str) -> str | None:
    today = datetime.now(timezone.utc).date()
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            raw = match.group(1)
            for fmt in _DATE_FORMATS:
                try:
                    parsed = datetime.strptime(raw, fmt)
                except ValueError:
                    continue
                if parsed.date() > today:
                    # Never suggest a future date -- a line like "Next
                    # Earnings Date" is a forward-looking calendar
                    # entry, not a past news event; keep scanning for a
                    # genuine (past-or-today) event date instead of
                    # stopping at the first date-shaped text found.
                    break
                return parsed.date().isoformat()
    return None


def interpret_current_news(extracted: "ExtractedDocument") -> CurrentNewsCandidate:
    """``ExtractedDocument`` -> conservative CURRENT_NEWS field
    suggestions. Every field is independently optional -- a confident
    title with no recognizable date/source is still a useful candidate;
    nothing here requires all three to proceed."""
    text = extracted.text or ""
    if not text.strip():
        return CurrentNewsCandidate()
    lines = [line for line in text.splitlines() if line.strip()]
    return CurrentNewsCandidate(
        title=_candidate_title(lines),
        source_url=_candidate_source_url(text),
        event_date=_candidate_event_date(text),
    )
