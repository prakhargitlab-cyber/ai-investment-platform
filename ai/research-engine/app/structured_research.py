from __future__ import annotations

import logging
import re
import time
from bisect import bisect_left
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from dataclasses import dataclass, field
from urllib.parse import urlparse

from app.models import (
    PortfolioResearchCompany,
    FinancialStatementPeriod,
    FinancialResultPeriod,
    ProvenancedValue,
    QuarterlyResult,
    ResearchDocument,
    ResearchEvent,
    ResearchEventType,
    ShareholdingChange,
    ShareholdingSnapshot,
    ShareholdingSnapshotValue,
    SourceClassification,
    SourceDiversity,
    SourceMode,
    ValuationAssessment,
    ValuationBenchmark,
    ValuationStateEvidence,
)
from app.fact_precedence import FinancialFact, FactSourceTier, SUPPORTED_FINANCIAL_SOURCE_TIERS, fact_source_authority


logger = logging.getLogger(__name__)


PERIOD_RE = re.compile(r"\b(Q[1-4]\s*(?:FY)?\s*\d{2,4}|(?:quarter|three months) ended\s+(?:\d{1,2}\s+)?[A-Za-z]+(?:\s+\d{1,2})?,?\s+\d{4})\b", re.I)
NUMBER = r"([-+]?\d[\d,]*(?:\.\d+)?)"
_NSE_QUARTER_HEADING = re.compile(r"\b(?:quarter|quaiter|three\s+months)(?:\s+and\s+year)?\s+ended\b", re.I)
_NSE_DATE = re.compile(r"\b(\d{1,2})(?:[lI]?st|nd|rd|th|tli)?\s*[\"'.,-]*\s*([A-Za-z\s]{3,12}?)\s+([2Z][0-9Z]{3})\b", re.I)
_NSE_MONTH_DAY_DATE = re.compile(r"\b([A-Za-z]{3,12})\s+(\d{1,2})(?:[lI]?st|nd|rd|th|tli)?\s*,?\s+([2Z][0-9Z]{3})\b", re.I)
_NSE_NUMERIC_DATE = re.compile(r"\b(\d{1,2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*([2Z][0-9Z]{3})\b")
_NSE_DAY_MONTH = re.compile(r"\b(\d{1,2})(?:[lI]?st|nd|rd|th|tli)?\s*[\"'.,-]*\s*([A-Za-z\s]{3,12}?)(?=\s+\d|\s+Z?\d{3}|\s+\d{1,2}(?:[lI]?st|nd|rd|th|tli)?|\s*$)", re.I)
_MONTHS = {name: index for index, name in enumerate(("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"), 1)}
_STATEMENT_HEADING = re.compile(r"\b(?:extract\s+of\s+)?(?:stateme[no]t\s+of\s+)?(?:(?:(?:unaud(?:\.?ited|[il]ted)|umaudtoed|aud[il]ted|standalone|consolidated)\s+){0,2})(?:f[il]nanc[il]al|fl\s+nclal)\s+results\b|\bquarterly\s+financial\s+results\b", re.I)
_STATEMENT_END = re.compile(r"\bstatement\s+of\s+(?:assets|financial\s+position|cash\s+flows?)\b|\bcash\s+flow\s+statement\b", re.I)
_GROUP_RE = re.compile(r"\b(quarter|nine\s+months?|half\s+year|year)\s+ended\b", re.I)
_PARTICULARS_RE = re.compile(r"\b(?:particulars|partlculars|parriculars|p8mculars)\b", re.I)
# Some PDF text extractors place the column groups immediately before the
# serial-number and ``Particulars`` cells, while leaving the date cells after
# them.  Match only the contiguous table-header suffix; this cannot pull an
# earlier narrative reference to a reporting period into the candidate.
_PRE_PARTICULARS_GROUPS_RE = re.compile(
    r"(?:(?:\b(?:quarter|nine\s+months?|half\s+year|year)\s+ended\b)\s*)+(?:(?:s[il]|no\.?)\s*)?$",
    re.I,
)
_ROW_REFERENCE_FORMULA_RE = re.compile(
    r"^\s*(?:(?:\(\s*\d+\s*\)(?:\s*[+\-*/•]\s*\(\s*\d+\s*\))+)|(?:\(\s*\d+(?:\s*[+\-*/•]\s*\d+)+\s*\)))\s*"
)
_BALANCE_SHEET_HEADING = re.compile(r"\b(?:balance\s+sheet|statement\s+of\s+(?:financial\s+position|assets\s+and\s+liabilities))\b", re.I)
_CASH_FLOW_HEADING = re.compile(r"\b(?:cash\s+fl\s*ows?\s+statement|statement\s+(?:of|for\s+the)\s+cash\s+fl\s*ows?)\b", re.I)


@dataclass(frozen=True)
class FinancialColumn:
    index: int
    period_end: str
    period_type: str
    header_group: str


@dataclass(frozen=True)
class FinancialStatement:
    region: str
    columns: tuple[FinancialColumn, ...]
    current_column: FinancialColumn
    unit_context: str


@dataclass(frozen=True)
class FinancialRow:
    normalized_label: str
    numeric_cells: tuple[Decimal, ...]


@dataclass(frozen=True)
class ParsedIncomeStatementPeriod:
    """One explicitly reported NSE income-statement column.

    This deliberately stays smaller than ``QuarterlyResult``: it is the
    normalized persistence read model for the G1 income-statement metrics.
    """
    period_end: str
    period_type: str
    reporting_basis: str | None
    metrics: tuple[tuple[str, ProvenancedValue], ...]


@dataclass(frozen=True)
class ParsedBalanceSheetPeriod:
    period_end: str
    period_type: str
    reporting_basis: str | None
    metrics: tuple[tuple[str, ProvenancedValue], ...]


@dataclass(frozen=True)
class ParsedCashFlowPeriod:
    period_end: str
    period_type: str
    reporting_basis: str | None
    metrics: tuple[tuple[str, ProvenancedValue], ...]


@dataclass(frozen=True)
class HeaderToken:
    kind: str
    value: str
    position: int


def enrich_company_research(
    company: PortfolioResearchCompany,
    documents: list[ResearchDocument],
    events: list[ResearchEvent],
    ownership_threshold: Decimal,
    financial_facts: list[FinancialFact] | None = None,
) -> PortfolioResearchCompany:
    company.source_diversity = source_diversity(documents)
    company.financial_result_history = financial_result_history_from_facts(financial_facts or [])
    company.balance_sheet_history = financial_statement_history_from_facts(
        financial_facts or [], period_type={"AS_AT", "QUARTERLY", "ANNUAL"}, metrics={
            "total_assets", "total_liabilities", "total_equity", "equity",
            "cash_and_cash_equivalents", "cash_and_equivalents", "total_debt",
            "debt_or_borrowings", "current_assets", "current_liabilities",
        },
    )
    company.cash_flow_history = financial_statement_history_from_facts(
        financial_facts or [], period_type={"QUARTERLY", "ANNUAL"}, metrics={
            "operating_cash_flow", "investing_cash_flow", "financing_cash_flow",
            "cash_flow_from_operating_activities", "cash_flow_from_investing_activities",
            "cash_flow_from_financing_activities",
        },
    )
    company.latest_quarterly_result = latest_quarterly_result_from_facts(financial_facts or [])
    if company.latest_quarterly_result is None:
        company.latest_quarterly_result = latest_quarterly_result(documents)
    if company.latest_quarterly_result and financial_facts:
        _apply_normalized_official_facts(company.latest_quarterly_result, financial_facts)
    company.quarterly_result_status = "EXTRACTED" if company.latest_quarterly_result else (
        "PDF_SCANNED_OCR_REQUIRED" if any(_is_failed_pdf_reference(document) for document in documents)
        or "PDF_SCANNED_OCR_REQUIRED" in str(company.safe_error_code or "")
        or "PDF_SCANNED_OCR_REQUIRED" in str(company.safe_error_message or "") else "NOT_AVAILABLE"
    )
    # Persisted NSE/XBRL snapshots retain the actual distinct reporting
    # periods.  Prefer them over the legacy document-text parser, whose two
    # period matches can originate from the same filing.
    company.shareholding_changes = (
        shareholding_changes_from_snapshots(company.shareholding_snapshots)
        or shareholding_changes(documents)
    )
    company.ownership_increases = sorted({
        change.category
        for change in company.shareholding_changes
        if change.category in {"PROMOTER", "FII_FPI", "DII"}
        and change.change_percentage_points >= ownership_threshold
    })
    company.valuation = valuation_assessment(documents)
    company.current_quarter_catalysts = current_quarter_catalysts(events)
    return company


def latest_quarterly_result_from_facts(facts: list[FinancialFact]) -> QuarterlyResult | None:
    """Build the newest usable quarterly result from persisted facts.

    The financial period is selected before field completeness.  A result may
    therefore be partial, but all populated fields come from one reporting
    basis: an explicit basis never silently absorbs an ``UNKNOWN``-basis fact.
    """
    history = financial_result_history_from_facts(facts, period_type="QUARTERLY")
    if not history:
        return None
    result = history[0]
    return QuarterlyResult(
        period=result.period, reporting_basis=result.reporting_basis,
        result_date=result.published_at or result.retrieved_at,
        revenue=result.revenue, pat=result.pat, eps=result.eps,
        source_name=result.source_name, source_url=result.source_url, source_type=result.source_type,
        published_at=result.published_at, retrieved_at=result.retrieved_at, confidence=result.confidence,
    )


def financial_result_history_from_facts(
    facts: list[FinancialFact], *, period_type: str | None = None,
) -> list[FinancialResultPeriod]:
    """Return up to four newest explicit periods per selected compatible basis.

    The newest period chooses the same authoritative basis as the legacy latest
    selector; subsequent rows are limited to that basis, so no series can be
    manufactured by composing consolidated, standalone, or unknown facts.
    """
    allowed_types = {period_type} if period_type else {"QUARTERLY", "ANNUAL"}
    grouped: dict[str, dict[tuple[str, str | None], dict[str, FinancialFact]]] = {}
    for fact in facts:
        if (
            fact.source_mode != SourceMode.REAL
            or fact.source_tier not in SUPPORTED_FINANCIAL_SOURCE_TIERS
            or fact.key.period_type not in allowed_types
            or not fact.key.period_end
            or fact.key.metric not in {"revenue", "operating_income", "ebit", "ebitda", "pat", "eps"}
        ):
            continue
        values = grouped.setdefault(fact.key.period_type, {}).setdefault((fact.key.period_end, fact.key.reporting_basis), {})
        existing = values.get(fact.key.metric)
        if existing is None or fact_source_authority(fact.source_tier) > fact_source_authority(existing.source_tier):
            values[fact.key.metric] = fact
    def basis_rank(item: tuple[str | None, dict[str, FinancialFact]]) -> tuple[int, int, int]:
        reporting_basis, values = item
        # Source authority remains meaningful when the same latest period has
        # multiple independently reported bases.  Explicit source bases then
        # win over UNKNOWN; CONSOLIDATED retains the existing deterministic
        # preference when authority is otherwise equal.
        return (
                max(fact_source_authority(fact.source_tier) for fact in values.values()),
            int(reporting_basis not in {None, "", "UNKNOWN"}),
            int(reporting_basis == "CONSOLIDATED"),
        )

    result: list[FinancialResultPeriod] = []
    for kind, groups in grouped.items():
        newest = max(period for period, _basis in groups)
        basis, _ = max(((basis, values) for (period, basis), values in groups.items() if period == newest), key=basis_rank)
        selected = 0
        # ``reporting_basis`` is intentionally optional.  Sort the explicit
        # reporting period first and use a stable text projection only as a
        # tie-breaker; do not normalize the stored basis identity.
        for (period, candidate_basis), values in sorted(
            groups.items(), key=lambda item: (item[0][0], item[0][1] or ""), reverse=True
        ):
            if candidate_basis != basis:
                continue
            if selected == 4:
                break
            primary = max(values.values(), key=lambda fact: fact_source_authority(fact.source_tier))
            result.append(FinancialResultPeriod(
                period=period, period_type=kind, reporting_basis=basis,
                revenue=values.get("revenue").value if values.get("revenue") else None,
                operating_income=values.get("operating_income").value if values.get("operating_income") else None,
                ebit=values.get("ebit").value if values.get("ebit") else None,
                ebitda=values.get("ebitda").value if values.get("ebitda") else None,
                pat=values.get("pat").value if values.get("pat") else None,
                eps=values.get("eps").value if values.get("eps") else None,
                source_name=primary.value.source_name, source_url=primary.value.source_url,
                source_type=primary.value.source_type, published_at=primary.value.as_of_date,
                retrieved_at=primary.value.retrieved_at, confidence=primary.value.confidence or 0.82,
            ))
            selected += 1
    return sorted(result, key=lambda item: (item.period_type, item.period), reverse=True) if period_type else result


def financial_statement_history_from_facts(
    facts: list[FinancialFact], *, period_type: str | set[str], metrics: set[str], limit: int = 4,
) -> list[FinancialStatementPeriod]:
    """Project persisted statement facts without crossing reporting bases.

    This is deliberately a read projection: it neither derives financial values
    nor changes durable precedence.  For a selected period/basis, each metric
    keeps the highest-authority persisted fact.
    """
    period_types = {period_type} if isinstance(period_type, str) else period_type
    grouped: dict[tuple[str, str, str | None], dict[str, FinancialFact]] = {}
    for fact in facts:
        if (
            fact.source_mode != SourceMode.REAL
            or fact.source_tier not in SUPPORTED_FINANCIAL_SOURCE_TIERS
            or fact.key.period_type not in period_types
            or not fact.key.period_end
            or fact.key.metric not in metrics
        ):
            continue
        values = grouped.setdefault((fact.key.period_end, fact.key.period_type, fact.key.reporting_basis), {})
        existing = values.get(fact.key.metric)
        if existing is None or fact_source_authority(fact.source_tier) > fact_source_authority(existing.source_tier):
            values[fact.key.metric] = fact

    if not grouped:
        return []

    def basis_rank(item: tuple[str | None, dict[str, FinancialFact]]) -> tuple[int, int, int]:
        basis, values = item
        return (
            max(fact_source_authority(fact.source_tier) for fact in values.values()),
            int(basis not in {None, "", "UNKNOWN"}),
            int(basis == "CONSOLIDATED"),
        )

    selected_basis_by_period: dict[tuple[str, str], str | None] = {}
    for period, kind, _basis in grouped:
        candidates = [(basis, values) for (candidate_period, candidate_kind, basis), values in grouped.items() if candidate_period == period and candidate_kind == kind]
        selected_basis_by_period[(period, kind)] = max(candidates, key=basis_rank)[0]

    result: list[FinancialStatementPeriod] = []
    # A caller asking for both annual and quarterly series gets a bounded
    # history for each series; do not let the more recent quarterlies erase
    # the annual view (or vice versa).
    selected_by_kind: dict[str, int] = {}
    for period, kind in sorted(selected_basis_by_period, reverse=True):
        if selected_by_kind.get(kind, 0) >= limit:
            continue
        basis = selected_basis_by_period[(period, kind)]
        values = grouped[(period, kind, basis)]
        result.append(FinancialStatementPeriod(
            period=period,
            period_type=kind,
            reporting_basis=basis,
            metrics={metric: fact.value for metric, fact in values.items()},
        ))
        selected_by_kind[kind] = selected_by_kind.get(kind, 0) + 1
    return result


def _apply_normalized_official_facts(result: QuarterlyResult, facts: list[FinancialFact]) -> None:
    for fact in facts:
        if (fact.source_tier not in SUPPORTED_FINANCIAL_SOURCE_TIERS
                or fact.key.period_type != "QUARTERLY"
                or fact.key.period_end not in {result.period, _canonical_quarter_end(result.period)}):
            continue
        if result.reporting_basis and fact.key.reporting_basis != result.reporting_basis:
            continue
        if fact.key.metric in {"revenue", "pat", "eps", "ebitda", "debt_or_borrowings"}:
            if fact.source_tier == FactSourceTier.YAHOO and getattr(result, fact.key.metric) is not None:
                continue
            setattr(result, fact.key.metric, fact.value)


def _is_failed_pdf_reference(document: ResearchDocument) -> bool:
    return (_enum_or_string_name(document.status) == "FAILED"
            and _enum_or_string_name(document.document_type) == "PDF_REFERENCE")


def _enum_or_string_name(value: object) -> str:
    """Return the stable domain name from persisted strings or enum instances."""
    if isinstance(value, str):
        return value.upper()
    if isinstance(value, Enum):
        return value.name.upper()
    return ""


def source_diversity(documents: list[ResearchDocument]) -> SourceDiversity:
    unique = {document.canonical_url: document for document in documents}.values()
    domains = {urlparse(document.canonical_url).hostname for document in unique if urlparse(document.canonical_url).hostname}
    classifications = [document.source_classification for document in unique]
    return SourceDiversity(
        sources_found=len(list(unique)),
        domains_found=len(domains),
        official_sources=sum(value in {SourceClassification.REGULATORY, SourceClassification.EXCHANGE, SourceClassification.OFFICIAL_COMPANY} for value in classifications),
        exchange_sources=sum(value == SourceClassification.EXCHANGE for value in classifications),
        company_sources=sum(value == SourceClassification.OFFICIAL_COMPANY for value in classifications),
        secondary_sources=sum(value not in {SourceClassification.REGULATORY, SourceClassification.EXCHANGE, SourceClassification.OFFICIAL_COMPANY} for value in classifications),
    )


@dataclass
class _FinancialParserDiagnostics:
    document_id: str | None
    source_name: str | None
    evidence_at: datetime | None
    text_length: int
    candidate_discovery_calls: int = 0
    candidate_parse_calls: int = 0
    candidate_parse_elapsed_ms: float = 0.0
    boundary_lookup_elapsed_ms: float = 0.0
    basis_lookup_calls: int = 0
    basis_lookup_elapsed_ms: float = 0.0
    region_build_calls: int = 0
    region_build_elapsed_ms: float = 0.0
    region_chars: int = 0
    max_region_chars: int = 0
    header_parse_calls: int = 0
    header_parse_elapsed_ms: float = 0.0
    header_input_calls: int = 0
    header_input_keys: set[tuple[str, datetime | None]] = field(default_factory=set)
    header_normalize_calls: int = 0
    header_normalize_elapsed_ms: float = 0.0
    first_row_search_calls: int = 0
    first_row_search_elapsed_ms: float = 0.0
    tokenize_header_calls: int = 0
    tokenize_header_requests: int = 0
    tokenize_header_cache_hits: int = 0
    tokenize_header_elapsed_ms: float = 0.0
    fallback_group_calls: int = 0
    fallback_group_elapsed_ms: float = 0.0
    header_cell_region_calls: int = 0
    header_cell_region_elapsed_ms: float = 0.0
    header_dates_calls: int = 0
    header_dates_elapsed_ms: float = 0.0
    financial_columns_calls: int = 0
    financial_columns_elapsed_ms: float = 0.0
    headingless_validation_calls: int = 0
    headingless_validation_elapsed_ms: float = 0.0
    candidate_rejection_counts: dict[str, int] = field(default_factory=dict)
    statement_quality_calls: int = 0
    statement_quality_elapsed_ms: float = 0.0


@dataclass(frozen=True)
class _StatementBoundaryIndex:
    """Document-local locations for the existing candidate-boundary regexes."""

    statement_headings: tuple[tuple[int, int], ...]
    particulars: tuple[tuple[int, int], ...]
    statement_ends: tuple[tuple[int, int], ...]

    @classmethod
    def from_text(cls, text: str) -> _StatementBoundaryIndex:
        return cls(
            statement_headings=tuple((match.start(), match.end()) for match in _STATEMENT_HEADING.finditer(text)),
            particulars=tuple((match.start(), match.end()) for match in _PARTICULARS_RE.finditer(text)),
            statement_ends=tuple((match.start(), match.end()) for match in _STATEMENT_END.finditer(text)),
        )

    @staticmethod
    def _at_or_after(spans: tuple[tuple[int, int], ...], offset: int) -> tuple[int, int] | None:
        index = bisect_left(spans, (offset, -1))
        return spans[index] if index < len(spans) else None

    def statement_heading_at_or_after(self, offset: int) -> tuple[int, int] | None:
        return self._at_or_after(self.statement_headings, offset)

    def particulars_at_or_after(self, offset: int) -> tuple[int, int] | None:
        return self._at_or_after(self.particulars, offset)

    def statement_end_at_or_after(self, offset: int) -> tuple[int, int] | None:
        return self._at_or_after(self.statement_ends, offset)


@dataclass
class _StatementCandidateParseContext:
    """Mutable caches scoped to one document's candidate-discovery pass."""

    boundary_index: _StatementBoundaryIndex
    tokenized_headers: dict[tuple[str, datetime], tuple[tuple[str, ...], tuple[str, ...]]] = field(default_factory=dict)


def latest_quarterly_result(documents: list[ResearchDocument]) -> QuarterlyResult | None:
    started_at = time.perf_counter()
    total_candidate_discovery_calls = 0
    total_statement_quality_calls = 0
    total_statement_quality_elapsed_ms = 0.0

    def log_total(extra_diagnostics: _FinancialParserDiagnostics | None = None) -> None:
        logger.info(
            "financial_parser_diag stage=TOTAL documentCount=%s durationMs=%s candidateDiscoveryCalls=%s "
            "statementQualityCalls=%s statementQualityDurationMs=%s",
            len(documents),
            round((time.perf_counter() - started_at) * 1000),
            total_candidate_discovery_calls + (extra_diagnostics.candidate_discovery_calls if extra_diagnostics else 0),
            total_statement_quality_calls + (extra_diagnostics.statement_quality_calls if extra_diagnostics else 0),
            round(total_statement_quality_elapsed_ms + (extra_diagnostics.statement_quality_elapsed_ms if extra_diagnostics else 0)),
        )

    candidates = []
    for document in documents:
        text = document.normalized_text or document.raw_text or ""
        evidence_at = document.published_at or document.retrieved_at
        diagnostics = _FinancialParserDiagnostics(
            document_id=document.document_id,
            source_name=document.source_name,
            evidence_at=evidence_at,
            text_length=len(text),
        )
        document_started_at = time.perf_counter()
        statement_candidates: list[FinancialStatement] = []
        try:
            statement_candidates = _nse_statement_candidates(
                text,
                evidence_at,
                diagnostics=diagnostics,
                diagnostic_pass="LATEST",
            )
            statement = _select_nse_income_statement(statement_candidates, diagnostics=diagnostics)
            period = statement.current_column.period_end if statement else _financial_result_period(text)
            if _STATEMENT_HEADING.search(text) and statement is None:
                continue
            if not period or not any(term in text.lower() for term in ("revenue", "income", "pat", "profit after tax", "ebitda", "eps")):
                continue
            official = document.source_classification in {
                SourceClassification.EXCHANGE, SourceClassification.REGULATORY, SourceClassification.OFFICIAL_COMPANY
            }
            basis = _reporting_basis(statement.region if statement else text)
            candidates.append((1 if official else 0, int(basis == "CONSOLIDATED"), _period_key(period),
                               evidence_at, document, period, statement, statement_candidates))
        finally:
            total_candidate_discovery_calls += diagnostics.candidate_discovery_calls
            total_statement_quality_calls += diagnostics.statement_quality_calls
            total_statement_quality_elapsed_ms += diagnostics.statement_quality_elapsed_ms
            logger.info(
                "financial_parser_diag stage=DOCUMENT documentId=%s source=%s evidenceAt=%s textLength=%s "
                "durationMs=%s statementCandidateCount=%s candidateDiscoveryCalls=%s "
                "statementQualityCalls=%s statementQualityDurationMs=%s",
                diagnostics.document_id,
                diagnostics.source_name,
                diagnostics.evidence_at,
                diagnostics.text_length,
                round((time.perf_counter() - document_started_at) * 1000),
                len(statement_candidates),
                diagnostics.candidate_discovery_calls,
                diagnostics.statement_quality_calls,
                round(diagnostics.statement_quality_elapsed_ms),
            )
    if not candidates:
        log_total()
        return None
    _, _, _, _, document, period, statement, statement_candidates = max(candidates, key=lambda item: (item[0], item[1], item[2], item[3]))
    text = document.normalized_text or document.raw_text or ""
    provenance = lambda value, unit=None: _provenance(document, value, unit=unit, period=period, confidence=0.82)
    revenue_yoy = _percent_metric(text, ("revenue", "total income"), "yoy", provenance)
    pat_yoy = _percent_metric(text, ("profit after tax", "pat"), "yoy", provenance)
    yoy_parts = []
    if revenue_yoy:
        yoy_parts.append(f"Revenue {revenue_yoy.value}% YoY")
    if pat_yoy:
        yoy_parts.append(f"PAT {pat_yoy.value}% YoY")
    selected_diagnostics = _FinancialParserDiagnostics(
        document_id=document.document_id,
        source_name=document.source_name,
        evidence_at=document.published_at or document.retrieved_at,
        text_length=len(text),
    )
    aligned = sorted(
        (candidate for candidate in statement_candidates if candidate.current_column.period_end == period),
        key=lambda candidate: _statement_quality(candidate, diagnostics=selected_diagnostics),
        reverse=True,
    )
    logger.info(
        "financial_parser_diag stage=QUALITY_SORT documentId=%s source=%s statementQualityCalls=%s "
        "statementQualityDurationMs=%s",
        selected_diagnostics.document_id,
        selected_diagnostics.source_name,
        selected_diagnostics.statement_quality_calls,
        round(selected_diagnostics.statement_quality_elapsed_ms),
    )
    log_total(selected_diagnostics)
    def table_metric(labels):
        for candidate in aligned:
            if value := _statement_metric(candidate, labels, provenance):
                return value
        return _metric(text, labels, provenance) if not statement else None
    def revenue_metric():
        for candidate in aligned:
            if value := _statement_revenue(candidate, provenance):
                return value
        return None
    def eps_metric():
        for candidate in aligned:
            if value := _statement_eps(candidate, provenance):
                return value
        return None
    return QuarterlyResult(
        period=period.upper().replace("  ", " "),
        document_title=document.title,
        reporting_basis=_reporting_basis(statement.region if statement else text),
        result_date=document.published_at,
        revenue=revenue_metric() if statement else table_metric(("revenue", "total income")),
        revenue_yoy_percent=revenue_yoy,
        revenue_qoq_percent=_percent_metric(text, ("revenue", "total income"), "qoq", provenance),
        ebitda=table_metric(("ebitda",)),
        ebitda_margin=_percent_metric(text, ("ebitda margin",), None, provenance),
        ebitda_yoy_percent=_percent_metric(text, ("ebitda",), "yoy", provenance),
        pat=table_metric(("semantic:PAT",)),
        pat_yoy_percent=pat_yoy,
        pat_qoq_percent=_percent_metric(text, ("profit after tax", "pat"), "qoq", provenance),
        eps=eps_metric() if statement else table_metric(("earning per share", "earnings per share", "eps")),
        # Balance-sheet rows have their own reporting dates; do not attach them
        # to a quarterly P&L table without a separately aligned parser.
        debt_or_borrowings=None if statement else table_metric(("total debt", "borrowings")),
        exceptional_items=_sentence_containing(text, ("exceptional item", "exceptional items")),
        segment_information=_sentence_containing(text, ("segment revenue", "segment result")),
        management_commentary=_commentary_highlights(text),
        yoy_summary="; ".join(yoy_parts) or None,
        nim=_percent_metric(text, ("net interest margin", "nim"), None, provenance),
        roa=_percent_metric(text, ("return on assets", "roa"), None, provenance),
        roe=_percent_metric(text, ("return on equity", "roe"), None, provenance),
        gross_npa=_percent_metric(text, ("gross npa", "gnpa"), None, provenance),
        net_npa=_percent_metric(text, ("net npa", "nnpa"), None, provenance),
        deposits=_metric(text, ("deposits", "total deposits"), provenance),
        advances=_metric(text, ("advances", "gross advances"), provenance),
        capital_adequacy=_percent_metric(text, ("capital adequacy", "capital adequacy ratio", "crar"), None, provenance),
        credit_cost=_percent_metric(text, ("credit cost",), None, provenance),
        source_name=document.source_name,
        source_url=document.canonical_url,
        source_type=str(document.source_type),
        published_at=document.published_at,
        retrieved_at=document.retrieved_at,
        confidence=0.82 if document.source_classification in {SourceClassification.EXCHANGE, SourceClassification.REGULATORY, SourceClassification.OFFICIAL_COMPANY} else 0.65,
    )


def parsed_nse_income_statement_periods(documents: list[ResearchDocument]) -> list[ParsedIncomeStatementPeriod]:
    """Return explicit quarterly/annual NSE income-statement columns only.

    Values are read from the table's aligned cells; no annual value is summed
    and no quarterly value is derived from a cumulative column.
    """
    periods: dict[tuple[str, str, str | None], dict[str, ProvenancedValue]] = {}
    for document in documents:
        document_periods: dict[tuple[str, str, str | None], dict[str, ProvenancedValue]] = {}
        metric_rankings: dict[tuple[tuple[str, str, str | None], str], tuple[int, int, int, int, int]] = {}
        text = document.normalized_text or document.raw_text or ""
        evidence_at = document.published_at or document.retrieved_at
        candidates = sorted(
            _nse_statement_candidates(text, evidence_at, require_quarterly=False), key=_statement_quality, reverse=True,
        )
        for statement in candidates:
            basis = _reporting_basis(statement.region)
            unit = _reported_unit(statement.unit_context[:400], statement.unit_context[:800])
            eps_cells, eps_direct_columns = _statement_eps_candidate(statement)
            rows = {
                "revenue": _statement_revenue_cells(statement),
                "pat": _classified_row_cells(statement, "PAT"),
                "eps": eps_cells,
            }
            for column in statement.columns:
                if column.period_type not in {"QUARTERLY", "ANNUAL"}:
                    continue
                values = {
                    metric: cells[column.index]
                    for metric, cells in rows.items()
                    if cells is not None and cells[column.index] is not None
                }
                if not values:
                    continue
                key = (column.period_end, column.period_type, basis)
                rank = _statement_quality(statement)
                for metric, value in values.items():
                    # For the same document and fact key, a direct aligned
                    # EPS row is structurally stronger than the legacy
                    # preceding-token fallback.  Table quality only breaks
                    # ties within the same extraction kind.
                    metric_rank = (int(metric != "eps" or eps_direct_columns[column.index]), *rank)
                    ranking_key = (key, metric)
                    if metric_rank > metric_rankings.get(ranking_key, (-1, -1, -1, -1, -1)):
                        document_periods.setdefault(key, {})[metric] = _provenance(
                            document,
                            value,
                            unit="INR per share" if metric == "eps" else unit,
                            period=column.period_end,
                            confidence=0.82,
                        )
                        metric_rankings[ranking_key] = metric_rank
        # Preserve the existing conservative cross-document first-source
        # behavior; candidate precedence above is only within one document.
        for key, metrics in document_periods.items():
            target = periods.setdefault(key, {})
            for metric, value in metrics.items():
                target.setdefault(metric, value)
    return [
        ParsedIncomeStatementPeriod(period_end, period_type, basis, tuple(metrics.items()))
        for (period_end, period_type, basis), metrics in sorted(
            periods.items(), key=lambda item: (item[0][1], item[0][2] or "", item[0][0]), reverse=True
        )
    ]


def parsed_nse_balance_sheet_periods(documents: list[ResearchDocument]) -> list[ParsedBalanceSheetPeriod]:
    """Return explicit NSE balance-sheet ``As at`` columns without derivation."""
    periods: dict[tuple[str, str, str | None], dict[str, ProvenancedValue]] = {}
    for document in documents:
        text = document.normalized_text or document.raw_text or ""
        for statement in _nse_balance_sheet_candidates(text, document.published_at or document.retrieved_at):
            basis = _reporting_basis(statement.region)
            unit = _reported_unit(statement.unit_context[:400], statement.unit_context[:800])
            rows = {
                "total_assets": _aligned_row_cells(statement, r"\btotal\s+assets\b"),
                "total_equity": _aligned_row_cells(statement, r"\btotal\s+equity\b"),
                "total_liabilities": _aligned_row_cells(statement, r"\btotal\s+liabilities\b(?!\s+and\s+equity)"),
                "current_assets": _aligned_row_cells(statement, r"\btotal\s+current\s+assets\b"),
                "current_liabilities": _aligned_row_cells(statement, r"\btotal\s+current\s+liabilities\b"),
                "cash_and_cash_equivalents": _aligned_row_cells(statement, r"\bcash\s+and\s+cash\s+equivalents?\b"),
                "total_debt": _aligned_row_cells(statement, r"\b(?:total\s+debt|total\s+borrowings?|outstanding\s+debt)\b"),
            }
            for column in statement.columns:
                values = {
                    metric: cells[column.index] for metric, cells in rows.items()
                    if cells is not None and cells[column.index] is not None
                }
                if not values:
                    continue
                key = (column.period_end, "AS_AT", basis)
                target = periods.setdefault(key, {})
                for metric, value in values.items():
                    target.setdefault(metric, _provenance(document, value, unit=unit, period=column.period_end, confidence=0.82))
    return [
        ParsedBalanceSheetPeriod(period_end, period_type, basis, tuple(metrics.items()))
        for (period_end, period_type, basis), metrics in sorted(periods.items(), reverse=True)
    ]


def parsed_nse_cash_flow_periods(documents: list[ResearchDocument]) -> list[ParsedCashFlowPeriod]:
    """Return only explicitly headed annual NSE cash-flow columns."""
    periods: dict[tuple[str, str, str | None], dict[str, ProvenancedValue]] = {}
    for document in documents:
        text = document.normalized_text or document.raw_text or ""
        for statement in _nse_cash_flow_candidates(text, document.published_at or document.retrieved_at):
            basis = _reporting_basis(statement.region)
            unit = _reported_unit(statement.unit_context[:400], statement.unit_context[:800])
            rows = {
                "cash_flow_from_operating_activities": _aligned_row_cells(statement, r"\bnet\s+cash\s+(?:(?:flow\s*/?\s*\(?used\)?\s+in|flow\s+from|generated\s+from|from))\s+(?:operating|operations)\s+activities\b"),
                "cash_flow_from_investing_activities": _aligned_row_cells(statement, r"\bnet\s+cash\s+(?:(?:flow\s*/?\s*\(?used\)?\s+in|flow\s+from|used\s+in|from))\s+investing\s+activities\b"),
                "cash_flow_from_financing_activities": _aligned_row_cells(statement, r"\bnet\s+cash\s+(?:(?:flow\s*/?\s*\(?used\)?\s+in|flow\s+from|used\s+in|from))\s+financing\s+activities\b"),
                "net_change_in_cash": _aligned_row_cells(statement, r"\bnet\s+(?:increase|decrease|change)\s+in\s+cash(?:\s+and\s+cash\s+equivalents?)?\b"),
            }
            for column in statement.columns:
                if column.period_type != "ANNUAL":
                    continue
                values = {metric: cells[column.index] for metric, cells in rows.items()
                          if cells is not None and cells[column.index] is not None}
                if not values:
                    continue
                key = (column.period_end, column.period_type, basis)
                target = periods.setdefault(key, {})
                for metric, value in values.items():
                    target.setdefault(metric, _provenance(document, value, unit=unit, period=column.period_end, confidence=0.82))
    return [ParsedCashFlowPeriod(period_end, period_type, basis, tuple(metrics.items()))
            for (period_end, period_type, basis), metrics in sorted(periods.items(), reverse=True)]


def _reporting_basis(text: str) -> str | None:
    lowered = text.lower()
    if "consolidated" in lowered:
        return "CONSOLIDATED"
    if "standalone" in lowered:
        return "STANDALONE"
    return None


def shareholding_changes(documents: list[ResearchDocument]) -> list[ShareholdingChange]:
    results = []
    labels = {
        "PROMOTER": ("promoter holding", "promoters"),
        "FII_FPI": ("fii/fpi", "fii", "fpi"),
        "DII": ("dii", "domestic institutional"),
        "PUBLIC": ("public holding", "public shareholders"),
        "PROMOTER_PLEDGED": ("promoter pledge", "pledged shares"),
    }
    for document in sorted(documents, key=lambda value: value.published_at or value.retrieved_at, reverse=True):
        if document.source_classification not in {
            SourceClassification.EXCHANGE, SourceClassification.REGULATORY, SourceClassification.OFFICIAL_COMPANY
        }:
            continue
        text = document.normalized_text or document.raw_text or ""
        periods = PERIOD_RE.findall(text)
        if len(periods) < 2:
            continue
        for category, aliases in labels.items():
            values = _two_percentages(text, aliases)
            if not values:
                continue
            current, previous = values
            results.append(ShareholdingChange(
                category=category,
                current=_provenance(document, current, unit="PERCENT", period=periods[0], confidence=0.80),
                previous=_provenance(document, previous, unit="PERCENT", period=periods[1], confidence=0.80),
                current_period=periods[0],
                previous_period=periods[1],
                change_percentage_points=current - previous,
                source_date=document.published_at,
            ))
        if results:
            break
    return results


def shareholding_changes_from_snapshots(
    snapshots: list[ShareholdingSnapshot],
) -> list[ShareholdingChange]:
    """Compare the two latest distinct structured shareholding periods.

    Snapshot values are already normalized semantic categories.  They are
    deliberately selected, never aggregated: XBRL parent/child rows are not
    additive ownership categories.
    """
    valid = [snapshot for snapshot in snapshots
             if snapshot.source_mode == SourceMode.REAL and snapshot.values]
    latest_by_period: dict[date, ShareholdingSnapshot] = {}
    for snapshot in sorted(
        valid,
        key=lambda value: (
            value.period_end,
            value.published_at or datetime.min.replace(tzinfo=timezone.utc),
            value.retrieved_at,
        ),
        reverse=True,
    ):
        latest_by_period.setdefault(snapshot.period_end.date(), snapshot)
    periods = sorted(latest_by_period, reverse=True)
    if len(periods) < 2:
        return []

    current_snapshot = latest_by_period[periods[0]]
    previous_snapshot = latest_by_period[periods[1]]
    current_values = _snapshot_values_by_category(current_snapshot)
    previous_values = _snapshot_values_by_category(previous_snapshot)
    results: list[ShareholdingChange] = []
    for category, current_value in current_values.items():
        previous_value = previous_values.get(category)
        if previous_value is None:
            continue
        results.append(ShareholdingChange(
            category=str(category),
            current=_snapshot_provenance(current_snapshot, current_value),
            previous=_snapshot_provenance(previous_snapshot, previous_value),
            current_period=current_snapshot.period_end.date().isoformat(),
            previous_period=previous_snapshot.period_end.date().isoformat(),
            change_percentage_points=current_value.percentage - previous_value.percentage,
            source_date=current_snapshot.published_at,
        ))
    return results


def _snapshot_values_by_category(
    snapshot: ShareholdingSnapshot,
) -> dict:
    """Return one explicit source value per category without aggregation."""
    values = {}
    for value in snapshot.values:
        values.setdefault(value.category, value)
    return values


def _snapshot_provenance(
    snapshot: ShareholdingSnapshot,
    value: ShareholdingSnapshotValue,
) -> ProvenancedValue:
    return ProvenancedValue(
        value=value.percentage,
        unit="PERCENT",
        as_of_date=snapshot.period_end,
        period=snapshot.period_end.date().isoformat(),
        source_url=snapshot.source_url,
        source_name=snapshot.source_provider,
        source_type=snapshot.source_type,
        published_at=snapshot.published_at,
        retrieved_at=snapshot.retrieved_at,
        confidence=float(snapshot.confidence),
    )


def valuation_assessment(documents: list[ResearchDocument]) -> ValuationAssessment:
    metrics = {}
    for document in sorted(documents, key=lambda value: value.published_at or value.retrieved_at, reverse=True):
        text = document.normalized_text or document.raw_text or ""
        for key, labels in {
            "current_pe": ("current p/e", "p/e ratio", "pe ratio"),
            "sector_pe": ("sector p/e", "industry p/e"),
            "peer_pe": ("peer p/e", "peer median p/e"),
            "historical_pe": ("historical p/e", "5 year p/e", "five year p/e"),
            "roe": ("roe", "return on equity"),
            "roce": ("roce", "return on capital employed"),
        }.items():
            extractor = _percentage_label_number if key in {"roe", "roce"} else _label_number
            if key not in metrics and (value := extractor(text, labels)) is not None:
                metrics[key] = _provenance(document, value, unit="PERCENT" if key in {"roe", "roce"} else "RATIO", confidence=0.65)
    current = _decimal_value(metrics.get("current_pe"))
    benchmark_metrics = [(kind, metrics.get(key)) for kind, key in (("SECTOR_PE", "sector_pe"), ("PEER_PE", "peer_pe"), ("HISTORICAL_PE", "historical_pe"))]
    benchmarks = [ValuationBenchmark(kind=kind, value=value) for kind, value in benchmark_metrics if value is not None and (_decimal_value(value) or Decimal(0)) > 0]
    comparisons = [_decimal_value(benchmark.value) for benchmark in benchmarks]
    state = "UNKNOWN"
    reason = "Insufficient comparable public valuation evidence."
    state_evidence = None
    if current and comparisons:
        benchmark = sum(comparisons) / Decimal(len(comparisons))
        ratio = current / benchmark
        if ratio <= Decimal("0.80"):
            state, reason = "CHEAP", "Current P/E is at least 20% below available sector, peer, or historical context."
        elif ratio >= Decimal("1.20"):
            state, reason = "EXPENSIVE", "Current P/E is at least 20% above available sector, peer, or historical context."
        else:
            state, reason = "FAIR", "Current P/E is within 20% of available sector, peer, or historical context."
        state_evidence = ValuationStateEvidence(primary_metric="CURRENT_PE", current_value=metrics["current_pe"], benchmarks=benchmarks, benchmark_value=benchmark, comparison_ratio=ratio, comparison_method="CURRENT_PE_VS_AVAILABLE_PE_BENCHMARK_MEAN", explanation=reason)
    return ValuationAssessment(state=state, reason=reason, state_evidence=state_evidence, **metrics)


def current_quarter_catalysts(events: list[ResearchEvent]) -> list[ResearchEvent]:
    now = datetime.now(timezone.utc)
    quarter = (now.month - 1) // 3
    aliases = {
        ResearchEventType.NEW_ORDER: ResearchEventType.ORDER_WIN,
        ResearchEventType.MAJOR_CONTRACT: ResearchEventType.NEW_CONTRACT,
        ResearchEventType.GOVERNMENT_CONTRACT: ResearchEventType.NEW_CONTRACT,
        ResearchEventType.NEW_CUSTOMER: ResearchEventType.CLIENT_WIN,
        ResearchEventType.CUSTOMER_EXPANSION: ResearchEventType.CLIENT_WIN,
        ResearchEventType.MAJOR_CUSTOMER: ResearchEventType.CLIENT_WIN,
        ResearchEventType.FACTORY_EXPANSION: ResearchEventType.NEW_PLANT,
        ResearchEventType.NEW_FACILITY: ResearchEventType.NEW_PLANT,
        ResearchEventType.DEBT_CHANGE: ResearchEventType.BORROWING_CHANGE,
        ResearchEventType.FUNDING: ResearchEventType.BORROWING_CHANGE,
        ResearchEventType.GUIDANCE_RAISED: ResearchEventType.MANAGEMENT_GUIDANCE,
        ResearchEventType.GUIDANCE_LOWERED: ResearchEventType.MANAGEMENT_GUIDANCE,
        ResearchEventType.GUIDANCE_MAINTAINED: ResearchEventType.MANAGEMENT_GUIDANCE,
        ResearchEventType.GUIDANCE_CUT: ResearchEventType.MANAGEMENT_GUIDANCE,
        ResearchEventType.REVENUE_GUIDANCE: ResearchEventType.MANAGEMENT_GUIDANCE,
        ResearchEventType.MARGIN_GUIDANCE: ResearchEventType.MANAGEMENT_GUIDANCE,
        ResearchEventType.REGULATORY_EVENT: ResearchEventType.MAJOR_CORPORATE_ANNOUNCEMENT,
    }
    current = []
    for event in events:
        event_time = event.event_date or event.published_at or event.detected_at
        if event_time.year == now.year and (event_time.month - 1) // 3 == quarter:
            current.append(event.model_copy(update={"event_type": aliases.get(event.event_type, event.event_type)}))
    return current


def _metric(text, labels, factory):
    for label in labels:
        match = re.search(rf"\b{re.escape(label)}\b[^\d+-]{{0,24}}{NUMBER}([^.;\n]{{0,40}})", text, re.I)
        if not match:
            continue
        value = _decimal(match.group(1))
        if value is None:
            continue
        reported_unit = _reported_unit(match.group(0), text[max(match.start() - 50, 0):match.end() + 50])
        return factory(value, reported_unit)
    return None


def _financial_result_period(text: str) -> str | None:
    if _NSE_QUARTER_HEADING.search(text):
        return _nse_table_period_end(text)
    return match.group(1) if (match := PERIOD_RE.search(text)) else None


def _parse_nse_income_statement(
    text: str,
    evidence_at: datetime,
    *,
    diagnostics: _FinancialParserDiagnostics | None = None,
) -> FinancialStatement | None:
    candidates = _nse_statement_candidates(
        text,
        evidence_at,
        diagnostics=diagnostics,
        diagnostic_pass="PARSE_INCOME",
    )
    return _select_nse_income_statement(candidates, diagnostics=diagnostics)


def _select_nse_income_statement(
    candidates: list[FinancialStatement],
    *,
    diagnostics: _FinancialParserDiagnostics | None = None,
) -> FinancialStatement | None:
    return max(candidates, key=lambda candidate: _statement_quality(candidate, diagnostics=diagnostics)) if candidates else None


def _nse_statement_candidates(
    text: str,
    evidence_at: datetime,
    *,
    require_quarterly: bool = True,
    diagnostics: _FinancialParserDiagnostics | None = None,
    diagnostic_pass: str | None = None,
) -> list[FinancialStatement]:
    started_at = time.perf_counter() if diagnostics else None
    candidates = []
    boundary_index = _StatementBoundaryIndex.from_text(text)
    parse_context = _StatementCandidateParseContext(boundary_index)
    heading_starts = [(start, True) for start, _ in boundary_index.statement_headings]
    particulars_starts = [(start, False) for start, _ in boundary_index.particulars]
    starts = heading_starts + particulars_starts
    for start, has_heading in starts:
        candidate = _parse_statement_candidate(
            text,
            evidence_at,
            start,
            has_heading,
            require_quarterly=require_quarterly,
            boundary_index=boundary_index,
            parse_context=parse_context,
            diagnostics=diagnostics,
        )
        if candidate is not None:
            candidates.append(candidate)
    if diagnostics:
        diagnostics.candidate_discovery_calls += 1
        logger.info(
            "financial_parser_diag stage=CANDIDATE_DISCOVERY pass=%s documentId=%s source=%s textLength=%s "
            "statementHeadingCount=%s particularsCount=%s candidateStartCount=%s acceptedCandidateCount=%s durationMs=%s",
            diagnostic_pass,
            diagnostics.document_id,
            diagnostics.source_name,
            diagnostics.text_length,
            len(heading_starts),
            len(particulars_starts),
            len(starts),
            len(candidates),
            round((time.perf_counter() - started_at) * 1000),
        )
        logger.info(
            "financial_parser_diag stage=CANDIDATE_INTERNALS pass=%s documentId=%s candidateParseCalls=%s "
            "candidateParseDurationMs=%s boundaryLookupDurationMs=%s basisLookupCalls=%s basisLookupDurationMs=%s "
            "regionBuildCalls=%s regionBuildDurationMs=%s regionChars=%s maxRegionChars=%s "
            "headerParseCalls=%s headerParseDurationMs=%s headinglessValidationCalls=%s "
            "headinglessValidationDurationMs=%s rejectionCounts=%s",
            diagnostic_pass,
            diagnostics.document_id,
            diagnostics.candidate_parse_calls,
            round(diagnostics.candidate_parse_elapsed_ms),
            round(diagnostics.boundary_lookup_elapsed_ms),
            diagnostics.basis_lookup_calls,
            round(diagnostics.basis_lookup_elapsed_ms),
            diagnostics.region_build_calls,
            round(diagnostics.region_build_elapsed_ms),
            diagnostics.region_chars,
            diagnostics.max_region_chars,
            diagnostics.header_parse_calls,
            round(diagnostics.header_parse_elapsed_ms),
            diagnostics.headingless_validation_calls,
            round(diagnostics.headingless_validation_elapsed_ms),
            diagnostics.candidate_rejection_counts,
        )
        logger.info(
            "financial_parser_diag stage=HEADER_INTERNALS pass=%s documentId=%s headerInputCalls=%s "
            "uniqueHeaderInputs=%s reusedHeaderInputCalls=%s headerNormalizeCalls=%s headerNormalizeDurationMs=%s "
            "firstRowSearchCalls=%s firstRowSearchDurationMs=%s tokenizeHeaderRequests=%s tokenizeHeaderCalls=%s "
            "tokenizeHeaderCacheHits=%s tokenizeHeaderDurationMs=%s "
            "fallbackGroupCalls=%s fallbackGroupDurationMs=%s headerCellRegionCalls=%s "
            "headerCellRegionDurationMs=%s headerDatesCalls=%s headerDatesDurationMs=%s "
            "financialColumnsCalls=%s financialColumnsDurationMs=%s",
            diagnostic_pass,
            diagnostics.document_id,
            diagnostics.header_input_calls,
            len(diagnostics.header_input_keys),
            diagnostics.header_input_calls - len(diagnostics.header_input_keys),
            diagnostics.header_normalize_calls,
            round(diagnostics.header_normalize_elapsed_ms),
            diagnostics.first_row_search_calls,
            round(diagnostics.first_row_search_elapsed_ms),
            diagnostics.tokenize_header_requests,
            diagnostics.tokenize_header_calls,
            diagnostics.tokenize_header_cache_hits,
            round(diagnostics.tokenize_header_elapsed_ms),
            diagnostics.fallback_group_calls,
            round(diagnostics.fallback_group_elapsed_ms),
            diagnostics.header_cell_region_calls,
            round(diagnostics.header_cell_region_elapsed_ms),
            diagnostics.header_dates_calls,
            round(diagnostics.header_dates_elapsed_ms),
            diagnostics.financial_columns_calls,
            round(diagnostics.financial_columns_elapsed_ms),
        )
    return candidates


def _nse_balance_sheet_candidates(text: str, evidence_at: datetime) -> list[FinancialStatement]:
    candidates = []
    for heading in _BALANCE_SHEET_HEADING.finditer(text):
        particulars = _PARTICULARS_RE.search(text, heading.end())
        if particulars is None or particulars.start() - heading.end() > 800:
            continue
        next_heading = _BALANCE_SHEET_HEADING.search(text, particulars.end())
        next_income = _STATEMENT_HEADING.search(text, particulars.end())
        end = min((match.start() for match in (next_heading, next_income) if match is not None), default=heading.start() + 8000)
        region_start = heading.start()
        basis_prefix = re.search(r"\b(?:standalone|consolidated)\s*$", text[max(0, heading.start() - 40):heading.start()], re.I)
        if basis_prefix:
            region_start = max(0, heading.start() - 40 + basis_prefix.start())
        region = text[region_start:end]
        local_particulars = _PARTICULARS_RE.search(region)
        if local_particulars is None:
            continue
        header = _normalize_nse_header_ocr(region[local_particulars.end():local_particulars.end() + 500])
        first_row = re.search(r"\b(?:total\s+assets|total\s+equity|cash\s+and\s+cash|current\s+assets|liabilities)\b", header, re.I)
        if first_row:
            header = header[:first_row.start()]
        dates = _header_dates(header, evidence_at, ["annual"])
        columns = _financial_columns(["annual"], dates)
        if not columns:
            continue
        candidates.append(FinancialStatement(region, tuple(columns), max(columns, key=lambda column: column.period_end), region[:500]))
    return candidates


def _compact_year_ended_dates(header: str) -> list[str]:
    """Tokenize a compact OCR ``YearEnded31March20Z5`` header structurally."""
    compact = header.translate(str.maketrans({"Z": "2", "z": "2"}))
    values = []
    for match in re.finditer(r"year\s*ended\D{0,8}(\d{1,2})\s*([A-Za-z]+)\s*(20\d{2})", compact, re.I):
        parsed = _nse_date(match.group(1), match.group(2), match.group(3))
        if parsed is not None:
            values.append(parsed.isoformat())
    return values


def _nse_cash_flow_candidates(text: str, evidence_at: datetime) -> list[FinancialStatement]:
    candidates = []
    for heading in _CASH_FLOW_HEADING.finditer(text):
        particulars = _PARTICULARS_RE.search(text, heading.end())
        if particulars is None or particulars.start() - heading.end() > 800:
            continue
        next_heading = _CASH_FLOW_HEADING.search(text, particulars.end())
        next_income = _STATEMENT_HEADING.search(text, particulars.end())
        next_balance = _BALANCE_SHEET_HEADING.search(text, particulars.end())
        end = min((match.start() for match in (next_heading, next_income, next_balance) if match is not None), default=heading.start() + 8000)
        region_start = heading.start()
        basis_prefix = re.search(r"\b(?:standalone|consolidated)\s*$", text[max(0, heading.start() - 40):heading.start()], re.I)
        if basis_prefix:
            region_start = max(0, heading.start() - 40 + basis_prefix.start())
        region = text[region_start:end]
        local_particulars = _PARTICULARS_RE.search(region)
        if local_particulars is None:
            continue
        header = _normalize_nse_header_ocr(region[local_particulars.end():local_particulars.end() + 500])
        # A March date alone is not a cash-flow period classification.
        if not re.search(r"year\s*ended", header, re.I):
            continue
        first_row = re.search(r"\b(?:net\s+cash|cash\s+flow)\b", header, re.I)
        if first_row:
            header = header[:first_row.start()]
        groups = [match.group(1).lower() for match in _GROUP_RE.finditer(header)]
        dates = _header_dates(header, evidence_at, groups)
        columns = _financial_columns(groups, dates)
        if not columns:
            continue
        candidates.append(FinancialStatement(region, tuple(columns), max(columns, key=lambda column: column.period_end), region[:500]))
    return candidates


def _parse_statement_candidate(
    text: str,
    evidence_at: datetime,
    start: int,
    has_heading: bool,
    *,
    require_quarterly: bool = True,
    boundary_index: _StatementBoundaryIndex | None = None,
    parse_context: _StatementCandidateParseContext | None = None,
    diagnostics: _FinancialParserDiagnostics | None = None,
) -> FinancialStatement | None:
    candidate_started_at = time.perf_counter() if diagnostics else None

    def finish(value: FinancialStatement | None, rejection: str | None = None) -> FinancialStatement | None:
        if diagnostics:
            diagnostics.candidate_parse_calls += 1
            diagnostics.candidate_parse_elapsed_ms += (time.perf_counter() - candidate_started_at) * 1000
            if rejection:
                diagnostics.candidate_rejection_counts[rejection] = diagnostics.candidate_rejection_counts.get(rejection, 0) + 1
        return value

    boundary_started_at = time.perf_counter() if diagnostics else None
    parse_context = parse_context or _StatementCandidateParseContext(
        boundary_index or _StatementBoundaryIndex.from_text(text),
    )
    boundary_index = parse_context.boundary_index
    heading = boundary_index.statement_heading_at_or_after(start) if has_heading else None
    if has_heading and (heading is None or heading[0] != start):
        return finish(None, "HEADING_OFFSET_MISMATCH")
    particulars_match = boundary_index.particulars_at_or_after(start)
    if particulars_match is None:
        return finish(None, "NO_PARTICULARS")
    particulars_start = particulars_match[0]
    if not has_heading and particulars_start != start:
        return finish(None, "PARTICULARS_OFFSET_MISMATCH")
    start = heading[0] if heading else 0
    next_statement = boundary_index.statement_heading_at_or_after(heading[1]) if heading else boundary_index.statement_heading_at_or_after(start + 1)
    statement_end = next_statement[0] if next_statement else None
    if next_statement and heading:
        basis_prefix = re.search(r"\b(?:standalone|consolidated)\s*$", text[max(heading[1], next_statement[0] - 40):next_statement[0]], re.I)
        if basis_prefix:
            statement_end = max(heading[1], next_statement[0] - 40) + basis_prefix.start()
    next_particulars = boundary_index.particulars_at_or_after(particulars_match[1])
    next_particulars_start = next_particulars[0] if next_particulars else None
    statement_end_match = boundary_index.statement_end_at_or_after(heading[1] if heading else start)
    end_candidates = [value for value in (statement_end_match[0] if statement_end_match else None, statement_end, next_particulars_start) if value is not None]
    end = min(end_candidates) if end_candidates else start + 8000
    if diagnostics:
        diagnostics.boundary_lookup_elapsed_ms += (time.perf_counter() - boundary_started_at) * 1000
    # Do not let a later statement re-read metric rows from the previous PDF
    # page. Keep only an immediately preceding reporting-basis label.
    basis_started_at = time.perf_counter() if diagnostics else None
    basis_context = re.search(r"\b(?:standalone|consolidated)\s*$", text[max(0, start - 40):start], re.I) if heading else None
    if diagnostics and heading:
        diagnostics.basis_lookup_calls += 1
        diagnostics.basis_lookup_elapsed_ms += (time.perf_counter() - basis_started_at) * 1000
    region_start = max(0, start - 40 + basis_context.start()) if basis_context else start
    region_started_at = time.perf_counter() if diagnostics else None
    region = text[region_start:end]
    if diagnostics:
        diagnostics.region_build_calls += 1
        diagnostics.region_build_elapsed_ms += (time.perf_counter() - region_started_at) * 1000
        diagnostics.region_chars += len(region)
        diagnostics.max_region_chars = max(diagnostics.max_region_chars, len(region))
    particulars = boundary_index.particulars_at_or_after(start)
    if particulars is None or particulars[1] > end:
        return finish(None, "NO_PARTICULARS_IN_REGION")
    header_started_at = time.perf_counter() if diagnostics else None
    try:
        header_offset = particulars[1] - region_start
        header = region[header_offset:header_offset + 700]
        # Preserve only an immediately adjacent group-label run such as
        # ``Quarter ended Year ended SI Particulars``.  The selected text is
        # already bounded by this statement candidate and the anchored match
        # prevents prose or a previous table from becoming header context.
        pre_particulars = region[:particulars[0] - region_start]
        preceding_groups = _PRE_PARTICULARS_GROUPS_RE.search(pre_particulars)
        if preceding_groups:
            header = f"{preceding_groups.group()} {header}"
        if diagnostics:
            diagnostics.header_input_calls += 1
            diagnostics.header_input_keys.add((header, evidence_at))
        header = _normalize_nse_header_ocr(header, diagnostics=diagnostics)
        first_row_started_at = time.perf_counter() if diagnostics else None
        first_row = re.search(r"\b(?:revenue\s+from\s+operations|total\s+revenue|total\s+income|net\s+profit|profit\s+after\s+tax|earning(?:s)?\s+per\s+share)\b", header, re.I)
        if diagnostics:
            diagnostics.first_row_search_calls += 1
            diagnostics.first_row_search_elapsed_ms += (time.perf_counter() - first_row_started_at) * 1000
        if first_row:
            header = header[:first_row.start()]
        if diagnostics:
            diagnostics.tokenize_header_requests += 1
        tokenization_key = (header, evidence_at)
        tokenized_header = parse_context.tokenized_headers.get(tokenization_key)
        if tokenized_header is None:
            tokenize_started_at = time.perf_counter() if diagnostics else None
            groups, dates = _tokenize_financial_header(header, evidence_at)
            tokenized_header = (tuple(groups), tuple(dates))
            parse_context.tokenized_headers[tokenization_key] = tokenized_header
            if diagnostics:
                diagnostics.tokenize_header_calls += 1
                diagnostics.tokenize_header_elapsed_ms += (time.perf_counter() - tokenize_started_at) * 1000
        elif diagnostics:
            diagnostics.tokenize_header_cache_hits += 1
        token_groups, token_dates = tokenized_header
        if token_groups:
            groups = token_groups
        else:
            fallback_groups_started_at = time.perf_counter() if diagnostics else None
            groups = _deduplicate_header_groups([match.group(1).lower() for match in _GROUP_RE.finditer(header)])
            if diagnostics:
                diagnostics.fallback_group_calls += 1
                diagnostics.fallback_group_elapsed_ms += (time.perf_counter() - fallback_groups_started_at) * 1000
        header_cell_region_started_at = time.perf_counter() if diagnostics else None
        header = _header_cell_region(header, groups)
        if diagnostics:
            diagnostics.header_cell_region_calls += 1
            diagnostics.header_cell_region_elapsed_ms += (time.perf_counter() - header_cell_region_started_at) * 1000
        if not heading:
            validation_started_at = time.perf_counter() if diagnostics else None
            has_income_row = re.search(r"\b(?:revenue\s+from\s+operations|total\s+revenue|total\s+income|profit\s+(?:after\s+tax|for\s+the\s+period)|net\s+profit)\b", region, re.I)
            if diagnostics:
                diagnostics.headingless_validation_calls += 1
                diagnostics.headingless_validation_elapsed_ms += (time.perf_counter() - validation_started_at) * 1000
            if not has_income_row:
                return finish(None, "HEADINGLESS_NO_INCOME_ROW")
        dates = token_dates or _header_dates(header, evidence_at, groups, diagnostics=diagnostics)
        # A quarter-and-nine-month filing can lose the ``nine months ended``
        # table-header token during PDF extraction and leave a spurious
        # ``year ended`` label nearby.  Its five date cells still describe
        # three quarterly and two cumulative nine-month columns, not annual
        # results.  The explicit report heading is stronger evidence than
        # that damaged table-group token.  Do not apply this repair to a
        # six-column table, which may genuinely contain an annual column.
        if len(dates) == 5 and _quarter_nine_month_reporting_semantics(region):
            groups = ["quarter", "nine month"]
        columns = _financial_columns(groups, dates, diagnostics=diagnostics)
    finally:
        if diagnostics:
            diagnostics.header_parse_calls += 1
            diagnostics.header_parse_elapsed_ms += (time.perf_counter() - header_started_at) * 1000
    quarterly = [column for column in columns if column.period_type == "QUARTERLY"]
    annual = [column for column in columns if column.period_type == "ANNUAL"]
    if (require_quarterly and not quarterly) or (not quarterly and not annual):
        return finish(None, "NO_SUPPORTED_COLUMNS")
    current_columns = quarterly or annual
    return finish(FinancialStatement(region, tuple(columns), max(current_columns, key=lambda column: column.period_end),
                                     text[max(0, start - 250):end]))


def _statement_quality(
    statement: FinancialStatement,
    *,
    diagnostics: _FinancialParserDiagnostics | None = None,
) -> tuple[int, int, int, int]:
    started_at = time.perf_counter() if diagnostics else None
    metrics = sum(_aligned_row_value(statement, pattern) is not None for pattern in (
        r"(?<!total )\brevenue\s+from\s+operations\b", r"\btotal\s+revenue\s+from\s+operations\b",
        r"\bnet\s+profit\b", r"\bprofit\s+after\s+tax\b", r"\bprofit\s+for\s+the\s+period\b",
        r"\bbasic\b"))
    # Prefer a statement with a valid after-tax/PAT row when the general
    # metric count ties. Deliberately exclude before-tax profit rows.
    has_pat = any(_aligned_row_value(statement, rf"\b{re.escape(label)}\b") is not None for label in (
        "net profit for the period after tax", "profit for the period from continuing operations",
        "profit after tax", "profit for the period", "pat"))
    result = metrics, int(has_pat), len(statement.columns), int(bool(_STATEMENT_HEADING.search(statement.region)))
    if diagnostics:
        diagnostics.statement_quality_calls += 1
        diagnostics.statement_quality_elapsed_ms += (time.perf_counter() - started_at) * 1000
    return result


def _normalize_nse_header_ocr(
    header: str,
    *,
    diagnostics: _FinancialParserDiagnostics | None = None,
) -> str:
    """Repair only observed date/header tokens, never financial-value cells."""
    started_at = time.perf_counter() if diagnostics else None
    for corrupted, repaired in (
        ("3lst", "31st"),
        ("3Lst", "31st"),
        ("315t", "31st"),
        ("Deceli`I]er", "December"),
        ("De[embei", "December"),
        ("Se|)tember", "September"),
        ("DΓé¼cembe]", "December"),
        ("D▒cembe]", "December"),
        ("D€cembe]", "December"),
        ("Slat March", "31st March"),
        ("Quar`er", "Quarter"),
        ("Quar`er€nded", "Quarter Ended"),
        ("Nlne Momh", "Nine Month"),
        ("Year [rrded", "Year Ended"),
        ("so September", "30 September"),
        ("2o25", "2025"),
        ("Decembe.", "December"),
        ("Man:h", "March"),
    ):
        header = header.replace(corrupted, repaired)
    if diagnostics:
        diagnostics.header_normalize_calls += 1
        diagnostics.header_normalize_elapsed_ms += (time.perf_counter() - started_at) * 1000
    return header


def _header_cell_region(header: str, groups: list[str]) -> str:
    """Keep the header through its expected year cells, excluding table rows."""
    expected = {("quarter", "year"): 5, ("quarter", "nine month", "year"): 6,
                ("quarter", "nine months", "year"): 6}.get(tuple(groups))
    if not expected:
        return header
    years = list(re.finditer(r"\b[2Z][0-9Z]{3}\b", header))
    return header[:years[expected - 1].end()] if len(years) >= expected else header


def _nse_header_dates(header: str) -> list[date | None]:
    """Read explicit day-month-year header cells in their source order."""
    parsed: list[tuple[int, date | None]] = []
    parsed.extend((match.start(), _nse_date(*match.groups())) for match in _NSE_DATE.finditer(header))
    parsed.extend((match.start(), _nse_date(match.group(2), match.group(1), match.group(3)))
                  for match in _NSE_MONTH_DAY_DATE.finditer(header))
    parsed.extend((match.start(), _nse_numeric_date(*match.groups())) for match in _NSE_NUMERIC_DATE.finditer(header))
    return [value for _offset, value in sorted(parsed)]


def _nse_numeric_date(day: str, month: str, year: str) -> date | None:
    try:
        return date(int(year.replace("Z", "2")), int(month), int(day))
    except ValueError:
        return None


def _tokenize_financial_header(header: str, evidence_at: datetime) -> tuple[list[str], list[str]]:
    """Interpret compact table headers as structural tokens, never value text."""
    raw = list(re.finditer(r"\S+", header[:700]))
    words = [re.sub(r"[^a-z]", "", item.group().lower()) for item in raw]
    groups: list[str] = []
    for index in range(len(words)):
        phrase = "".join(words[index:index + 3])
        if _edit_distance(phrase[:len("quarterended")], "quarterended") <= 2:
            groups.append("quarter")
        elif _edit_distance(phrase[:len("ninemonthended")], "ninemonthended") <= 3:
            groups.append("nine month")
        elif _edit_distance(phrase[:len("yearended")], "yearended") <= 2:
            groups.append("year")
    groups = _deduplicate_header_groups(groups)
    expected = {("quarter", "year"): 5, ("quarter", "nine month", "year"): 6}.get(tuple(groups), 0)
    if not expected:
        return [], []
    months = []
    for word in words:
        match = next((name for name in _MONTHS if _edit_distance(word, name) <= 2), None)
        months.append(match)
    pairs = []
    for index, month in enumerate(months):
        if not month or index == 0:
            continue
        day = re.sub(r"(?i)(st|nd|rd|th)$", "", raw[index - 1].group())
        day = day.translate(str.maketrans({"s": "3", "S": "3", "o": "0", "O": "0", "l": "1", "I": "1"}))
        digits = re.sub(r"\D", "", day)
        if digits and 1 <= int(digits) <= 31:
            pairs.append((int(digits), month))
    years = []
    for item in raw:
        normalized = item.group().translate(str.maketrans({"Z": "2", "z": "2", "o": "0", "O": "0", "l": "1", "I": "1"}))
        digits = re.sub(r"\D", "", normalized)
        if len(digits) == 4 and digits.startswith("20"):
            years.append(int(digits))
    if len(pairs) < expected or len(years) < expected:
        return [], []
    try:
        dates = [date(year, _MONTHS[month], day).isoformat() for (day, month), year in zip(pairs[:expected], years[:expected])]
    except ValueError:
        # An OCR-flattened header can pair a real day token with the wrong
        # month.  Reject this tokenized header rather than manufacturing a
        # calendar date or aborting the surrounding document reconciliation.
        return [], []
    floor, ceiling = evidence_at.year - 3, evidence_at.year + 1
    return (groups, dates) if all(floor <= int(value[:4]) <= ceiling for value in dates) else ([], [])


def _deduplicate_header_groups(groups: list[str]) -> list[str]:
    """OCR may duplicate an adjacent header group without adding columns."""
    result = []
    for group in groups:
        if not result or result[-1] != group:
            result.append(group)
    return result


def _header_dates(
    header: str,
    evidence_at: datetime,
    groups: list[str],
    *,
    diagnostics: _FinancialParserDiagnostics | None = None,
) -> list[str]:
    started_at = time.perf_counter() if diagnostics else None
    try:
        header = _normalize_nse_header_ocr(header, diagnostics=diagnostics)
        groups = _deduplicate_header_groups(groups)
        direct = _nse_header_dates(header)
        candidates = [[value for value in direct if value is not None]]
        fragments = list(_NSE_DAY_MONTH.finditer(header))
        month_day_fragments = [
            match for match in re.finditer(
                r"\b([A-Za-z]{3,12})\s+(\d{1,2})(?:[lI]?st|nd|rd|th|tli)?(?:\s*,?\s*([2Z][0-9Z]{3}))?\b",
                header,
            )
            if match.group(1).lower() in _MONTHS
        ]
        years = re.findall(r"\b([2Z][0-9Z]{3})\b", header)
        if len(fragments) == len(years):
            split = [_nse_date(match.group(1), match.group(2), year.replace("Z", "2"))
                     for match, year in zip(fragments, years)]
            if all(value is not None for value in split):
                candidates.insert(0, split)
        explicit_years = [match.group(3) for match in month_day_fragments if match.group(3)]
        deferred_years = list(years)
        for explicit in explicit_years:
            deferred_years.remove(explicit)
        if len(month_day_fragments) == len(explicit_years) + len(deferred_years):
            deferred = iter(deferred_years)
            split = [
                _nse_date(match.group(2), match.group(1), (match.group(3) or next(deferred)).replace("Z", "2"))
                for match in month_day_fragments
            ]
            if all(value is not None for value in split):
                candidates.insert(0, split)
        dates = next((candidate for candidate in candidates if _financial_columns(groups, [value.isoformat() for value in candidate], diagnostics=diagnostics)), [])
        if not dates:
            return []
        floor = evidence_at.year - 3
        ceiling = evidence_at.year + 1
        if any(value.year < floor or value.year > ceiling for value in dates):
            return []
        return [value.isoformat() for value in dates]
    finally:
        if diagnostics:
            diagnostics.header_dates_calls += 1
            diagnostics.header_dates_elapsed_ms += (time.perf_counter() - started_at) * 1000


def _financial_columns(
    groups: list[str],
    dates: list[str],
    *,
    diagnostics: _FinancialParserDiagnostics | None = None,
) -> list[FinancialColumn]:
    """Associate date cells to explicit header groups for common NSE layouts."""
    started_at = time.perf_counter() if diagnostics else None
    try:
        groups = _deduplicate_header_groups(groups)
        normalized = ["QUARTERLY" if group == "quarter" else "NINE_MONTH" if group.startswith("nine")
                      else "HALF_YEAR" if group.startswith("half") else "ANNUAL" for group in groups]
        count = len(dates)
        if normalized == ["QUARTERLY", "ANNUAL"] and count == 3:
            widths = [2, 1]
        elif normalized == ["QUARTERLY", "ANNUAL"] and count in {4, 5}:
            widths = [3, count - 3]
        elif normalized == ["QUARTERLY", "NINE_MONTH", "ANNUAL"] and count == 6:
            widths = [3, 2, 1]
        elif normalized == ["QUARTERLY", "NINE_MONTH"] and count == 5:
            widths = [3, 2]
        elif normalized == ["ANNUAL", "QUARTERLY"] and count in {4, 5}:
            widths = [count - 3, 3]
        elif normalized == ["QUARTERLY"] and count in {2, 3}:
            widths = [count]
        elif normalized == ["ANNUAL"] and count in {1, 2, 3, 4}:
            widths = [count]
        else:
            return []
        result, index = [], 0
        for group, width in zip(normalized, widths):
            for _ in range(width):
                result.append(FinancialColumn(index, dates[index], group, group))
                index += 1
        return result
    finally:
        if diagnostics:
            diagnostics.financial_columns_calls += 1
            diagnostics.financial_columns_elapsed_ms += (time.perf_counter() - started_at) * 1000


def _statement_metric(statement: FinancialStatement | None, labels, factory):
    if statement is None:
        return None
    for label in labels:
        if label.startswith("semantic:"):
            value = _classified_row_value(statement, label.split(":", 1)[1])
            if value is not None:
                return factory(value, _reported_unit(statement.unit_context[:400], statement.unit_context[:800]))
            continue
        pattern = label[3:] if label.startswith("re:") else rf"\b{re.escape(label)}\b"
        value = _aligned_row_value(statement, pattern)
        if value is not None:
            return factory(value, _reported_unit(statement.unit_context[:400], statement.unit_context[:800]))
    return None


def _row_semantic_tokens(label: str) -> frozenset[str]:
    """Classify OCR-damaged accounting labels without touching numeric cells."""
    words = [re.sub(r"[^a-z]", "", word.lower()) for word in re.findall(r"[A-Za-z]+", label)]
    concepts = set()
    for word in words:
        if not word:
            continue
        if word.startswith("profit") or _edit_distance(word[:6], "profit") <= 2:
            concepts.add("PROFIT")
        if word.startswith("period") or _edit_distance(word[:6], "period") <= 2:
            concepts.add("PERIOD")
        if word.startswith("continu") or _edit_distance(word[:10], "continuing") <= 3:
            concepts.add("CONTINUING_OPERATIONS")
        if word.startswith("discontinu"):
            concepts.add("DISCONTINUED_OPERATIONS")
        if word.startswith("before") or _edit_distance(word[:6], "before") <= 2:
            concepts.add("BEFORE_TAX")
        if word.startswith("after") or _edit_distance(word[:5], "after") <= 2:
            concepts.add("AFTER_TAX")
        if word.startswith("tax"):
            concepts.add("TAX")
        if word.startswith("comprehens"):
            concepts.add("COMPREHENSIVE_INCOME")
        if word.startswith("revenue"):
            concepts.add("REVENUE")
    return frozenset(concepts)


def _classify_financial_row(label: str) -> str | None:
    tokens = _row_semantic_tokens(label)
    if "COMPREHENSIVE_INCOME" in tokens or ("TAX" in tokens and "PROFIT" not in tokens):
        return None
    if "PROFIT" not in tokens:
        return None
    if "BEFORE_TAX" in tokens:
        return "PBT"
    if "AFTER_TAX" in tokens or "PERIOD" in tokens:
        return "PAT"
    return None


def _classified_row_value(statement: FinancialStatement, classification: str) -> Decimal | None:
    cells = _classified_row_cells(statement, classification)
    return cells[statement.current_column.index] if cells is not None else None


def _classified_row_cells(statement: FinancialStatement, classification: str) -> tuple[Decimal | None, ...] | None:
    text = _structural_row_text(statement.region)
    for match in re.finditer(r"\b(?:profit|proflt|ptoflt)\w*", text, re.I):
        tail = text[match.end():match.end() + 260]
        # Flattened NSE tables may retain a row/note reference immediately
        # after the semantic label, for example ``Net Profit ... (5)``.  It
        # is not the first financial cell.  This is deliberately local to
        # the semantic PAT/PBT extractor: an integer parenthesized reference
        # is only discarded before locating aligned table values.
        tail = _ROW_REFERENCE_FORMULA_RE.sub("", tail)
        tail = re.sub(r"\(\s*\d+(?:\s*[+\-*/•]\s*\d+)+\s*\)", "", tail)
        tail = re.sub(r"^\s*\(\s*\d{1,2}\s*\)\s*", "", tail)
        tokens = list(re.finditer(r"\S+", tail))
        def is_row_reference(index: int, token: re.Match[str]) -> bool:
            # A standalone integer in parentheses is a common NSE row/note
            # marker.  Treat it as such only when a complete aligned row
            # follows, so an incomplete value row still fails closed.
            if not re.fullmatch(r"\(\s*\d{1,2}\s*\)", token.group()):
                return False
            following = tokens[index + 1:index + 1 + len(statement.columns)]
            return len(following) == len(statement.columns) and all(
                _financial_number(value.group()) is not None for value in following
            )
        first = next((
            index for index, token in enumerate(tokens)
            if re.search(r"\d", token.group())
            and not _ROW_REFERENCE_FORMULA_RE.fullmatch(token.group())
            and not is_row_reference(index, token)
        ), None)
        if first is None:
            continue
        label = text[match.start():match.end() + tokens[first].start()]
        if _classify_financial_row(label) != classification:
            continue
        raw_cells = _coalesce_split_financial_cells(
            [token.group() for token in tokens[first:first + len(statement.columns) + 2]]
        )[:len(statement.columns)]
        if len(raw_cells) != len(statement.columns):
            continue
        cells = tuple(_financial_number(cell) for cell in raw_cells)
        if any(value is not None for value in cells):
            return cells
    return None


def _quarter_nine_month_reporting_semantics(region: str) -> bool:
    """Recognize the filing-level period semantics before table OCR damage."""
    return bool(re.search(
        r"\b(?:quarter|three\s+months?)\b.{0,80}\bnine\s+months?\b.{0,80}\bended\b",
        region[:700],
        re.I,
    ))


def _statement_revenue(statement: FinancialStatement | None, factory):
    cells = _statement_revenue_cells(statement)
    if statement is None or cells is None:
        return None
    value = cells[statement.current_column.index]
    return factory(value, _reported_unit(statement.unit_context[:400], statement.unit_context[:800])) if value is not None else None


def _statement_revenue_cells(statement: FinancialStatement | None) -> tuple[Decimal | None, ...] | None:
    if statement is None:
        return None
    for pattern in (
        r"\btotal\s+revenue\s+from\s+operations\b",
        r"(?<!total )\brevenue\s+from\s+operations\b",
        r"\btotal\s+income\b",
    ):
        if (cells := _aligned_row_cells(statement, pattern)) is not None:
            return cells
        if re.search(pattern, _structural_row_text(statement.region), re.I):
            return None
    return None


def _statement_eps(statement: FinancialStatement | None, factory):
    cells = _statement_eps_cells(statement)
    if statement is None or cells is None:
        return None
    value = cells[statement.current_column.index]
    return factory(value, "INR per share") if value is not None else None


def _statement_eps_cells(statement: FinancialStatement | None) -> tuple[Decimal | None, ...] | None:
    return _statement_eps_candidate(statement)[0]


def _statement_eps_candidate(
    statement: FinancialStatement | None,
) -> tuple[tuple[Decimal | None, ...] | None, tuple[bool, ...]]:
    """Return EPS cells and per-column direct-alignment provenance."""
    if statement is None or not re.search(
        r"\b(?:earn(?:ing|lng)s?\s+per(?:\s+(?:equity|eciuity))?\s+(?:share|sr\)?are)|(?:basic|diluted)\s+eps)\b",
        statement.region,
        re.I,
    ):
        return None, ()
    basic_cells = _aligned_row_cells(statement, r"\bbasic\s+(?:earnings?\s+per\s+share|eps)\b|\bbasic\b(?:\s*\(\s*rs\.?\s*\))?")
    diluted_cells = _aligned_row_cells(statement, r"\bdiluted\s+(?:earnings?\s+per\s+share|eps)\b|\bdiluted\b(?:\s*\(\s*rs\.?\s*\))?")
    # Use one complete aligned EPS row.  Combining Basic and Diluted cells
    # column-by-column could shift values when one OCR row is malformed.
    direct_source = max(
        (cells for cells in (basic_cells, diluted_cells) if cells is not None),
        key=lambda cells: sum(value is not None for value in cells),
        default=None,
    )
    minimum_aligned_cells = 1 if len(statement.columns) == 1 else 2
    if direct_source is not None and sum(value is not None for value in direct_source) >= minimum_aligned_cells:
        direct_cells = direct_source
        direct_flags = tuple(value is not None for value in direct_cells)
    else:
        direct_cells = tuple(None for _ in statement.columns)
        direct_flags = tuple(False for _ in statement.columns)
    row_region = _structural_row_text(statement.region)
    label = re.search(r"earn(?:ing|lng)s?\s+per\s+(?:equity|eciuity)\s+(?:sh(?:are|are)|sr\)?are)\b", row_region, re.I)
    fallback_cells: tuple[Decimal | None, ...] | None = None
    if label:
        tokens = list(re.finditer(r"\S+", row_region[max(0, label.start() - 120):label.start()]))
        fallback_values = [_financial_number(token.group()) for token in tokens]
        valid = [cell for cell in fallback_values if cell is not None]
        if len(valid) >= len(statement.columns):
            fallback_cells = tuple(valid[-len(statement.columns):])
    if fallback_cells is None:
        return (direct_cells if any(direct_flags) else basic_cells), direct_flags
    cells = tuple(
        direct_cells[index] if direct_flags[index] else fallback_cells[index]
        for index in range(len(statement.columns))
    )
    return cells, direct_flags


def _aligned_row_value(statement: FinancialStatement, label_pattern: str) -> Decimal | None:
    cells = _aligned_row_cells(statement, label_pattern)
    return cells[statement.current_column.index] if cells is not None else None


def _aligned_row_cells(statement: FinancialStatement, label_pattern: str) -> tuple[Decimal | None, ...] | None:
    row_region = _structural_row_text(statement.region)
    matches = re.finditer(label_pattern, row_region, re.I)
    match = next((candidate for candidate in matches
                  if not re.match(r"\s*\(?before\b", row_region[candidate.end():], re.I)), None)
    if not match:
        return None
    tail = row_region[match.end():match.end() + 360]
    # Strip leading row annotations, but retain parenthesized numeric cells:
    # ``(A+B+C) (5,507.81)`` has an annotation followed by a legitimate
    # negative first-column value.
    tail = re.sub(
        r"^\s*(?:from\s+continuing\s+operations\s*)?(?:\((?![^)]*\d)[^)]+\)\s*)+",
        "",
        tail,
        flags=re.I,
    )
    # Cash-flow rows commonly carry an alphabetic section marker such as
    # ``(A+B+C)`` between the row label and its aligned cells.
    tail = re.sub(r"^\s*(?:\([A-Za-z+]+\)\s*)+", "", tail)
    # Flattened statements retain row-reference formulae between a label and
    # its values, e.g. ``(1)+(2)`` or ``(10)-(11)``.  They are not financial
    # cells: require two parenthesized row references joined by arithmetic
    # operators, preserving legitimate parenthesized negative values.
    tail = _ROW_REFERENCE_FORMULA_RE.sub("", tail)
    tail = re.sub(r"^\s*eps\b", "", tail, flags=re.I)
    tokens = list(re.finditer(r"\S+", tail))
    def is_separator(token: str) -> bool:
        return token.strip().strip("()[]:;,." ) in {"I", "l", "|"}
    first_index = next((index for index, token in enumerate(tokens)
                        if not is_separator(token.group(0)) and re.search(r"[0-9ZSOIl|\\]", token.group(0))), None)
    if first_index is None:
        return None
    prefix = re.sub(r"\(\s*rs\.?\s*\)", "", tail[:tokens[first_index].start()], flags=re.I)
    prefix = re.sub(r"\b[I|l]\b", "", prefix)
    prefix = re.sub(r"\bfrom\s+continuing\s+operations\b\s*(?:\([^)]+\))?", "", prefix, flags=re.I)
    if re.search(r"[A-Za-z]", prefix):
        return None
    raw_cells = _coalesce_split_financial_cells(
        [token.group(0) for token in tokens[first_index:] if not is_separator(token.group(0))]
    )[:len(statement.columns)]
    if len(raw_cells) != len(statement.columns):
        return None
    cells = tuple(_financial_number(token) for token in raw_cells)
    # A small whole number immediately followed by decimal financial cells is
    # a row/note marker in flattened statements, not a current-period value.
    if cells[0] is not None and cells[0] == cells[0].to_integral_value() and cells[0] <= 99 and any(
            value is not None and value != value.to_integral_value() for value in cells[1:]):
        return None
    return cells


def _coalesce_split_financial_cells(tokens: list[str]) -> list[str]:
    """Rejoin a bounded OCR split such as ``551 .59`` into one table cell."""
    result: list[str] = []
    for token in tokens:
        if re.fullmatch(r"\.\d{1,2}", token) and result and re.fullmatch(r"[-+]?\d[\d,]*", result[-1]):
            result[-1] += token
        else:
            result.append(token)
    return result


def _structural_row_text(text: str) -> str:
    """Repair bounded OCR row labels only; numeric cells are never altered."""
    for corrupted, repaired in (
        ("contjp±±i?gop,engtions", "continuing operations"),
        ("contjp▒▒i?gop,engtions", "continuing operations"),
        ("Total Reveiiue I:ron operations", "Total Revenue From operations"),
        ("Total Revenue I:ron operations", "Total Revenue From operations"),
        ("Profltforthe perfod From contliiLilng operatlons", "Profit for the period From continuing operations"),
        ("PTofltforthe perfod From contliiLilng operatlons", "Profit for the period From continuing operations"),
        ("Net Profk for the D€riod", "Net Profit for the period"),
        ("Re`/enue FiwTi oneratlan=", "Revenue From operations"),
        ("Re`/enue FiwTi oneratlan", "Revenue From operations"),
    ):
        text = text.replace(corrupted, repaired)
    return re.sub(r"contjp.{4}i\?gop,engtions", "continuing operations", text)


def _financial_number(raw: str) -> Decimal | None:
    """Parse a whole OCR cell only when bounded substitutions yield one valid number."""
    token = raw.strip().strip("()[]:;")
    if not re.fullmatch(r"[-+0-9,\.ZSOIl|]+", token, re.I):
        return None
    normalized = token.translate(str.maketrans({"Z": "2", "z": "2", "S": "5", "s": "5", "O": "0", "o": "0", "I": "1", "l": "1", "|": "1"}))
    if re.fullmatch(r"\d{1,3}\.\d{3}\.\d{2}", normalized):
        normalized = normalized.replace(".", ",", 1)
    if not re.fullmatch(
        r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})*,\d{3}|\d+)(?:\.\d{1,2})?",
        normalized,
    ):
        return None
    return _decimal(normalized)


def _nse_table_period_end(text: str) -> str | None:
    """NSE financial statements place the current reporting date first."""
    heading = _NSE_QUARTER_HEADING.search(text)
    if not heading:
        return None
    header = text[heading.end():heading.end() + 260]
    fragment = _NSE_DAY_MONTH.search(header)
    year = re.search(r"\b([2Z]\d{3})\b", header)
    if fragment and year:
        parsed = _nse_date(fragment.group(1), fragment.group(2), year.group(1).replace("Z", "2"))
        if parsed:
            return parsed.isoformat()
    for match in _NSE_DATE.finditer(text, heading.end(), min(len(text), heading.end() + 220)):
        if parsed := _nse_date(*match.groups()):
            return parsed.isoformat()
    return None


def _nse_date(day: str, month_text: str, year: str) -> date | None:
    compact = re.sub(r"[^a-z]", "", month_text.lower())
    month = _MONTHS.get(compact)
    if month is None:
        matches = [number for name, number in _MONTHS.items() if _edit_distance(compact, name) <= 2]
        month = matches[0] if len(matches) == 1 else None
    try:
        return date(int(year.replace("Z", "2")), month or 0, int(day))
    except ValueError:
        return None


def _edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for row, char in enumerate(left, 1):
        current = [row]
        for column, other in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[column] + 1, previous[column - 1] + (char != other)))
        previous = current
    return previous[-1]


def _nse_table_metric(text, labels, factory):
    """A table row's first numeric cell is the current-period column."""
    for label in labels:
        match = re.search(rf"\b{re.escape(label)}\b[^\d+-]{{0,160}}{NUMBER}", text, re.I)
        if match and (value := _decimal(match.group(1))) is not None:
            return factory(value, _reported_unit(match.group(0), text[:match.end() + 120]))
    return None


def _nse_revenue_metric(text, factory):
    # Prefer the explicitly named operations row; total revenue is a fallback
    # only when the statement does not expose that canonical row.
    for pattern in (r"(?<!total )\brevenue\s+from\s+operations\b", r"\btotal\s+revenue\s+from\s+operations\b", r"\btotal\s+income\b"):
        match = re.search(rf"{pattern}[^\d+-]{{0,160}}{NUMBER}", text, re.I)
        if match and (value := _decimal(match.group(1))) is not None:
            return factory(value, _reported_unit(match.group(0), text[:match.end() + 120]))
    return None


def _nse_eps_metric(text, factory):
    """Read the Basic row, never the share face value in the EPS heading."""
    heading = re.search(r"\bearning(?:s)?\s+per\s+share\b", text, re.I)
    if not heading:
        return None
    basic = re.search(r"\bbasic\b\s*(?:\(\s*rs\.?\s*\))?[^\d+-]{0,48}" + NUMBER, text[heading.end():heading.end() + 360], re.I)
    if not basic or (value := _decimal(basic.group(1))) is None:
        return None
    return factory(value, "INR per share")


def _percent_metric(text, labels, qualifier, factory):
    for label in labels:
        suffix = rf"[^.\n]{{0,90}}?{re.escape(qualifier)}" if qualifier else r"[^.\n]{0,30}?"
        match = re.search(rf"\b{re.escape(label)}\b{suffix}[^\d+-]{{0,12}}{NUMBER}\s*%", text, re.I)
        if match:
            return factory(_decimal(match.group(1)), "PERCENT")
    return None


def _label_number(text, labels):
    for label in labels:
        match = re.search(rf"\b{re.escape(label)}\b[^\d+-]{{0,24}}{NUMBER}", text, re.I)
        if match:
            return _decimal(match.group(1))
    return None


def _percentage_label_number(text, labels):
    """Prefer the resulting level after a basis-point delta, not the delta itself."""
    for label in labels:
        match = re.search(
            rf"\b{re.escape(label)}\b[^\n.]{{0,100}}?[-+]?{NUMBER}\s*bps\b[^\n.]{{0,60}}?\bto\s+{NUMBER}\s*%",
            text,
            re.I,
        )
        if match:
            return _decimal(match.group(2))
    return _label_number(text, labels)


def _two_percentages(text, labels):
    for label in labels:
        match = re.search(rf"\b{re.escape(label)}\b[^\n]{{0,160}}?{NUMBER}\s*%[^\n]{{0,80}}?{NUMBER}\s*%", text, re.I)
        if match:
            return _decimal(match.group(1)), _decimal(match.group(2))
    return None


def _decimal(raw):
    try:
        return Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None


def _decimal_value(value):
    return Decimal(str(value.value)) if value is not None else None


def _provenance(document, value, *, unit=None, period=None, confidence=None, calculation_basis=None):
    return ProvenancedValue(value=value, unit=unit, as_of_date=document.published_at, period=period,
        source_url=document.canonical_url, source_name=document.source_name, source_type=str(document.source_type),
        published_at=document.published_at, retrieved_at=document.retrieved_at, confidence=confidence,
        calculation_basis=calculation_basis)


def _period_key(period: str) -> tuple[int, int]:
    if iso := re.fullmatch(r"(20\d{2})-(\d{2})-(\d{2})", period):
        return int(iso.group(1)), (int(iso.group(2)) - 1) // 3 + 1
    quarter = re.search(r"Q([1-4])\s*(?:FY)?\s*(\d{2,4})", period, re.I)
    if quarter:
        year = int(quarter.group(2))
        if year < 100:
            year += 2000
        return year, int(quarter.group(1))
    ended = re.search(r"ended\s+(?:(\d{1,2})\s+)?([A-Za-z]+)(?:\s+\d{1,2})?,?\s+(\d{4})", period, re.I)
    if ended:
        month = {name.lower(): index for index, name in enumerate(
            ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"), 1
        )}.get(ended.group(2).lower(), 0)
        return int(ended.group(3)), (month - 1) // 3 + 1 if month else 0
    return 0, 0


def _canonical_quarter_end(period: str) -> str | None:
    if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", period):
        return period
    match = re.fullmatch(r"Q([1-4])\s*FY\s*(\d{2,4})", period.strip(), re.I)
    if not match:
        return None
    financial_year = int(match.group(2))
    if financial_year < 100:
        financial_year += 2000
    month, day = {"1": (6, 30), "2": (9, 30), "3": (12, 31), "4": (3, 31)}[match.group(1)]
    return date(financial_year - 1 if match.group(1) != "4" else financial_year, month, day).isoformat()


def _reported_unit(value_text: str, context: str) -> str | None:
    """Preserve the reported scale; never silently compare lakh/crore/millions."""
    evidence = f"{value_text} {context}".lower()
    currency = "INR" if "₹" in evidence or "inr" in evidence or re.search(r"\b(?:rs\.?|rupees?)\b", evidence) else "EUR" if "€" in evidence or "eur" in evidence else "USD" if "$" in evidence or "usd" in evidence else None
    scale = next((name for name in ("crore", "lakh", "billion", "million", "thousand") if re.search(rf"\b{name}s?\b", evidence)), None)
    if currency and scale:
        return f"{currency} {scale}"
    return currency or scale


def _sentence_containing(text: str, labels: tuple[str, ...]) -> str | None:
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        if any(label in sentence.lower() for label in labels):
            return sentence.strip()[:500] or None
    return None


def _commentary_highlights(text: str) -> list[str]:
    highlights = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        lowered = sentence.lower()
        if any(term in lowered for term in ("management said", "management commentary", "outlook", "guidance")):
            cleaned = sentence.strip()
            if cleaned:
                highlights.append(cleaned[:500])
        if len(highlights) == 3:
            break
    return highlights
