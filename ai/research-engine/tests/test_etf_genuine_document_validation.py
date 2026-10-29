"""Genuine-document validation for the two user-supplied AMC product-note PDFs.

Continues the Codex ETF Radar document-validation task after it hit its
usage limit. Uses the ACTUAL downloaded PDFs and their saved extraction
artifacts under .tmp/etf-validation/ (gitignored, local-only; these tests
skip cleanly wherever that directory is absent -- CI included), plus the
offline NSE identity evidence already independently re-derived from the
2026-10-18 local PostgreSQL backup (see .tmp/etf-validation/
nse-identity-manifest.json and export_nse_identity.py, which parses the
raw PGDMP archive itself with no DB connection). The identity constants
below are copied verbatim from that manifest -- never invented.

Nothing here performs a network call, a live Radar cycle, or a production
database write. Persistence is an in-memory SqliteResearchPersistence,
discarded at the end of each test.
"""
import json
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest

from app.etf_acquisition import EtfAcquisitionService
from app.etf_document_discovery import OfficialDocumentRegistry, register_official_document
from app.etf_evidence import EtfListing, EtfMetric, EtfProvenance
from app.models import EtfResearchProfile, ResearchDocument
from app.persistence import SqliteResearchPersistence

VALIDATION_DIR = Path(__file__).resolve().parents[3] / ".tmp" / "etf-validation"

# Verbatim from .tmp/etf-validation/nse-identity-manifest.json, itself
# independently re-derived (byte-for-byte, re-verified in this task) from
# backups/postgres/2026-10-18_004011_334c80/database.dump -- not invented.
IDENTITIES = {
    "NIFTYBEES": {
        "isin": "INF204KB14I2",
        "official_security_name": "NIPINDETFNIFTYBEES",
        "canonical_instrument_uuid": "805e0ebc-ea95-4859-b739-bdfbf5db327e",
    },
    "JUNIORBEES": {
        "isin": "INF732E01045",
        "official_security_name": "NIP IND ETF JUNIOR BEES",
        "canonical_instrument_uuid": "526e78c2-a5af-46e0-af0e-af470ca966ca",
    },
}

# The exact, user-reported SHA-256 of each downloaded PDF -- this test
# fails if the file on disk has since changed, which would mean the
# "verified" document is no longer the one actually inspected.
EXPECTED_PDF_SHA256 = {
    "NIFTYBEES": "06b71b85aceb99cbd101605b26714ce34cd5ce78a5011d8503cfa5d5bdb8c8f8",
    "JUNIORBEES": "f0159f3840e10147a51e340dfa83cad6dfdcac9c1d63635c8064ae50e3e92c56",
}

NOW = datetime(2026, 10, 28, 16, 0, tzinfo=timezone.utc)
DOCUMENT_URL = {
    "NIFTYBEES": "https://mf.nipponindiaim.com/FundsAndPerformance/ProductNotes/NipponIndia-ETF-Nifty-50-BeES-Jun-2026.pdf",
    "JUNIORBEES": "https://mf.nipponindiaim.com/FundsAndPerformance/ProductNotes/NipponIndia-ETF-Nifty-Next-50-Junior-BeES-Mar-2026.pdf",
}


def _skip_if_missing():
    if not VALIDATION_DIR.is_dir():
        pytest.skip(".tmp/etf-validation is local-only and gitignored; not present in this checkout")


@pytest.fixture(scope="module")
def extraction():
    _skip_if_missing()
    out = {}
    for symbol in IDENTITIES:
        path = VALIDATION_DIR / f"{symbol}.extraction.json"
        if not path.is_file():
            pytest.skip(f"{path} not present in this checkout")
        out[symbol] = json.loads(path.read_text(encoding="utf-8"))
    return out


def test_original_pdf_file_hashes_match_the_reported_hashes():
    """The exact bytes on disk are still the ones originally hashed and reported."""
    _skip_if_missing()
    for symbol, expected in EXPECTED_PDF_SHA256.items():
        pdf_path = VALIDATION_DIR / f"{symbol}-official.pdf"
        if not pdf_path.is_file():
            pytest.skip(f"{pdf_path} not present in this checkout")
        actual = sha256(pdf_path.read_bytes()).hexdigest()
        assert actual == expected, f"{symbol} PDF hash changed on disk since original validation"
    # The two documents are genuinely different files, not the same PDF renamed twice.
    assert EXPECTED_PDF_SHA256["NIFTYBEES"] != EXPECTED_PDF_SHA256["JUNIORBEES"]


def test_extracted_text_integrity_matches_its_own_recorded_hash(extraction):
    """Catches silent corruption/edit of the saved extraction artifact itself."""
    for symbol, payload in extraction.items():
        text = payload["text"]
        assert sha256(text.encode("utf-8")).hexdigest() == payload["text_sha256"]
        assert payload["filename_is_identity_evidence"] is False, (
            "A user-assigned filename must never be treated as proof of document identity")


def test_each_document_prints_its_own_expected_nse_symbol_and_no_cross_contamination(extraction):
    """Each PDF is scheme-specific: it names its own ETF and never the other one."""
    nifty_text, junior_text = extraction["NIFTYBEES"]["text"], extraction["JUNIORBEES"]["text"]
    assert "NSE Symbol NIFTYBEES" in nifty_text
    assert "NSE Symbol JUNIORBEES" in junior_text
    assert "JUNIORBEES" not in nifty_text
    assert "NIFTYBEES" not in junior_text


def test_documents_contain_no_extractable_isin_aum_or_ter(extraction):
    """Re-verifies Codex's finding directly against the real extracted text,
    using the SAME identifier shape the production parser looks for
    (app.etf_document_parser: a bare ISIN-shaped token, or a labelled
    "ISIN:" line), not a loose substring search that would false-positive
    on words like "arising" (which contains the substring "isin")."""
    import re
    for symbol, payload in extraction.items():
        text = payload["text"]
        isin_like = re.findall(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b", text)
        assert isin_like == [], f"{symbol}: unexpected ISIN-shaped token(s): {isin_like}"
        labelled_isin = re.findall(r"(?im)^\s*ISIN\s*:\s*(\S+)\s*$", text)
        assert labelled_isin == [], f"{symbol}: unexpected labelled ISIN line"
        assert not re.search(r"\bAUM\b", text, re.I), f"{symbol}: unexpected AUM label"
        assert not re.search(r"\bTER\b", text), f"{symbol}: unexpected standalone TER label"
        assert not re.search(r"(?i)expense ratio", text), f"{symbol}: unexpected Expense Ratio label"
        # No ISO-format date appears anywhere (the parser's metric dates are
        # ISO-only), so there is no reporting-style date in this text that
        # could even be mistaken for an AUM/TER effective date.
        assert re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text) == []


def _profile(symbol):
    ident = IDENTITIES[symbol]
    return EtfResearchProfile(instrument_id=UUID(ident["canonical_instrument_uuid"]),
        fund_id=UUID(ident["canonical_instrument_uuid"]), fund_name=ident["official_security_name"],
        ticker=symbol, exchange="NSE", mic="XNSE", isin=ident["isin"], currency="INR",
        known_domains=["mf.nipponindiaim.com"])


def _seeded_store(symbol):
    store = SqliteResearchPersistence(":memory:")
    ident = IDENTITIES[symbol]
    store.save_etf_evidence(EtfListing(instrument_id=UUID(ident["canonical_instrument_uuid"]),
        isin=ident["isin"], symbol=symbol, name=ident["official_security_name"],
        provenance=EtfProvenance(provider="NSE", source_type="NSE_OFFICIAL_ETF_SECURITY_LIST",
            source_identity=f"fixture-list-{symbol}",
            source_url="https://nsearchives.nseindia.com/content/equities/eq_etfseclist.csv",
            authority="OFFICIAL_EXCHANGE", reliability_level="LEVEL_A", retrieved_at=NOW)))
    return store


def _registry_and_document(profile_symbol, text, document_symbol):
    """Registers `text` (belonging to `document_symbol`'s real PDF) as if it
    were being offered as evidence for `profile_symbol` -- same URL/ISIN
    pairing either way, so a wrong-document test looks identical to a
    caller except for which text was actually extracted."""
    ident = IDENTITIES[profile_symbol]
    url = DOCUMENT_URL[document_symbol]
    digest = sha256(text.encode("utf-8")).hexdigest()
    registry = OfficialDocumentRegistry()
    register_official_document(registry=registry, instrument_id=UUID(ident["canonical_instrument_uuid"]),
        document_url=url, canonical_url=url, source_name="Nippon India Mutual Fund product note",
        scheme_isin=ident["isin"], approval_reference="manual-offline-review-2026-10-28",
        normalized_text_sha256=digest, retrieved_at=NOW, published_at=NOW,
        source_type="COMPANY_WEBSITE", source_classification="OFFICIAL_COMPANY",
        document_type="PDF_REFERENCE", authority="OFFICIAL_FUND", reliability_level="LEVEL_A")
    document = ResearchDocument(instrument_id=UUID(ident["canonical_instrument_uuid"]), canonical_url=url,
        original_url=url, source_type="COMPANY_WEBSITE", source_classification="OFFICIAL_COMPANY",
        source_name="Nippon India Mutual Fund product note", published_at=NOW, retrieved_at=NOW,
        content_type="application/pdf", document_type="PDF_REFERENCE", normalized_text=text,
        content_hash=digest, status="PROCESSED", reliability_level="LEVEL_A", source_mode="REAL")
    return registry, document


@pytest.mark.parametrize("profile_symbol,document_symbol", [
    ("NIFTYBEES", "NIFTYBEES"),   # correct pairing
    ("JUNIORBEES", "JUNIORBEES"), # correct pairing
    ("NIFTYBEES", "JUNIORBEES"),  # wrong document for this ETF
    ("JUNIORBEES", "NIFTYBEES"),  # wrong document for this ETF
])
def test_real_document_never_produces_a_fabricated_aum_or_ter_fact(extraction, profile_symbol, document_symbol):
    """The headline finding: with the two PDFs AS THEY ACTUALLY ARE, no
    combination -- correct or wrong document -- ever reaches a persisted
    AUM/TER value. Both fail at the document-identity gate
    (verified_scheme_section), because neither PDF carries a labelled ISIN
    for ANY scheme. This is a fail-closed identity defect in the SOURCE
    DOCUMENTS, not a defect in the verification code: the gate runs, and
    runs correctly, before either document's symbol/name could even be
    compared -- so this does not yet exercise the wrong-symbol/wrong-name
    rejection branches (those remain covered only by the synthetic fixtures
    in test_etf_document_trust.py).
    """
    text = extraction[document_symbol]["text"]
    store = _seeded_store(profile_symbol)
    try:
        registry, document = _registry_and_document(profile_symbol, text, document_symbol)
        profile = _profile(profile_symbol)
        service = EtfAcquisitionService(store, clock=lambda: NOW, document_registry=registry)
        for metric in (EtfMetric.AUM, EtfMetric.EXPENSE_RATIO):
            attempt = service.acquire_official_document(profile, document, metric)
            assert attempt.outcome == "TECHNICAL_FAILURE"
            assert attempt.reason == "OFFICIAL_DOCUMENT_FAILED:MISSING_CONFLICTING_OR_AMBIGUOUS_SCHEME_ISIN"
            # No incorrect fact persistence, whichever document/profile pairing this is.
            assert store.etf_facts(profile.instrument_id, metric) == []
    finally:
        store._connection.close()


def test_wrong_document_is_rejected_at_least_as_strongly_as_the_correct_one(extraction):
    """Explicit wrong-ETF-rejection check for Task 2: swapping in the OTHER
    ETF's real document must never succeed where the correct document also
    failed, and must never produce a different (weaker) outcome."""
    correct_outcomes, wrong_outcomes = [], []
    for profile_symbol, document_symbol, bucket in [
        ("NIFTYBEES", "NIFTYBEES", correct_outcomes),
        ("JUNIORBEES", "JUNIORBEES", correct_outcomes),
        ("NIFTYBEES", "JUNIORBEES", wrong_outcomes),
        ("JUNIORBEES", "NIFTYBEES", wrong_outcomes),
    ]:
        text = extraction[document_symbol]["text"]
        store = _seeded_store(profile_symbol)
        try:
            registry, document = _registry_and_document(profile_symbol, text, document_symbol)
            profile = _profile(profile_symbol)
            service = EtfAcquisitionService(store, clock=lambda: NOW, document_registry=registry)
            attempt = service.acquire_official_document(profile, document, EtfMetric.AUM)
            bucket.append(attempt.outcome)
        finally:
            store._connection.close()
    assert set(correct_outcomes) == {"TECHNICAL_FAILURE"}
    assert set(wrong_outcomes) == {"TECHNICAL_FAILURE"}
    # Never a case where the wrong document "succeeds" while the correct one fails.
    assert "SUCCESS_WITH_DATA" not in wrong_outcomes
