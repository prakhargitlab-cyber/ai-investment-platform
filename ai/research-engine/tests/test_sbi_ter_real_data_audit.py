"""Read-only integration adapter tests; all identities in fixture are synthetic."""
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import UUID

import pytest

from app.etf_evidence import EtfAuthority, EtfListing, EtfProvenance
from app.sbi_ter_real_data_audit import (
    ReadOnlySqliteEtfEvidence, read_portfolio_etf_snapshot, read_reconciliation_report,
)
from app.sbi_ter_real_data_audit_cli import run

ID = UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime(2026, 11, 4, tzinfo=timezone.utc)


def write_json(path, obj):
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


def canonical_export(ticker="TESTETF", count=1, total=1):
    return {
        "page": 0, "size": 500, "totalElements": total,
        "instruments": [
            dict(globalInstrumentId=str(ID), assetType="ETF", status="ACTIVE",
                 exchange="NSE", ticker=ticker, isin="INF999999999")
            for _ in range(count)
        ],
    }


def review_export():
    return dict(
        status="REVIEW_ONLY_NO_DIRECT_CODE_ISIN_PROOF",
        approved_for_persistence=False, database="IN_MEMORY_ONLY",
        production_evidence_written=False, nse_circular="NSE/CML/77115",
        nse_pdf_sha256="a" * 64, ter_workbook_sha256="b" * 64,
        nse_effective_date="2026-10-30",
        unmatched_scheme_codes=[],
        candidates=[dict(
            nsdl_scheme_code="SYNTHETIC/CODE", scheme_name="SBI Example",
            isin="INF999999999", old_nse_symbol="OLDTEST",
            new_nse_symbol="TESTETF", nse_circular_row=8, nse_circular_page=1,
            nse_pdf_sha256="a" * 64, ter_workbook_sha256="b" * 64,
            nse_effective_date="2026-10-30",
            status="REVIEW_ONLY_EXACT_NAME_JOIN_NOT_CODE_ISIN_PROOF",
            approved_for_persistence=False,
        )],
    )


def sqlite_db(path, listing=None):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE etf_listing_observations (payload TEXT, source_mode TEXT);
        CREATE TABLE etf_acquisition_attempts (
            instrument_id TEXT, metric TEXT, provider TEXT,
            attempted_at TEXT, payload TEXT
        );
    """)
    if listing is not None:
        conn.execute("INSERT INTO etf_listing_observations VALUES (?, ?)",
                     (listing.model_dump_json(), "REAL"))
    conn.commit()
    conn.close()
    return path


def sample_listing():
    return EtfListing(
        isin="INF999999999", symbol="TESTETF", name="SBI Example",
        instrument_id=ID, provenance=EtfProvenance(
            provider="NSE", source_type="EXCHANGE_LISTING",
            source_identity="NSE:ETF:SYNTHETIC",
            source_url="https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
            authority=EtfAuthority.OFFICIAL_EXCHANGE,
            source_mode="REAL", retrieved_at=NOW,
        ),
    )


def test_real_controller_field_names_are_normalized(tmp_path):
    result = read_portfolio_etf_snapshot(write_json(tmp_path / "canonical.json", canonical_export()))
    assert result[0]["primarySymbol"] == "TESTETF"
    assert result[0]["primaryExchange"] == "NSE"
    assert result[0]["globalInstrumentId"] == str(ID)


@pytest.mark.parametrize("change", [
    {"page": 1}, {"totalElements": 2}, {"size": 0},
    {"instruments": []}, {"totalElements": 3001},
])
def test_partial_or_invalid_canonical_snapshot_rejected(tmp_path, change):
    with pytest.raises(ValueError, match="CANONICAL_EXPORT_INCOMPLETE"):
        read_portfolio_etf_snapshot(write_json(tmp_path / "bad.json",
                                               {**canonical_export(), **change}))


def test_review_only_json_is_rehydrated_not_approved(tmp_path):
    result = read_reconciliation_report(write_json(tmp_path / "review.json", review_export()))
    assert result.candidate_rows[0].isin == "INF999999999"
    assert result.approved_for_persistence is False


def test_promoted_review_json_rejected(tmp_path):
    bad = review_export()
    bad["approved_for_persistence"] = True
    with pytest.raises(ValueError, match="NOT_REVIEW_ONLY"):
        read_reconciliation_report(write_json(tmp_path / "review.json", bad))


def test_sqlite_is_opened_read_only_and_absence_does_not_create_file(tmp_path):
    p = tmp_path / "missing.db"
    with pytest.raises(ValueError, match="SQLITE_NOT_FOUND"):
        ReadOnlySqliteEtfEvidence(p)
    assert not p.exists()


def test_genuine_sqlite_models_read_without_modification(tmp_path):
    p = sqlite_db(tmp_path / "research.db", sample_listing())
    before = p.read_bytes()
    with ReadOnlySqliteEtfEvidence(p) as store:
        assert len(store.etf_listings()) == 1
        assert store.etf_attempts(ID) == []
        with pytest.raises(sqlite3.OperationalError):
            store._connection.execute("CREATE TABLE forbidden (id INT)")
    assert p.read_bytes() == before


def test_integrated_no_network_review_only_report(tmp_path):
    review = write_json(tmp_path / "review.json", review_export())
    canonical = write_json(tmp_path / "canonical.json", canonical_export())
    database = sqlite_db(tmp_path / "research.db", sample_listing())
    output = run(review, canonical, database, now=NOW)
    assert output["candidate_count"] == 1
    assert output["outcomes"] == {"VERIFIED_CANONICAL_CANDIDATE": 1}
    assert output["rows"][0]["canonical_instrument_id"] == str(ID)
    assert output["approved_for_persistence"] is False
    assert output["ter_fact_written"] is False


def test_integrated_missing_persisted_listing_is_reported(tmp_path):
    review = write_json(tmp_path / "review.json", review_export())
    canonical = write_json(tmp_path / "canonical.json", canonical_export())
    database = sqlite_db(tmp_path / "research.db")
    output = run(review, canonical, database, now=NOW)
    assert output["outcomes"] == {"MISSING_NSE_LISTING": 1}
