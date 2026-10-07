"""Manual evidence upload: draft/extract → review → explicit accept.

This is a reusable Research Readiness capability for ALL evidence-backed
requirement types.  SHAREHOLDING is the first complete vertical slice.

Architecture invariants (non-negotiable):
  * Extraction creates a DRAFT/proposal only.  Extracted structured facts
    MUST NOT directly modify normalized investment data.
  * A user Review + explicit Accept is mandatory.  Only Accept may persist
    validated normalized facts.
  * Original evidence, content hash, provenance, extraction method,
    instrument, reporting period, validation results and user corrections
    are all preserved.
  * Existing trusted official evidence must never be silently overwritten.
    Conflicts require explicit reconciliation.
  * Acceptance is atomic.
  * After acceptance the normal Research Readiness path is invoked.
    Readiness is NEVER hard-coded.

Public surface:
  * ManualEvidenceIngestor  -- upload a file, get a DRAFT back (no mutation)
  * ManualEvidenceAcceptor  -- validate + accept a reviewed draft (atomic write
    through the existing repository/persistence contracts, then readiness
    recalculation)
"""
from __future__ import annotations

import hashlib
import io
import logging
import mimetypes
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from app.fact_precedence import FactSourceTier, FinancialFact, FinancialFactKey
from app.models import (
    EventImpact,
    ProvenancedValue,
    ReliabilityLevel,
    ResearchEvent,
    ResearchEventType,
    ShareholdingCategory,
    ShareholdingSnapshot,
    ShareholdingSnapshotValue,
    SourceClassification,
    SourceMode,
    SourceType,
    TimeHorizon,
)

# _ORDER_EVENTS / _GOVERNANCE_EVENTS are the SAME frozensets
# app.research_readiness_runtime._append_events() already uses to route a
# ResearchEvent into ORDER_BOOK_CAPEX_GUIDANCE / GOVERNANCE_HISTORY evidence
# -- imported directly (not duplicated) so the manual-upload event-type
# picker can never drift from what readiness itself actually consumes.
# There is no import cycle: research_readiness_runtime does not import
# app.manual_evidence.
from app.research_readiness_runtime import _GOVERNANCE_EVENTS, _ORDER_EVENTS

logger = logging.getLogger(__name__)


def _json_safe(obj: Any) -> Any:
    """JSON serializer for objects not natively serializable by json.dumps."""
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Evidence types supported by the manual upload pipeline.  Each type maps to
# a set of requirement IDs it can satisfy.  SHAREHOLDING is the first vertical
# slice; the architecture is extensible to other evidence-backed requirements.
class EvidenceType(StrEnum):
    SHAREHOLDING = "SHAREHOLDING"
    CURRENT_NEWS = "CURRENT_NEWS"
    # ResearchEvent-backed, same architecture as CURRENT_NEWS (see
    # RESEARCH_EVENT_EVIDENCE_TYPES below): a manually accepted event is
    # persisted through the IDENTICAL repository.events.add() /
    # persistence.upsert_event() path the automated news/order/governance
    # pipelines write through, then read back and classified into
    # ORDER_BOOK_CAPEX_GUIDANCE / GOVERNANCE_HISTORY evidence by the SAME
    # app.research_readiness_runtime._append_events() routing automated
    # events already go through (_ORDER_EVENTS / _GOVERNANCE_EVENTS). No
    # parallel manual-event database.
    ORDER_BOOK_CAPEX_GUIDANCE = "ORDER_BOOK_CAPEX_GUIDANCE"
    GOVERNANCE_HISTORY = "GOVERNANCE_HISTORY"
    # FinancialFact-backed requirements (see FINANCIAL_FACT_EVIDENCE_TYPES
    # below). VALUATION_INPUTS is deliberately NOT a member here: its
    # mandatory inputs LATEST_USABLE_PRICE and PB are derived exclusively
    # from StructuredMarketSnapshotRecord (see
    # _structured_fact_coverage in research_readiness_runtime.py), not from
    # FinancialFact, and manual upload of "latest price" was already
    # excluded on market-integrity grounds in the prior manual-evidence
    # pass. A manual EARNINGS_BASIS fact can be built with the same
    # mechanism below, but doing so can only ever move VALUATION_INPUTS from
    # MISSING towards PARTIAL -- never to READY_FRESH -- so it is not
    # exposed as a distinct evidence type; see the final report.
    BUSINESS_QUALITY_FACTS = "BUSINESS_QUALITY_FACTS"
    GROWTH_FACTS = "GROWTH_FACTS"
    BALANCE_SHEET_FACTS = "BALANCE_SHEET_FACTS"
    QUARTERLY_FINANCIALS = "QUARTERLY_FINANCIALS"
    # Universal non-READY_FRESH manual evidence rule closure: every
    # requirement must have a legitimate manual evidence contract, not just
    # a frontend button. These four were previously excluded on
    # market-integrity grounds (see the comment above about
    # LATEST_USABLE_PRICE/PB); they are now supported with the SAME
    # lowest-authority USER_UPLOAD tier/precedence boundary every other
    # manual evidence type already uses, so they can only ever fill a
    # genuine gap, never outrank or overwrite trusted automated evidence,
    # and never bypass deterministic valuation/scoring calculations (see
    # PRICE_EVIDENCE_TYPES and ALLOWED_METRICS_BY_EVIDENCE_TYPE below).
    LATEST_PRICE = "LATEST_PRICE"
    HISTORICAL_PRICE_SERIES = "HISTORICAL_PRICE_SERIES"
    VALUATION_INPUTS = "VALUATION_INPUTS"
    SECTOR_MACRO = "SECTOR_MACRO"


SUPPORTED_EVIDENCE_TYPES: tuple[EvidenceType, ...] = (
    EvidenceType.SHAREHOLDING,
    EvidenceType.CURRENT_NEWS,
    EvidenceType.ORDER_BOOK_CAPEX_GUIDANCE,
    EvidenceType.GOVERNANCE_HISTORY,
    EvidenceType.BUSINESS_QUALITY_FACTS,
    EvidenceType.GROWTH_FACTS,
    EvidenceType.BALANCE_SHEET_FACTS,
    EvidenceType.QUARTERLY_FINANCIALS,
    EvidenceType.LATEST_PRICE,
    EvidenceType.HISTORICAL_PRICE_SERIES,
    EvidenceType.VALUATION_INPUTS,
    EvidenceType.SECTOR_MACRO,
)

# Evidence types backed by the canonical ResearchEvent / research_events
# model (app.models.ResearchEvent), persisted through
# repository.events.add() + persistence.upsert_event() -- the exact same
# path the automated news/order/governance acquisition pipelines write
# through. CURRENT_NEWS is the pre-existing member; ORDER_BOOK_CAPEX_GUIDANCE
# and GOVERNANCE_HISTORY reuse the identical persistence/readiness
# architecture (see _append_events() in research_readiness_runtime.py,
# which already routes ANY ResearchEvent whose event_type is in
# _ORDER_EVENTS / _GOVERNANCE_EVENTS into those two requirements' evidence
# -- that routing is automated-vs-manual agnostic).
RESEARCH_EVENT_EVIDENCE_TYPES: frozenset[EvidenceType] = frozenset({
    EvidenceType.CURRENT_NEWS,
    EvidenceType.ORDER_BOOK_CAPEX_GUIDANCE,
    EvidenceType.GOVERNANCE_HISTORY,
})

# Canonical ResearchEventType values a manual ORDER_BOOK_CAPEX_GUIDANCE /
# GOVERNANCE_HISTORY upload is permitted to submit -- derived EXACTLY from
# the _ORDER_EVENTS / _GOVERNANCE_EVENTS frozensets
# app.research_readiness_runtime._append_events() itself uses to decide
# which requirement(s) a ResearchEvent counts toward (imported above, not
# duplicated). A manual upload can never submit an event_type readiness
# would not actually recognize as belonging to that requirement.
ALLOWED_EVENT_TYPES_BY_EVIDENCE_TYPE: dict[EvidenceType, frozenset[ResearchEventType]] = {
    EvidenceType.ORDER_BOOK_CAPEX_GUIDANCE: frozenset(_ORDER_EVENTS),
    EvidenceType.GOVERNANCE_HISTORY: frozenset(_GOVERNANCE_EVENTS),
}

# Evidence types backed by the canonical FinancialFact / global_financial_facts
# model (app.fact_precedence), as opposed to ShareholdingSnapshot or
# ResearchEvent. Each accepted fact row becomes a real FinancialFact tagged
# FactSourceTier.USER_UPLOAD -- the lowest-authority tier -- and is merged
# into global_financial_facts via the SAME merge_fact()/upsert_financial_fact
# precedence boundary every automated provider uses, so it can only ever
# fill a genuine gap, never outrank or overwrite automated evidence.
FINANCIAL_FACT_EVIDENCE_TYPES: frozenset[EvidenceType] = frozenset({
    EvidenceType.BUSINESS_QUALITY_FACTS,
    EvidenceType.GROWTH_FACTS,
    EvidenceType.BALANCE_SHEET_FACTS,
    EvidenceType.QUARTERLY_FINANCIALS,
    # VALUATION_INPUTS is included here ONLY for its one FinancialFact-backed
    # input (EARNINGS_BASIS, satisfied by eps/pat/net_income/net_profit --
    # see ALLOWED_METRICS_BY_EVIDENCE_TYPE and
    # app.research_readiness_runtime._financial_fact_coverage). Its other
    # mandatory inputs (LATEST_USABLE_PRICE, PE, PB, EV_EBITDA, FCF_YIELD)
    # are derived ratios computed exclusively from trusted structured market
    # data (app.valuation_evidence.materialize_valuation) and are NOT
    # satisfiable by manual upload -- a manual upload can therefore move
    # VALUATION_INPUTS from MISSING towards PARTIAL, never fabricate a
    # derived ratio or bypass the deterministic valuation calculation.
    EvidenceType.VALUATION_INPUTS,
})

# LATEST_PRICE / HISTORICAL_PRICE_SERIES: structured, NOT a free-text/repeating
# "facts" row upload like FINANCIAL_FACT_EVIDENCE_TYPES above. Each is backed
# by its own dedicated validate/build method (see _validate_latest_price_fields
# / _build_latest_price_fact and _validate_historical_price_series_fields /
# _build_historical_price_series_facts) because price data has a materially
# different shape (price+currency+asOf+source; a date-ordered observation
# series) from a generic metric/periodEnd/periodType fact row. Both still
# persist as real FinancialFact rows tagged FactSourceTier.USER_UPLOAD through
# the SAME merge_fact()/upsert_financial_fact precedence boundary every other
# manual evidence type uses -- see PRICE_EVIDENCE_METRICS below for the
# synthetic metric names app.research_readiness_runtime._financial_fact_coverage
# recognizes, and the module docstring note on why these two types were
# reintroduced after being previously excluded on market-integrity grounds.
PRICE_EVIDENCE_TYPES: frozenset[EvidenceType] = frozenset({
    EvidenceType.LATEST_PRICE,
    EvidenceType.HISTORICAL_PRICE_SERIES,
})

# Synthetic FinancialFact metric names used ONLY by the two evidence types
# above -- never exposed to the user, never overlapping with any real
# automated metric name (see ALLOWED_METRICS_BY_EVIDENCE_TYPE for the
# automated-aligned metric vocabulary FINANCIAL_FACT_EVIDENCE_TYPES uses).
# app.research_readiness_runtime._financial_fact_coverage recognizes these
# exact strings to route coverage to LATEST_PRICE/HISTORICAL_PRICE_SERIES/
# VALUATION_INPUTS(LATEST_USABLE_PRICE), mirroring how the trusted
# structured-market path already covers the same inputs (see
# _structured_fact_coverage's "latestPrice" handling) -- NOT duplicating
# that trusted path, since these facts live in global_financial_facts, not
# global_market_price_observations/global_daily_market_bars, and are never
# read by stock_rule_engine/ranking (see FactSourceTier.USER_UPLOAD's own
# docstring on SUPPORTED_FINANCIAL_SOURCE_TIERS exclusion).
MANUAL_LATEST_PRICE_METRIC = "manual_latest_price"
MANUAL_HISTORICAL_CLOSE_METRIC = "manual_historical_close"

# SECTOR_MACRO is document-backed (app.research_readiness_runtime
# _append_documents' qualitative macro-term scan over document text), NOT
# ResearchEvent-backed like ORDER_BOOK_CAPEX_GUIDANCE/GOVERNANCE_HISTORY --
# see _build_sector_macro_document / _ensure_sector_macro_document. A manual
# SECTOR_MACRO upload is NEVER routed through the automated _MACRO_TERMS
# keyword heuristic (that heuristic exists only to classify automated/
# unlabeled document text; an explicit manual SECTOR_MACRO submission
# already carries unambiguous human intent and would be fragile/surprising
# if it silently failed to register just because the user's wording did not
# happen to match one of 8 hardcoded terms) -- see
# _append_manual_sector_macro_evidence, a small dedicated function that
# unconditionally recognizes it via a distinct canonical_url marker. This
# NEVER mutates CANONICAL_SECTOR (canonical instrument classification);
# it only ever contributes RELEVANT_MACRO_EVENT_EXPOSURE, the same
# qualitative input an automated macro-relevant document contributes.
SECTOR_MACRO_DOCUMENT_URL_PREFIX = "manual-evidence:sector-macro:"

# Canonical metric names a manual upload is permitted to submit for each
# FinancialFact-backed evidence type, derived EXACTLY from
# app.research_readiness_runtime._financial_fact_coverage (the single
# authoritative metric -> requirement-input mapping readiness itself uses).
# Never extended with a metric that function does not already recognize --
# that would let a manual upload manufacture an input readiness cannot
# actually validate.
ALLOWED_METRICS_BY_EVIDENCE_TYPE: dict[EvidenceType, frozenset[str]] = {
    EvidenceType.BUSINESS_QUALITY_FACTS: frozenset({
        "pat", "net_income", "net_profit",
        "roe", "return_on_equity",
        "roce", "return_on_capital_employed",
        "operating_margin", "profit_margin", "ebitda_margin", "gross_margin",
        "free_cash_flow", "operating_cash_flow", "cash_flow_from_operating_activities",
    }),
    EvidenceType.GROWTH_FACTS: frozenset({
        "revenue", "total_revenue",
        "eps", "pat", "net_income", "net_profit",
    }),
    EvidenceType.BALANCE_SHEET_FACTS: frozenset({
        "total_debt", "debt", "debt_or_borrowings", "borrowings",
        "total_equity", "equity", "net_worth",
        "cash", "total_cash", "cash_and_cash_equivalents", "cash_and_equivalents",
        "interest_expense", "finance_cost", "finance_costs", "ebit", "operating_profit",
        "current_assets", "current_liabilities", "current_ratio",
    }),
    EvidenceType.QUARTERLY_FINANCIALS: frozenset({
        "revenue", "total_revenue",
        "pat", "net_income", "net_profit",
        "eps",
        "ebitda", "operating_profit", "operating_income",
        "operating_margin", "profit_margin", "ebitda_margin",
    }),
    # Only the metrics app.research_readiness_runtime._financial_fact_coverage
    # actually maps to VALUATION_INPUTS (-> EARNINGS_BASIS). PE/PB/EV_EBITDA/
    # FCF_YIELD/LATEST_USABLE_PRICE are deliberately absent -- see
    # FINANCIAL_FACT_EVIDENCE_TYPES' comment on VALUATION_INPUTS above.
    EvidenceType.VALUATION_INPUTS: frozenset({
        "eps", "pat", "net_income", "net_profit",
    }),
}

# CURRENT_NEWS cannot be safely auto-extracted from arbitrary prose: a
# regex/NLP guess at headline/date/impact risks silently fabricating exactly
# the canonical fields (ResearchEvent.event_date, .impact, .time_horizon,
# .event_type) that drive readiness freshness. Instead the uploaded file is
# kept as supporting evidence and the user supplies these fields explicitly
# at Accept time; deterministic validation (below) rejects a missing or
# unparseable value rather than defaulting it. The frontend uses this schema
# to render the required-fields form for a given evidence type.
CURRENT_NEWS_MANUAL_FIELDS: tuple[str, ...] = (
    "title", "summary", "eventDate", "sourceUrl", "eventType", "impact", "timeHorizon",
)
# ORDER_BOOK_CAPEX_GUIDANCE / GOVERNANCE_HISTORY take the SAME flat-scalar
# canonical ResearchEvent fields as CURRENT_NEWS (narrative guidance stays
# traceable to source -- no separate numeric amount/value/currency field is
# exposed here: ResearchEvent.monetary_value/currency and
# capacity_value/capacity_unit exist on the model but readiness itself
# never reads them for these two requirements -- see _append_events(), which
# only inspects event_type/event_date/impact/status -- so exposing them as
# "mandatory structured facts" would manufacture authoritative numeric
# guidance from prose the task explicitly forbids). The one difference from
# CURRENT_NEWS is eventType: validated against
# ALLOWED_EVENT_TYPES_BY_EVIDENCE_TYPE instead of the full ResearchEventType
# set, and the frontend renders it as a restricted dropdown, not free text.
ORDER_BOOK_CAPEX_GUIDANCE_MANUAL_FIELDS: tuple[str, ...] = CURRENT_NEWS_MANUAL_FIELDS
GOVERNANCE_HISTORY_MANUAL_FIELDS: tuple[str, ...] = CURRENT_NEWS_MANUAL_FIELDS
# FinancialFact-backed types take a single repeating-row field ("facts": a
# list of {metric, value, periodEnd, periodType, reportingBasis?, unit,
# sourceUrl} objects) rather than flat scalar fields like CURRENT_NEWS --
# GROWTH_FACTS and QUARTERLY_FINANCIALS both require multiple periods to
# satisfy their mandatory inputs (e.g. COMPARABLE_QUARTERS), which a single
# flat form cannot express. This schema shape is intentionally NOT wired
# into the existing generic scalar-field upload form (see Section 8 of the
# task: "do not expose a generic free-text form that can manufacture
# financial facts") -- a dedicated structured multi-row review table is a
# separate frontend undertaking, reported as a remaining gap.
FINANCIAL_FACT_MANUAL_FIELDS: tuple[str, ...] = ("facts",)

# LATEST_PRICE: a single structured observation. asOf is the real market
# as-of date/time the price was observed -- never defaulted to upload time,
# same discipline as periodEnd in the FinancialFact rows above.
LATEST_PRICE_MANUAL_FIELDS: tuple[str, ...] = ("price", "currency", "asOf", "source")
# HISTORICAL_PRICE_SERIES: "observations" is a structured, line-oriented
# "YYYY-MM-DD,close" list (one observation per line) rather than free prose
# -- see _validate_historical_price_series_fields for the deterministic
# parse/validate rules (dates, numeric range, duplicates, future dates).
# currency/source apply to the whole submitted batch.
HISTORICAL_PRICE_SERIES_MANUAL_FIELDS: tuple[str, ...] = ("observations", "currency", "source")
# SECTOR_MACRO: same discipline as CURRENT_NEWS -- no auto-extraction of a
# sector/macro-exposure judgment from arbitrary prose; the user explicitly
# supplies a dated, sourced summary. See SECTOR_MACRO_DOCUMENT_URL_PREFIX.
SECTOR_MACRO_MANUAL_FIELDS: tuple[str, ...] = ("summary", "asOf", "source")

MANUAL_FIELD_SCHEMA: dict[EvidenceType, tuple[str, ...]] = {
    EvidenceType.CURRENT_NEWS: CURRENT_NEWS_MANUAL_FIELDS,
    EvidenceType.ORDER_BOOK_CAPEX_GUIDANCE: ORDER_BOOK_CAPEX_GUIDANCE_MANUAL_FIELDS,
    EvidenceType.GOVERNANCE_HISTORY: GOVERNANCE_HISTORY_MANUAL_FIELDS,
    EvidenceType.BUSINESS_QUALITY_FACTS: FINANCIAL_FACT_MANUAL_FIELDS,
    EvidenceType.GROWTH_FACTS: FINANCIAL_FACT_MANUAL_FIELDS,
    EvidenceType.BALANCE_SHEET_FACTS: FINANCIAL_FACT_MANUAL_FIELDS,
    EvidenceType.QUARTERLY_FINANCIALS: FINANCIAL_FACT_MANUAL_FIELDS,
    EvidenceType.VALUATION_INPUTS: FINANCIAL_FACT_MANUAL_FIELDS,
    EvidenceType.LATEST_PRICE: LATEST_PRICE_MANUAL_FIELDS,
    EvidenceType.HISTORICAL_PRICE_SERIES: HISTORICAL_PRICE_SERIES_MANUAL_FIELDS,
    EvidenceType.SECTOR_MACRO: SECTOR_MACRO_MANUAL_FIELDS,
}


def _normalize_metric(value: str) -> str:
    """Match app.research_readiness_runtime._metric's normalization exactly
    so an allowed-metric lookup here never diverges from how readiness
    itself will later canonicalize the same string."""
    return str(value).strip().casefold().replace("-", "_").replace(" ", "_")

# File types with a complete extraction path. Keep this capability contract
# aligned with what an upload can actually turn into a reviewable draft.
#
# PNG/JPEG (Defect 2 closure): extraction goes through
# app.image_evidence_extraction's bounded, subprocess-isolated OCR path
# (_extract_image_text below), never an unbounded in-process OCR/model
# call. A scanned PDF (no extractable text layer) also falls back to that
# same bounded path -- see _extract_pdf_text.
SUPPORTED_FILE_TYPES: dict[str, str] = {
    "application/pdf": "PDF",
    "text/csv": "CSV",
    "text/plain": "TXT",
    "image/png": "PNG",
    "image/jpeg": "JPG",
    # DOCX (paragraphs + tables via python-docx, no LibreOffice/Word
    # runtime) -- gated the same way PNG/JPG are: see
    # _ocr_dependent_labels_available / runtime_supported_file_type_labels
    # below, which only advertise "DOCX" when python-docx is actually
    # importable (app.document_extraction.docx_extraction_available()).
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "DOCX",
}

# Extensions without standard MIME types.
_EXTENSION_MAPPING: dict[str, str] = {
    ".pdf": "application/pdf",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def detect_mime_type(filename: str | None, content_type: str | None) -> str | None:
    """Detect the MIME type from filename extension or provided content type."""
    if filename:
        ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext in _EXTENSION_MAPPING:
            return _EXTENSION_MAPPING[ext]
    if content_type and content_type in SUPPORTED_FILE_TYPES:
        return content_type
    if filename:
        guessed, _ = mimetypes.guess_type(filename)
        if guessed and guessed in SUPPORTED_FILE_TYPES:
            return guessed
    return None


def is_supported_file_type(filename: str | None, content_type: str | None) -> bool:
    """Return True if the file type is supported for ingestion."""
    mime = detect_mime_type(filename, content_type)
    return mime is not None and mime in SUPPORTED_FILE_TYPES


SUPPORTED_FILE_TYPE_LABELS = sorted(set(SUPPORTED_FILE_TYPES.values()))

# File types whose complete extraction path additionally depends on an OS
# binary (OCR) that may not be installed in a given runtime image, mapped to
# the lightweight availability check that must pass before that label is
# truthfully advertised as supported. PDF/CSV/TXT have no such runtime
# dependency (native pypdf/CSV/text parsing only) and are therefore never
# gated here -- only the OCR-dependent PNG/JPEG path is.
def _ocr_dependent_labels_available() -> dict[str, bool]:
    from app.document_extraction import docx_extraction_available
    from app.image_evidence_extraction import image_ocr_available
    available = image_ocr_available()
    return {"PNG": available, "JPG": available, "DOCX": docx_extraction_available()}


def runtime_supported_file_type_labels() -> list[str]:
    """Fail-closed capability advertisement (Defect: runtime OCR blocker).

    Returns the file-type labels that are ACTUALLY usable right now in this
    process, not merely the ones this codebase has an extraction path for.
    PNG/JPEG are only included when the tesseract binary they depend on is
    actually present (a cheap ``shutil.which`` lookup -- no OCR is ever run
    here). This prevents a deployment that is missing the tesseract-ocr /
    poppler-utils OS packages from advertising an Upload Evidence feature
    that would fail on every image/scanned-PDF attempt.
    """
    gated = _ocr_dependent_labels_available()
    return sorted(
        label for label in set(SUPPORTED_FILE_TYPES.values())
        if gated.get(label, True)
    )


# ---------------------------------------------------------------------------
# Draft model
# ---------------------------------------------------------------------------


class DraftStatus(StrEnum):
    DRAFT = "DRAFT"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


@dataclass
class ProposedFact:
    """A single extracted/normalized fact proposed for acceptance.

    For SHAREHOLDING, this maps directly to a ShareholdingSnapshotValue.
    The ``value`` field holds the raw proposed value (e.g. a Decimal
    percentage); the ``field`` identifies which shareholding category or
    sub-field the fact addresses.
    """
    field: str
    value: Any
    source_locator: str | None = None
    evidence_text: str | None = None
    raw_source_label: str | None = None
    metric_basis: str | None = None
    confidence: float = 0.0
    validation_error: str | None = None


@dataclass
class ValidationResult:
    """Result of deterministic validation of a draft's proposed facts."""
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


@dataclass
class ManualEvidenceDraft:
    """A draft proposal created by ingestion/extraction.

    Drafts are stored in-process only until accepted.  They are NEVER
    written to the durable persistence layer as normalized evidence.
    """
    draft_id: UUID
    evidence_type: EvidenceType
    content_hash: str
    original_filename: str
    content_type: str
    instrument_id: UUID | None
    reporting_period: datetime | None
    proposed_facts: list[ProposedFact]
    validation_results: ValidationResult
    extraction_method: str
    created_at: datetime
    status: DraftStatus = DraftStatus.DRAFT
    original_bytes: bytes | None = None
    corrections: dict[str, Any] = field(default_factory=dict)
    # True for an evidence type whose canonical fields cannot be safely
    # auto-extracted (see CURRENT_NEWS_MANUAL_FIELDS above): the frontend
    # must collect MANUAL_FIELD_SCHEMA[evidence_type] from the user and pass
    # them as `corrections` to accept(). Always False for SHAREHOLDING.
    requires_manual_fields: bool = False
    manual_field_schema: tuple[str, ...] = field(default_factory=tuple)
    # Read-only reference text surfaced to the Review screen so the user
    # does not have to re-read the original file by eye while filling in
    # manual_field_schema (CURRENT_NEWS / ORDER_BOOK_CAPEX_GUIDANCE /
    # GOVERNANCE_HISTORY / FinancialFact families). Never auto-populates
    # any field and is never itself treated as a proposed fact -- it is
    # strictly informational provenance the user may copy from. None when
    # no text could be extracted (see validation_results warnings instead).
    extracted_text: str | None = None
    # Conservative, NON-BINDING field suggestions produced by an Evidence
    # Interpreter (see app.evidence_interpretation.interpret_current_news)
    # from extracted_text -- e.g. {"title": "...", "eventDate": "...",
    # "source": "..."}. Never auto-applied, never validated as a fact,
    # never required: the frontend may offer these as pre-fill for
    # MANUAL_FIELD_SCHEMA, but the user still explicitly supplies and
    # submits every field as `corrections` at accept() time, unchanged.
    # None for evidence types with no suggestion interpreter.
    field_suggestions: dict[str, str] | None = None


# ---------------------------------------------------------------------------
# In-process draft store (never persisted to durable DB)
# ---------------------------------------------------------------------------

_drafts: dict[UUID, ManualEvidenceDraft] = {}


# ---------------------------------------------------------------------------
# Ingestor: file-type dispatch, content hashing, extraction
# ---------------------------------------------------------------------------


class ManualEvidenceIngestor:
    """Ingests an uploaded file and produces a ManualEvidenceDraft (DRAFT only).

    Paste and Browse feed exactly the same pipeline: the raw bytes, filename,
    and content type are provided to ``ingest``; the method dispatches by
    file type, hashes the content, extracts text, and runs the
    evidence-type-specific extractor.
    No normalized data is mutated.
    """

    def __init__(self, repository=None):
        self.repository = repository

    # -- public API --------------------------------------------------------

    def ingest(
        self,
        *,
        evidence_type: str,
        file_bytes: bytes,
        filename: str,
        content_type: str | None = None,
        instrument_id: UUID | None = None,
    ) -> ManualEvidenceDraft:
        """Upload a file and produce a DRAFT extraction proposal.

        No normalized investment data is modified.
        """
        mime = detect_mime_type(filename, content_type)
        if mime is None or mime not in SUPPORTED_FILE_TYPES:
            raise ValueError(
                f"UNSUPPORTED_FILE_TYPE: mime={mime}, filename={filename}, "
                f"content_type={content_type}. Supported: {SUPPORTED_FILE_TYPE_LABELS}"
            )

        try:
            evidence_type_enum = EvidenceType(evidence_type)
        except ValueError:
            raise ValueError(
                f"UNSUPPORTED_EVIDENCE_TYPE: {evidence_type}. "
                f"Supported: {[e.value for e in SUPPORTED_EVIDENCE_TYPES]}"
            )

        digest = content_hash_hex(file_bytes)

        # Extract text from the file depending on its type.
        text = self._extract_text(file_bytes, mime, filename)

        # Check for duplicate content (dedup by content hash).
        duplicate_of = self._find_duplicate(digest, evidence_type_enum)

        extraction_method = f"{SUPPORTED_FILE_TYPES[mime]}_TEXT"
        if text is None:
            extraction_method += "_OCR_ATTEMPTED_NO_TEXT"
            text = ""

        reporting_period = None
        proposed_facts: list[ProposedFact] = []
        validation = ValidationResult(valid=True)
        requires_manual_fields = False
        field_suggestions: dict[str, str] | None = None

        if evidence_type_enum == EvidenceType.SHAREHOLDING:
            proposed_facts, reporting_period, validation, shareholding_supplementary = self._extract_shareholding(
                text, instrument_id, digest, filename, mime
            )
            # Non-canonical reference metadata the table interpreter
            # recognized alongside the canonical category values (e.g.
            # shareholder count, the original source-period text) but
            # with no field in the existing ShareholdingCategory domain
            # model -- surfaced read-only via the same advisory
            # field_suggestions mechanism CURRENT_NEWS uses, never
            # persisted as a canonical fact, never required at accept().
            field_suggestions = shareholding_supplementary
        elif evidence_type_enum == EvidenceType.CURRENT_NEWS:
            # No auto-extraction of CANONICAL facts: see
            # CURRENT_NEWS_MANUAL_FIELDS. The file is kept as supporting
            # evidence (original_bytes/content_hash below); proposed_facts
            # stays empty -- nothing is proposed as fact until the user
            # supplies the required fields explicitly at accept(). A
            # separate, conservative Evidence Interpreter MAY surface
            # non-binding field SUGGESTIONS (see field_suggestions below)
            # to reduce retyping -- this is advisory pre-fill, never a
            # proposed fact, and never changes the accept() contract.
            requires_manual_fields = True
            if not text.strip():
                validation = ValidationResult(
                    valid=True,
                    warnings=["NO_TEXT_EXTRACTED: the file could not be read as text; "
                              "it is still kept as supporting evidence, but none of its "
                              "content could be used to help you fill in the required fields"],
                )
            else:
                field_suggestions = self._suggest_current_news_fields(text)
        elif evidence_type_enum in (EvidenceType.ORDER_BOOK_CAPEX_GUIDANCE, EvidenceType.GOVERNANCE_HISTORY):
            # Same rationale as CURRENT_NEWS: converting arbitrary order-book
            # / capex / governance prose into a structured, authoritative
            # ResearchEvent (event_type, event_date, impact) is exactly the
            # fabrication the task prohibits. The uploaded file is kept only
            # as supporting context; the user supplies every canonical field
            # explicitly as corrections at accept() time, where it is
            # deterministically validated against the restricted canonical
            # event-type set for this requirement (see
            # ALLOWED_EVENT_TYPES_BY_EVIDENCE_TYPE) -- nothing is ever
            # defaulted or guessed from the text.
            requires_manual_fields = True
            if not text.strip():
                validation = ValidationResult(
                    valid=True,
                    warnings=["NO_TEXT_EXTRACTED: the file could not be read as text; "
                              "it is still kept as supporting evidence, but none of its "
                              "content could be used to help you fill in the required fields"],
                )
        elif evidence_type_enum in FINANCIAL_FACT_EVIDENCE_TYPES:
            # Same rationale as CURRENT_NEWS: a regex/NLP guess at numeric
            # financial facts from arbitrary prose risks silently fabricating
            # exactly the canonical value/period/metric identity that drives
            # readiness. The uploaded file (if any text could be read) is
            # kept only as supporting context; the user supplies each fact
            # row explicitly as corrections["facts"] at accept() time, where
            # it is deterministically validated -- nothing here is ever
            # defaulted or guessed.
            requires_manual_fields = True
            if not text.strip():
                validation = ValidationResult(
                    valid=True,
                    warnings=["NO_TEXT_EXTRACTED: the file could not be read as text; "
                              "it is still kept as supporting evidence, but none of its "
                              "content could be used to help you fill in the required fact rows"],
                )
        elif evidence_type_enum in PRICE_EVIDENCE_TYPES or evidence_type_enum == EvidenceType.SECTOR_MACRO:
            # Same rationale again: a price, OHLC observation, or
            # sector/macro exposure judgment is exactly the kind of
            # authoritative, freshness-driving value that must never be
            # guessed from arbitrary prose. The uploaded file (if any) is
            # kept only as supporting context; the user supplies the
            # structured fields explicitly as corrections at accept() time
            # (see MANUAL_FIELD_SCHEMA / _validate_latest_price_fields /
            # _validate_historical_price_series_fields /
            # _validate_sector_macro_fields).
            requires_manual_fields = True
            if not text.strip():
                validation = ValidationResult(
                    valid=True,
                    warnings=["NO_TEXT_EXTRACTED: the file could not be read as text; "
                              "it is still kept as supporting evidence, but none of its "
                              "content could be used to help you fill in the required fields"],
                )

        draft = ManualEvidenceDraft(
            draft_id=uuid4(),
            evidence_type=evidence_type_enum,
            content_hash=digest,
            original_filename=filename,
            content_type=mime,
            instrument_id=instrument_id,
            reporting_period=reporting_period,
            proposed_facts=proposed_facts,
            validation_results=validation,
            extraction_method=extraction_method,
            created_at=datetime.now(timezone.utc),
            original_bytes=file_bytes,
            corrections={},
            requires_manual_fields=requires_manual_fields,
            manual_field_schema=MANUAL_FIELD_SCHEMA.get(evidence_type_enum, ()),
            # Informational only (see ManualEvidenceDraft.extracted_text):
            # surfaced for manual-field evidence types where there are no
            # proposed_facts to review against, so the user is not left
            # re-reading the original file by eye. Never set for
            # SHAREHOLDING, whose proposed_facts already carry evidence_text
            # per field.
            extracted_text=(text.strip() or None) if requires_manual_fields else None,
            field_suggestions=field_suggestions,
        )
        if duplicate_of is not None:
            draft.validation_results.warnings.append(
                f"DUPLICATE_CONTENT: identical content already accepted for "
                f"instrument_id={duplicate_of.instrument_id}, "
                f"reporting_period={duplicate_of.reporting_period}"
            )

        _drafts[draft.draft_id] = draft
        logger.info(
            "manual_evidence_ingested draftId=%s evidenceType=%s contentHash=%s "
            "filename=%s proposedFacts=%d",
            draft.draft_id, evidence_type_enum, digest, filename, len(proposed_facts),
        )
        return draft

    def get_draft(self, draft_id: UUID) -> ManualEvidenceDraft | None:
        """Retrieve a draft by ID (in-process only)."""
        return _drafts.get(draft_id)

    @staticmethod
    def _suggest_current_news_fields(text: str) -> dict[str, str] | None:
        """Conservative, NON-BINDING CURRENT_NEWS field suggestions.

        Delegates to app.evidence_interpretation.interpret_current_news --
        a candidate title/eventDate/source extracted from noisy
        screenshot/OCR text, never fabricated, never auto-accepted. The
        result is advisory pre-fill only: the user still explicitly
        supplies and submits every CURRENT_NEWS field as `corrections` at
        accept() time (see _validate_current_news_fields), completely
        unchanged by this. Returns None (no suggestions block at all)
        when nothing recognizable was found for any field.
        """
        from types import SimpleNamespace

        from app.evidence_interpretation import interpret_current_news

        candidate = interpret_current_news(SimpleNamespace(text=text))
        suggestions: dict[str, str] = {}
        if candidate.title:
            suggestions["title"] = candidate.title
        if candidate.event_date:
            suggestions["eventDate"] = candidate.event_date
        if candidate.source_url:
            # Matches CURRENT_NEWS_MANUAL_FIELDS' "sourceUrl" key exactly,
            # so the frontend can pre-fill the same field the user must
            # ultimately submit as a correction.
            suggestions["sourceUrl"] = candidate.source_url
        return suggestions or None

    # -- text extraction ---------------------------------------------------

    def _extract_text(self, file_bytes: bytes, mime: str, filename: str) -> str | None:
        """Extract text from a file based on its MIME type.

        Delegates to the provider-neutral app.document_extraction.
        DocumentExtractor -- this method is now a thin adapter that keeps
        ManualEvidenceIngestor's existing str|None contract (None means
        "no extraction path exists for this MIME type at all"; "" means
        "a path exists but nothing could be extracted this time") so
        every existing caller (ingest(), _extract_shareholding, ...) is
        unaffected by the refactor. The actual extraction logic -- PDF/
        DOCX/image/CSV/TXT, OCR bounds, subprocess isolation -- lives
        entirely in DocumentExtractor now, independent of this module, so
        it can be reused by a future LLM-based interpreter without
        depending on ManualEvidenceIngestor or ResearchReadinessRuntime.
        """
        from app.document_extraction import DocumentExtractor, DocumentInput, EXTRACTABLE_MIME_TYPES

        if mime not in EXTRACTABLE_MIME_TYPES:
            return None
        extracted = DocumentExtractor().extract(DocumentInput(
            file_bytes=file_bytes, mime_type=mime, filename=filename,
        ))
        for warning in extracted.warnings:
            logger.info("document_extraction_warning mime=%s method=%s warning=%s", mime, extracted.extraction_method, warning)
        return extracted.text

    # -- shareholding extraction --------------------------------------------

    def _extract_shareholding(
        self,
        text: str,
        instrument_id: UUID | None,
        content_digest: str,
        filename: str,
        mime: str,
    ) -> tuple[list[ProposedFact], datetime | None, ValidationResult, dict[str, str] | None]:
        """Extract shareholding facts from extracted text.

        Delegates the actual interpretation (extracted text -> candidate
        shareholding values) to app.evidence_interpretation.
        interpret_shareholding -- EVIDENCE INTERPRETATION is kept separate
        from DOCUMENT EXTRACTION (app.document_extraction, see
        _extract_text above) and from this module's own CANDIDATE DRAFT /
        VALIDATION responsibilities, so the same interpretation step can
        later be reused by a future LLM-based interpreter without
        depending on ManualEvidenceIngestor. This method remains a thin
        adapter converting the interpreter's output into this module's
        ProposedFact/ValidationResult contract -- zero behavior change
        from the previous inline implementation.
        """
        from types import SimpleNamespace

        from app.evidence_interpretation import interpret_shareholding

        interpretation = interpret_shareholding(
            SimpleNamespace(text=text),
            instrument_id=instrument_id,
            content_digest=content_digest,
            filename=filename,
            mime=mime,
        )
        if interpretation.errors or not interpretation.values:
            return [], None, ValidationResult(
                valid=False,
                errors=interpretation.errors or [
                    "NO_SHAREHOLDING_VALUES_EXTRACTED: could not find "
                    "recognizable shareholding category percentages in the document"
                ],
                warnings=list(interpretation.warnings),
            ), interpretation.supplementary

        proposed: list[ProposedFact] = []
        for value in interpretation.values:
            category_str = value.category.value if hasattr(value.category, "value") else str(value.category)
            proposed.append(ProposedFact(
                field=f"shareholding:{category_str}",
                value=value.percentage,
                source_locator=value.source_locator,
                evidence_text=value.evidence_text,
                raw_source_label=value.raw_source_label,
                metric_basis=value.metric_basis,
                confidence=0.90,
            ))

        validation = self._validate_shareholding_draft(
            proposed, SimpleNamespace(period_end=interpretation.period_end)
        )
        # Non-blocking interpreter warnings (e.g. a table row whose column
        # count did not match the header, an implausible ownership-percentage
        # sum, or a reporting period that could not be mapped to a
        # canonical quarter-end date) -- surfaced for Review, never
        # silently dropped and never escalated into a hard error here.
        validation.warnings = [*validation.warnings, *interpretation.warnings]

        return proposed, interpretation.period_end, validation, interpretation.supplementary

    def _validate_shareholding_draft(
        self, proposed: list[ProposedFact], snapshot
    ) -> ValidationResult:
        """Deterministic validation of extracted shareholding facts.

        Checks:
          * At least one mandatory category (PROMOTER, FII_FPI, DII,
            PUBLIC_RETAIL) is present.
          * Percentages are in [0, 100].
          * PROMOTER_PLEDGE values have an explicit metric basis.
          * Period end is a valid quarter-end date.
        """
        from app.repository import _is_shareholding_quarter_period

        errors: list[str] = []
        warnings: list[str] = []

        categories = set()
        for f in proposed:
            cat_str = f.field.split(":", 1)[1] if ":" in f.field else f.field
            try:
                categories.add(ShareholdingCategory(cat_str))
            except ValueError:
                errors.append(f"UNKNOWN_CATEGORY: {f.field}")

        mandatory = {"PROMOTER", "FII_FPI", "DII", "PUBLIC_RETAIL"}
        if not {str(c.value) for c in categories} & mandatory:
            errors.append(
                "MISSING_MANDATORY_CATEGORIES: at least one of "
                "PROMOTER/FII_FPI/DII/PUBLIC_RETAIL must be present"
            )

        for f in proposed:
            pct = f.value
            if not isinstance(pct, Decimal) or pct < 0 or pct > 100:
                errors.append(f"INVALID_PERCENTAGE: {f.field} = {pct}")

        for f in proposed:
            if f.field == f"shareholding:{ShareholdingCategory.PROMOTER_PLEDGE.value}":
                if not f.metric_basis:
                    errors.append(
                        f"PROMOTER_PLEDGE_REQUIRES_METRIC_BASIS: {f.field}"
                    )

        if snapshot.period_end is not None:
            # Semantic quarter-period check: accepts EITHER an explicit
            # calendar quarter-end day OR the first-of-month day a
            # MONTH-precision source period ("Jun 2026") normalizes to --
            # both represent the same semantic reporting period (see
            # app.repository._is_shareholding_quarter_period).
            if not _is_shareholding_quarter_period(snapshot.period_end):
                warnings.append(
                    f"PERIOD_NOT_QUARTER_END: {snapshot.period_end.date().isoformat()}"
                )

        return ValidationResult(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
        )

    # -- dedup ---------------------------------------------------------------

    def _find_duplicate(self, digest: str, evidence_type: EvidenceType) -> ManualEvidenceDraft | None:
        """Check whether the same content was already accepted for this evidence type.

        Checks both the in-process draft store (for unaccepted drafts) and
        the persistence layer (for previously accepted drafts).
        """
        # Check in-process drafts first.
        for draft in _drafts.values():
            if draft.content_hash == digest and draft.evidence_type == evidence_type:
                return draft
        # Check persistence layer for previously accepted drafts.
        persistence = getattr(self.repository, "_persistence", None) if self.repository else None
        loader = getattr(persistence, "load_manual_evidence_drafts", None) if persistence else None
        if not callable(loader):
            return None
        for record in loader():
            if record["content_hash"] == digest and record["evidence_type"] == evidence_type.value:
                # Return a minimal draft-like object for the warning message.
                return ManualEvidenceDraft(
                    draft_id=uuid4(),
                    evidence_type=evidence_type,
                    content_hash=digest,
                    original_filename=record["original_filename"],
                    content_type=record["content_type"],
                    instrument_id=UUID(record["instrument_id"]) if record.get("instrument_id") else None,
                    reporting_period=record.get("reporting_period"),
                    proposed_facts=[],
                    validation_results=ValidationResult(valid=True),
                    extraction_method=record["extraction_method"],
                    created_at=datetime.now(timezone.utc),
                    status=DraftStatus.ACCEPTED,
                )
        return None


def _record_id(record: ShareholdingSnapshot | ResearchEvent | Any) -> UUID:
    """The persisted record's own identity, regardless of evidence type
    (ShareholdingSnapshot.id vs ResearchEvent.event_id vs
    ResearchDocument.document_id for the SECTOR_MACRO document path)."""
    if isinstance(record, ShareholdingSnapshot):
        return record.id
    if hasattr(record, "event_id"):
        return record.event_id
    return record.document_id


def content_hash_hex(data: bytes) -> str:
    """Compute a SHA-256 content hash for raw bytes (used for dedup).

    Note: this is distinct from ``app.normalization.content_hash`` which
    normalizes text before hashing.  For binary uploads we hash the raw
    bytes so identical images/PDFs are recognized as duplicates.
    """
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Acceptor: validation, conflict detection, atomic accept
# ---------------------------------------------------------------------------


class AcceptanceError(Exception):
    """Raised when a draft cannot be accepted (conflict, invalid, etc.)."""
    pass



class ManualEvidenceAcceptor:
    """Accepts a reviewed manual evidence draft.

    Responsibilities:
      * Re-validate the draft and any user corrections.
      * Detect conflicts with existing trusted official evidence.  Conflicts
        require explicit reconciliation (the caller must pass
        ``reconcile_with_conflicts=True``).
      * Atomically persist the normalized facts through the existing
        repository persistence contract (``persist_shareholding_snapshot``
        for SHAREHOLDING evidence).
      * Signal evidence commitment so the readiness runtime re-reads.
      * Recalculate readiness through the normal Research Readiness path
        (NEVER hard-coded READY).
    """

    def __init__(self, repository: Any, runtime=None):
        self.repository = repository
        self.runtime = runtime

    async def accept(
        self,
        *,
        draft_id: UUID,
        corrections: dict[str, Any] | None = None,
        reconcile_with_conflicts: bool = False,
    ) -> dict[str, Any]:
        """Accept a reviewed draft and persist normalized facts.

        Args:
            draft_id: The draft to accept (must be in DRAFT status).
            corrections: User-applied corrections to proposed facts, keyed
                by fact field (e.g. ``shareholding:PROMOTER``).
            reconcile_with_conflicts: When True, acceptance proceeds even if
                the proposed facts conflict with existing trusted official
                evidence.  When False (default), any conflict causes an
                ``AcceptanceError``.

        Returns:
            A dict with acceptance metadata including the persisted
            ``snapshotId`` and the recalculated readiness status.
        """
        ingestor = ManualEvidenceIngestor(repository=self.repository)
        draft = ingestor.get_draft(draft_id)
        if draft is None:
            raise AcceptanceError(f"DRAFT_NOT_FOUND: {draft_id}")

        if draft.status != DraftStatus.DRAFT:
            raise AcceptanceError(
                f"DRAFT_NOT_ACCEPTABLE: draft {draft_id} has status {draft.status}"
            )

        # Apply user corrections to proposed facts. SHAREHOLDING corrects
        # individual extracted fact values; CURRENT_NEWS has no extracted
        # facts to correct -- `corrections` IS the user-supplied submission
        # (title/summary/eventDate/sourceUrl/eventType/impact/timeHorizon).
        if corrections:
            draft.corrections = dict(corrections)
            if draft.evidence_type == EvidenceType.SHAREHOLDING:
                self._apply_corrections(draft)

        if draft.evidence_type == EvidenceType.CURRENT_NEWS and not draft.corrections:
            raise AcceptanceError(
                "DRAFT_INVALID: CURRENT_NEWS requires the manual fields "
                f"{MANUAL_FIELD_SCHEMA[EvidenceType.CURRENT_NEWS]} to be supplied as corrections"
            )
        if (
            draft.evidence_type in (EvidenceType.ORDER_BOOK_CAPEX_GUIDANCE, EvidenceType.GOVERNANCE_HISTORY)
            and not draft.corrections
        ):
            raise AcceptanceError(
                f"DRAFT_INVALID: {draft.evidence_type.value} requires the manual fields "
                f"{MANUAL_FIELD_SCHEMA[draft.evidence_type]} to be supplied as corrections"
            )
        if draft.evidence_type in FINANCIAL_FACT_EVIDENCE_TYPES and not draft.corrections:
            raise AcceptanceError(
                f"DRAFT_INVALID: {draft.evidence_type.value} requires fact rows to be "
                f"supplied as corrections['facts']"
            )
        if (
            draft.evidence_type in PRICE_EVIDENCE_TYPES or draft.evidence_type == EvidenceType.SECTOR_MACRO
        ) and not draft.corrections:
            raise AcceptanceError(
                f"DRAFT_INVALID: {draft.evidence_type.value} requires the manual fields "
                f"{MANUAL_FIELD_SCHEMA[draft.evidence_type]} to be supplied as corrections"
            )

        # Re-validate after corrections.
        draft.validation_results = self._revalidate(draft)
        if not draft.validation_results.valid:
            raise AcceptanceError(
                f"DRAFT_INVALID: {'; '.join(draft.validation_results.errors)}"
            )

        # Build the normalized record from the (possibly corrected) facts.
        # `snapshot` is generically either a ShareholdingSnapshot or a
        # ResearchEvent -- both are "the normalized record this draft
        # persists", kept under one name so the shared accept/response
        # plumbing below does not need a second branch per evidence type.
        if draft.evidence_type == EvidenceType.SHAREHOLDING:
            snapshot = self._build_shareholding_snapshot(draft)
        elif draft.evidence_type == EvidenceType.CURRENT_NEWS:
            snapshot = self._build_current_news_event(draft)
        elif draft.evidence_type in (EvidenceType.ORDER_BOOK_CAPEX_GUIDANCE, EvidenceType.GOVERNANCE_HISTORY):
            # Same canonical ResearchEvent model CURRENT_NEWS already
            # builds, reused generically -- see _build_manual_research_event.
            snapshot = self._build_manual_research_event(draft)
        elif draft.evidence_type in FINANCIAL_FACT_EVIDENCE_TYPES:
            # A list[FinancialFact], not a single record -- GROWTH_FACTS and
            # QUARTERLY_FINANCIALS both require multiple periods in one
            # accept. Handled generically below wherever code must branch on
            # "one record" vs "a batch" (record_id, _persist_and_signal).
            snapshot = self._build_financial_facts(draft)
        elif draft.evidence_type == EvidenceType.LATEST_PRICE:
            snapshot = self._build_latest_price_fact(draft)
        elif draft.evidence_type == EvidenceType.HISTORICAL_PRICE_SERIES:
            snapshot = self._build_historical_price_series_facts(draft)
        elif draft.evidence_type == EvidenceType.SECTOR_MACRO:
            # A ResearchDocument, not an Event -- SECTOR_MACRO's
            # RELEVANT_MACRO_EVENT_EXPOSURE input is document-backed (see
            # SECTOR_MACRO_DOCUMENT_URL_PREFIX / _append_manual_sector_macro_evidence).
            snapshot = self._build_sector_macro_document(draft)
        else:
            raise AcceptanceError(
                f"UNSUPPORTED_EVIDENCE_TYPE_FOR_ACCEPT: {draft.evidence_type}"
            )
        # A batch of FinancialFact rows has no single record id of its own
        # (FinancialFactKey is not a UUID); the draft_id stands in for it.
        record_id = draft.draft_id if isinstance(snapshot, list) else _record_id(snapshot)

        # Conflict detection against existing trusted official evidence.
        # CURRENT_NEWS has no competing numeric value to conflict over --
        # the existing per-event dedup key (_event_key, used by
        # repository.events.add()) is what prevents a duplicate upload from
        # creating a second event; see _persist_and_signal.
        conflicts = self._detect_conflicts(snapshot) if draft.evidence_type == EvidenceType.SHAREHOLDING else []
        if conflicts and not reconcile_with_conflicts:
            raise AcceptanceError(
                f"CONFLICT_WITH_TRUSTED_EVIDENCE: {'; '.join(conflicts)}. "
                f"Pass reconcile_with_conflicts=True to proceed."
            )

        if conflicts and reconcile_with_conflicts:
            draft.validation_results.warnings.append(
                f"CONFLICT_RECONCILED: {'; '.join(conflicts)}"
            )

        # Atomic acceptance: persist through the existing repository contract.
        # persist_shareholding_snapshot uses _run_blocking_persistence which
        # handles thread serialization and bumps the readiness mutation
        # generation counter.
        created = await self._persist_and_signal(draft, snapshot)

        # Update draft status and remove from in-process store.
        draft.status = DraftStatus.ACCEPTED
        _drafts.pop(draft.draft_id, None)

        # Recalculate readiness through the normal path.
        readiness_result = await self._recalculate_readiness(draft.instrument_id)

        logger.info(
            "manual_evidence_accepted draftId=%s snapshotId=%s instrumentId=%s "
            "evidenceType=%s readinessStatus=%s",
            draft_id, record_id, draft.instrument_id, draft.evidence_type,
            readiness_result.get("overallStatus") if readiness_result else "UNKNOWN",
        )

        return {
            "draftId": str(draft_id),
            "snapshotId": str(record_id),
            "financialFactsSubmitted": len(snapshot) if isinstance(snapshot, list) else None,
            "contentHash": draft.content_hash,
            "evidenceType": draft.evidence_type.value,
            "extractionMethod": draft.extraction_method,
            "reportingPeriod": draft.reporting_period.isoformat() if draft.reporting_period else None,
            "corrections": draft.corrections,
            "validationResults": {
                "valid": draft.validation_results.valid,
                "errors": draft.validation_results.errors,
                "warnings": draft.validation_results.warnings,
                "conflicts": draft.validation_results.conflicts,
            },
            "conflictsReconciled": bool(conflicts) and reconcile_with_conflicts,
            "persisted": created,
            "readiness": readiness_result,
        }

    async def _persist_and_signal(self, draft, snapshot) -> bool:
        """Persist the snapshot and provenance record, then signal commitment.

        All operations happen under the repository's persistence worker lock
        for serialization, ensuring atomicity of the snapshot + provenance
        write.  The evidence_committed signal is emitted so the readiness
        runtime re-reads durable evidence.
        """
        import json

        def _do_persist():
            lock = getattr(self.repository, "_persistence_worker_lock", None)
            def _inner():
                if draft.evidence_type == EvidenceType.SHAREHOLDING:
                    created = self.repository.persist_shareholding_snapshot(snapshot)
                elif draft.evidence_type == EvidenceType.CURRENT_NEWS:
                    # research_events.source_document_id is NOT NULL and FKs
                    # to research_documents.document_id, so a real document
                    # row must exist first (unlike the SHAREHOLDING path,
                    # whose snapshot table has no such FK). This mirrors a
                    # manually-sourced article the way news_acquisition.py
                    # stores a fetched one -- it is the provenance record
                    # for the uploaded file, not a duplicate of the generic
                    # global_manual_evidence audit row.
                    snapshot.source_document_id = self._ensure_current_news_source_document(draft)
                    # Same dedup key (_event_key) the automated news pipeline
                    # uses: repository.events.add() returns False (no-op, not
                    # an error) when this exact event already exists, which
                    # is how re-uploading the same article is kept idempotent
                    # rather than creating a second event.
                    created = self.repository.events.add(
                        snapshot, durable=self.repository._documents_are_durable())
                    if created:
                        self.repository._persistence.upsert_event(snapshot)
                elif draft.evidence_type in (EvidenceType.ORDER_BOOK_CAPEX_GUIDANCE, EvidenceType.GOVERNANCE_HISTORY):
                    # Identical persistence path to CURRENT_NEWS above --
                    # same research_events table/model, same FK-satisfying
                    # document row, same dedup key (_event_key), same
                    # readiness re-read. No parallel manual-event database.
                    snapshot.source_document_id = self._ensure_manual_research_event_source_document(draft)
                    created = self.repository.events.add(
                        snapshot, durable=self.repository._documents_are_durable())
                    if created:
                        self.repository._persistence.upsert_event(snapshot)
                elif draft.evidence_type in FINANCIAL_FACT_EVIDENCE_TYPES or draft.evidence_type in PRICE_EVIDENCE_TYPES:
                    # merge_fact() (via upsert_financial_fact) is the ONLY
                    # precedence authority here: USER_UPLOAD's authority is
                    # lower than every automated tier, so this can only ever
                    # fill a genuine gap (existing is None) or be rejected in
                    # favor of existing/newer automated evidence -- never
                    # overwrite it. No pre-check/reconcile step is needed
                    # (unlike SHAREHOLDING) because the existing conflict-
                    # preservation architecture already enforces this
                    # declaratively at the write boundary. LATEST_PRICE /
                    # HISTORICAL_PRICE_SERIES reuse this IDENTICAL FinancialFact
                    # persistence path (synthetic metric names -- see
                    # PRICE_EVIDENCE_TYPES), never global_market_price_observations
                    # / global_daily_market_bars, so they are never read by
                    # stock_rule_engine/ranking.
                    written = self.repository.persist_international_financial_facts(snapshot)
                    created = written > 0
                elif draft.evidence_type == EvidenceType.SECTOR_MACRO:
                    # `snapshot` is the ResearchDocument itself here (see
                    # _build_sector_macro_document) -- persisted directly,
                    # with the same dedup-by-content_hash/canonical_url
                    # semantics upsert_document already gives every other
                    # document (re-uploading the identical summary reuses
                    # the existing row rather than creating a duplicate).
                    persistence_for_doc = getattr(self.repository, "_persistence", None)
                    if persistence_for_doc is None or not hasattr(persistence_for_doc, "upsert_document"):
                        raise AcceptanceError(
                            "NO_DOCUMENT_PERSISTENCE: cannot durably persist SECTOR_MACRO "
                            "evidence without a document store"
                        )
                    created = persistence_for_doc.upsert_document(snapshot)
                    # upsert_document() only writes the durable row -- unlike
                    # the CURRENT_NEWS/ORDER_BOOK_CAPEX_GUIDANCE/GOVERNANCE_HISTORY
                    # paths above, this document IS the evidence itself (read
                    # back via repository.documents_for() -> _append_documents()
                    # / _append_manual_sector_macro_evidence()), not merely an
                    # FK target for an event that repository.events.add()
                    # already registers in its own resident index. Without
                    # this, documents_for() returns an empty list forever
                    # (its ref index, not the DB row, is what it actually
                    # reads) and the manual evidence is accepted/persisted
                    # but invisible to every future readiness read.
                    self.repository.remember_persisted_document(snapshot)
                else:
                    raise AcceptanceError(
                        f"UNSUPPORTED_EVIDENCE_TYPE_FOR_ACCEPT: {draft.evidence_type}")
                # Persist provenance record for the accepted manual evidence.
                persistence = getattr(self.repository, "_persistence", None)
                if persistence is not None:
                    upserter = getattr(persistence, "upsert_manual_evidence_draft", None)
                    if callable(upserter):
                        validation_dict = {
                            "valid": draft.validation_results.valid,
                            "errors": draft.validation_results.errors,
                            "warnings": draft.validation_results.warnings,
                            "conflicts": draft.validation_results.conflicts,
                        }
                        upserter(
                            draft_id=draft.draft_id,
                            evidence_type=draft.evidence_type.value,
                            content_hash=draft.content_hash,
                            original_filename=draft.original_filename,
                            content_type=draft.content_type,
                            instrument_id=draft.instrument_id,
                            reporting_period=draft.reporting_period,
                            extraction_method=draft.extraction_method,
                            extraction_results=json.dumps(_json_safe({
                                "proposed_facts": [
                                    {"field": f.field, "value": str(f.value),
                                     "source_locator": f.source_locator,
                                     "evidence_text": f.evidence_text,
                                     "raw_source_label": f.raw_source_label,
                                     "metric_basis": f.metric_basis,
                                     "confidence": f.confidence}
                                    for f in draft.proposed_facts
                                ],
                            })),
                            validation_results=json.dumps(validation_dict),
                            corrections=json.dumps(_json_safe(draft.corrections)),
                            accepted_at=datetime.now(timezone.utc),
                        )
                from app.readiness_signals import evidence_committed
                evidence_committed(draft.instrument_id)
                return created
            if lock is not None:
                with lock:
                    return _inner()
            return _inner()
        return _do_persist()

    # -- corrections ---------------------------------------------------------

    def _apply_corrections(self, draft: ManualEvidenceDraft) -> None:
        """Apply user corrections to the draft's proposed facts in-place."""
        for field_key, new_value in draft.corrections.items():
            for fact in draft.proposed_facts:
                if fact.field == field_key:
                    fact.value = new_value
                    fact.confidence = 1.0  # User-corrected = trusted

    def _revalidate(self, draft: ManualEvidenceDraft) -> ValidationResult:
        """Re-run validation after corrections have been applied."""
        if draft.evidence_type == EvidenceType.SHAREHOLDING:
            from app.repository import _is_shareholding_quarter_period
            snapshot_values = self._draft_facts_to_snapshot_values(draft)
            errors: list[str] = []
            warnings: list[str] = []
            categories = {v.category for v in snapshot_values}
            mandatory = {"PROMOTER", "FII_FPI", "DII", "PUBLIC_RETAIL"}
            if not {str(c) for c in categories} & mandatory:
                errors.append(
                    "MISSING_MANDATORY_CATEGORIES: at least one of "
                    "PROMOTER/FII_FPI/DII/PUBLIC_RETAIL must be present"
                )
            for v in snapshot_values:
                pct = v.percentage
                if not isinstance(pct, Decimal) or pct < 0 or pct > 100:
                    errors.append(f"INVALID_PERCENTAGE: {v.category} = {pct}")
            for v in snapshot_values:
                if v.category == ShareholdingCategory.PROMOTER_PLEDGE and not v.metric_basis:
                    errors.append(f"PROMOTER_PLEDGE_REQUIRES_METRIC_BASIS: {v.category}")
            if draft.reporting_period and not _is_shareholding_quarter_period(draft.reporting_period):
                warnings.append(
                    f"PERIOD_NOT_QUARTER_END: {draft.reporting_period.date().isoformat()}"
                )
            return ValidationResult(valid=len(errors) == 0, errors=errors, warnings=warnings)
        if draft.evidence_type == EvidenceType.CURRENT_NEWS:
            return self._validate_current_news_fields(draft)
        if draft.evidence_type in (EvidenceType.ORDER_BOOK_CAPEX_GUIDANCE, EvidenceType.GOVERNANCE_HISTORY):
            return self._validate_manual_research_event_fields(
                draft, ALLOWED_EVENT_TYPES_BY_EVIDENCE_TYPE[draft.evidence_type]
            )
        if draft.evidence_type in FINANCIAL_FACT_EVIDENCE_TYPES:
            return self._validate_financial_fact_rows(draft)
        if draft.evidence_type == EvidenceType.LATEST_PRICE:
            return self._validate_latest_price_fields(draft)
        if draft.evidence_type == EvidenceType.HISTORICAL_PRICE_SERIES:
            return self._validate_historical_price_series_fields(draft)
        if draft.evidence_type == EvidenceType.SECTOR_MACRO:
            return self._validate_sector_macro_fields(draft)
        return draft.validation_results

    def _validate_financial_fact_rows(self, draft: ManualEvidenceDraft) -> ValidationResult:
        """Deterministically validate user-supplied FinancialFact rows.

        Nothing here is ever defaulted or guessed: a missing/unparseable
        field, or a metric outside this evidence type's canonical set (see
        ALLOWED_METRICS_BY_EVIDENCE_TYPE), is a hard error. In particular the
        reporting period (periodEnd) is required from the user and is NEVER
        defaulted to "now" -- that would let an upload timestamp masquerade
        as the financial reporting/as-of date, which freshness is computed
        from (see _build_financial_facts / _evidence_from_financial_fact).
        """
        errors: list[str] = []
        warnings: list[str] = []
        allowed = ALLOWED_METRICS_BY_EVIDENCE_TYPE.get(draft.evidence_type, frozenset())
        rows = (draft.corrections or {}).get("facts")
        if not rows or not isinstance(rows, list):
            errors.append(
                "MISSING_FACTS: at least one fact row is required under corrections['facts']"
            )
            return ValidationResult(valid=False, errors=errors, warnings=warnings)

        today = datetime.now(timezone.utc).date()
        seen_keys: set[tuple[str, str, str, str]] = set()
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                errors.append(f"INVALID_ROW[{idx}]: must be an object")
                continue

            metric_raw = row.get("metric")
            metric = _normalize_metric(str(metric_raw)) if metric_raw else None
            if not metric or metric not in allowed:
                errors.append(
                    f"UNSUPPORTED_METRIC[{idx}]: '{metric_raw}' is not a canonical input "
                    f"for {draft.evidence_type.value}. Allowed: {sorted(allowed)}"
                )
                continue

            value_raw = row.get("value")
            try:
                value = Decimal(str(value_raw))
                if not value.is_finite():
                    raise ValueError("not finite")
            except Exception:
                errors.append(f"INVALID_VALUE[{idx}]: '{value_raw}' is not a finite number")
                continue

            period_end_raw = row.get("periodEnd") or row.get("period_end")
            if not period_end_raw:
                errors.append(
                    f"MISSING_PERIOD_END[{idx}]: the real reporting/as-of period is "
                    f"required -- freshness is computed from this date and is never "
                    f"defaulted to the upload time"
                )
                continue
            try:
                parsed_period = datetime.fromisoformat(str(period_end_raw)[:10]).date()
            except (ValueError, TypeError):
                errors.append(
                    f"INVALID_PERIOD_END[{idx}]: could not parse '{period_end_raw}' as a "
                    f"date (expected YYYY-MM-DD)"
                )
                continue
            if parsed_period > today:
                errors.append(f"PERIOD_END_IN_FUTURE[{idx}]: '{period_end_raw}'")

            period_type = str(row.get("periodType") or row.get("period_type") or "").upper()
            if period_type not in ("QUARTERLY", "ANNUAL"):
                errors.append(
                    f"INVALID_PERIOD_TYPE[{idx}]: '{row.get('periodType')}' must be "
                    f"QUARTERLY or ANNUAL"
                )
                continue
            if draft.evidence_type == EvidenceType.QUARTERLY_FINANCIALS and period_type != "QUARTERLY":
                errors.append(
                    f"QUARTERLY_FINANCIALS_REQUIRES_QUARTERLY_PERIOD[{idx}]: got {period_type}"
                )

            unit = row.get("unit")
            if not unit or not str(unit).strip():
                errors.append(
                    f"MISSING_UNIT[{idx}]: a unit (e.g. 'INR crore', 'USD million', '%') "
                    f"is required"
                )

            source_url = row.get("sourceUrl") or row.get("source_url")
            if not source_url or not str(source_url).strip():
                errors.append(f"MISSING_SOURCE[{idx}]: a source reference is required")

            reporting_basis = str(row.get("reportingBasis") or row.get("reporting_basis") or "UNKNOWN").upper()
            dedup_key = (metric, str(period_end_raw)[:10], period_type, reporting_basis)
            if dedup_key in seen_keys:
                errors.append(
                    f"DUPLICATE_FACT_IN_BATCH[{idx}]: {metric}/{period_end_raw}/{period_type}/"
                    f"{reporting_basis} already supplied by another row in this batch"
                )
            seen_keys.add(dedup_key)

        if not draft.instrument_id:
            errors.append("MISSING_INSTRUMENT: instrument_id is required")

        return ValidationResult(valid=len(errors) == 0, errors=errors, warnings=warnings)

    def _build_financial_facts(self, draft: ManualEvidenceDraft) -> list[FinancialFact]:
        """Build real FinancialFact rows from the validated draft corrections.

        Each row is tagged FactSourceTier.USER_UPLOAD (the lowest-authority
        tier -- see app.fact_precedence) and carries NO as_of_date of its
        own, so _evidence_from_financial_fact derives readiness freshness
        from the user-supplied periodEnd alone, never from acceptance/upload
        time (as_of = fact.value.as_of_date or _period_datetime(period_end)).
        retrieved_at records the (honest) acceptance time -- that is a
        provenance timestamp, not a reporting date, and is never read as one.
        """
        rows = (draft.corrections or {}).get("facts") or []
        now = datetime.now(timezone.utc)
        facts: list[FinancialFact] = []
        for row in rows:
            metric = _normalize_metric(str(row["metric"]))
            period_end = str(row.get("periodEnd") or row.get("period_end"))[:10]
            period_type = str(row.get("periodType") or row.get("period_type")).upper()
            reporting_basis = str(row.get("reportingBasis") or row.get("reporting_basis") or "UNKNOWN").upper()
            value = Decimal(str(row["value"]))
            unit = row.get("unit")
            source_url = str(row.get("sourceUrl") or row.get("source_url"))
            key = FinancialFactKey(
                instrument_id=draft.instrument_id,
                metric=metric,
                period_end=period_end,
                period_type=period_type,
                reporting_basis=reporting_basis,
            )
            provenanced = ProvenancedValue(
                value=value,
                unit=unit,
                as_of_date=None,
                source_url=source_url,
                source_name="USER_UPLOAD",
                source_type="MANUAL_UPLOAD",
                retrieved_at=now,
                confidence=1.0,
            )
            facts.append(FinancialFact(
                key=key,
                value=provenanced,
                source_tier=FactSourceTier.USER_UPLOAD,
                source_provider="USER_UPLOAD",
                source_identity=f"manual-evidence:{draft.content_hash}:{metric}:{period_end}:{period_type}",
                source_mode=SourceMode.REAL,
            ))
        return facts

    # -- LATEST_PRICE -------------------------------------------------------

    def _validate_latest_price_fields(self, draft: ManualEvidenceDraft) -> ValidationResult:
        """Deterministically validate a single structured manual price
        observation. Nothing here is ever defaulted or guessed: a missing
        or unparseable field is a hard error, and asOf is NEVER defaulted
        to upload time (that would fabricate freshness)."""
        c = draft.corrections or {}
        errors: list[str] = []
        warnings: list[str] = []

        price_raw = c.get("price")
        price: Decimal | None = None
        if not price_raw and price_raw != 0:
            errors.append("MISSING_PRICE: price is required")
        else:
            try:
                price = Decimal(str(price_raw))
                if not price.is_finite() or price <= 0:
                    raise ValueError("not a positive finite number")
            except Exception:
                errors.append(f"INVALID_PRICE: '{price_raw}' is not a positive finite number")
                price = None

        currency = str(c.get("currency") or "").strip().upper()
        if not currency:
            errors.append("MISSING_CURRENCY: currency is required (e.g. 'INR', 'USD')")
        elif not (currency.isalpha() and len(currency) == 3):
            errors.append(f"INVALID_CURRENCY: '{c.get('currency')}' must be a 3-letter code (e.g. 'INR')")

        as_of_raw = c.get("asOf") or c.get("as_of")
        if not as_of_raw:
            errors.append(
                "MISSING_AS_OF: the real market as-of date/time is required -- "
                "freshness is computed from this value and is never defaulted "
                "to the upload time"
            )
        else:
            try:
                as_of = datetime.fromisoformat(str(as_of_raw).replace("Z", "+00:00"))
                if as_of.tzinfo is None:
                    as_of = as_of.replace(tzinfo=timezone.utc)
                if as_of > datetime.now(timezone.utc):
                    errors.append(f"AS_OF_IN_FUTURE: '{as_of_raw}'")
            except (ValueError, TypeError):
                errors.append(f"INVALID_AS_OF: could not parse '{as_of_raw}' as a date/time")

        source = str(c.get("source") or "").strip()
        if not source:
            errors.append("MISSING_SOURCE: a source reference is required")

        if not draft.instrument_id:
            errors.append("MISSING_INSTRUMENT: instrument_id is required")

        return ValidationResult(valid=len(errors) == 0, errors=errors, warnings=warnings)

    def _build_latest_price_fact(self, draft: ManualEvidenceDraft) -> list[FinancialFact]:
        """Build a single USER_UPLOAD-tier FinancialFact carrying the manual
        price observation. Stored under a synthetic metric name
        (MANUAL_LATEST_PRICE_METRIC) that
        app.research_readiness_runtime._financial_fact_coverage recognizes
        and routes to LATEST_PRICE/VALUATION_INPUTS(LATEST_USABLE_PRICE) --
        this is a real FinancialFact row in global_financial_facts, merged
        through the SAME merge_fact() precedence boundary as every other
        manual fact, so it can only ever fill a genuine gap."""
        c = draft.corrections or {}
        now = datetime.now(timezone.utc)
        as_of = datetime.fromisoformat(str(c.get("asOf") or c.get("as_of")).replace("Z", "+00:00"))
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)
        price = Decimal(str(c["price"]))
        currency = str(c["currency"]).strip().upper()
        source = str(c["source"]).strip()
        period_end = as_of.date().isoformat()
        key = FinancialFactKey(
            instrument_id=draft.instrument_id,
            metric=MANUAL_LATEST_PRICE_METRIC,
            period_end=period_end,
            period_type="POINT_IN_TIME",
            reporting_basis="UNKNOWN",
        )
        provenanced = ProvenancedValue(
            value=price,
            unit=currency,
            as_of_date=None,
            source_url=source,
            source_name="USER_UPLOAD",
            source_type="MANUAL_UPLOAD",
            retrieved_at=now,
            confidence=1.0,
        )
        return [FinancialFact(
            key=key,
            value=provenanced,
            source_tier=FactSourceTier.USER_UPLOAD,
            source_provider="USER_UPLOAD",
            source_identity=f"manual-evidence:{draft.content_hash}:{MANUAL_LATEST_PRICE_METRIC}:{period_end}",
            source_mode=SourceMode.REAL,
        )]

    # -- HISTORICAL_PRICE_SERIES ---------------------------------------------

    _MAX_HISTORICAL_PRICE_OBSERVATIONS = 2000

    def _parse_historical_observations(self, raw: str) -> tuple[list[tuple[Any, Decimal]], list[str]]:
        """Parse a line-oriented 'YYYY-MM-DD,close' observation list.

        Returns (parsed observations, errors). Never raises -- every
        malformed line is reported as a specific error, never silently
        skipped or defaulted.
        """
        errors: list[str] = []
        parsed: list[tuple[Any, Decimal]] = []
        today = datetime.now(timezone.utc).date()
        seen_dates: set[Any] = set()
        lines = [line.strip() for line in str(raw).splitlines() if line.strip()]
        if not lines:
            errors.append("MISSING_OBSERVATIONS: at least one 'date,close' observation is required")
            return parsed, errors
        if len(lines) > self._MAX_HISTORICAL_PRICE_OBSERVATIONS:
            errors.append(
                f"TOO_MANY_OBSERVATIONS: {len(lines)} exceeds the "
                f"{self._MAX_HISTORICAL_PRICE_OBSERVATIONS}-row bound per upload"
            )
            return parsed, errors
        for idx, line in enumerate(lines):
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 2:
                errors.append(f"INVALID_OBSERVATION_ROW[{idx}]: '{line}' must be 'date,close'")
                continue
            date_raw, close_raw = parts[0], parts[1]
            try:
                parsed_date = datetime.fromisoformat(date_raw[:10]).date()
            except (ValueError, TypeError):
                errors.append(f"INVALID_OBSERVATION_DATE[{idx}]: could not parse '{date_raw}' as YYYY-MM-DD")
                continue
            if parsed_date > today:
                errors.append(f"OBSERVATION_DATE_IN_FUTURE[{idx}]: '{date_raw}'")
                continue
            try:
                close = Decimal(close_raw)
                if not close.is_finite() or close <= 0:
                    raise ValueError("not a positive finite number")
            except Exception:
                errors.append(f"INVALID_OBSERVATION_CLOSE[{idx}]: '{close_raw}' is not a positive finite number")
                continue
            if parsed_date in seen_dates:
                errors.append(f"DUPLICATE_OBSERVATION_DATE[{idx}]: '{date_raw}' already supplied by another row")
                continue
            seen_dates.add(parsed_date)
            parsed.append((parsed_date, close))
        return parsed, errors

    def _validate_historical_price_series_fields(self, draft: ManualEvidenceDraft) -> ValidationResult:
        c = draft.corrections or {}
        errors: list[str] = []
        warnings: list[str] = []

        currency = str(c.get("currency") or "").strip().upper()
        if not currency:
            errors.append("MISSING_CURRENCY: currency is required (e.g. 'INR', 'USD')")
        elif not (currency.isalpha() and len(currency) == 3):
            errors.append(f"INVALID_CURRENCY: '{c.get('currency')}' must be a 3-letter code (e.g. 'INR')")

        source = str(c.get("source") or "").strip()
        if not source:
            errors.append("MISSING_SOURCE: a source reference is required")

        observations_raw = c.get("observations")
        if not observations_raw or not str(observations_raw).strip():
            errors.append("MISSING_OBSERVATIONS: at least one 'date,close' observation is required")
        else:
            _, parse_errors = self._parse_historical_observations(str(observations_raw))
            errors.extend(parse_errors)

        if not draft.instrument_id:
            errors.append("MISSING_INSTRUMENT: instrument_id is required")

        return ValidationResult(valid=len(errors) == 0, errors=errors, warnings=warnings)

    def _build_historical_price_series_facts(self, draft: ManualEvidenceDraft) -> list[FinancialFact]:
        """Build one USER_UPLOAD-tier FinancialFact per validated daily
        observation. See _build_latest_price_fact's docstring -- the same
        merge_fact() precedence boundary, metric-routing mechanism, and
        scoring-invisibility guarantee apply here."""
        c = draft.corrections or {}
        now = datetime.now(timezone.utc)
        currency = str(c["currency"]).strip().upper()
        source = str(c["source"]).strip()
        observations, _ = self._parse_historical_observations(str(c["observations"]))
        facts: list[FinancialFact] = []
        for observed_date, close in observations:
            period_end = observed_date.isoformat()
            key = FinancialFactKey(
                instrument_id=draft.instrument_id,
                metric=MANUAL_HISTORICAL_CLOSE_METRIC,
                period_end=period_end,
                period_type="DAILY",
                reporting_basis="UNKNOWN",
            )
            provenanced = ProvenancedValue(
                value=close,
                unit=currency,
                as_of_date=None,
                source_url=source,
                source_name="USER_UPLOAD",
                source_type="MANUAL_UPLOAD",
                retrieved_at=now,
                confidence=1.0,
            )
            facts.append(FinancialFact(
                key=key,
                value=provenanced,
                source_tier=FactSourceTier.USER_UPLOAD,
                source_provider="USER_UPLOAD",
                source_identity=f"manual-evidence:{draft.content_hash}:{MANUAL_HISTORICAL_CLOSE_METRIC}:{period_end}",
                source_mode=SourceMode.REAL,
            ))
        return facts

    # -- SECTOR_MACRO ---------------------------------------------------------

    def _validate_sector_macro_fields(self, draft: ManualEvidenceDraft) -> ValidationResult:
        """Deterministically validate the user-supplied SECTOR_MACRO fields.
        No auto-extraction: the user explicitly asserts the sector/macro
        exposure summary, its as-of date, and a source -- see
        SECTOR_MACRO_DOCUMENT_URL_PREFIX for why this is never routed
        through the automated _MACRO_TERMS keyword heuristic."""
        c = draft.corrections or {}
        errors: list[str] = []
        warnings: list[str] = []

        summary = str(c.get("summary") or "").strip()
        if not summary:
            errors.append("MISSING_SUMMARY: a sector/macro exposure summary is required")

        as_of_raw = c.get("asOf") or c.get("as_of")
        if not as_of_raw:
            errors.append("MISSING_AS_OF: the real as-of date is required")
        else:
            try:
                parsed = datetime.fromisoformat(str(as_of_raw)[:10]).date()
                if parsed > datetime.now(timezone.utc).date():
                    errors.append(f"AS_OF_IN_FUTURE: '{as_of_raw}'")
            except (ValueError, TypeError):
                errors.append(f"INVALID_AS_OF: could not parse '{as_of_raw}' as a date (expected YYYY-MM-DD)")

        source = str(c.get("source") or "").strip()
        if not source:
            errors.append("MISSING_SOURCE: a source reference is required")

        if not draft.instrument_id:
            errors.append("MISSING_INSTRUMENT: instrument_id is required")

        return ValidationResult(valid=len(errors) == 0, errors=errors, warnings=warnings)

    def _validate_current_news_fields(self, draft: ManualEvidenceDraft) -> ValidationResult:
        """Deterministically validate the user-supplied CURRENT_NEWS fields.

        Nothing here is ever defaulted or guessed: a missing/unparseable
        field is a hard error, not a silent fallback (e.g. to "now" for the
        event date, which would fabricate freshness).
        """
        c = draft.corrections or {}
        errors: list[str] = []
        warnings: list[str] = []

        def _get(*keys):
            for k in keys:
                v = c.get(k)
                if v:
                    return v
            return None

        title = (_get("title") or "").strip()
        summary = (_get("summary") or "").strip()
        source_url = (_get("sourceUrl", "source_url") or "").strip()
        if not title:
            errors.append("MISSING_TITLE: a headline/title is required")
        if not summary:
            errors.append("MISSING_SUMMARY: a brief summary of the event is required")
        if not source_url:
            errors.append("MISSING_SOURCE: a source reference (URL or publication name) is required")

        event_date_raw = _get("eventDate", "event_date")
        if not event_date_raw:
            errors.append(
                "MISSING_EVENT_DATE: the real publication/event date is required -- "
                "CURRENT_NEWS freshness is computed from this date and is never "
                "defaulted to the upload time"
            )
        else:
            try:
                event_date = datetime.fromisoformat(str(event_date_raw).replace("Z", "+00:00"))
                if event_date.tzinfo is None:
                    event_date = event_date.replace(tzinfo=timezone.utc)
                if event_date > datetime.now(timezone.utc) + timedelta(days=1):
                    errors.append("EVENT_DATE_IN_FUTURE: event/publication date cannot be in the future")
            except (ValueError, TypeError):
                errors.append(f"INVALID_EVENT_DATE: could not parse '{event_date_raw}' as a date")

        event_type_raw = _get("eventType", "event_type")
        try:
            if not event_type_raw or ResearchEventType(event_type_raw) is None:
                raise ValueError
        except ValueError:
            errors.append(
                f"INVALID_EVENT_TYPE: '{event_type_raw}' is not a recognized event type "
                f"(see ResearchEventType)"
            )

        impact_raw = _get("impact")
        try:
            if not impact_raw or EventImpact(impact_raw) is None:
                raise ValueError
        except ValueError:
            errors.append(f"INVALID_IMPACT: '{impact_raw}' is not a recognized impact classification")

        time_horizon_raw = _get("timeHorizon", "time_horizon")
        try:
            if not time_horizon_raw or TimeHorizon(time_horizon_raw) is None:
                raise ValueError
        except ValueError:
            errors.append(f"INVALID_TIME_HORIZON: '{time_horizon_raw}' is not a recognized time horizon")

        return ValidationResult(valid=len(errors) == 0, errors=errors, warnings=warnings)

    def _validate_manual_research_event_fields(
        self, draft: ManualEvidenceDraft, allowed_event_types: frozenset[ResearchEventType]
    ) -> ValidationResult:
        """Deterministically validate user-supplied ORDER_BOOK_CAPEX_GUIDANCE
        / GOVERNANCE_HISTORY fields.

        Identical in structure to _validate_current_news_fields (same
        canonical ResearchEvent fields, same never-default-the-date
        discipline) with ONE difference: eventType must be one of the
        canonical event types this specific requirement actually maps to
        readiness evidence for (ALLOWED_EVENT_TYPES_BY_EVIDENCE_TYPE), not
        just any ResearchEventType -- this is what keeps the structured
        editor from accepting, say, a SHAREHOLDING-flavoured event type for
        ORDER_BOOK_CAPEX_GUIDANCE, or a non-governance event type for
        GOVERNANCE_HISTORY.
        """
        c = draft.corrections or {}
        errors: list[str] = []
        warnings: list[str] = []

        def _get(*keys):
            for k in keys:
                v = c.get(k)
                if v:
                    return v
            return None

        title = (_get("title") or "").strip()
        summary = (_get("summary") or "").strip()
        source_url = (_get("sourceUrl", "source_url") or "").strip()
        if not title:
            errors.append("MISSING_TITLE: a headline/title is required")
        if not summary:
            errors.append("MISSING_SUMMARY: a brief summary of the event is required")
        if not source_url:
            errors.append("MISSING_SOURCE: a source reference (URL or publication name) is required")

        event_date_raw = _get("eventDate", "event_date")
        if not event_date_raw:
            errors.append(
                f"MISSING_EVENT_DATE: the real event/reporting date is required -- "
                f"{draft.evidence_type.value} freshness is computed from this date and is "
                f"never defaulted to the upload time"
            )
        else:
            try:
                event_date = datetime.fromisoformat(str(event_date_raw).replace("Z", "+00:00"))
                if event_date.tzinfo is None:
                    event_date = event_date.replace(tzinfo=timezone.utc)
                if event_date > datetime.now(timezone.utc) + timedelta(days=1):
                    errors.append("EVENT_DATE_IN_FUTURE: event/reporting date cannot be in the future")
            except (ValueError, TypeError):
                errors.append(f"INVALID_EVENT_DATE: could not parse '{event_date_raw}' as a date")

        event_type_raw = _get("eventType", "event_type")
        try:
            event_type = ResearchEventType(event_type_raw) if event_type_raw else None
            if event_type is None or event_type not in allowed_event_types:
                raise ValueError
        except ValueError:
            allowed_values = sorted(t.value for t in allowed_event_types)
            errors.append(
                f"INVALID_EVENT_TYPE: '{event_type_raw}' is not a canonical "
                f"{draft.evidence_type.value} event type. Allowed: {allowed_values}"
            )

        impact_raw = _get("impact")
        try:
            if not impact_raw or EventImpact(impact_raw) is None:
                raise ValueError
        except ValueError:
            errors.append(f"INVALID_IMPACT: '{impact_raw}' is not a recognized impact classification")

        time_horizon_raw = _get("timeHorizon", "time_horizon")
        try:
            if not time_horizon_raw or TimeHorizon(time_horizon_raw) is None:
                raise ValueError
        except ValueError:
            errors.append(f"INVALID_TIME_HORIZON: '{time_horizon_raw}' is not a recognized time horizon")

        return ValidationResult(valid=len(errors) == 0, errors=errors, warnings=warnings)

    def _ensure_manual_research_event_source_document(self, draft: ManualEvidenceDraft) -> UUID:
        """Persist a minimal ResearchDocument for an ORDER_BOOK_CAPEX_GUIDANCE
        / GOVERNANCE_HISTORY upload and return the real, FK-satisfying
        document_id to use as the event's source_document_id.

        Identical logic to _ensure_current_news_source_document (kept as a
        separate method rather than shared, so the existing, already-tested
        CURRENT_NEWS code path is never touched by this change) -- see that
        method's docstring for why this document row must exist before
        upsert_event() runs.
        """
        from app.models import DocumentStatus, DocumentType, ResearchDocument

        persistence = getattr(self.repository, "_persistence", None)
        if persistence is None or not hasattr(persistence, "upsert_document"):
            raise AcceptanceError(
                f"NO_DOCUMENT_PERSISTENCE: cannot durably persist {draft.evidence_type.value} "
                f"evidence without a document store"
            )

        c = draft.corrections or {}
        company_id = None
        try:
            if draft.instrument_id is not None:
                company_id = self.repository.profile(draft.instrument_id).company_id
        except StopIteration:
            company_id = None

        document = ResearchDocument(
            document_id=draft.draft_id,
            canonical_url=f"manual-evidence:{draft.content_hash}",
            original_url=f"manual-evidence:{draft.content_hash}",
            title=(c.get("title") or draft.original_filename or None),
            source_type=SourceType.NEWS,
            source_classification=SourceClassification.OTHER,
            source_name="USER_UPLOAD",
            published_at=None,
            retrieved_at=datetime.now(timezone.utc),
            content_type=draft.content_type or "text/plain",
            document_type=DocumentType.TEXT,
            status=DocumentStatus.PROCESSED,
            reliability_level=ReliabilityLevel.LEVEL_D,
            instrument_id=draft.instrument_id,
            company_id=company_id,
            source_mode=SourceMode.REAL,
            freshness="REAL",
            content_hash=draft.content_hash,
        )
        inserted = persistence.upsert_document(document)
        if inserted:
            return document.document_id
        return document.duplicate_of_document_id or document.document_id

    def _build_manual_research_event(self, draft: ManualEvidenceDraft) -> ResearchEvent:
        """Build a ResearchEvent for an ORDER_BOOK_CAPEX_GUIDANCE /
        GOVERNANCE_HISTORY draft from the user-supplied, validated fields.

        Identical construction to _build_current_news_event (kept separate
        rather than shared for the same "never touch the tested CURRENT_NEWS
        path" reason as above). event_type has already been validated
        against ALLOWED_EVENT_TYPES_BY_EVIDENCE_TYPE[draft.evidence_type] by
        _validate_manual_research_event_fields before this is called, so
        _append_events() in research_readiness_runtime.py will route this
        exact event into the correct requirement's evidence the same way it
        routes any automated event with that event_type.
        """
        c = draft.corrections or {}

        def _get(*keys):
            for k in keys:
                v = c.get(k)
                if v:
                    return v
            return None

        event_date_raw = _get("eventDate", "event_date")
        event_date = datetime.fromisoformat(str(event_date_raw).replace("Z", "+00:00"))
        if event_date.tzinfo is None:
            event_date = event_date.replace(tzinfo=timezone.utc)

        if draft.instrument_id is None:
            raise AcceptanceError(
                f"MISSING_INSTRUMENT_ID: {draft.evidence_type.value} evidence requires an instrument"
            )
        try:
            profile = self.repository.profile(draft.instrument_id)
            company_id = profile.company_id
        except StopIteration:
            raise AcceptanceError(f"PROFILE_NOT_FOUND: {draft.instrument_id}")

        return ResearchEvent(
            instrument_id=draft.instrument_id,
            company_id=company_id,
            event_type=ResearchEventType(_get("eventType", "event_type")),
            event_date=event_date,
            title=(_get("title") or "").strip(),
            summary=(_get("summary") or "").strip(),
            # Placeholder: _persist_and_signal's elif branch for these two
            # types calls _ensure_manual_research_event_source_document()
            # and overwrites this with the real, FK-satisfying
            # research_documents.document_id before the event is durably
            # persisted (source_document_id is NOT NULL + FOREIGN KEY
            # REFERENCES research_documents).
            source_document_id=draft.draft_id,
            source_url=(_get("sourceUrl", "source_url") or "").strip(),
            source_type=SourceType.NEWS,
            source_classification=SourceClassification.OTHER,
            reliability=ReliabilityLevel.LEVEL_D,
            source_mode=SourceMode.REAL,
            confidence=0.6,
            impact=EventImpact(_get("impact")),
            time_horizon=TimeHorizon(_get("timeHorizon", "time_horizon")),
            raw_evidence_reference=f"manual-evidence:{draft.content_hash}",
            published_at=event_date,
            retrieved_at=datetime.now(timezone.utc),
        )

    def _ensure_current_news_source_document(self, draft: ManualEvidenceDraft) -> UUID:
        """Persist a minimal ResearchDocument for the uploaded article and
        return the real, FK-satisfying document_id to use as the event's
        source_document_id.

        research_events.source_document_id is NOT NULL and FOREIGN KEY
        REFERENCES research_documents(document_id), so this row must exist
        durably before upsert_event() is called -- there is no way to
        reference "this acceptance" without a real document row, unlike
        ShareholdingSnapshot which has no such column/FK. Deduplicated by
        content_hash/canonical_url the same way upsert_document already
        dedupes any other document (re-uploading the identical article
        reuses the existing row rather than creating a duplicate).
        """
        from app.models import DocumentStatus, DocumentType, ResearchDocument

        persistence = getattr(self.repository, "_persistence", None)
        if persistence is None or not hasattr(persistence, "upsert_document"):
            raise AcceptanceError(
                "NO_DOCUMENT_PERSISTENCE: cannot durably persist CURRENT_NEWS "
                "evidence without a document store"
            )

        c = draft.corrections or {}
        company_id = None
        try:
            if draft.instrument_id is not None:
                company_id = self.repository.profile(draft.instrument_id).company_id
        except StopIteration:
            company_id = None

        document = ResearchDocument(
            document_id=draft.draft_id,
            canonical_url=f"manual-evidence:{draft.content_hash}",
            original_url=f"manual-evidence:{draft.content_hash}",
            title=(c.get("title") or draft.original_filename or None),
            source_type=SourceType.NEWS,
            source_classification=SourceClassification.OTHER,
            source_name="USER_UPLOAD",
            published_at=None,
            retrieved_at=datetime.now(timezone.utc),
            content_type=draft.content_type or "text/plain",
            document_type=DocumentType.TEXT,
            status=DocumentStatus.PROCESSED,
            reliability_level=ReliabilityLevel.LEVEL_D,
            instrument_id=draft.instrument_id,
            company_id=company_id,
            source_mode=SourceMode.REAL,
            freshness="REAL",
            content_hash=draft.content_hash,
        )
        inserted = persistence.upsert_document(document)
        if inserted:
            return document.document_id
        # Already existed (e.g. identical re-upload): reuse its real id.
        return document.duplicate_of_document_id or document.document_id

    def _build_current_news_event(self, draft: ManualEvidenceDraft) -> ResearchEvent:
        """Build a ResearchEvent from the user-supplied, validated fields.

        This is the SAME model/table the automated news-acquisition pipeline
        writes (see ResearchRepository._apply_ingested_event_async) -- no
        parallel readiness model. source_mode=REAL and reliability=LEVEL_D
        mark it as real but low-tier (self-reported, unverified) evidence,
        matching the SHAREHOLDING USER_UPLOAD precedent.
        """
        c = draft.corrections or {}

        def _get(*keys):
            for k in keys:
                v = c.get(k)
                if v:
                    return v
            return None

        event_date_raw = _get("eventDate", "event_date")
        event_date = datetime.fromisoformat(str(event_date_raw).replace("Z", "+00:00"))
        if event_date.tzinfo is None:
            event_date = event_date.replace(tzinfo=timezone.utc)

        if draft.instrument_id is None:
            raise AcceptanceError("MISSING_INSTRUMENT_ID: CURRENT_NEWS evidence requires an instrument")
        try:
            profile = self.repository.profile(draft.instrument_id)
            company_id = profile.company_id
        except StopIteration:
            raise AcceptanceError(f"PROFILE_NOT_FOUND: {draft.instrument_id}")

        return ResearchEvent(
            instrument_id=draft.instrument_id,
            company_id=company_id,
            event_type=ResearchEventType(_get("eventType", "event_type")),
            event_date=event_date,
            title=(_get("title") or "").strip(),
            summary=(_get("summary") or "").strip(),
            # Placeholder: _persist_and_signal's CURRENT_NEWS branch calls
            # _ensure_current_news_source_document() and overwrites this
            # with the real, FK-satisfying research_documents.document_id
            # before the event is durably persisted (source_document_id is
            # NOT NULL + FOREIGN KEY REFERENCES research_documents). Using
            # draft.draft_id here keeps this object constructible/valid on
            # its own (e.g. for conflict-detection, which runs before
            # persistence) without yet touching the document store.
            source_document_id=draft.draft_id,
            source_url=(_get("sourceUrl", "source_url") or "").strip(),
            source_type=SourceType.NEWS,
            source_classification=SourceClassification.OTHER,
            reliability=ReliabilityLevel.LEVEL_D,
            source_mode=SourceMode.REAL,
            confidence=0.6,
            impact=EventImpact(_get("impact")),
            time_horizon=TimeHorizon(_get("timeHorizon", "time_horizon")),
            raw_evidence_reference=f"manual-evidence:{draft.content_hash}",
            published_at=event_date,
            retrieved_at=datetime.now(timezone.utc),
        )

    def _build_sector_macro_document(self, draft: ManualEvidenceDraft):
        """Build a ResearchDocument carrying the user-supplied sector/macro
        exposure summary as its normalized_text.

        Deliberately a ResearchDocument, not a ResearchEvent: SECTOR_MACRO's
        RELEVANT_MACRO_EVENT_EXPOSURE input is document-backed in
        app.research_readiness_runtime (see
        _append_manual_sector_macro_evidence / SECTOR_MACRO_DOCUMENT_URL_PREFIX),
        not event-backed like ORDER_BOOK_CAPEX_GUIDANCE/GOVERNANCE_HISTORY.
        The distinguishing canonical_url prefix is what lets readiness
        recognize this specific document as an explicit manual SECTOR_MACRO
        submission (always counted) rather than running it through the
        automated _MACRO_TERMS keyword heuristic meant for unlabeled text.
        This never touches CANONICAL_SECTOR (canonical instrument
        classification) -- only the qualitative RELEVANT_MACRO_EVENT_EXPOSURE
        input.
        """
        from app.models import DocumentStatus, DocumentType, ResearchDocument

        c = draft.corrections or {}
        as_of_raw = c.get("asOf") or c.get("as_of")
        as_of = datetime.fromisoformat(str(as_of_raw)[:10]).replace(tzinfo=timezone.utc)

        company_id = None
        try:
            if draft.instrument_id is not None:
                company_id = self.repository.profile(draft.instrument_id).company_id
        except StopIteration:
            company_id = None

        return ResearchDocument(
            document_id=draft.draft_id,
            canonical_url=f"{SECTOR_MACRO_DOCUMENT_URL_PREFIX}{draft.content_hash}",
            # The user's real source reference (never the synthetic marker
            # above, which exists only so readiness can recognize this as an
            # explicit manual SECTOR_MACRO submission -- see
            # _append_manual_sector_macro_evidence, which reads source_url
            # from original_url, not canonical_url).
            original_url=str(c.get("source") or "").strip(),
            title=(c.get("summary") or draft.original_filename or None),
            normalized_text=str(c.get("summary") or "").strip(),
            source_type=SourceType.NEWS,
            source_classification=SourceClassification.OTHER,
            source_name="USER_UPLOAD",
            published_at=as_of,
            retrieved_at=datetime.now(timezone.utc),
            content_type=draft.content_type or "text/plain",
            document_type=DocumentType.TEXT,
            status=DocumentStatus.PROCESSED,
            reliability_level=ReliabilityLevel.LEVEL_D,
            instrument_id=draft.instrument_id,
            company_id=company_id,
            source_mode=SourceMode.REAL,
            freshness="REAL",
            content_hash=draft.content_hash,
        )

    def _draft_facts_to_snapshot_values(
        self, draft: ManualEvidenceDraft
    ) -> list[ShareholdingSnapshotValue]:
        """Convert draft proposed facts back to ShareholdingSnapshotValue objects."""
        from app.models import ShareholdingSnapshotValue
        values: list[ShareholdingSnapshotValue] = []
        for fact in draft.proposed_facts:
            category_str = fact.field.split(":", 1)[1] if ":" in fact.field else fact.field
            values.append(ShareholdingSnapshotValue(
                category=ShareholdingCategory(category_str),
                percentage=fact.value if isinstance(fact.value, Decimal) else Decimal(str(fact.value)),
                metric_basis=fact.metric_basis,
                raw_source_label=fact.raw_source_label,
                source_locator=fact.source_locator or f"manual-evidence:{draft.content_hash}",
                evidence_text=fact.evidence_text,
                created_at=datetime.now(timezone.utc),
            ))
        return values

    def _build_shareholding_snapshot(self, draft: ManualEvidenceDraft) -> ShareholdingSnapshot:
        """Build a ShareholdingSnapshot from the accepted draft's facts."""
        values = self._draft_facts_to_snapshot_values(draft)
        if not values:
            raise AcceptanceError("NO_VALUES_TO_PERSIST")
        return ShareholdingSnapshot(
            instrument_id=draft.instrument_id,
            period_end=draft.reporting_period or datetime.now(timezone.utc),
            filing_basis="STANDALONE",
            source_provider="USER_UPLOAD",
            source_type="MANUAL_EVIDENCE",
            source_identity_key=f"manual-evidence:{draft.content_hash}",
            source_url=f"manual-evidence:{draft.content_hash}",
            research_document_id=None,
            published_at=draft.reporting_period,
            retrieved_at=datetime.now(timezone.utc),
            confidence=Decimal(str(min(f.confidence for f in draft.proposed_facts) or 0.5)),
            reliability_level=ReliabilityLevel.LEVEL_D,
            source_mode=SourceMode.REAL,
            values=values,
        )

    # -- conflict detection -------------------------------------------------

    def _detect_conflicts(self, snapshot: ShareholdingSnapshot) -> list[str]:
        """Detect conflicts between proposed facts and existing trusted evidence.

        A conflict occurs when:
          * An existing official NSE snapshot for the same instrument and
            period already covers the same category with a meaningfully
            different percentage.

        Lower-authority (USER_UPLOAD) snapshots never overwrite trusted
        official evidence; they can only fill genuine gaps.
        """
        if not self.repository or snapshot.instrument_id is None:
            return []
        conflicts: list[str] = []
        persistence = getattr(self.repository, "_persistence", None)
        loader = getattr(persistence, "load_shareholding_snapshots", None)
        existing_snapshots = []
        if callable(loader):
            existing_snapshots = loader(
                instrument_ids={snapshot.instrument_id}
            ) if snapshot.instrument_id is not None else []
        for existing in existing_snapshots:
            if (existing.instrument_id == snapshot.instrument_id
                    and existing.period_end == snapshot.period_end
                    and existing.source_mode == SourceMode.REAL):
                existing_is_official = (
                    existing.source_provider.upper() == "NSE"
                    and existing.reliability_level == ReliabilityLevel.LEVEL_A
                )
                if not existing_is_official:
                    continue
                existing_values = {v.category: v.percentage for v in existing.values}
                for new_value in snapshot.values:
                    if new_value.category in existing_values:
                        existing_pct = existing_values[new_value.category]
                        if abs(new_value.percentage - existing_pct) > Decimal("0.01"):
                            cat_str = new_value.category.value if hasattr(new_value.category, "value") else str(new_value.category)
                            conflicts.append(
                                f"{cat_str} ({new_value.percentage}%) conflicts "
                                f"with trusted official evidence ({existing_pct}%)"
                            )
        return conflicts

    # -- readiness recalculation --------------------------------------------

    async def _recalculate_readiness(self, instrument_id: UUID | None) -> dict[str, Any] | None:
        """Recalculate readiness through the normal research readiness path.

        NEVER hard-codes READY.  Invokes the existing read path which
        re-reads durable evidence from the repository.
        """
        if not instrument_id or not self.runtime:
            return None
        try:
            profile = self.repository.profile(instrument_id)
            from app.research_readiness_runtime import jurisdiction_for_profile, readiness_response
            result = await self.runtime.read(
                instrument_id,
                jurisdiction=jurisdiction_for_profile(profile),
            )
            return readiness_response(
                result,
                self.runtime.requirement_registry,
            )
        except StopIteration:
            logger.warning(
                "readiness_recalc_failed instrumentId=%s reason=PROFILE_NOT_FOUND",
                instrument_id,
            )
            return None
        except Exception as exc:
            logger.warning(
                "readiness_recalc_failed instrumentId=%s reason=%s",
                instrument_id, exc,
            )
            return None
