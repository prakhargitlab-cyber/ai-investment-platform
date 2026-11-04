"""Offline coverage for the explicit SBI live-smoke identity inventory."""
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.sbi_ter_live_smoke import make_staging_smoke_report
from app.sbi_ter_workbook_adapter import AdapterOutcome, SbiTerAdapterResult


def acquired(*rows):
    return SimpleNamespace(
        source_url="https://www.sbimf.com/docs/default-source/ter_allschemes/current-year-ter.xlsx",
        retrieved_at=datetime(2026, 11, 4, 12, tzinfo=timezone.utc),
        sha256="b" * 64, bytes_downloaded=12345, results=rows
    )


def stage(code, name):
    return SbiTerAdapterResult(
        scheme_code=code, scheme_name=name,
        outcome=AdapterOutcome.IDENTITY_UNRESOLVED_STAGED_ONLY,
    )


def test_default_smoke_report_has_no_scheme_names_or_metrics():
    report = make_staging_smoke_report(acquired(stage("SBI/2", "SBI Example ETF")))
    assert report["staged_scheme_count"] == 1
    assert report["production_evidence_written"] is False
    assert report["verified_identity_mapping"] is False
    assert "staged_schemes" not in report
    assert "identity_inventory_status" not in report
    assert report["source_sha256"] == "b" * 64


def test_opt_in_inventory_prints_exact_codes_sorted_without_inferring_isin():
    report = make_staging_smoke_report(
        acquired(stage("SBI/2", "SBI Secondary ETF"), stage("SBI/1", "SBI Example ETF")),
        include_staged_schemes=True,
    )
    assert report["staged_scheme_count"] == 2
    assert report["identity_inventory_status"] == "OFFICIAL_TER_CODES_ONLY_NO_ISIN_MAPPING"
    assert [row["nsdl_scheme_code"] for row in report["staged_schemes"]] == ["SBI/1", "SBI/2"]
    assert report["staged_schemes"][0]["scheme_name_for_review_only"] == "SBI Example ETF"
    assert all(row["isin"] is None and row["canonical_instrument_id"] is None
               and row["identity_verified"] is False for row in report["staged_schemes"])
    assert all("ter" not in key.lower() and "expense" not in key.lower()
               for row in report["staged_schemes"] for key in row)


@pytest.mark.parametrize("rows", [
    (),
    (SbiTerAdapterResult("SBI/1", "Fund", AdapterOutcome.INGESTED, instrument_id=UUID(int=1)),),
    (SbiTerAdapterResult("SBI/1", "Fund", AdapterOutcome.ALREADY_INGESTED),),
    (stage("SBI/1", "Fund"), stage("SBI/1", "Duplicate")),
    (stage("", "Empty code"),),
])
def test_invalid_or_resolved_source_never_prints_inventory(rows):
    with pytest.raises(ValueError, match="SBI_TER_SMOKE_"):
        make_staging_smoke_report(acquired(*rows), include_staged_schemes=True)


def test_existing_in_memory_report_contains_no_production_database_reference():
    report = make_staging_smoke_report(acquired(stage("SBI/1", "Fund")), include_staged_schemes=True)
    assert report["database"] == "IN_MEMORY_ONLY"
    assert report["production_evidence_written"] is False
