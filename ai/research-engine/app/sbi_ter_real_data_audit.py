"""Offline, read-only adapters for actual portfolio ETF API exports and SQLite NSE evidence.

Never create listings or populate canonical identities. Input JSON is a captured,
untrusted snapshot, not proof of source retrieval or production approval.
"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3

from app.etf_evidence import EtfAcquisitionAttempt, EtfListing
from app.sbi_ter_nse_circular_review import (
    SbiNseIdentityReviewCandidate, SbiNseReconciliationReport,
)

MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_CANONICAL_ROWS = 3000
MAX_LISTINGS = 20000
MAX_ATTEMPTS_PER_INSTRUMENT = 10000


def _read_json_file(path: Path):
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_INPUT_BYTES:
        raise ValueError("SBI_AUDIT_JSON_MISSING_OR_TOO_LARGE")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("SBI_AUDIT_JSON_INVALID") from exc


def read_reconciliation_report(path: Path) -> SbiNseReconciliationReport:
    """Reconstruct only the existing review-only contract; never elevate status."""
    raw = _read_json_file(path)
    if not isinstance(raw, dict) or raw.get("status") != "REVIEW_ONLY_NO_DIRECT_CODE_ISIN_PROOF" or raw.get("approved_for_persistence") is not False:
        raise ValueError("SBI_AUDIT_RECONCILIATION_NOT_REVIEW_ONLY")
    if raw.get("production_evidence_written") is not False or raw.get("database") != "IN_MEMORY_ONLY":
        raise ValueError("SBI_AUDIT_RECONCILIATION_SOURCE_NOT_STAGED")
    try:
        rows = raw["candidates"]
        if not isinstance(rows, list) or not 1 <= len(rows) <= 22:
            raise ValueError("SBI_AUDIT_RECONCILIATION_CANDIDATE_LIMIT")
        candidates = tuple(SbiNseIdentityReviewCandidate(
            nsdl_scheme_code=row["nsdl_scheme_code"],
            scheme_name=row["scheme_name"],
            isin=row["isin"],
            old_nse_symbol=row["old_nse_symbol"],
            new_nse_symbol=row["new_nse_symbol"],
            nse_circular_row=row["nse_circular_row"],
            nse_circular_page=row["nse_circular_page"],
            nse_pdf_sha256=row["nse_pdf_sha256"],
            ter_workbook_sha256=row["ter_workbook_sha256"],
            nse_effective_date=datetime.fromisoformat(row["nse_effective_date"]).date(),
            status=row["status"],
            approved_for_persistence=row["approved_for_persistence"],
        ) for row in rows)
        return SbiNseReconciliationReport(
            nse_pdf_sha256=raw["nse_pdf_sha256"],
            ter_workbook_sha256=raw["ter_workbook_sha256"],
            circular_reference=raw["nse_circular"],
            nse_effective_date=datetime.fromisoformat(raw["nse_effective_date"]).date(),
            candidate_rows=candidates,
            unmatched_scheme_codes=tuple(raw["unmatched_scheme_codes"]),
            status=raw["status"],
            approved_for_persistence=raw["approved_for_persistence"],
        )
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ValueError("SBI_AUDIT_RECONCILIATION_INVALID") from exc


def read_portfolio_etf_snapshot(path: Path) -> list[dict]:
    """Require a complete export of the actual GET /api/v1/instruments?assetType=ETF.

    Controller returns 'ticker' and 'exchange', not 'primarySymbol' and
    'primaryExchange' used by the existing canonical verifier.
    A single incomplete page is rejected to prevent false 'missing' outcomes.
    """
    payload = _read_json_file(path)
    if not isinstance(payload, dict):
        raise ValueError("SBI_AUDIT_CANONICAL_EXPORT_INVALID")
    try:
        page, size, total, instruments = (
            payload["page"], payload["size"], payload["totalElements"], payload["instruments"]
        )
        if (not isinstance(page, int) or isinstance(page, bool) or page != 0
                or not isinstance(size, int) or isinstance(size, bool) or size < 1
                or not isinstance(total, int) or isinstance(total, bool)
                or not 0 <= total <= MAX_CANONICAL_ROWS
                or not isinstance(instruments, list)
                or len(instruments) != total
                or len(instruments) > size):
            raise ValueError("SBI_AUDIT_CANONICAL_EXPORT_INCOMPLETE")
        normalized = []
        for row in instruments:
            if not isinstance(row, dict) or any(key not in row for key in (
                "globalInstrumentId", "isin", "ticker", "exchange", "status", "assetType"
            )):
                raise ValueError("SBI_AUDIT_CANONICAL_ROW_INVALID")
            normalized.append(dict(
                globalInstrumentId=row["globalInstrumentId"],
                isin=row["isin"], primarySymbol=row["ticker"],
                primaryExchange=row["exchange"], status=row["status"],
                assetType=row["assetType"],
            ))
        return normalized
    except (KeyError, TypeError) as exc:
        raise ValueError("SBI_AUDIT_CANONICAL_EXPORT_INVALID") from exc


class ReadOnlySqliteEtfEvidence:
    """Open existing local SQLite using mode=ro and SELECT only.

    Does NOT create database files/tables, write rows or load demo records.
    Does NOT support PostgreSQL. Close on CLI completion.
    """
    def __init__(self, path: Path):
        if not path.is_file():
            raise ValueError("SBI_AUDIT_RESEARCH_SQLITE_NOT_FOUND")
        self._connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        self._connection.row_factory = sqlite3.Row

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self._connection.close()

    def etf_listings(self):
        try:
            rows = self._connection.execute(
                "SELECT payload FROM etf_listing_observations WHERE source_mode='REAL' LIMIT ?",
                (MAX_LISTINGS + 1,),
            ).fetchall()
            if len(rows) > MAX_LISTINGS:
                raise ValueError("SBI_AUDIT_NSE_LISTING_LIMIT")
            return [EtfListing.model_validate_json(row["payload"]) for row in rows]
        except sqlite3.Error as exc:
            raise ValueError("SBI_AUDIT_NSE_LISTING_TABLE_UNAVAILABLE") from exc

    def etf_attempts(self, instrument_id):
        try:
            rows = self._connection.execute(
                "SELECT payload FROM etf_acquisition_attempts WHERE instrument_id=? "
                "AND metric='IDENTITY' AND provider='NSE' "
                "ORDER BY attempted_at DESC LIMIT ?",
                (str(instrument_id), MAX_ATTEMPTS_PER_INSTRUMENT + 1),
            ).fetchall()
            if len(rows) > MAX_ATTEMPTS_PER_INSTRUMENT:
                raise ValueError("SBI_AUDIT_NSE_ATTEMPT_LIMIT")
            return [EtfAcquisitionAttempt.model_validate_json(row["payload"]) for row in rows]
        except sqlite3.Error as exc:
            raise ValueError("SBI_AUDIT_NSE_ATTEMPT_TABLE_UNAVAILABLE") from exc
