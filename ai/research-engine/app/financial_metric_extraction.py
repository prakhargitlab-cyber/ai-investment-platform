"""DI-20D: Generic semantic extraction layer for NSE financial statement metrics.

Transforms a resolved financial statement into validated semantic facts through:

    resolved statement  ->  frozen FinancialColumn[]
    ->  metric-row identification
    ->  row-owned FinancialCell[]
    ->  deterministic cell-column binding
    ->  semantic validation
    ->  validated extracted financial facts

This layer is generic across NSE financial-result document structures.
No company/ticker/document-id-specific production logic is present.

The historical accepted_facts list remains a shadow output. DI-20E retains
undeduplicated statement/row/cell evidence for financial_projection, the only
adapter allowed to turn this output into durable facts. The DI-20C structural
result is also retained for explicit reconciliation-completeness validation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal

from app.financial_structure import parse_financial_structure
from app.financial_structure_types import FinancialParseResult
from app.pdf_structure import PdfTextStructure, SourceRegion, preserve_pdf_structure
from app.structured_research import (
    _aligned_row_cells,
    _coalesce_split_financial_cells,
    _financial_number,
    _nse_cash_flow_candidates,
    _nse_statement_candidates,
    _reporting_basis,
    _ROW_REFERENCE_FORMULA_RE,
    _reported_unit,
    _structural_row_text,
    _classify_financial_row,
    FinancialStatement,
)

# ---------------------------------------------------------------------------
# Canonical metric ontology
# ---------------------------------------------------------------------------

#: Generic metric canonical names.  ``revenue_from_operations`` is distinct
#: from ``total_income`` -- they are never conflated.
METRIC_REVENUE_FROM_OPERATIONS = "revenue_from_operations"
METRIC_TOTAL_INCOME = "total_income"
METRIC_PROFIT_BEFORE_TAX = "profit_before_tax"
METRIC_PROFIT_AFTER_TAX = "profit_after_tax"
METRIC_EPS_BASIC = "eps_basic"
METRIC_EPS_DILUTED = "eps_diluted"
METRIC_FINANCE_COSTS = "finance_costs"
METRIC_TAX = "tax"
METRIC_TOTAL_EXPENSES = "total_expenses"
METRIC_EBITDA = "ebitda"

#: Cash-flow canonical names.
METRIC_CASH_FLOW_OPERATING = "cash_flow_from_operating_activities"
METRIC_CASH_FLOW_INVESTING = "cash_flow_from_investing_activities"
METRIC_CASH_FLOW_FINANCING = "cash_flow_from_financing_activities"

INCOME_STATEMENT_METRICS = frozenset({
    METRIC_REVENUE_FROM_OPERATIONS,
    METRIC_TOTAL_INCOME,
    METRIC_PROFIT_BEFORE_TAX,
    METRIC_PROFIT_AFTER_TAX,
    METRIC_EPS_BASIC,
    METRIC_EPS_DILUTED,
    METRIC_FINANCE_COSTS,
    METRIC_TAX,
    METRIC_TOTAL_EXPENSES,
    METRIC_EBITDA,
})

CASH_FLOW_METRICS = frozenset({
    METRIC_CASH_FLOW_OPERATING,
    METRIC_CASH_FLOW_INVESTING,
    METRIC_CASH_FLOW_FINANCING,
})

# ---------------------------------------------------------------------------
# Row-label patterns (generic, not company-specific)
# ---------------------------------------------------------------------------

#: Revenue from operations -- the ``(?<!total\s)`` lookbehind excludes
#: ``"total revenue from operations"`` so the two metrics are never conflated.
_REVENUE_FROM_OPERATIONS_PATTERN = r"(?<!total\s)\brevenue\s+from\s+operations\b"
_TOTAL_INCOME_PATTERN = r"\btotal\s+income\b"
_TAX_PATTERN = r"\btax\s+(?:expense|on\s+(?:profit|income|loss|chargeable))\b"
_FINANCE_COSTS_PATTERN = r"\bfinance\s+costs?\b"
_TOTAL_EXPENSES_PATTERN = r"\btotal\s+expenses?\b"
_EBITDA_PATTERN = r"\bebitda\b"

_EPS_BASIC_PATTERN = (
    r"\bbasic\s+(?:earnings?\s+per\s+share|eps)\b"
    r"|\bbasic\b(?:\s*\(\s*rs\.?\s*\))?"
)
_EPS_DILUTED_PATTERN = (
    r"\bdiluted\s+(?:earnings?\s+per\s+share|eps)\b"
    r"|\bdiluted\b(?:\s*\(\s*rs\.?\s*\))?"
)

_CASH_FLOW_OPERATING_PATTERN = re.compile(
    r"\bnet\s+cash\s+(?:flows?\s+)?"
    r"(?:\(\s*used\s+in\s*\)\s*/\s*generated\s+from"
    r"|generated\s+from\s*/\s*\(?used\)?\s*in"
    r"|flow\s*/?\s*\(?used\)?\s+in"
    r"|flow\s+from"
    r"|generated\s+from"
    r"|used\s+in"
    r"|from)\s+"
    r"(?:operating|operations)\s+activities\b",
    re.I,
)
_CASH_FLOW_INVESTING_PATTERN = _CASH_FLOW_OPERATING_PATTERN.pattern.replace(
    "operating|operations", "investing"
)
_CASH_FLOW_FINANCING_PATTERN = _CASH_FLOW_OPERATING_PATTERN.pattern.replace(
    "operating|operations", "financing"
)

#: Row-label substrings that cause hard exclusion of a metric row.
_EXCLUSION_KEYWORDS: dict[str, str] = {
    "segment": "SEGMENT_REVENUE",
    "non-controlling interest": "NON_CONTROLLING_INTEREST",
    "noncontrolling interest": "NON_CONTROLLING_INTEREST",
    "owners of": "OWNERS_OF_PARENT_ATTRIBUTION",
    "shareholders of the": "OWNERS_OF_PARENT_ATTRIBUTION",
}

#: Row-label substrings that add a qualifier (the row is still accepted).
_QUALIFIER_KEYWORDS: dict[str, str] = {
    "continuing operations": "CONTINUING_OPERATIONS",
    "continuing ops": "CONTINUING_OPERATIONS",
    "discontinued operations": "DISCONTINUED_OPERATIONS",
    "discontinued ops": "DISCONTINUED_OPERATIONS",
    "before exceptional": "BEFORE_EXCEPTIONAL_ITEMS",
    "after exceptional": "AFTER_EXCEPTIONAL_ITEMS",
    "exceptional items": "EXCEPTIONAL_ITEMS",
    "before exceptionals": "BEFORE_EXCEPTIONAL_ITEMS",
    "after exceptionals": "AFTER_EXCEPTIONAL_ITEMS",
}

#: Metrics that must be rejected when the label contains "total consolidated".
_TOTAL_CONSOLIDATED_EXCLUDE_METRICS = frozenset({
    METRIC_PROFIT_AFTER_TAX,
})


# ---------------------------------------------------------------------------
# Semantic model (DI-20D frozen dataclasses)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FinancialCell:
    """One cell owned by a metric row, bound to a frozen column ordinal."""

    column_ordinal: int
    original_token: str
    value: Decimal | None
    is_negative: bool
    is_missing: bool
    source_region: SourceRegion | None
    normalization: tuple[str, ...]


@dataclass(frozen=True)
class FinancialMetricRow:
    """A proven metric row with row-owned cells and resolution metadata."""

    canonical_metric: str
    original_label: str
    normalized_label: str
    qualifiers: frozenset[str]
    reporting_scope: str | None
    source_region: SourceRegion | None
    cells: tuple[FinancialCell, ...]
    resolution_state: str  # "ACCEPTED" | "REJECTED" | "AMBIGUOUS"
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class SemanticFact:
    """A validated semantic financial fact bound to a specific column."""

    metric: str
    period_end: str
    period_type: str
    reporting_basis: str | None
    column_ordinal: int
    value: Decimal
    unit: str | None


@dataclass(frozen=True)
class ExtractionResult:
    """Outcome of semantic extraction over a single document."""

    accepted_facts: tuple[SemanticFact, ...]
    rejected_rows: tuple[FinancialMetricRow, ...]
    diagnostics: tuple[str, ...]
    shadow_version: str
    state: str  # "SUCCESS" | "REJECTED" | "PARTIAL" | "NO_STATEMENT"
    structural_fingerprint: str = ""
    statements: tuple[SemanticStatementEvidence, ...] = ()
    structural_result: FinancialParseResult | None = None


@dataclass(frozen=True)
class SemanticStatementEvidence:
    """Undeduplicated evidence retained for the strict durable projection gate.

    The legacy shadow fact list loses qualifiers and candidate ownership; it
    must never be the input to authoritative persistence by itself.
    """
    statement_type: str
    statement: FinancialStatement
    rows: tuple[FinancialMetricRow, ...]
    source_region: SourceRegion | None
    unit: str | None


# ---------------------------------------------------------------------------
# Cell extraction with original-token capture
# ---------------------------------------------------------------------------


def _is_separator_token(token: str) -> bool:
    return token.strip().strip("()[]:;,.") in {"I", "l", "|"}


def _tail_info(statement: FinancialStatement, label_pattern: str) -> tuple[list, int | None] | None:
    """Replicate the tail-anchoring logic of ``_aligned_row_cells``.

    Returns ``(tokens, first_index)`` or ``None`` when the row cannot be
    structurally anchored.  Validation is performed separately by
    ``_aligned_row_cells``; this helper is only called *after* validation
    has passed.
    """
    row_region = _structural_row_text(statement.region)
    matches = re.finditer(label_pattern, row_region, re.I)
    match = next(
        (candidate for candidate in matches
         if not re.match(r"\s*\(?before\b", row_region[candidate.end():], re.I)),
        None,
    )
    if not match:
        return None
    tail = row_region[match.end():match.end() + 360]
    # Strip leading row annotations, retaining parenthesised numeric cells.
    tail = re.sub(
        r"^\s*(?:from\s+continuing\s+operations\s*)?(?:\((?![^)]*\d)[^)]+\)\s*)+",
        "",
        tail,
        flags=re.I,
    )
    tail = re.sub(r"^\s*(?:\([A-Za-z+]+\)\s*)+", "", tail)
    tail = _ROW_REFERENCE_FORMULA_RE.sub("", tail)
    tail = re.sub(r"^\s*eps\b", "", tail, flags=re.I)
    tokens = list(re.finditer(r"\S+", tail))
    first_index = next(
        (index for index, token in enumerate(tokens)
         if not _is_separator_token(token.group(0))
         and re.search(r"[0-9ZSOIl|\\]", token.group(0))),
        None,
    )
    if first_index is None:
        return None
    prefix = re.sub(r"\(\s*rs\.?\s*\)", "", tail[:tokens[first_index].start()], flags=re.I)
    prefix = re.sub(r"\b[I|l]\b", "", prefix)
    prefix = re.sub(
        r"\bfrom\s+continuing\s+operations\b\s*(?:\([^)]+\))?",
        "",
        prefix, flags=re.I,
    )
    if re.search(r"[A-Za-z]", prefix):
        return None
    return tokens, first_index


def _extract_cell_tokens(statement: FinancialStatement, label_pattern: str) -> list[str] | None:
    """Extract the raw cell token strings for a label-pattern row.

    Must be called only after ``_aligned_row_cells`` has confirmed the row
    passes all structural guards.
    """
    result = _tail_info(statement, label_pattern)
    if result is None:
        return None
    tokens, first_index = result
    raw_cells = _coalesce_split_financial_cells(
        [token.group(0) for token in tokens[first_index:]
         if not _is_separator_token(token.group(0))]
    )[:len(statement.columns)]
    return raw_cells if len(raw_cells) == len(statement.columns) else None


def _build_cells(
    statement: FinancialStatement,
    label_pattern: str,
    *,
    structure: PdfTextStructure | None = None,
) -> tuple[FinancialCell, ...] | None:
    """Build row-owned ``FinancialCell`` objects for a label-pattern row.

    Returns ``None`` when ``_aligned_row_cells`` rejects the row structurally.
    """
    cell_values = _aligned_row_cells(statement, label_pattern)
    if cell_values is None:
        return None
    raw_tokens = _extract_cell_tokens(statement, label_pattern)
    synthesized = raw_tokens is None or len(raw_tokens) != len(cell_values)
    if raw_tokens is None or len(raw_tokens) != len(cell_values):
        # Fallback: synthesise tokens from parsed values so diagnostics
        # remain useful even if token reconstruction diverges.
        raw_tokens = [str(v) if v is not None else "-" for v in cell_values]
    cells: list[FinancialCell] = []
    for ordinal, (token, value) in enumerate(zip(raw_tokens, cell_values)):
        cells.append(FinancialCell(
            column_ordinal=ordinal,
            original_token=token,
            value=value,
            is_negative=(value is not None and value < 0),
            is_missing=(value is None),
            source_region=_cell_source_region(token, statement, ordinal, structure),
            normalization=(*_normalization_ops(token, value), *(("SYNTHESIZED_TOKEN",) if synthesized else ())),
        ))
    return tuple(cells)


def _cell_source_region(token: str, statement: FinancialStatement, ordinal: int,
                        structure: PdfTextStructure | None) -> SourceRegion | None:
    """Best-effort positional provenance for a cell token."""
    if structure is None:
        return None
    search = statement.region
    pos = search.find(token)
    if pos < 0:
        return None
    return structure.locate(pos, pos + len(token))


def _normalization_ops(token: str, value: Decimal | None) -> tuple[str, ...]:
    """Describe the OCR/scanning repairs applied to produce *value* from *token*."""
    ops: list[str] = []
    stripped = token.strip()
    if (
        value is not None
        and value < 0
        and stripped.startswith("(")
        and stripped.endswith(")")
    ):
        ops.append("ACCOUNTING_PARENTHESES_NEGATED")
    inner = token.strip("()[]:;,. ")
    ocr_chars = set("ZSOIl|")
    if any(ch in inner for ch in ocr_chars):
        ops.append("OCR_CHAR_REPAIR")
    return tuple(ops)


# ---------------------------------------------------------------------------
# Label extraction
# ---------------------------------------------------------------------------


def _extract_row_label(statement: FinancialStatement, label_pattern: str) -> str | None:
    """Extract the original label text for a row."""
    row_region = _structural_row_text(statement.region)
    matches = re.finditer(label_pattern, row_region, re.I)
    match = next(
        (candidate for candidate in matches
         if not re.match(r"\s*\(?before\b", row_region[candidate.end():], re.I)),
        None,
    )
    if not match:
        return None
    tail = row_region[match.end():match.end() + 120]
    numeric_pos = re.search(r"\b[-+]?\d", tail)
    if numeric_pos:
        label = row_region[match.start():match.end() + numeric_pos.start()]
    else:
        label = match.group()
    return label.strip()


def _extract_classified_label(
    statement: FinancialStatement, classification: str,
) -> tuple[str, tuple[Decimal | None, ...], list[str]] | None:
    """Extract label, cells, and raw tokens for a PAT/PBT classified row."""
    text = _structural_row_text(statement.region)
    for match in re.finditer(r"\b(?:profit|proflt|ptoflt)\w*", text, re.I):
        tail = text[match.end():match.end() + 260]
        tail = _ROW_REFERENCE_FORMULA_RE.sub("", tail)
        tail = re.sub(r"\(\s*\d+(?:\s*[+\-*/\u2022]\s*\d+)+\s*\)", "", tail)
        tail = re.sub(r"^\s*\(\s*\d{1,2}\s*\)\s*", "", tail)
        tokens = list(re.finditer(r"\S+", tail))

        def _is_row_reference(index: int, token) -> bool:
            if not re.fullmatch(r"\(\s*\d{1,2}\s*\)", token.group()):
                return False
            following = tokens[index + 1:index + 1 + len(statement.columns)]
            return (
                len(following) == len(statement.columns)
                and all(_financial_number(v.group()) is not None for v in following)
            )

        first = next(
            (index for index, token in enumerate(tokens)
             if re.search(r"\d", token.group())
             and not _ROW_REFERENCE_FORMULA_RE.fullmatch(token.group())
             and not _is_row_reference(index, token)),
            None,
        )
        if first is None:
            continue
        label = text[match.start():match.end() + tokens[first].start()]
        if _classify_financial_row(label) != classification:
            continue
        # Reject PAT candidates whose label includes "and other" (e.g.
        # "Profit for the period after tax and Other") -- this is a
        # comprehensive-income subtotal, NOT a pure PAT row.
        if classification == "PAT" and re.search(r"and\s+other\b", label, re.I):
            continue
        raw_cells = _coalesce_split_financial_cells(
            [token.group() for token in tokens[first:first + len(statement.columns) + 2]]
        )[:len(statement.columns)]
        if len(raw_cells) != len(statement.columns):
            continue
        cells = tuple(_financial_number(cell) for cell in raw_cells)
        if any(value is not None for value in cells):
            return label, cells, raw_cells
    return None


def _build_classified_cells(
    statement: FinancialStatement,
    classification: str,
    *,
    structure: PdfTextStructure | None = None,
) -> tuple[str, tuple[FinancialCell, ...]] | None:
    """Build cells for a PAT/PBT classified row, returning (label, cells)."""
    result = _extract_classified_label(statement, classification)
    if result is None:
        return None
    label, cell_values, raw_tokens = result
    cells: list[FinancialCell] = []
    for ordinal, (token, value) in enumerate(zip(raw_tokens, cell_values)):
        cells.append(FinancialCell(
            column_ordinal=ordinal,
            original_token=token,
            value=value,
            is_negative=(value is not None and value < 0),
            is_missing=(value is None),
            source_region=None,
            normalization=_normalization_ops(token, value),
        ))
    return label, tuple(cells)


# ---------------------------------------------------------------------------
# Label scanning / validation
# ---------------------------------------------------------------------------


def _scan_label(label: str) -> tuple[set[str], set[str]]:
    """Return ``(exclusion_reasons, qualifier_names)`` for a row label."""
    lowered = label.lower()
    exclusions: set[str] = set()
    qualifiers: set[str] = set()
    for keyword, reason in _EXCLUSION_KEYWORDS.items():
        if keyword in lowered:
            exclusions.add(reason)
    for keyword, qualifier in _QUALIFIER_KEYWORDS.items():
        if keyword in lowered:
            qualifiers.add(qualifier)
    return exclusions, qualifiers


def _validate_metric_row(row: FinancialMetricRow) -> tuple[str, tuple[str, ...], frozenset[str]]:
    """Return ``(resolution_state, diagnostics, qualifiers)`` after validation."""
    diagnostics: list[str] = []
    label = row.original_label.lower() if row.original_label else ""

    exclusions, qualifiers = _scan_label(row.original_label)
    if exclusions:
        diagnostics.append(f"EXCLUDED: {', '.join(sorted(exclusions))}")

    # "Total consolidated PAT" is a cross-basis aggregation, never a single
    # period's profit-after-tax.
    if row.canonical_metric in _TOTAL_CONSOLIDATED_EXCLUDE_METRICS and "total consolidated" in label:
        exclusions.add("TOTAL_CONSOLIDATED_PAT")
        diagnostics.append("EXCLUDED: TOTAL_CONSOLIDATED_PAT")

    state = "REJECTED" if exclusions else "ACCEPTED"
    return state, tuple(diagnostics), frozenset(qualifiers)


def _normalize_label(label: str) -> str:
    """Lowercase and collapse whitespace; retain OCR artefact letters."""
    return re.sub(r"\s+", " ", label.strip()).lower()


# ---------------------------------------------------------------------------
# Metric-row extraction from income-statement candidates
# ---------------------------------------------------------------------------

#: ``(canonical_metric, label_pattern)`` specs for non-classified rows.
_METRIC_SPECS: list[tuple[str, str]] = [
    (METRIC_REVENUE_FROM_OPERATIONS, _REVENUE_FROM_OPERATIONS_PATTERN),
    (METRIC_TOTAL_INCOME, _TOTAL_INCOME_PATTERN),
    (METRIC_EPS_BASIC, _EPS_BASIC_PATTERN),
    (METRIC_EPS_DILUTED, _EPS_DILUTED_PATTERN),
    (METRIC_FINANCE_COSTS, _FINANCE_COSTS_PATTERN),
    (METRIC_TAX, _TAX_PATTERN),
    (METRIC_TOTAL_EXPENSES, _TOTAL_EXPENSES_PATTERN),
    (METRIC_EBITDA, _EBITDA_PATTERN),
]

#: ``(classification, canonical_metric)`` specs for PAT / PBT rows.
_CLASSIFIED_METRIC_SPECS: list[tuple[str, str]] = [
    ("PBT", METRIC_PROFIT_BEFORE_TAX),
    ("PAT", METRIC_PROFIT_AFTER_TAX),
]

#: Metrics whose presence is required for a candidate to be considered viable.
#: If a candidate yields no ACCEPTED core metric, all of its rows are rejected
#: (prevents fabrication of PBT / finance-cost facts from misaligned columns).
_CORE_METRICS = frozenset({
    METRIC_REVENUE_FROM_OPERATIONS,
    METRIC_TOTAL_INCOME,
    METRIC_PROFIT_AFTER_TAX,
    METRIC_EPS_BASIC,
    METRIC_EPS_DILUTED,
})


def _extract_income_metric_rows(
    statement: FinancialStatement,
    structure: PdfTextStructure | None,
) -> list[FinancialMetricRow]:
    """Extract all income-statement metric rows from a candidate."""
    basis = _reporting_basis(statement.region)
    rows: list[FinancialMetricRow] = []

    # PAT / PBT via classified row extraction
    for classification, canonical in _CLASSIFIED_METRIC_SPECS:
        result = _build_classified_cells(statement, classification, structure=structure)
        if result is not None:
            label, cells = result
            row = FinancialMetricRow(
                canonical_metric=canonical,
                original_label=label,
                normalized_label=_normalize_label(label),
                qualifiers=frozenset(),
                reporting_scope=basis,
                source_region=None,
                cells=cells,
                resolution_state="ACCEPTED",
                diagnostics=(),
            )
            state, diags, quals = _validate_metric_row(row)
            rows.append(replace(row, resolution_state=state, diagnostics=diags, qualifiers=quals))

    # Other income-statement metrics via aligned row extraction
    for canonical, pattern in _METRIC_SPECS:
        cells = _build_cells(statement, pattern, structure=structure)
        if cells is None:
            continue
        label = _extract_row_label(statement, pattern) or canonical
        row = FinancialMetricRow(
            canonical_metric=canonical,
            original_label=label,
            normalized_label=_normalize_label(label),
            qualifiers=frozenset(),
            reporting_scope=basis,
            source_region=None,
            cells=cells,
            resolution_state="ACCEPTED",
            diagnostics=(),
        )
        state, diags, quals = _validate_metric_row(row)
        rows.append(replace(row, resolution_state=state, diagnostics=diags, qualifiers=quals))

    return rows


# ---------------------------------------------------------------------------
# Cash-flow metric-row extraction
# ---------------------------------------------------------------------------

_CASH_FLOW_SPECS: list[tuple[str, str]] = [
    (METRIC_CASH_FLOW_OPERATING, _CASH_FLOW_OPERATING_PATTERN.pattern),
    (METRIC_CASH_FLOW_INVESTING, _CASH_FLOW_INVESTING_PATTERN),
    (METRIC_CASH_FLOW_FINANCING, _CASH_FLOW_FINANCING_PATTERN),
]


def _extract_cash_flow_metric_rows(
    statement: FinancialStatement,
    structure: PdfTextStructure | None,
) -> list[FinancialMetricRow]:
    """Extract all cash-flow metric rows from a candidate."""
    basis = _reporting_basis(statement.region)
    rows: list[FinancialMetricRow] = []

    for canonical, pattern in _CASH_FLOW_SPECS:
        cells = _build_cells(statement, pattern, structure=structure)
        if cells is None:
            continue
        label = _extract_row_label(statement, pattern) or canonical
        row = FinancialMetricRow(
            canonical_metric=canonical,
            original_label=label,
            normalized_label=_normalize_label(label),
            qualifiers=frozenset(),
            reporting_scope=basis,
            source_region=None,
            cells=cells,
            resolution_state="ACCEPTED",
            diagnostics=(),
        )
        state, diags, quals = _validate_metric_row(row)
        rows.append(replace(row, resolution_state=state, diagnostics=diags, qualifiers=quals))

    return rows


# ---------------------------------------------------------------------------
# Semantic fact production (deterministic cell -> column binding)
# ---------------------------------------------------------------------------


def _build_semantic_facts(
    rows: list[FinancialMetricRow],
    statement: FinancialStatement,
    unit: str | None,
    *,
    seen: set[tuple[str, str, str, str | None, int]] | None = None,
) -> list[SemanticFact]:
    """Bind row-owned cells to frozen column ordinals, producing SemanticFacts.

    Blank / missing cells are preserved as gaps (no fact emitted).  Cell order
    never shifts left when a middle value is blank.

    The *seen* set is shared across candidates so that when multiple
    candidates produce the same (metric, period_end, period_type, basis,
    ordinal) key, only the first-produced fact is retained.
    """
    if seen is None:
        seen = set()
    facts: list[SemanticFact] = []
    for row in rows:
        if row.resolution_state != "ACCEPTED":
            continue
        for cell in row.cells:
            if cell.is_missing:
                continue
            column = statement.columns[cell.column_ordinal]
            key = (
                row.canonical_metric,
                column.period_end,
                column.period_type,
                row.reporting_scope,
                cell.column_ordinal,
            )
            if key in seen:
                continue
            seen.add(key)
            cell_unit = (
                "INR per share"
                if row.canonical_metric in (METRIC_EPS_BASIC, METRIC_EPS_DILUTED)
                else unit
            )
            facts.append(SemanticFact(
                metric=row.canonical_metric,
                period_end=column.period_end,
                period_type=column.period_type,
                reporting_basis=row.reporting_scope,
                column_ordinal=cell.column_ordinal,
                value=cell.value,
                unit=cell_unit,
            ))
    return facts


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def extract_semantic_facts(
    text: str,
    *,
    evidence_at: datetime | None = None,
    structure: PdfTextStructure | None = None,
) -> ExtractionResult:
    """Extract semantic facts and undeduplicated evidence from NSE text.

    The DI-20C ``FinancialParseResult`` is retained for structural diagnostics
    and the durable projection gate. The actual metric rows are recovered from legacy
    ``FinancialStatement`` candidates that carry the frozen DI-20C
    ``FinancialColumn[]`` column model.
    """
    evidence_at = evidence_at or datetime.now(timezone.utc)
    structure = structure or preserve_pdf_structure(text, legacy=True)

    # --- Shadow parse (DI-20C structural layer, diagnostics only) ---------
    parse_result = parse_financial_structure(text, structure=structure)

    # --- Legacy candidates carrying DI-20C FinancialColumn[] -------------
    income_candidates = _nse_statement_candidates(text, evidence_at, require_quarterly=False)
    cash_flow_statements = _nse_cash_flow_candidates(text, evidence_at)

    # Shared dedup set across ALL candidates (standalone + consolidated)
    # so that when two candidates share the same column model and basis,
    # only the first-produced fact per (metric, period_end, period_type,
    # basis, ordinal) is retained.
    seen: set[tuple[str, str, str, str | None, int]] = set()

    all_rows: list[FinancialMetricRow] = []
    all_facts: list[SemanticFact] = []
    evidence: list[SemanticStatementEvidence] = []

    def retain(kind, statement, rows, unit):
        rows = list(rows)
        # Explicit total operating revenue is the same operations concept,
        # not total income. Keep it as evidence without changing shadow counts.
        extra = {"revenue_from_operations": r"\btotal\s+revenue\s+from\s+operations\b"} if kind == "INCOME_STATEMENT" else (
            {"net_change_in_cash": r"\bnet\s+(?:increase|decrease|change)\s+in\s+cash(?:\s+and\s+cash\s+equivalents?)?\b"}
            if kind == "CASH_FLOW" else {})
        for metric, pattern in extra.items():
            cells = _build_cells(statement, pattern, structure=structure)
            if cells is not None:
                label = _extract_row_label(statement, pattern) or metric
                rows.append(FinancialMetricRow(metric, label, _normalize_label(label), frozenset(),
                            _reporting_basis(statement.region), None, cells, "ACCEPTED", ()))
        offset = text.find(statement.region)
        region = SourceRegion(offset, offset + len(statement.region), coordinate_space="NORMALIZED_TEXT") if offset >= 0 else None
        evidence.append(SemanticStatementEvidence(kind, statement, tuple(rows), region, unit))

    for statement in income_candidates:
        unit = _reported_unit(statement.unit_context[:400], statement.unit_context[:800])
        rows = _extract_income_metric_rows(statement, structure)

        # Candidate viability: if no ACCEPTED row produces a core metric,
        # the candidate is structurally unsound (misaligned columns, OCR
        # noise in narrative, etc.).  Reject every row so no fabricated
        # facts leak through.
        has_core = any(
            row.canonical_metric in _CORE_METRICS
            and row.resolution_state == "ACCEPTED"
            for row in rows
        )
        if rows and not has_core:
            rows = [
                replace(
                    row,
                    resolution_state="REJECTED",
                    diagnostics=(
                        *row.diagnostics,
                        "CANDIDATE_NOT_VIABLE: no core metric row accepted",
                    ),
                )
                for row in rows
            ]

        all_rows.extend(rows)
        retain("INCOME_STATEMENT", statement, rows, unit)
        all_facts.extend(_build_semantic_facts(rows, statement, unit, seen=seen))

    for statement in cash_flow_statements:
        rows = _extract_cash_flow_metric_rows(statement, structure)

        # Cash-flow candidates also require viability: at least one
        # accepted cash-flow metric row.
        has_core_cf = any(
            row.canonical_metric in CASH_FLOW_METRICS
            and row.resolution_state == "ACCEPTED"
            for row in rows
        )
        if rows and not has_core_cf:
            rows = [
                replace(
                    row,
                    resolution_state="REJECTED",
                    diagnostics=(
                        *row.diagnostics,
                        "CANDIDATE_NOT_VIABLE: no core cash-flow metric row accepted",
                    ),
                )
                for row in rows
            ]

        all_rows.extend(rows)
        retain("CASH_FLOW", statement, rows, _reported_unit(statement.unit_context[:400], statement.unit_context[:800]))
        all_facts.extend(_build_semantic_facts(rows, statement, None, seen=seen))

    # Preserve the existing explicitly labelled balance-sheet contract through
    # the same row/cell evidence gate. These are not added to the DI-20D shadow
    # fact list, whose historical meaning remains unchanged.
    from app.structured_research import _nse_balance_sheet_candidates, _CASH_FLOW_HEADING
    balance_patterns = {
        "total_assets": r"\btotal\s+assets\b",
        "total_equity": r"\btotal\s+equity\b",
        "total_liabilities": r"\btotal\s+liabilities\b(?!\s+and\s+equity)",
        "current_assets": r"\btotal\s+current\s+assets\b",
        "current_liabilities": r"\btotal\s+current\s+liabilities\b",
        "cash_and_cash_equivalents": r"\bcash\s+and\s+cash\s+equivalents?\b",
        "total_debt": r"\b(?:total\s+debt|total\s+borrowings?|outstanding\s+debt)\b",
    }
    for statement in _nse_balance_sheet_candidates(text, evidence_at):
        boundary = _CASH_FLOW_HEADING.search(statement.region)
        if boundary:
            statement = replace(statement, region=statement.region[:boundary.start()])
        rows = []
        for metric, pattern in balance_patterns.items():
            cells = _build_cells(statement, pattern, structure=structure)
            if cells is not None:
                label = _extract_row_label(statement, pattern) or metric
                rows.append(FinancialMetricRow(metric, label, _normalize_label(label), frozenset(),
                            _reporting_basis(statement.region), None, cells, "ACCEPTED", ()))
        retain("BALANCE_SHEET", statement, rows, _reported_unit(statement.unit_context[:400], statement.unit_context[:800]))

    # --- Assemble diagnostics --------------------------------------------
    diagnostics: list[str] = []
    diagnostics.append(f"shadow_parser_version={parse_result.parser_version}")
    diagnostics.append(f"shadow_state={parse_result.state}")
    diagnostics.append(
        f"shadow_accepted={len(parse_result.accepted_statements)} "
        f"shadow_rejected={len(parse_result.rejected_statements)} "
        f"legacy_income_candidates={len(income_candidates)} "
        f"legacy_cash_flow_candidates={len(cash_flow_statements)}"
    )
    for diag in parse_result.diagnostics:
        diagnostics.append(f"shadow:{diag}")

    rejected_rows = [row for row in all_rows if row.resolution_state != "ACCEPTED"]

    # --- Determine overall state -----------------------------------------
    if not all_facts:
        if not income_candidates and not cash_flow_statements:
            state = "REJECTED"
        elif parse_result.state in ("REJECTED", "NO_STATEMENT"):
            state = "REJECTED"
        else:
            state = "NO_STATEMENT"
    else:
        state = "SUCCESS"

    return ExtractionResult(
        accepted_facts=tuple(all_facts),
        rejected_rows=tuple(rejected_rows),
        diagnostics=tuple(diagnostics),
        shadow_version=parse_result.parser_version,
        state=state,
        structural_fingerprint=parse_result.structural_fingerprint,
        statements=tuple(evidence),
        structural_result=parse_result,
    )
