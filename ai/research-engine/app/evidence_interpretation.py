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


def classify_shareholding_fields(result: "ShareholdingInterpretation") -> dict[str, str]:
    """TASK E item 3 -- partial-extraction classification.

    Classifies each of the four primary ownership categories (reusing
    the existing ``ShareholdingCategory`` domain model -- no new/
    competing schema) as one of:
      * "EXTRACTED" -- a value for this category is in ``result.values``.
      * "INVALID"   -- the category's row/label was matched but its
                       value could not be resolved (a cross-checked
                       spatial/positional disagreement, a column-count
                       mismatch, or an ambiguous value on its own row)
                       -- never fabricated, left unset with a warning.
      * "AMBIGUOUS" -- the row's label itself fuzzy-matched more than
                       one category and was therefore left unassigned.
      * "MISSING"   -- the category was not mentioned at all; nothing
                       in the source let it be recognized.

    This never discards or recomputes anything -- it only reads the
    warnings/values ``interpret_shareholding`` already produced, so a
    failure to resolve one category can never affect another's
    classification.
    """
    from app.models import ShareholdingCategory

    primary_fields: tuple[ShareholdingCategory, ...] = (
        ShareholdingCategory.PROMOTER,
        ShareholdingCategory.FII_FPI,
        ShareholdingCategory.DII,
        ShareholdingCategory.PUBLIC_RETAIL,
    )
    resolved = {v.category for v in result.values}
    classification: dict[str, str] = {}
    for category in primary_fields:
        if category in resolved:
            classification[category.value] = "EXTRACTED"
            continue
        tag = f"'{category.value}'"
        is_invalid = any(
            tag in w
            and (
                "ROW_COLUMN_COUNT_MISMATCH" in w
                or "SPATIAL_POSITIONAL_DISAGREEMENT" in w
                or "VERTICAL_ROW_AMBIGUOUS_VALUE" in w
            )
            for w in result.warnings
        )
        if is_invalid:
            classification[category.value] = "INVALID"
            continue
        is_ambiguous = any(
            w.startswith("AMBIGUOUS_CATEGORY_LABEL") and category.value in w
            for w in result.warnings
        )
        classification[category.value] = "AMBIGUOUS" if is_ambiguous else "MISSING"
    return classification


def resolve_corrected_shareholding_period(text: str) -> "datetime | None":
    """TASK E2 item 5 -- parse a user-supplied reporting-period
    correction, never a value manufactured out of nothing.

    Accepts TWO equally-legitimate input shapes, both reusing existing
    conventions rather than inventing a third date parser:
      * An ISO date -- "YYYY-MM-DD" (DAY precision, used exactly as
        given) or "YYYY-MM" (MONTH precision, restricted to a quarter
        month 3/6/9/12 exactly like extraction's own MONTH-precision
        rule) -- the SAME ``datetime.fromisoformat``-style convention
        every other manual evidence date field in this app already uses
        (see e.g. FinancialFact ``periodEnd`` corrections above).
      * The same free-text shape recognized during extraction itself
        ("Jun 2026", "30 Jun 2026") -- reusing
        ``app.reporting_period.parse_reporting_period`` +
        ``_shareholding_canonical_period_end`` directly, so a user
        pasting the source document's own period text behaves exactly
        like the original extraction would have.

    Returns ``None`` when the text does not resolve to a valid
    SHAREHOLDING quarter-end/quarter-month period -- the caller
    (``app.manual_evidence``) turns that into a validation error rather
    than guessing or defaulting.
    """
    stripped = (text or "").strip()
    if not stripped:
        return None

    iso_day = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", stripped)
    if iso_day:
        year, month, day = (int(g) for g in iso_day.groups())
        try:
            return datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            return None

    iso_month = re.fullmatch(r"(\d{4})-(\d{2})", stripped)
    if iso_month:
        year, month = (int(g) for g in iso_month.groups())
        if not (1 <= month <= 12) or month not in (3, 6, 9, 12):
            return None
        return datetime(year, month, 1, tzinfo=timezone.utc)

    from app.reporting_period import parse_reporting_period

    period = parse_reporting_period(stripped)
    period_end = _shareholding_canonical_period_end(period)
    if period_end is None:
        return None
    return datetime(period_end.year, period_end.month, period_end.day, tzinfo=timezone.utc)


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
    ocr_words = getattr(extracted, "metadata", None) and extracted.metadata.get("ocrWords")
    table_result = _parse_shareholding_table(extracted.text, words=ocr_words)
    if table_result is not None and (table_result.values or table_result.errors):
        return ShareholdingInterpretation(
            values=table_result.values,
            period_end=table_result.period_end,
            errors=table_result.errors,
            warnings=table_result.warnings,
            period_precision=table_result.period_precision,
            supplementary=table_result.supplementary,
        )

    # Neither the prose "as on <date>" parser nor the horizontal
    # multi-period TABLE parser found a recognizable shape. Try the
    # VERTICAL key-value layout (one label, one value per line, e.g. a
    # cropped or differently-formatted screenshot with no header row of
    # period columns at all) before giving up.
    vertical_result = _parse_vertical_key_value_shareholding(extracted.text)
    if vertical_result is not None and (vertical_result.values or vertical_result.errors):
        return ShareholdingInterpretation(
            values=vertical_result.values,
            period_end=vertical_result.period_end,
            errors=vertical_result.errors,
            warnings=vertical_result.warnings,
            period_precision=vertical_result.period_precision,
            supplementary=vertical_result.supplementary,
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
        (ShareholdingCategory.PROMOTER, (
            "promoter and promoter group", "promoter holding", "promoter group", "promoters", "promoter",
        )),
        (ShareholdingCategory.FII_FPI, (
            "foreign institutional investors", "foreign portfolio investors", "fii/fpi", "fiis", "fii", "fpis", "fpi",
        )),
        (ShareholdingCategory.DII, ("domestic institutional investors", "diis", "dii")),
        (ShareholdingCategory.PUBLIC_RETAIL, (
            "public shareholding", "public shareholders", "public/retail", "public",
        )),
    )


def _levenshtein(a: str, b: str) -> int:
    """Classic O(len(a)*len(b)) edit distance -- no new dependency, and
    this module only ever calls it against short (<= ~35 char) explicit
    label variants, never arbitrary document text, so the cost is
    trivially bounded."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
        previous = current
    return previous[-1]


_DECORATIVE_LABEL_PREFIX = re.compile(
    r"^[\s\-\u2010-\u2015\u2022\u25CF\u25AA\u25B6\u2192\*#>\|\+]+"
)


def _strip_decorative_prefix(text: str) -> str:
    """Strip a leading run of decorative/punctuation characters a real
    table screenshot or OCR pass commonly adds before a row label --
    bullets, dashes, stars, arrows, pipes (e.g. "- Promoters", "* FIIs",
    "-> DIIs", "\u2022 Public") -- so semantic label matching is not
    defeated by decoration that carries no meaning. Never touches the
    label text itself or anything after it, only what comes strictly
    before the first letter/digit, so a genuinely different label is
    never turned into a different one.
    """
    return _DECORATIVE_LABEL_PREFIX.sub("", text.strip()).strip()


def _match_label_prefix(line: str, variants: tuple[str, ...]) -> str | None:
    """Returns the remainder of ``line`` after an EXACT case-insensitive
    prefix match of one of ``variants``, or None if no variant matches
    the start of the line. Never a fuzzy match -- see
    ``_fuzzy_label_candidates`` for the bounded OCR-typo fallback, which
    is deliberately a separate, cross-category-aware step (see its
    docstring for why fuzzy matching cannot safely be done one category
    at a time)."""
    stripped = _strip_decorative_prefix(line)
    lowered = stripped.lower()
    for variant in variants:
        if lowered.startswith(variant):
            return stripped[len(variant):]
    return None


def _fuzzy_label_candidates(
    line: str, labels: tuple[tuple[Any, tuple[str, ...]], ...]
) -> list[tuple[Any, str]]:
    """Bounded OCR-typo fallback for category-row label matching, tried
    ONLY after an exact prefix match against EVERY category's variants
    has already failed (see the caller). Returns every (category,
    remainder) this line fuzzy-matches, across ALL categories -- the
    caller must treat more than one candidate as genuinely ambiguous and
    refuse to guess, rather than taking "whichever category happened to
    be checked first".

    This safety requirement is NOT optional: ``_SHAREHOLDING_TABLE_LABELS``
    deliberately contains short, visually/phonetically similar real
    labels (e.g. "fii"/"fiis" vs "dii"/"diis" differ by a single
    character), so checking fuzzy distance one category at a time and
    returning the first hit would silently misclassify one real label as
    a DIFFERENT real label (e.g. "DIIs" fuzzy-matching FII_FPI's "fiis"
    before DII's own variants are ever tried) -- which is worse than the
    "detect ambiguity" requirement this closure exists to satisfy, it is
    a wrong answer presented with false confidence. Collecting candidates
    across every category first, and only proceeding when exactly one
    came back, is what makes this safe.

    Never applied to a variant shorter than 6 characters (anything
    shorter -- "fii", "dii", "fpi" -- is too close to another real label
    for ANY edit-distance tolerance to be safe), and the character
    immediately after the matched slice must not be a letter (so
    "Promoterish" is never treated as a misspelled "Promoter" row).
    """
    stripped = _strip_decorative_prefix(line)
    lowered = stripped.lower()
    candidates: list[tuple[Any, str]] = []
    for category, variants in labels:
        for variant in variants:
            if len(variant) < 6 or len(lowered) < len(variant):
                continue
            candidate = lowered[:len(variant)]
            tolerance = 1 if len(variant) <= 9 else 2
            if _levenshtein(candidate, variant) <= tolerance:
                next_char = lowered[len(variant):len(variant) + 1]
                if next_char and next_char.isalpha():
                    continue
                candidates.append((category, stripped[len(variant):]))
                break  # one candidate per category is enough
    return candidates


def _margin_safe_short_variant_match(
    line: str, labels: tuple[tuple[Any, tuple[str, ...]], ...]
) -> "tuple[Any, str] | None":
    """FINAL CLOSURE fix -- confidently resolve a SHORT (3-5 char)
    category variant despite a single OCR typo, but ONLY when exactly
    one category is the clear, unique closest match across ALL
    categories. A genuinely tied/ambiguous garble is never resolved
    here and falls through to ``_resembling_category_candidates``'s
    conservative warning-only path, unchanged.

    Short real labels in this domain are close to EACH OTHER ("fiis"
    and "diis" are themselves only 1 edit apart), so a single-character
    OCR typo in "FIIs" (e.g. the real runtime shape "Flis", one of the
    two capital I's misread as a lowercase l) can coincidentally also
    land within a fixed tolerance of "DIIs". Blindly auto-assigning
    whenever exactly one candidate happens to pass a FIXED tolerance
    (as the existing >=6-char fuzzy path safely does for long variants)
    would therefore be unsafe here -- that is exactly why this domain's
    short variants were originally routed to a warning-only detector
    instead. This function adds the missing safety margin instead of
    removing the caution: it keeps the BEST (lowest-distance) candidate
    per category and commits ONLY when that category's distance is
    STRICTLY lower than every other category's best distance for this
    SAME line -- a real, measured margin, not just "nothing else
    happened to also pass" and not just "the user's prompt lists this
    as a recognized alias" (aliases still only ever apply through exact
    matching first; this is the bounded, measured fallback for a real
    typo). "Flis" is reliably 1 edit from "fiis" but 2 from "diis": a
    clear, safe margin -> FII_FPI. "Flls"/"Dlls" (two letters corrupted)
    are each reliably 1 edit closer to their own true category than to
    the other, by this same measured rule. A line that lands EQUALLY
    close to two categories is never resolved by this function.
    """
    stripped = _strip_decorative_prefix(line)
    lowered = stripped.lower()
    best_by_category: dict[Any, tuple[int, str]] = {}
    for category, variants in labels:
        for variant in variants:
            if len(variant) > 5 or len(lowered) < len(variant):
                continue  # >=6-char variants are _fuzzy_label_candidates's job
            candidate = lowered[:len(variant)]
            if candidate == variant:
                continue  # exact match is handled elsewhere, before this ever runs
            tolerance = 2 if len(variant) >= 4 else 1
            distance = _levenshtein(candidate, variant)
            if distance > tolerance:
                continue
            next_char = lowered[len(variant):len(variant) + 1]
            if next_char and next_char.isalpha():
                continue
            remainder = stripped[len(variant):]
            if not _extract_row_numbers(remainder):
                continue  # no number follows the label -- not a table row at all
            existing = best_by_category.get(category)
            if existing is None or distance < existing[0]:
                best_by_category[category] = (distance, remainder)

    if not best_by_category:
        return None
    ranked = sorted(best_by_category.items(), key=lambda item: item[1][0])
    best_category, (best_distance, best_remainder) = ranked[0]
    if len(ranked) > 1 and ranked[1][1][0] <= best_distance:
        return None  # tied (or worse) with another category -- genuinely ambiguous
    return best_category, best_remainder


def _resembling_category_candidates(
    line: str, labels: tuple[tuple[Any, tuple[str, ...]], ...]
) -> list[tuple[Any, str]]:
    """Bounded 'this looks like it might be a shareholding category, but
    we refuse to guess which' detector -- used ONLY to raise a warning,
    NEVER to assign a category (the caller must never use this result to
    populate a value). Deliberately separate from
    ``_fuzzy_label_candidates``, which only ever acts on variants >= 6
    characters (long enough that a tolerance-1 edit is safe to
    auto-assign from). The SHORT 3-5 character variants this domain's
    labels are built from ("fii", "dii", "fpi", "fiis", "diis", "fpis")
    are too close to EACH OTHER for any auto-assignment to be safe (an
    "l"/"I" OCR misread turns "FIIs" into "Flls" just as easily as it
    would turn "DIIs" into "Dlls" -- both land equally close to both
    labels), but a line that resembles one of them within a small edit
    distance is still clearly worth a human's attention rather than
    vanishing with zero trace.

    Only fires on a line that otherwise looks like a genuine table row
    (at least one number follows the candidate label) and whose
    character immediately after the matched slice is not a letter, so
    arbitrary unrelated prose or a random short OCR token never
    triggers this -- see the task's explicit "do not emit warnings for
    arbitrary unrelated text or every OCR token" requirement.
    """
    stripped = _strip_decorative_prefix(line)
    lowered = stripped.lower()
    candidates: list[tuple[Any, str]] = []
    for category, variants in labels:
        for variant in variants:
            if len(variant) > 5 or len(lowered) < len(variant):
                continue  # >=6-char variants are _fuzzy_label_candidates's job
            candidate = lowered[:len(variant)]
            if candidate == variant:
                continue  # exact match is handled elsewhere, before this ever runs
            tolerance = 2 if len(variant) >= 4 else 1
            if _levenshtein(candidate, variant) > tolerance:
                continue
            next_char = lowered[len(variant):len(variant) + 1]
            if next_char and next_char.isalpha():
                continue
            remainder = stripped[len(variant):]
            if not _extract_row_numbers(remainder):
                continue  # no number follows the label -- not a table row at all
            candidates.append((category, remainder))
            break
    return candidates


def _looks_like_pure_value_remainder(remainder: str) -> bool:
    """True when ``remainder`` (the text immediately after a matched
    category-label prefix) contains nothing but table VALUES -- numbers,
    percent signs, commas, whitespace -- never an ordinary word. This is
    the "table context" check: it is what tells a genuine ownership-
    category table ROW ("FIIs  1.87  1.58") apart from a narrative
    sentence that merely happens to START with the same word ("FII
    inflows were strong this quarter", "FII category includes GDRs") --
    an investment-flow or other financial-concept mention, not an
    ownership-percentage row. A real row's remainder, once whitespace is
    discounted, is only numbers; a sentence's has ordinary words in it.
    An empty remainder (nothing at all after the label) also counts as
    "pure" here -- the caller separately rejects rows with zero
    extracted numbers, so an empty remainder is already handled there.
    """
    stripped = remainder.strip()
    if not stripped:
        return True
    # Recognized non-numeric VALUE placeholders ("N/A", "NA", "nil") are
    # still a value, not narrative text -- remove them first.
    without_placeholders = re.sub(r"(?i)\bn/?a\b|\bnil\b", "", stripped)
    # Remove every numeric/percentage token (handles both space- and
    # comma-SEPARATED rows -- e.g. a CSV upload's "73.67,74.24,74.24" has
    # no spaces at all between values, so splitting on whitespace alone
    # would treat the whole comma-joined remainder as one non-numeric
    # "token" and misfire; matching numeric tokens directly avoids that).
    without_numbers = _PERCENT_TOKEN.sub("", without_placeholders)
    # Whatever is left should be nothing but separators/punctuation/
    # whitespace. Any letter still present means this is narrative text
    # (an investment-flow/other financial-concept mention), not a
    # genuine ownership-category table row.
    # "+" is included here (ROOT CAUSE of the Gokul Agro runtime
    # regression): a real screenshot's OCR pass commonly misreads a
    # table gridline/divider as a literal "+" character inside a row's
    # remainder. Without "+" in this removal set, that ONE stray
    # character was enough to make leftover non-empty, so the table-
    # context guard misclassified every genuine numeric row (PROMOTER/
    # FII_FPI/DII/PUBLIC_RETAIL alike) as narrative prose and discarded
    # it entirely -- never a label-matching problem, a value-remainder
    # one.
    leftover = re.sub(r"[\s,.\-%:;/\+]+", "", without_numbers)
    return leftover == ""


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


def _parse_shareholding_table(text: str, words: "list[Any] | None" = None) -> "_ShareholdingTableResult | None":
    """Conservatively interpret a multi-column shareholding table:
    reporting-period columns across a header row, category rows below,
    mapping values from the LATEST column only. Returns None when no
    header row with at least two recognizable period columns is found
    at all -- the caller falls back to treating this as unrecognized
    text rather than guessing a table structure that is not genuinely
    there.

    When ``words`` (word-level OCR bounding boxes, see
    app.spatial_table_reconstruction) is available, each row's value is
    associated with the latest header column by actual HORIZONTAL
    POSITION -- the column whose x-center the value's x-center genuinely
    lines up under -- rather than by counting "the Nth number found on
    this line" as before. This is the fix for the general defect: a
    single dropped/merged/reordered OCR token anywhere on a row no
    longer silently shifts every later value onto the wrong column, and
    a value that doesn't spatially line up under ANY column, or lines up
    ambiguously between two, is left unset/flagged rather than guessed.
    When ``words`` is None (non-image evidence, or OCR ran without
    spatial data), this falls back to the original position-counting
    behavior unchanged -- no regression for any existing caller.
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

    # Spatial column centers, aligned to the SAME header line this
    # text-based pass already located (by index among non-blank lines),
    # so latest_column still indexes consistently into both
    # representations. Only attempted when words were supplied AND the
    # spatial reconstruction of that line agrees on finding >= 2 period
    # columns in the same left-to-right order -- any mismatch (e.g. the
    # text and words disagree, which should not normally happen since
    # both come from the same OCR pass, but is checked rather than
    # assumed) silently falls back to the position-counting path for
    # safety rather than risking a misaligned spatial assignment.
    column_centers: list[float] | None = None
    spatial_rows: dict[int, list[Any]] | None = None
    if words:
        from app.spatial_table_reconstruction import (
            find_header_period_columns, group_into_lines, line_text, looks_like_number_token,
            nearest_column_index,
        )
        spatial_lines = group_into_lines(words)
        spatial_texts = [line_text(sl) for sl in spatial_lines]
        if (
            len(spatial_lines) == len(lines)
            and header_index < len(spatial_lines)
            and spatial_texts[header_index].split() == lines[header_index].split()
        ):
            header_columns = find_header_period_columns(spatial_lines[header_index])
            if len(header_columns) == len(header_periods):
                column_centers = [center for _period, center in header_columns]
                spatial_rows = {
                    row_index: [w for w in spatial_lines[row_index] if looks_like_number_token(w.text)]
                    for row_index in range(header_index + 1, len(spatial_lines))
                }

    def _spatial_value_for_latest_column(line_index: int, row_label: str) -> "tuple[Decimal | None, str | None]":
        """Resolve the latest-period value for one row by spatial column
        assignment. Returns (value, warning); value is None (with a
        warning explaining why) when it cannot be determined without
        guessing -- no value lining up under the latest column at all,
        or more than one value lining up near it."""
        numeric_words = spatial_rows.get(line_index, []) if spatial_rows is not None else []
        assigned: list[tuple[Any, bool]] = []
        for word in numeric_words:
            index, ambiguous = nearest_column_index(word.x_center, column_centers or [])
            if index == latest_column:
                assigned.append((word, ambiguous))
        if not assigned:
            return None, (
                f"COLUMN_VALUE_NOT_SPATIALLY_ALIGNED: '{row_label}' has no value positioned "
                f"under the latest period column ('{latest_period.original_text}') -- left "
                "unset rather than guessed from reading order"
            )
        if len(assigned) > 1 or any(ambiguous for _word, ambiguous in assigned):
            return None, (
                f"COLUMN_VALUE_AMBIGUOUS: '{row_label}' has more than one value positioned "
                f"near the latest period column ('{latest_period.original_text}') -- left "
                "unset rather than guessed"
            )
        raw = assigned[0][0].text.strip().rstrip("%").replace(",", "")
        try:
            return Decimal(raw), None
        except InvalidOperation:
            return None, None

    use_spatial = column_centers is not None and spatial_rows is not None

    def _cross_checked_spatial_value(
        line_index: int, row_label: str, remainder_for_positional: str
    ) -> "tuple[Decimal | None, str | None]":
        """Resolve a row's latest-column value via spatial assignment,
        then cross-check it against what plain positional counting
        would have produced from the SAME row. Spatial assignment is
        never trusted blindly: when both methods disagree (and
        positional counting could itself resolve a value -- i.e. the
        row had enough numeric tokens from the plain-text pass), that is
        a genuine signal that something is wrong with this row (an
        unexpected token, an OCR artifact split into an extra
        number, etc.) and the value is left unset for manual review
        rather than silently preferring one answer over the other.
        """
        spatial_value, warning = _spatial_value_for_latest_column(line_index, row_label)
        if spatial_value is None:
            return spatial_value, warning
        positional_numbers = _extract_row_numbers(remainder_for_positional)
        if len(positional_numbers) > latest_column:
            positional_value = positional_numbers[latest_column]
            if positional_value != spatial_value:
                return None, (
                    f"SPATIAL_POSITIONAL_DISAGREEMENT: '{row_label}' -- spatial column "
                    f"assignment found {spatial_value} but positional counting of the same "
                    f"row found {positional_value} for the latest period column -- left "
                    "unset rather than silently preferring either; review the source row"
                )
        return spatial_value, warning

    for line_index, line in enumerate(lines[header_index + 1:], start=header_index + 1):
        matched_category = None
        remainder = None
        for category, variants in _SHAREHOLDING_TABLE_LABELS:
            remainder = _match_label_prefix(line, variants)
            if remainder is not None:
                matched_category = category
                break
        if matched_category is None:
            # No EXACT match against any category. Only now consider the
            # bounded OCR-typo fuzzy fallback -- and only act on it when
            # exactly one category came back, across ALL categories at
            # once. Short real labels in this domain are mutually similar
            # ("fii"/"dii"), so checking fuzzy distance category-by-category
            # and taking the first hit (the old behavior) could silently
            # misclassify one real label as a different one; collecting
            # candidates globally first is what makes this safe.
            fuzzy_candidates = _fuzzy_label_candidates(line, _SHAREHOLDING_TABLE_LABELS)
            if len(fuzzy_candidates) == 1:
                matched_category, remainder = fuzzy_candidates[0]
            elif len(fuzzy_candidates) > 1:
                warnings.append(
                    "AMBIGUOUS_CATEGORY_LABEL: '" + line.strip() + "' fuzzy-matches more "
                    "than one category (" + ", ".join(c.value for c, _r in fuzzy_candidates)
                    + ") -- left unmatched rather than guessed"
                )
            else:
                # Neither an exact nor a safe (>=6-char) fuzzy match --
                # FINAL CLOSURE fix: before falling back to a mere
                # warning, check whether exactly one category is the
                # clear, MARGIN-safe closest short-variant match (e.g.
                # the real runtime shape "Flis", a single-character OCR
                # typo of "FIIs" that is reliably closer to "fiis" than
                # to any other category) -- if so, resolve it with
                # confidence, exactly like the >=6-char fuzzy path does.
                # A genuinely tied/ambiguous garble is never resolved
                # here (see _margin_safe_short_variant_match) and still
                # falls through to the conservative resemblance warning
                # below, unchanged.
                margin_match = _margin_safe_short_variant_match(line, _SHAREHOLDING_TABLE_LABELS)
                if margin_match is not None:
                    matched_category, remainder = margin_match
                else:
                    # Still check the SHORT-variant resemblance detector
                    # before giving up silently. This NEVER assigns a
                    # category (matched_category stays None either way);
                    # it only raises a warning so a genuinely garbled
                    # FII/DII/FPI-like row is diagnosable instead of
                    # vanishing with no trace at all.
                    resembling = _resembling_category_candidates(line, _SHAREHOLDING_TABLE_LABELS)
                    if len(resembling) == 1:
                        warnings.append(
                            "UNMATCHED_CATEGORY_LABEL_RESEMBLANCE: '" + line.strip() + "' resembles "
                            + resembling[0][0].value + " but was not confidently matched -- left "
                            "unassigned rather than guessed; review the source row"
                        )
                    elif len(resembling) > 1:
                        warnings.append(
                            "AMBIGUOUS_CATEGORY_LABEL_RESEMBLANCE: '" + line.strip() + "' resembles "
                            "more than one category (" + ", ".join(c.value for c, _r in resembling)
                            + ") -- left unassigned rather than guessed"
                        )

        if matched_category is not None and not _looks_like_pure_value_remainder(remainder):
            # TABLE-CONTEXT guard: the text right after the matched
            # label is not just numbers (e.g. "FII inflows were strong
            # this quarter", "FII category includes GDRs") -- this is a
            # narrative mention of an investment flow or other financial
            # concept, not a genuine ownership-category table row. Never
            # treated as a match: no value, no warning, because this is
            # simply not a table row at all (not an ambiguous or garbled
            # one either), so raising a warning here would violate "do
            # not emit warnings for arbitrary unrelated text".
            matched_category = None
            remainder = None

        if matched_category is None:
            shareholder_remainder = _match_label_prefix(line, _SHAREHOLDER_COUNT_LABELS)
            if shareholder_remainder is not None:
                if use_spatial:
                    value, warning = _cross_checked_spatial_value(
                        line_index, "No. of Shareholders", shareholder_remainder
                    )
                    if warning:
                        warnings.append(warning)
                else:
                    numbers = _extract_row_numbers(shareholder_remainder)
                    if len(numbers) > latest_column:
                        value = numbers[latest_column]
                    else:
                        value = None
                        warnings.append(
                            "SHAREHOLDER_COUNT_COLUMN_COUNT_MISMATCH: row had fewer "
                            "values than header periods; shareholder count left unset"
                        )
                if value is not None:
                    supplementary["shareholderCount"] = str(int(value))
            continue

        if use_spatial:
            value, warning = _cross_checked_spatial_value(line_index, matched_category.value, remainder)
            if warning:
                warnings.append(warning)
        else:
            numbers = _extract_row_numbers(remainder)
            if len(numbers) <= latest_column:
                value = None
                warnings.append(
                    f"ROW_COLUMN_COUNT_MISMATCH: '{matched_category.value}' row had "
                    f"{len(numbers)} value(s) but the header has {len(header_periods)} "
                    "period columns -- left unset rather than guessed"
                )
            else:
                value = numbers[latest_column]
        if value is None:
            continue

        if value < 0 or value > 100:
            errors.append(f"INVALID_PERCENTAGE_IN_TABLE: {matched_category.value} = {value}")
            continue
        if matched_category in collected:
            errors.append(f"DUPLICATE_CATEGORY_ROW: {matched_category.value} appears more than once")
            continue
        collected[matched_category] = value

    supplementary["sourcePeriodText"] = latest_period.original_text

    if not collected and not errors:
        # TASK "FINAL FIX" item 5/6 -- even when NO category values were
        # recognized at all, the LATEST reporting period and any
        # supplementary fields (shareholderCount) the loop above already
        # found are real, independently-recognized facts and must not be
        # thrown away just because every category row failed. Previously
        # this branch hard-reset period_end/period_precision/
        # supplementary to None/None/None, which is why a runtime draft
        # with zero extracted ownership facts also showed a BLANK
        # reporting period and lost shareholderCount, even though both
        # had genuinely been recognized.
        period_end = _shareholding_canonical_period_end(latest_period)
        return _ShareholdingTableResult(
            values=[],
            period_end=(
                datetime(period_end.year, period_end.month, period_end.day, tzinfo=timezone.utc)
                if period_end else None
            ),
            period_precision=latest_period.precision,
            supplementary=supplementary,
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


def _parse_vertical_key_value_shareholding(text: str) -> "_ShareholdingTableResult | None":
    """Fallback for a VERTICAL key-value layout: one category label and
    one value per line (no header row of period columns at all), e.g.

        Promoter: 74.24%
        FII/FPI: 1.87%
        DII: 0.11%
        Public: 23.78%
        Period: Jun 2026

    Only attempted when ``_parse_shareholding_table`` found no header
    row with >= 2 period columns (the horizontal-table shape), so this
    never second-guesses a genuine multi-period table. Each value is
    tied to the label on ITS OWN line only -- never inferred from
    another line or from numerical proximity -- reusing the exact same
    label variants/fuzzy-match safety as the horizontal table parser and
    the exact same deterministic period parser the rest of this module
    uses. Conservative by construction: requires exactly ONE recognizable
    reporting-period token somewhere in the document (zero or more than
    one is left unset rather than guessed which one applies), and skips
    any line whose category or value cannot be unambiguously determined.
    """
    from app.reporting_period import find_period_tokens

    _init_shareholding_table_labels()
    lines = [line for line in text.splitlines() if line.strip()]

    period_tokens: list[Any] = []
    for line in lines:
        period_tokens.extend(find_period_tokens(line))
    distinct_periods = {p.sort_key: p for p in period_tokens}
    if len(distinct_periods) != 1:
        # No period at all, or more than one distinct period mentioned
        # (e.g. this is actually a horizontal table header line that
        # just didn't satisfy the >=2-period-columns table shape) --
        # never guess which period the values belong to.
        return None
    period = next(iter(distinct_periods.values()))

    collected: dict[Any, Decimal] = {}
    supplementary: dict[str, str] = {}
    warnings: list[str] = []
    errors: list[str] = []

    for line in lines:
        matched_category = None
        remainder = None
        for category, variants in _SHAREHOLDING_TABLE_LABELS:
            remainder = _match_label_prefix(line, variants)
            if remainder is not None:
                matched_category = category
                break
        if matched_category is None:
            fuzzy_candidates = _fuzzy_label_candidates(line, _SHAREHOLDING_TABLE_LABELS)
            if len(fuzzy_candidates) == 1:
                matched_category, remainder = fuzzy_candidates[0]
            elif len(fuzzy_candidates) > 1:
                warnings.append(
                    "AMBIGUOUS_CATEGORY_LABEL: '" + line.strip() + "' fuzzy-matches more "
                    "than one category (" + ", ".join(c.value for c, _r in fuzzy_candidates)
                    + ") -- left unmatched rather than guessed"
                )
            else:
                # See the table parser's identical block for why this
                # NEVER assigns a category -- warning only.
                resembling = _resembling_category_candidates(line, _SHAREHOLDING_TABLE_LABELS)
                if len(resembling) == 1:
                    warnings.append(
                        "UNMATCHED_CATEGORY_LABEL_RESEMBLANCE: '" + line.strip() + "' resembles "
                        + resembling[0][0].value + " but was not confidently matched -- left "
                        "unassigned rather than guessed; review the source row"
                    )
                elif len(resembling) > 1:
                    warnings.append(
                        "AMBIGUOUS_CATEGORY_LABEL_RESEMBLANCE: '" + line.strip() + "' resembles "
                        "more than one category (" + ", ".join(c.value for c, _r in resembling)
                        + ") -- left unassigned rather than guessed"
                    )
        if matched_category is None:
            shareholder_remainder = _match_label_prefix(line, _SHAREHOLDER_COUNT_LABELS)
            if shareholder_remainder is not None:
                numbers = _extract_row_numbers(shareholder_remainder)
                if len(numbers) == 1:
                    supplementary["shareholderCount"] = str(int(numbers[0]))
                elif len(numbers) > 1:
                    warnings.append(
                        "SHAREHOLDER_COUNT_AMBIGUOUS_VALUE: more than one number found on "
                        "the shareholder-count line -- left unset rather than guessed"
                    )
            continue

        numbers = _extract_row_numbers(remainder)
        if len(numbers) != 1:
            # Zero values (label with no number), or more than one
            # number on a single-value line -- this line is not
            # genuinely a one-label/one-value vertical row; skip rather
            # than guess which number is the real one.
            if numbers:
                warnings.append(
                    f"VERTICAL_ROW_AMBIGUOUS_VALUE: '{matched_category.value}' line has "
                    f"{len(numbers)} numbers but exactly one value is expected -- left unset"
                )
            continue
        value = numbers[0]

        if value < 0 or value > 100:
            errors.append(f"INVALID_PERCENTAGE_IN_TABLE: {matched_category.value} = {value}")
            continue
        if matched_category in collected:
            errors.append(f"DUPLICATE_CATEGORY_ROW: {matched_category.value} appears more than once")
            continue
        collected[matched_category] = value

    supplementary["sourcePeriodText"] = period.original_text

    if not collected and not errors:
        return None  # not a recognizable vertical key-value shareholding layout at all

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

    period_end = _shareholding_canonical_period_end(period)
    if period_end is None and not errors:
        warnings.append(
            f"PERIOD_NOT_RECOGNIZED_AS_SHAREHOLDING_QUARTER: '{period.original_text}' "
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
        period_precision=period.precision,
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


# ---------------------------------------------------------------------------
# VALUATION_INPUTS
# ---------------------------------------------------------------------------
#
# TASK F -- automatic extraction for VALUATION_INPUTS manual evidence, for a
# genuinely tabular source (metric rows x reporting-period columns), reusing
# the SAME conservative table-row discipline SHAREHOLDING already established
# above (_match_label_prefix, _looks_like_pure_value_remainder) and the SAME
# never-invent-a-day period normalization rule documented on
# _shareholding_canonical_period_end (MONTH precision -> first-of-month, NEVER
# a month-end/quarter-end date manufactured here).
#
# Scope, deliberately narrow (see ALLOWED_METRICS_BY_EVIDENCE_TYPE in
# app.manual_evidence -- VALUATION_INPUTS permits only eps/pat/net_income/
# net_profit, never revenue): a recognized row is turned into a proposed
# FinancialFact-shaped fact ONLY when (a) its metric is PAT or EPS (Net
# Income/PAT/Net Profit are three aliases of the SAME canonical 'pat' metric
# -- never emitted as three separate facts for one row/value, which would
# fabricate duplicate observations) and (b) its column is a real calendar
# QUARTERLY or ANNUAL period. Total Revenue (a real figure in the source, but
# not a VALUATION_INPUTS-allowed metric) and any TTM column (a rolling
# trailing-twelve-month window, not a fixed calendar period this platform's
# FinancialFact periodType domain contract recognizes -- see
# app.manual_evidence._validate_financial_fact_rows's periodType in ("QUARTERLY",
# "ANNUAL") check) are both recognized but routed to `observations` only --
# never proposed as an acceptable valuation fact, never silently dropped.

@dataclass
class ValuationFactColumn:
    """One recognized header column in a VALUATION_INPUTS table."""
    label: str
    period_type: str  # "QUARTERLY" | "ANNUAL" | "TRAILING_TWELVE_MONTHS"
    period_end: "date | None"  # None only for TRAILING_TWELVE_MONTHS


@dataclass
class ValuationInputsFact:
    """A confidently recognized, IN-CONTRACT (metric, period) value --
    safe to propose as an acceptable VALUATION_INPUTS FinancialFact row.
    """
    metric: str
    value: Decimal
    unit: "str | None"
    period_end: date
    period_type: str
    column_label: str


@dataclass
class ValuationInputsObservation:
    """A recognized value that is NOT a VALUATION_INPUTS-allowed metric
    (e.g. Total Revenue) or not a calendar period this domain's periodType
    contract supports (TTM) -- real information from the source, routed to
    read-only reference display, never to an acceptable proposed fact and
    never silently dropped.
    """
    metric: str
    value: Decimal
    unit: "str | None"
    column_label: str
    period_type: str
    reason: str


@dataclass
class ValuationInputsInterpretation:
    facts: list[ValuationInputsFact] = field(default_factory=list)
    observations: list[ValuationInputsObservation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


_VALUATION_METRIC_ROW_LABELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Net Income / PAT / Net Profit are three printed names for the SAME
    # canonical metric -- all map to "pat" (a name already in
    # ALLOWED_METRICS_BY_EVIDENCE_TYPE[VALUATION_INPUTS]), never emitted as
    # separate facts for the same row/value.
    ("pat", ("net income", "pat", "net profit")),
    ("eps", ("earnings per share", "eps")),
    # Recognized, but deliberately NOT in VALUATION_INPUTS's allowed-metric
    # set -- routed to `observations`, never a proposed fact (see module
    # docstring above).
    ("revenue", ("total revenue", "revenue from operations", "revenue")),
)

_FY_RANGE = re.compile(r"\bFY\s*(\d{4})\s*[-\u2013\u2014/]\s*(\d{2,4})\b", re.I)
_VALUATION_CELL_VALUE = re.compile(r"(-?\d[\d,]*(?:\.\d+)?)([^\d]*)")


def _parse_fy_range_column(cell: str) -> "ValuationFactColumn | None":
    """Recognize an Indian-FY-convention annual header cell such as
    "FY 2025-26" or "FY 2025\u201326". The fiscal year is deemed to END in
    March of the SECOND year (standard, documented Indian FY convention --
    not a guess) -- normalized the SAME way MONTH precision is normalized
    everywhere else in this platform (first day of the stated month,
    see _shareholding_canonical_period_end's docstring): year=<second
    year>, month=3, day=1. Returns None when the cell is not an FY-range
    label at all -- never guessed from an ordinary year or date cell.
    """
    match = _FY_RANGE.search(cell)
    if not match:
        return None
    end_year_raw = match.group(2)
    end_year = int(end_year_raw) if len(end_year_raw) == 4 else 2000 + int(end_year_raw)
    return ValuationFactColumn(
        label=cell.strip(), period_type="ANNUAL", period_end=date(end_year, 3, 1),
    )


def _find_valuation_header_columns(line: str) -> list[ValuationFactColumn]:
    """Find every recognizable period-column token in a header LINE, in
    left-to-right order of appearance -- deliberately NOT a whitespace-run
    cell split (real OCR text extraction commonly collapses a table's
    original multi-space column gaps down to single spaces, so a header
    like "Q1 FY27 / Jun 2026   FY 2025-26   TTM" can arrive as one
    single-spaced line with no reliable column-boundary whitespace left
    at all; matching each recognizable token directly, wherever it
    falls, is robust to that). TTM and an FY-range are matched directly;
    everything else is delegated to the EXISTING
    app.reporting_period.find_period_tokens parser (never a duplicated
    month-name dictionary) and only a MONTH/DAY-precision result is kept
    -- a YEAR-only token is not a plausible quarterly/annual column
    here and is left unrecognized rather than guessed.
    """
    from app.reporting_period import find_period_tokens

    tokens: list[tuple[int, ValuationFactColumn]] = []
    for match in re.finditer(r"\bTTM\b", line, re.I):
        tokens.append((match.start(), ValuationFactColumn(
            label=match.group(0), period_type="TRAILING_TWELVE_MONTHS", period_end=None,
        )))
    for match in _FY_RANGE.finditer(line):
        end_year_raw = match.group(2)
        end_year = int(end_year_raw) if len(end_year_raw) == 4 else 2000 + int(end_year_raw)
        tokens.append((match.start(), ValuationFactColumn(
            label=match.group(0).strip(), period_type="ANNUAL", period_end=date(end_year, 3, 1),
        )))
    search_from = 0
    for period in find_period_tokens(line):
        if period.precision not in ("DAY", "MONTH"):
            continue
        idx = line.find(period.original_text, search_from)
        if idx == -1:
            idx = line.find(period.original_text)
        if idx == -1:
            continue
        search_from = idx + len(period.original_text)
        tokens.append((idx, ValuationFactColumn(
            label=period.original_text, period_type="QUARTERLY", period_end=period.normalized_date,
        )))
    tokens.sort(key=lambda item: item[0])
    return [column for _offset, column in tokens]


def _parse_valuation_row_values(remainder: str) -> list["tuple[Decimal, str | None]"]:
    """Scan a row's remainder (everything after its matched label) for
    an in-order sequence of (number, trailing-unit-text) chunks --
    "123 INR crore 369.4 INR crore 420 INR crore" -> [(123, "INR
    crore"), (369.4, "INR crore"), (420, "INR crore")] -- regardless of
    how much whitespace actually separates them (see
    _find_valuation_header_columns for why that cannot be relied on).
    Each chunk's unit text runs up to, but never including, the next
    number -- never guessed past that boundary.
    """
    chunks: list[tuple[Decimal, str | None]] = []
    for match in _VALUATION_CELL_VALUE.finditer(remainder):
        raw_value = match.group(1).replace(",", "")
        try:
            value = Decimal(raw_value)
        except InvalidOperation:
            continue
        unit = match.group(2).strip() or None
        chunks.append((value, unit))
    return chunks


def interpret_valuation_inputs(extracted: "ExtractedDocument") -> ValuationInputsInterpretation:
    """``ExtractedDocument`` -> candidate VALUATION_INPUTS values, from a
    genuinely tabular source (metric rows x reporting-period columns).

    Conservative by construction, same discipline as
    ``interpret_shareholding``: a header row needs >= 2 recognizable period
    columns to even attempt table parsing; a data row needs its column
    count to match the header exactly to be split into per-column values
    (never positionally guessed when counts disagree); an unparseable cell
    is skipped with a warning, never defaulted. Nothing here is ever
    accepted evidence -- the caller (app.manual_evidence) still owns
    Review/Accept.
    """
    text = extracted.text or ""
    result = ValuationInputsInterpretation()
    lines = [line for line in text.splitlines() if line.strip()]
    header_index = -1
    header_columns: list[ValuationFactColumn] = []
    for index, line in enumerate(lines):
        columns = _find_valuation_header_columns(line)
        if len(columns) >= 2:
            header_index = index
            header_columns = columns
            break
    if header_index == -1:
        return result

    for line in lines[header_index + 1:]:
        # Row labels in a VALUATION_INPUTS source commonly spell out
        # several aliases of the same metric on one line ("Net Income /
        # PAT / Net Profit", matching the literal confirmed runtime
        # table). _match_label_prefix only needs to confirm the line
        # STARTS WITH one recognized alias -- whatever text follows
        # (more aliases, then the values) is scanned directly by
        # _parse_valuation_row_values below, which looks for number
        # chunks wherever they occur and ignores any non-numeric alias
        # text in between, so it is never required to isolate "the"
        # label text first the way a single-word category match would.
        matched_metric = None
        remainder = None
        for metric, variants in _VALUATION_METRIC_ROW_LABELS:
            remainder = _match_label_prefix(line, variants)
            if remainder is not None:
                matched_metric = metric
                break
        if matched_metric is None:
            continue
        value_chunks = _parse_valuation_row_values(remainder)
        if len(value_chunks) != len(header_columns):
            result.warnings.append(
                f"ROW_COLUMN_COUNT_MISMATCH: '{matched_metric}' row has {len(value_chunks)} "
                f"value(s) but the header has {len(header_columns)} period column(s) -- "
                "left unset rather than guessed"
            )
            continue
        for column, (value, unit) in zip(header_columns, value_chunks):
            if column.period_type == "TRAILING_TWELVE_MONTHS":
                ttm_reason = (
                    "UNSUPPORTED_PERIOD_TYPE_TTM: trailing-twelve-months is not a "
                    "calendar QUARTERLY/ANNUAL period this platform's VALUATION_INPUTS "
                    "persistence contract recognizes"
                )
                result.observations.append(ValuationInputsObservation(
                    metric=matched_metric, value=value, unit=unit,
                    column_label=column.label, period_type=column.period_type,
                    reason=ttm_reason,
                ))
            elif matched_metric not in ("pat", "eps"):
                metric_reason = (
                    f"UNSUPPORTED_METRIC_FOR_VALUATION_INPUTS: '{matched_metric}' is not an "
                    "allowed VALUATION_INPUTS metric (only eps/pat are) -- shown for "
                    "reference, never an accepted valuation fact"
                )
                result.observations.append(ValuationInputsObservation(
                    metric=matched_metric, value=value, unit=unit,
                    column_label=column.label, period_type=column.period_type,
                    reason=metric_reason,
                ))
            else:
                result.facts.append(ValuationInputsFact(
                    metric=matched_metric, value=value, unit=unit,
                    period_end=column.period_end, period_type=column.period_type,
                    column_label=column.label,
                ))
    return result
