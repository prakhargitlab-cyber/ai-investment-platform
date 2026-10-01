"""Focused tests for the PDF-structure double-computation fix.

Scope: ``ResearchFetcher`` (app/research_fetching.py) already builds
``FetchResult.pdf_structure`` once via ``preserve_pdf_structure(text)`` for
``application/pdf`` responses -- from the exact same ``text`` that becomes
``FetchResult.text``. ``_prepare_ingested_document`` previously called
``preserve_pdf_structure(body)`` a SECOND time on that identical string
during ingestion, rebuilding the whole ``PdfTextStructure`` for no reason.

It now accepts an optional ``precomputed_pdf_structure`` keyword and reuses
it BY IDENTITY when supplied (see app/repository.py's
``_prepare_ingested_document`` / ``_ingest_registered_fetch_result_async``),
falling back to building it exactly as before when nothing is supplied.
``preserve_pdf_structure`` is a pure function of its text argument and every
``PdfTextStructure``/``PdfTextPage``/``PdfTextLine``/``SourceRegion`` is a
frozen dataclass, so identity-reuse is provably equivalent to rebuilding --
these tests confirm that in practice, plus that no other behavior moved.

Provider-free: no real NSE/Yahoo/search calls, no network, no opportunity
cycle, no DB reset -- pure unit-level exercise of ResearchRepository with an
in-memory SqliteResearchPersistence() and a fake fetcher.
"""
from __future__ import annotations

from dataclasses import replace as dc_replace
from uuid import UUID

import pytest

import app.pdf_structure as pdf_structure_module
from app.financial_structure import parse_financial_structure
from app.models import (CompanyResearchProfile, DocumentStatus, ReliabilityLevel, SourceClassification,
                        SourceMode, SourceType)
from app.pdf_structure import preserve_pdf_structure
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository
from app.research_fetching import FetchResult
from app.settings import Settings
from test_di11c_official_document_budget import _official_source

DATES = '31.03.2026 31.12.2025 31.03.2025 | 31.03.2026 31.03.2025'
HEADER = 'Quarter ended | Year ended\n' + DATES + '\nAudited Unaudited Audited | Audited Audited'


def _financial_pdf_text() -> str:
    body = f'Statement of Standalone financial results\nParticulars\n{HEADER}\nRevenue from operations 1.01 2.02'
    return f'[PDF_PAGE 1]\n{body}\n[PDF_PAGE 2]\nNotes to accounts'


def _profile() -> CompanyResearchProfile:
    return CompanyResearchProfile(
        instrument_id=UUID(int=7010), company_id=UUID(int=7020), company_name="Example Structure Reuse Limited",
        isin="INE000SR1010", ticker="EXSTRR", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        provider_instrument_ids={"NSE": "EXSTRR"},
    )


def _repo() -> ResearchRepository:
    repo = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_demo_enabled=False),
        persistence=SqliteResearchPersistence(),
    )
    repo.profiles = [_profile()]
    return repo


def _prepare_kwargs(**overrides):
    kwargs = dict(
        original_url="https://example.test/structural.pdf", source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
        source_name="fixture", publisher="fixture", content_type="application/pdf",
        reliability=ReliabilityLevel.LEVEL_A, published_at=None, source_mode=SourceMode.DEMO,
        source_classification=SourceClassification.EXCHANGE, discovered_at=None,
        discovery_provider=None, expected_profile=None, document_status=DocumentStatus.PARSED,
        allow_empty_content=False, trusted_profile_identity=None,
    )
    kwargs.update(overrides)
    return kwargs


def _tracking_preserve_pdf_structure(monkeypatch, calls):
    original = pdf_structure_module.preserve_pdf_structure

    def _tracking(*args, **kw):
        calls.append(args)
        return original(*args, **kw)

    monkeypatch.setattr(pdf_structure_module, "preserve_pdf_structure", _tracking)


# 1. Precomputed structure is reused BY IDENTITY, not reconstructed --------
def test_precomputed_pdf_structure_is_reused_by_identity(monkeypatch):
    repo = _repo()
    text = _financial_pdf_text()
    precomputed = preserve_pdf_structure(text)

    calls: list = []
    _tracking_preserve_pdf_structure(monkeypatch, calls)

    document = repo._prepare_ingested_document(
        **_prepare_kwargs(body=text, precomputed_pdf_structure=precomputed))

    assert document.pdf_structure is precomputed  # identity, not equality
    assert calls == []  # preserve_pdf_structure never invoked -- reused, not rebuilt


def test_precomputed_pdf_structure_wins_even_when_it_disagrees_with_body():
    # A stronger identity proof: hand in a precomputed structure built from
    # DIFFERENT text than `body`. If the code ever fell back to rebuilding
    # from body, the returned structure's extracted_text would equal body
    # instead of the precomputed structure's own (different) text.
    repo = _repo()
    body_text = _financial_pdf_text()
    other_text = "[PDF_PAGE 1]\nCompletely unrelated precomputed document text"
    precomputed = preserve_pdf_structure(other_text)

    document = repo._prepare_ingested_document(
        **_prepare_kwargs(body=body_text, precomputed_pdf_structure=precomputed))

    assert document.pdf_structure is precomputed
    assert document.pdf_structure.extracted_text == other_text
    assert document.pdf_structure.extracted_text != body_text
    # The durable flattened-text contract is untouched: normalized_text still
    # comes from the real `body`, never from the (unrelated) precomputed text.
    from app.normalization import normalize_text
    assert document.normalized_text == normalize_text(body_text)


# 2. No precomputed structure supplied -> fallback still builds it ---------
def test_fallback_still_builds_structure_when_not_supplied(monkeypatch):
    repo = _repo()
    text = _financial_pdf_text()

    calls: list = []
    _tracking_preserve_pdf_structure(monkeypatch, calls)

    document = repo._prepare_ingested_document(**_prepare_kwargs(body=text))

    assert len(calls) == 1
    assert calls[0] == (text,)
    assert document.pdf_structure is not None
    assert document.pdf_structure.extracted_text == text
    assert document.pdf_structure == preserve_pdf_structure(text)


# 3. Non-PDF content_type is unaffected: pdf_structure stays None -----------
@pytest.mark.parametrize("supply_precomputed", [False, True])
def test_non_pdf_content_type_pdf_structure_stays_none(supply_precomputed):
    repo = _repo()
    text = "<html><body>Not a PDF, has plenty of content for extraction and normalization to succeed.</body></html>"
    precomputed = preserve_pdf_structure(_financial_pdf_text()) if supply_precomputed else None

    document = repo._prepare_ingested_document(
        **_prepare_kwargs(content_type="text/html", body=text, precomputed_pdf_structure=precomputed))

    assert document.pdf_structure is None


# 4. Integration: the real ingestion path reuses FetchResult.pdf_structure
#    and financial parsing/structure behavior is unchanged -----------------
@pytest.mark.asyncio
async def test_real_ingestion_path_reuses_fetch_result_structure_and_preserves_financial_parsing():
    repo = _repo()
    profile = _profile()
    text = _financial_pdf_text()
    precomputed = preserve_pdf_structure(text)

    class _Fetcher:
        async def fetch(self, url):
            return FetchResult(final_url=url, status_code=200, content_type="application/pdf",
                                text=text, bytes_read=len(text), pdf_structure=precomputed)

    repo._fetcher = _Fetcher()
    source = dc_replace(
        _official_source(profile, "financial-results", "FINANCIAL_RESULTS"),
        official_nse_profile_symbol=profile.provider_instrument_ids["NSE"],
    )

    document = await repo._fetch_registered_source(profile, source, expected_profile=profile)

    # The exact object built once inside the fetcher survives all the way
    # through ingestion -- never rebuilt, never copied.
    assert document.pdf_structure is precomputed

    # Financial parsing/structure behavior is unaffected by the reuse: the
    # same accepted statements come out whether parsed from a freshly built
    # structure or from the identity-reused one attached to the document.
    direct = parse_financial_structure(structure=preserve_pdf_structure(text))
    via_document = parse_financial_structure(structure=document.pdf_structure)
    assert len(via_document.accepted_statements) >= 1
    assert via_document.accepted_statements == direct.accepted_statements
