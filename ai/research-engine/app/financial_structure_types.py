"""Immutable structural evidence; never a replacement for durable financial facts."""
from __future__ import annotations

from dataclasses import dataclass
from app.pdf_structure import SourceRegion

PARSER_VERSION = "DI20C-1"


@dataclass(frozen=True)
class FinancialColumn:
    index: int
    period_end: str
    period_type: str
    header_group: str
    duration_months: int | None = None
    audit_status: str | None = None
    group_evidence: tuple[SourceRegion, ...] = ()
    source_region: SourceRegion | None = None
    resolution_state: str = "LEGACY"


@dataclass(frozen=True)
class HeaderTokenEvidence:
    kind: str
    original: str
    value: str | None
    region: SourceRegion


@dataclass(frozen=True)
class HeaderEvidence:
    tokens: tuple[HeaderTokenEvidence, ...]
    strategies: tuple[str, ...] = ()


@dataclass(frozen=True)
class StatementDiagnostic:
    stage: str
    reason: str
    source_region: SourceRegion
    details: tuple[tuple[str, str], ...] = ()
    severity: str = "INFO"


@dataclass(frozen=True)
class HeaderResolution:
    columns: tuple[FinancialColumn, ...]
    evidence: HeaderEvidence
    diagnostics: tuple[StatementDiagnostic, ...]


@dataclass(frozen=True)
class FinancialStatementCandidate:
    statement_type: str
    reporting_basis: str | None
    source_region: SourceRegion
    header_region: SourceRegion | None
    body_region: SourceRegion | None
    unit_evidence: tuple[HeaderTokenEvidence, ...]
    header: HeaderResolution
    validation_state: str
    structural_fingerprint: str


@dataclass(frozen=True)
class FinancialParseResult:
    accepted_statements: tuple[FinancialStatementCandidate, ...]
    rejected_statements: tuple[FinancialStatementCandidate, ...]
    diagnostics: tuple[StatementDiagnostic, ...]
    structural_fingerprint: str
    state: str
    parser_version: str = PARSER_VERSION
    completeness: str = "STRUCTURE_ONLY_NO_FACT_COMPLETENESS"
