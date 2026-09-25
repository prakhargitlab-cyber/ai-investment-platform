"""Focused DI-11C tests for the two narrow official-document / earnings fixes.

Scope (READ-ONLY-safe behavior tests, no production data):
  Fix 1 -- an unsupported official attachment (e.g. NSE ``.zip``) MUST NOT
  consume the per-refresh attempt budget (research_official_document_max_attempts_per_refresh).
  Fix 2 -- a bare ``earnings`` substring MUST NOT emit EARNINGS_RELEASE; only
  bounded financial-results phrases do.
  C9 -- DIAGNOSTIC documenting the CURRENT date-ordering behavior for an
  unparseable NSE ``an_dt`` (no production code changed; per the task, the date
  ordering itself is intentionally left untouched).
"""

from datetime import datetime, timezone
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from app.extraction import RuleBasedEventExtractor
from app.models import (
    CompanyResearchProfile,
    DocumentType,
    ReliabilityLevel,
    ResearchDocument,
    ResearchEventType,
    SourceClassification,
    SourceMode,
    SourceType,
)
from app.research_fetching import FetchError
from app.repository import (
    ResearchRepository,
    _official_filing_content_type_unsupported,
)
from app.settings import Settings
from app.source_discovery import DiscoveryResult, OfficialFilingDiscovery, _nse_datetime
from app.source_registry import RegisteredResearchSource


INSTRUMENT_ID = UUID("99999999-9999-9999-9999-999999999999")
COMPANY_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _doc(text: str) -> ResearchDocument:
    return ResearchDocument(
        canonical_url="https://example.com/press.html",
        original_url="https://example.com/press.html",
        source_type=SourceType.NEWS,
        source_name="Test Press",
        content_type="text/html",
        document_type=DocumentType.HTML,
        content_hash="fixture",
        normalized_text=text,
        instrument_id=INSTRUMENT_ID,
        company_id=COMPANY_ID,
        reliability_level=ReliabilityLevel.LEVEL_A,
    )


def _official_source(profile: CompanyResearchProfile, suffix: str, category: str,
                     ext: str = ".pdf") -> RegisteredResearchSource:
    url = f"https://nsearchives.nseindia.com/corporate/{suffix}{ext}"
    return RegisteredResearchSource(
        source_id=f"nse-{category}-{suffix}-{ext}",
        instrument_id=profile.instrument_id,
        url=url,
        source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
        source_classification=SourceClassification.EXCHANGE,
        source_name="NSE corporate announcements",
        publisher="NSE",
        reliability_level=ReliabilityLevel.LEVEL_A,
        company_id=profile.company_id,
        discovery_method="NSE_OFFICIAL_API",
        categories=(category,),
    )


class _RecordingFetcher:
    """Mirror of the fixture in test_shareholding.py: records every attempted
    URL and raises FetchError (as a non-processable result) to emulate a fetch
    that the budget/scheduling layer already paid for."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    async def fetch(self, url):  # noqa: ANN001 - signature mirrors existing fixture
        self.urls.append(url)
        raise FetchError("fixture failure")


# --------------------------------------------------------------------------------------
# C1: unsupported .zip does not consume an attempt slot; a later supported PDF still
#     receives the freed slot (Castrol production pattern: 2 PDFs + 1 ZIP + 1 more PDF).
# --------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_di11c_unsupported_zip_does_not_consume_attempt_budget() -> None:
    settings = Settings(
        research_live_enabled=True,
        research_official_document_max_attempts_per_refresh=3,
    )
    repository = ResearchRepository(settings=settings)
    profile = next(p for p in repository.list_profiles() if p.ticker == "RELIANCE")

    pdf_a = _official_source(profile, "analyst-ppt", "INVESTOR_RELEASE")
    pdf_b = _official_source(profile, "pr", "INVESTOR_RELEASE")
    zip_c = _official_source(profile, "CASTROL_01_03112015174758", "INVESTOR_RELEASE", ext=".zip")
    pdf_d = _official_source(profile, "capex-4", "INVESTOR_RELEASE")

    fetcher = _RecordingFetcher()
    repository._fetcher = fetcher

    # A single shared category keeps _fair_official_filing_order's round-robin
    # deterministic so the zip is the third candidate (isolating the skip).
    filings = [
        DiscoveryResult(pdf_a.categories[0], pdf_a),
        DiscoveryResult(pdf_b.categories[0], pdf_b),
        DiscoveryResult(zip_c.categories[0], zip_c),
        DiscoveryResult(pdf_d.categories[0], pdf_d),
    ]
    await repository._fetch_official_filings(profile, filings, set())

    assert zip_c.url not in fetcher.urls
    assert not any(url.endswith(".zip") for url in fetcher.urls)
    assert fetcher.urls == [pdf_a.url, pdf_b.url, pdf_d.url]
    assert len(fetcher.urls) == 3  # bounded at exactly 3


# --------------------------------------------------------------------------------------
# C2: extensionless / ambiguous URLs are NOT pre-skipped (retains existing behavior).
# --------------------------------------------------------------------------------------
def test_di11c_extensionless_url_is_not_pre_skipped() -> None:
    assert _official_filing_content_type_unsupported("https://nsearchives.nseindia.com/corporate/result?id=5") is False
    assert _official_filing_content_type_unsupported("https://nsearchives.nseindia.com/corporate/2026-results") is False
    assert _official_filing_content_type_unsupported("https://nsearchives.nseindia.com/corporate/annual_return") is False
    # Supported / known processable extensions are not pre-skipped:
    assert _official_filing_content_type_unsupported("https://nsearchives.nseindia.com/corporate/results.pdf") is False
    # The proven unsupported case IS pre-skipped:
    assert _official_filing_content_type_unsupported("https://nsearchives.nseindia.com/corporate/CASTROL_01_03112015174758.zip") is True


# --------------------------------------------------------------------------------------
# C3: existing supported official-document behavior is unchanged (core-first round-robin).
# --------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_di11c_supported_pdfs_unaffected_by_unsupported_skip() -> None:
    settings = Settings(
        research_live_enabled=True,
        research_official_document_max_attempts_per_refresh=3,
    )
    repository = ResearchRepository(settings=settings)
    profile = next(p for p in repository.list_profiles() if p.ticker == "RELIANCE")

    financial = [_official_source(profile, f"financial-{i}", "FINANCIAL_RESULTS") for i in range(3)]
    shareholding = [_official_source(profile, f"shareholding-{i}", "SHAREHOLDING_PATTERN") for i in range(2)]

    fetcher = _RecordingFetcher()
    repository._fetcher = fetcher
    filings = [
        *(DiscoveryResult("FINANCIAL_RESULTS", s) for s in financial),
        *(DiscoveryResult("SHAREHOLDING_PATTERN", s) for s in shareholding),
    ]
    await repository._fetch_official_filings(profile, filings, set())

    assert fetcher.urls == [financial[0].url, shareholding[0].url, financial[1].url]
    assert len(fetcher.urls) == 3


# --------------------------------------------------------------------------------------
# C4: VERIFIED NSE identity guard unchanged (broker-derived alias never becomes NSE).
# --------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_di11c_verified_nse_identity_guard_unchanged() -> None:
    profile = CompanyResearchProfile(
        instrument_id=uuid4(), company_id=uuid4(), company_name="Generic India Equity",
        ticker="BROKER_ALIAS", exchange="NSE", mic="XNSE", country="IN", currency="INR",
    )
    discovery = OfficialFilingDiscovery()
    assert await discovery.discover(profile, {"SHAREHOLDING_PATTERN"}, set()) == []


# --------------------------------------------------------------------------------------
# C5: budget remains exactly 3.
# --------------------------------------------------------------------------------------
def test_di11c_budget_remains_exactly_three() -> None:
    assert Settings().research_official_document_max_attempts_per_refresh == 3


# --------------------------------------------------------------------------------------
# C6: bare "earnings" alone does NOT emit EARNINGS_RELEASE.
# --------------------------------------------------------------------------------------
def test_di11c_bare_earnings_does_not_emit_results_release() -> None:
    extractor = RuleBasedEventExtractor()
    doc = _doc(
        "Castrol June recycling press release: the business generated earnings from "
        "collected oil and recyclers during the quarter and expects continued "
        "margin improvement from its waste-oil collection network."
    )
    events = extractor.extract(doc)
    assert not [e for e in events if e.event_type == ResearchEventType.EARNINGS_RELEASE]


# --------------------------------------------------------------------------------------
# C7: genuine quarterly / earnings results wording DOES emit EARNINGS_RELEASE.
# --------------------------------------------------------------------------------------
def test_di11c_genuine_quarterly_results_emits_results_release() -> None:
    extractor = RuleBasedEventExtractor()
    doc = _doc(
        "Castrol Standalone Unaudited Quarterly Results for the quarter ended June 30 2026. "
        "Earnings per share (Rs. 10 each) basic Rs. 1.47. Revenue from operations "
        "Rs. 8,261.11 crore, Net profit for the period after tax Rs. 1,927.21 crore."
    )
    events = extractor.extract(doc)
    assert any(e.event_type == ResearchEventType.EARNINGS_RELEASE for e in events)


# --------------------------------------------------------------------------------------
# C8: the original legitimate results phrases still classify.
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("phrase", [
    "quarterly results",
    "half year results",
    "half-year results",
    "h1 results",
    "q2 results",
])
def test_di11c_existing_results_phrases_still_emit(phrase: str) -> None:
    extractor = RuleBasedEventExtractor()
    doc = _doc(f"Financial announcement containing {phrase} and revenue growth.")
    events = extractor.extract(doc)
    assert any(e.event_type == ResearchEventType.EARNINGS_RELEASE for e in events)


# --------------------------------------------------------------------------------------
# C9 (DIAGNOSTIC, DATE ORDERING): documents CURRENT behavior of an unparseable NSE
# `an_dt` in the actual `OfficialFilingDiscovery.discover` sorting implementation.
# NO production date-ordering code is changed here (decided separately per task).
# --------------------------------------------------------------------------------------
@pytest.mark.asyncio
@respx.mock
async def test_di11c_unparseable_nse_date_sorts_oldest_within_priority() -> None:
    repository = ResearchRepository(settings=Settings())
    profile = next(p for p in repository.list_profiles() if p.ticker == "RELIANCE")
    # Default RELIANCE profile has no NSE provider mapping; mirror test_L:
    profile.provider_instrument_ids.clear()
    profile.provider_instrument_ids["NSE"] = "RELIANCE"

    # Step A: prove the fallback itself.
    assert _nse_datetime("not-a-real-date") == datetime.min.replace(tzinfo=timezone.utc)

    # Step B: prove the real discover() sort consequence.
    discovery = OfficialFilingDiscovery()
    recent_url = "https://nsearchives.nseindia.com/corporate/recent.pdf"
    stale_url = "https://nsearchives.nseindia.com/corporate/stale.pdf"
    respx.get("https://www.nseindia.com/api/corporate-announcements").mock(
        return_value=httpx.Response(200, json=[
            {"an_dt": "10-Aug-2026 13:52:28", "desc": "Financial results",
             "attchmntText": "Financial results", "attchmntFile": recent_url},
            {"an_dt": "not-a-real-date", "desc": "Financial results",
             "attchmntText": "Financial results", "attchmntFile": stale_url},
        ])
    )

    accepted = await discovery.discover(profile, {"FINANCIAL_RESULTS"}, set())
    urls = [result.source.url for result in accepted]

    # Sort key is (priority, -published.timestamp()) (source_discovery.py:908).
    # datetime.min (ts -6.21e10) -> -ts = +6.21e10 -> sorts LAST (oldest position),
    # NOT newest. A parseable 2026 date sorts FIRST.
    assert urls == [recent_url, stale_url]
