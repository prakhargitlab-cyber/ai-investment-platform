"""Bounded, fail-closed adapter for the genuine SBI Mutual Fund
"Current Year TER for All Schemes" disclosure workbook.

Scope (exactly what this module does and nothing more):

  1. Parse the workbook under a strict, explicit input contract (Task 1).
     An unexpected sheet name, header row, or column count is a hard
     ``WorkbookSchemaError`` -- there is no silent best-effort fallback.
  2. Classify each row as ETF, ETF_FUND_OF_FUND, or OTHER using the
     scheme's own disclosed name text (never the NSDL scheme-code prefix
     alone, which is not a documented classification scheme). Fund-of-funds
     are explicitly excluded from ETF evidence -- a "... ETF FOF" / "...
     Fund of Fund" scheme is never treated as the ETF it tracks.
  3. Select, per normalized scheme code, the single latest dated
     observation by (scheme_code, TER Date) -- never by row position --
     and fail closed on any same-(code,date) value conflict rather than
     silently picking one.
  4. Treat "Direct Plan - Total TER (%) == 0" as NOT APPLICABLE, never as a
     verified zero expense ratio. Exchange-traded ETFs have no investor
     "Direct Plan" distinct from a "Regular Plan" (that split only matters
     for mutual-fund schemes investors buy through a distributor); every
     genuine ETF row in the real workbook carries ``Direct Plan - Total TER
     == 0`` for every single daily observation across the full year, which
     is a placeholder, not a measured value. The adapter always sources the
     ETF's expense ratio from the "Regular Plan - Total TER" column and
     raises rather than silently using a Direct Plan figure for a row
     classified as ETF.
  5. Only ever construct and persist an ``EtfFact`` for a scheme code whose
     identity has been independently verified by the caller-supplied
     ``identity_resolver`` (NSDL scheme code -> canonical ETF instrument
     UUID). No such verified mapping exists in this repository today for
     any SBI ETF (see the module-level note below) -- the default resolver
     returns ``None`` for every code, so by default every row is staged
     only (``IDENTITY_UNRESOLVED_STAGED_ONLY``) and nothing is persisted.
     This is deliberate, not a placeholder to "fix later" by relaxing the
     check: joining the workbook's full scheme name (e.g. "SBI Nifty
     Consumption ETF") to the NSE's abbreviated official security name
     (e.g. "SBIAMC-SBIETFCON") would be exactly the fuzzy-name-only match
     this adapter must never perform.

No network calls are made anywhere in this module. No scheduler, Radar
cycle, or deployment step is touched. At most ``max_instruments`` distinct
resolved instrument identities may be persisted to in a single
``acquire()`` call (bounded rollout cap, enforced at call time).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from uuid import UUID

import openpyxl

from app.etf_evidence import (
    EtfAcquisitionAttempt,
    EtfAcquisitionOutcome,
    EtfAuthority,
    EtfFact,
    EtfMetric,
    EtfProvenance,
    evidence_id,
)

EXPECTED_SHEET_NAME = "Sheet1"
EXPECTED_HEADERS = (
    "NSDL Scheme Code",
    "Scheme Name",
    "TER Date\n(DD/MM/\nYYYY)",
    "Regular Plan - Base Expense Ratio (BER) (%)",
    "Regular Plan - Brokerage cost (%)",
    "Regular Plan - Transaction Cost incurred for the purpose of execution of trade (%)",
    "Regular Plan - Statutory Levies (including GST) (%)",
    "Regular Plan - Total TER (%)",
    "Direct Plan - Base Expense Ratio (BER) (%)",
    "Direct Plan - Brokerage cost (%)",
    "Direct Plan - Transaction Cost incurred for the purpose of execution of trade (%)",
    "Direct Plan - Statutory Levies (including GST) (%)",
    "Direct Plan - Total TER (%)",
)
MAX_TER_PERCENT = Decimal("100")


class WorkbookSchemaError(ValueError):
    """The workbook does not match the exact SBI TER disclosure schema."""


class SchemeClassification(StrEnum):
    ETF = "ETF"
    ETF_FUND_OF_FUND = "ETF_FUND_OF_FUND"
    OTHER = "OTHER"


class AdapterOutcome(StrEnum):
    IDENTITY_UNRESOLVED_STAGED_ONLY = "IDENTITY_UNRESOLVED_STAGED_ONLY"
    INGESTED = "INGESTED"
    ALREADY_INGESTED = "ALREADY_INGESTED"
    CONFLICTING_OFFICIAL_METRIC_FOR_PERIOD = "CONFLICTING_OFFICIAL_METRIC_FOR_PERIOD"


@dataclass(frozen=True)
class StagedTerObservation:
    """A single scheme's latest TER observation, pre-identity-resolution."""
    scheme_code: str
    scheme_name: str
    classification: SchemeClassification
    ter_date: date
    regular_total_ter_percent: Decimal
    direct_total_ter_percent: Decimal
    direct_plan_applicable: bool


@dataclass(frozen=True)
class SbiTerAdapterResult:
    scheme_code: str
    scheme_name: str
    outcome: AdapterOutcome
    instrument_id: UUID | None = None
    fact: EtfFact | None = None


def _normalize_code(value) -> str:
    return str(value).strip()


def classify_scheme_name(name: str) -> SchemeClassification:
    lowered = name.casefold()
    if "fof" in lowered or "fund of fund" in lowered:
        return SchemeClassification.ETF_FUND_OF_FUND if "etf" in lowered else SchemeClassification.OTHER
    if "etf" in lowered:
        return SchemeClassification.ETF
    return SchemeClassification.OTHER


def _parse_raw_rows(path: Path):
    workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    if workbook.sheetnames[:1] != [EXPECTED_SHEET_NAME]:
        raise WorkbookSchemaError(f"WORKBOOK_SCHEMA_UNSUPPORTED_SHEET_NAME:{workbook.sheetnames!r}")
    sheet = workbook[EXPECTED_SHEET_NAME]
    rows = sheet.iter_rows(values_only=True)
    header = next(rows, None)
    if header != EXPECTED_HEADERS:
        raise WorkbookSchemaError(f"WORKBOOK_SCHEMA_UNSUPPORTED_HEADER_ROW:{header!r}")
    for row in rows:
        if row is None or row[0] is None:
            continue
        if len(row) != len(EXPECTED_HEADERS):
            raise WorkbookSchemaError(f"WORKBOOK_SCHEMA_UNSUPPORTED_COLUMN_COUNT:{len(row)}")
        yield row


def build_staged_observations(path: str | Path) -> list[StagedTerObservation]:
    """Parse + classify + select-latest. ETF_FUND_OF_FUND and OTHER rows are
    dropped here: only genuine ETF rows are ever staged for identity
    resolution. Raises WorkbookSchemaError on any header/sheet mismatch and
    ValueError("CONFLICTING_TER_OBSERVATION_FOR_SAME_SCHEME_AND_DATE") on a
    genuine same-(code,date) value disagreement.
    """
    latest: dict[str, tuple[date, tuple]] = {}
    names: dict[str, str] = {}
    classifications: dict[str, SchemeClassification] = {}
    for row in _parse_raw_rows(Path(path)):
        code = _normalize_code(row[0])
        name = str(row[1]).strip()
        classification = classify_scheme_name(name)
        if classification is not SchemeClassification.ETF:
            continue
        ter_date_raw = row[2]
        if not isinstance(ter_date_raw, datetime):
            raise WorkbookSchemaError(f"WORKBOOK_SCHEMA_UNSUPPORTED_TER_DATE_CELL:{ter_date_raw!r}")
        ter_date = ter_date_raw.date()
        classifications[code] = classification
        names[code] = name
        existing = latest.get(code)
        if existing is None:
            latest[code] = (ter_date, row)
        elif ter_date > existing[0]:
            latest[code] = (ter_date, row)
        elif ter_date == existing[0] and row != existing[1]:
            raise ValueError(
                f"CONFLICTING_TER_OBSERVATION_FOR_SAME_SCHEME_AND_DATE:{code}:{ter_date.isoformat()}")

    staged = []
    for code, (ter_date, row) in latest.items():
        regular_total = Decimal(str(row[7]))
        direct_total = Decimal(str(row[12]))
        if not (0 <= regular_total <= MAX_TER_PERCENT):
            raise ValueError(f"INVALID_REGULAR_TOTAL_TER_PERCENT:{code}:{regular_total}")
        staged.append(StagedTerObservation(
            scheme_code=code, scheme_name=names[code], classification=classifications[code],
            ter_date=ter_date, regular_total_ter_percent=regular_total,
            direct_total_ter_percent=direct_total,
            # A zero Direct Plan Total TER on an ETF row is the workbook's
            # placeholder for "no Direct Plan exists for this exchange-traded
            # scheme" -- never a verified zero Direct-Plan expense ratio.
            direct_plan_applicable=direct_total != 0,
        ))
    return staged


class BoundedSbiTerAdapter:
    """Resolve identity (if possible) and persist at most ``max_instruments``
    distinct ETF expense-ratio facts from one SBI TER workbook.

    ``identity_resolver(scheme_code) -> UUID | None`` must return an
    independently verified canonical ETF instrument UUID for that exact
    NSDL scheme code, or None if no trustworthy mapping exists. The default
    resolver always returns None, matching the current, honest state of
    this repository: no SBI scheme-code-to-ISIN-to-UUID mapping has been
    independently verified (see the module docstring).
    """

    def __init__(self, identity_resolver: Callable[[str], UUID | None] = lambda code: None,
                 *, max_instruments: int = 3, clock: Callable[[], datetime] | None = None):
        self._identity_resolver = identity_resolver
        self._max_instruments = max_instruments
        self._clock = clock or datetime.now

    def acquire(self, persistence, workbook_path: str | Path, *, source_identity: str,
                retrieved_at: datetime) -> list[SbiTerAdapterResult]:
        staged = build_staged_observations(workbook_path)
        results: list[SbiTerAdapterResult] = []
        resolved_instruments: set[UUID] = set()
        for observation in staged:
            instrument_id = self._identity_resolver(observation.scheme_code)
            if instrument_id is None:
                results.append(SbiTerAdapterResult(
                    scheme_code=observation.scheme_code, scheme_name=observation.scheme_name,
                    outcome=AdapterOutcome.IDENTITY_UNRESOLVED_STAGED_ONLY))
                continue
            resolved_instruments.add(instrument_id)
            if len(resolved_instruments) > self._max_instruments:
                raise ValueError("BOUNDED_ROLLOUT_INSTRUMENT_LIMIT_EXCEEDED")
            results.append(self._ingest_one(persistence, observation, instrument_id,
                                              source_identity=source_identity, retrieved_at=retrieved_at))
        return results

    def _ingest_one(self, persistence, observation: StagedTerObservation, instrument_id: UUID,
                     *, source_identity: str, retrieved_at: datetime) -> SbiTerAdapterResult:
        provenance = EtfProvenance(
            provider="SBI_MUTUAL_FUND", source_type="SBI_MF_TER_DISCLOSURE_WORKBOOK",
            source_identity=f"{source_identity}#{observation.scheme_code}#{observation.ter_date.isoformat()}",
            source_url="https://www.sbimf.com/docs/default-source/ter_allschemes/current-year-ter.xlsx",
            source_locator=f"scheme_code:{observation.scheme_code}",
            source_metadata={"scheme_name": observation.scheme_name,
                              "direct_plan_applicable": observation.direct_plan_applicable},
            retrieved_at=retrieved_at, authority=EtfAuthority.OFFICIAL_FUND, reliability_level="LEVEL_B")
        fact = EtfFact(instrument_id=instrument_id, metric=EtfMetric.EXPENSE_RATIO,
                        value=observation.regular_total_ter_percent, unit="percent",
                        as_of_date=observation.ter_date, provenance=provenance)
        existing = persistence.etf_facts(instrument_id, EtfMetric.EXPENSE_RATIO)
        for old in existing:
            if old.as_of_date == fact.as_of_date and old.provenance.authority == EtfAuthority.OFFICIAL_FUND:
                if old.value == fact.value and old.unit == fact.unit:
                    return SbiTerAdapterResult(scheme_code=observation.scheme_code,
                        scheme_name=observation.scheme_name, outcome=AdapterOutcome.ALREADY_INGESTED,
                        instrument_id=instrument_id, fact=old)
                return SbiTerAdapterResult(scheme_code=observation.scheme_code,
                    scheme_name=observation.scheme_name,
                    outcome=AdapterOutcome.CONFLICTING_OFFICIAL_METRIC_FOR_PERIOD, instrument_id=instrument_id)
        attempt = EtfAcquisitionAttempt(instrument_id=instrument_id, metric=str(EtfMetric.EXPENSE_RATIO),
            provider="SBI_MUTUAL_FUND", attempted_at=retrieved_at,
            outcome=EtfAcquisitionOutcome.SUCCESS_WITH_DATA, reason="EXPLICIT_SOURCE_EVIDENCE",
            evidence_ids=[evidence_id(fact)])
        persistence.save_etf_acquisition([fact], attempt)
        return SbiTerAdapterResult(scheme_code=observation.scheme_code, scheme_name=observation.scheme_name,
                                    outcome=AdapterOutcome.INGESTED, instrument_id=instrument_id, fact=fact)
