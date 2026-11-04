"""Offline unit tests; ALL mappings are synthetic and NOT verified SBI links."""
from datetime import date, timedelta
from uuid import UUID

import pytest
from app.sbi_ter_identity import ReviewedSbiSchemeIdentity, build_reviewed_sbi_resolver

ID = UUID(int=1)
ISIN = "INF200A01026"
CODE = "SBIM/O/O/EET/1"  # synthetic mapping for tests only


def canonical(**kw):
    row = dict(globalInstrumentId=str(ID), assetType="ETF", status="ACTIVE",
               primaryExchange="NSE", isin=ISIN, primarySymbol="SBIETFX")
    row.update(kw)
    return row


def reviewed(**kw):
    row = dict(scheme_code=CODE, isin=ISIN, ticker="SBIETFX",
               evidence_url="https://www.sbimf.com/reviewed-identity.pdf",
               evidence_sha256="a" * 64, reviewed_by="Example Reviewer",
               reviewed_on=date.today(), approval_reference="TEST-ONLY")
    row.update(kw)
    return ReviewedSbiSchemeIdentity(**row)


def test_exact_reviewed_crosswalk_and_unknown_code():
    resolver = build_reviewed_sbi_resolver([reviewed()], [canonical()])
    assert resolver(CODE) == ID
    assert resolver(" " + CODE + " ") == ID
    assert resolver("NOT_IN_CROSSWALK") is None


@pytest.mark.parametrize("change,reason", [
    ({"isin": "INF999999999"}, "REVIEWED_IDENTITY_NOT_IN_CANONICAL_UNIVERSE"),
    ({"ticker": "SBIETFY"}, "REVIEWED_IDENTITY_NOT_IN_CANONICAL_UNIVERSE"),
    ({"evidence_sha256": ""}, "IDENTITY_EVIDENCE_HASH_INVALID"),
    ({"evidence_url": "http://example.org/file"}, "IDENTITY_EVIDENCE_URL_INVALID"),
    ({"approval_reference": ""}, "IDENTITY_APPROVAL_MISSING"),
    ({"reviewed_on": date.today() + timedelta(days=1)}, "IDENTITY_REVIEW_DATE_INVALID"),
])
def test_bad_review_refused(change, reason):
    with pytest.raises(ValueError, match=reason):
        build_reviewed_sbi_resolver([reviewed(**change)], [canonical()])


def test_inactive_or_non_etf_canonical_refused():
    with pytest.raises(ValueError, match="CANONICAL_NOT_ACTIVE_NSE_ETF"):
        build_reviewed_sbi_resolver([reviewed()], [canonical(status="INACTIVE")])


def test_duplicate_code_rejected_even_when_same_mapping():
    with pytest.raises(ValueError, match="DUPLICATE_SBI_SCHEME_CODE"):
        build_reviewed_sbi_resolver([reviewed(), reviewed()], [canonical()])


def test_two_codes_to_one_etf_rejected():
    with pytest.raises(ValueError, match="MULTIPLE_SBI_SCHEME_CODES_FOR_CANONICAL_ETF"):
        build_reviewed_sbi_resolver([reviewed(), reviewed(scheme_code="OTHER")], [canonical()])


def test_ambiguous_canonical_identity_rejected():
    with pytest.raises(ValueError, match="CANONICAL_IDENTITY_AMBIGUOUS"):
        build_reviewed_sbi_resolver(
            [reviewed()], [canonical(), canonical(globalInstrumentId=str(UUID(int=2)))],
        )


def test_no_reviewed_crosswalk_never_resolves_any_sbi_code():
    resolver = build_reviewed_sbi_resolver([], [canonical()])
    assert resolver(CODE) is None
