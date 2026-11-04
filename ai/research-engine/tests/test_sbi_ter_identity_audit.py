"""Identity audit is offline, fail-closed, and never joins on fund names."""
from dataclasses import dataclass
from datetime import date
from uuid import UUID
import pytest

from app.sbi_ter_identity import ReviewedSbiSchemeIdentity
from app.sbi_ter_identity_audit import audit_staged_sbi_schemes

ID = UUID(int=5)
ISIN = "INF200A01026"


@dataclass(frozen=True)
class Staged:
    scheme_code: str
    scheme_name: str


def canonical():
    return [dict(globalInstrumentId=str(ID), assetType="ETF", status="ACTIVE",
                 primaryExchange="NSE", isin=ISIN, primarySymbol="SBIETFX")]


def reviewed():
    return ReviewedSbiSchemeIdentity(
        scheme_code="CODE_1", isin=ISIN, ticker="SBIETFX",
        evidence_url="https://www.sbimf.com/verified-document.pdf",
        evidence_sha256="a" * 64, reviewed_by="Test Reviewer",
        reviewed_on=date.today(), approval_reference="SYNTHETIC_TEST_ONLY",
    )


def test_no_crosswalk_all_staged_only_despite_similar_names():
    rows = audit_staged_sbi_schemes(
        [Staged("CODE_1", "SBI Nifty ETF"), Staged("CODE_2", "SBI Nifty ETF")],
        [], canonical())
    assert len(rows) == 2
    assert all(row.status == "IDENTITY_UNRESOLVED_STAGED_ONLY"
               and row.canonical_instrument_id is None for row in rows)


def test_only_reviewed_code_maps_and_name_does_not_override_mapping():
    rows = audit_staged_sbi_schemes(
        [Staged("CODE_1", "Completely Different Name"), Staged("CODE_2", "SBI Nifty ETF")],
        [reviewed()], canonical())
    assert rows[0].canonical_instrument_id == ID
    assert rows[0].reviewer_reference == "SYNTHETIC_TEST_ONLY"
    assert rows[1].canonical_instrument_id is None


def test_duplicate_staged_scheme_code_fails_closed():
    with pytest.raises(ValueError, match="DUPLICATE_STAGED_SBI_SCHEME_CODE"):
        audit_staged_sbi_schemes(
            [Staged("CODE_1", "One"), Staged("CODE_1", "Two")],
            [], canonical())


def test_invalid_reviewed_isin_fails_closed():
    with pytest.raises(ValueError, match="REVIEWED_IDENTITY_NOT_IN_CANONICAL_UNIVERSE"):
        audit_staged_sbi_schemes(
            [Staged("CODE_1", "SBI Nifty ETF")],
            [ReviewedSbiSchemeIdentity(
                scheme_code="CODE_1", isin="INF999999999", ticker="SBIETFX",
                evidence_url="https://www.sbimf.com/verified-document.pdf",
                evidence_sha256="a" * 64, reviewed_by="Test Reviewer",
                reviewed_on=date.today(), approval_reference="SYNTHETIC_TEST_ONLY",
            )],
            canonical())
