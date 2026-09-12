from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.models import CompanyResearchProfile, SourceClassification
from app.source_discovery import CandidateSearchResult, SearchDateWindow, SearchDiscoveryService, _candidate_has_positive_issuer_evidence


class _StaticSearchProvider:
    provider_name = "test-search"

    def __init__(self, candidates: list[CandidateSearchResult]) -> None:
        self.candidates = candidates

    async def discover(self, company, category, date_window: SearchDateWindow):
        return self.candidates


def _federal_bank_profile() -> CompanyResearchProfile:
    return CompanyResearchProfile(
        instrument_id=uuid4(), company_id=uuid4(), company_name="FEDERAL BANK LTD",
        aliases=["FEDERAL BANK"], provider_instrument_ids={"NSE": "FEDERALBNK"},
        isin="INE171A01029", ticker="FEDBAN", exchange="NSE", mic="XNSE",
        country="IN", currency="INR", known_domains=["federalbank.co.in"],
    )


def _candidate(url: str, title: str = "", snippet: str = "") -> CandidateSearchResult:
    return CandidateSearchResult(
        title=title, url=url, snippet=snippet, discovered_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        provider="test-search", query_id="FINANCIAL_RESULTS:1", query='"FEDERAL BANK LTD" results',
        category="FINANCIAL_RESULTS",
    )


@pytest.mark.asyncio
async def test_generic_bse_result_naming_other_issuer_is_rejected_before_discovery_result() -> None:
    profile = _federal_bank_profile()
    search = SearchDiscoveryService(_StaticSearchProvider([
        _candidate("https://www.bseindia.com/xml-data/corpfiling/AttachLive/TAMILNADU.pdf",
                   "TAMILNADU PETROPRODUCTS LTD financial results", "Quarterly result"),
    ]))

    assert await search.discover(profile, {"FINANCIAL_RESULTS"}, set()) == []
    assert search.last_stats.rejected_reasons == {"COMPANY_RELEVANCE_FAILED": 1}
    assert search.rejected_candidates[0].reason == "COMPANY_RELEVANCE_FAILED"


@pytest.mark.asyncio
async def test_trusted_isin_accepts_generic_exchange_candidate() -> None:
    profile = _federal_bank_profile()
    search = SearchDiscoveryService(_StaticSearchProvider([
        _candidate("https://www.bseindia.com/xml-data/corpfiling/AttachLive/federal.pdf",
                   "Financial results", "INE171A01029 quarterly financial results"),
    ]))

    results = await search.discover(profile, {"FINANCIAL_RESULTS"}, set())

    assert len(results) == 1
    assert results[0].source.source_classification == SourceClassification.EXCHANGE


@pytest.mark.asyncio
async def test_trusted_provider_symbol_as_real_token_accepts_generic_exchange_candidate() -> None:
    profile = _federal_bank_profile()
    search = SearchDiscoveryService(_StaticSearchProvider([
        _candidate("https://www.nseindia.com/corporates/federal.pdf", "FEDERALBNK financial results", "Quarterly result"),
    ]))

    assert len(await search.discover(profile, {"FINANCIAL_RESULTS"}, set())) == 1


@pytest.mark.asyncio
async def test_known_company_domain_remains_accepted_without_metadata_name() -> None:
    profile = _federal_bank_profile()
    search = SearchDiscoveryService(_StaticSearchProvider([
        _candidate("https://www.federalbank.co.in/investors/results.pdf", "Investor document"),
    ]))

    results = await search.discover(profile, {"FINANCIAL_RESULTS"}, set())

    assert len(results) == 1
    assert results[0].source.source_classification == SourceClassification.OFFICIAL_COMPANY


def test_exchange_domain_alone_is_not_positive_issuer_evidence() -> None:
    profile = _federal_bank_profile()
    candidate = _candidate("https://www.bseindia.com/xml-data/corpfiling/AttachLive/filing.pdf", "Financial results")

    assert _candidate_has_positive_issuer_evidence(profile, f"{candidate.title} {candidate.snippet} {candidate.url}") is False


@pytest.mark.asyncio
async def test_untrusted_local_symbol_cannot_override_verified_provider_identity() -> None:
    profile = _federal_bank_profile()
    search = SearchDiscoveryService(_StaticSearchProvider([
        _candidate("https://www.nseindia.com/corporates/other.pdf",
                   "FEDBAN TAMILNADU PETROPRODUCTS LTD financial results", "Quarterly result"),
    ]))

    assert await search.discover(profile, {"FINANCIAL_RESULTS"}, set()) == []
    assert search.last_stats.rejected_reasons == {"COMPANY_RELEVANCE_FAILED": 1}


@pytest.mark.asyncio
async def test_unsafe_and_duplicate_rejections_remain_unchanged() -> None:
    profile = _federal_bank_profile()
    duplicate = "https://www.federalbank.co.in/investors/results.pdf"
    search = SearchDiscoveryService(_StaticSearchProvider([
        _candidate("http://127.0.0.1/private", "FEDERAL BANK LTD results"),
        _candidate(duplicate, "Investor document"),
    ]))

    assert await search.discover(profile, {"FINANCIAL_RESULTS"}, {duplicate}) == []
    assert search.last_stats.rejected_reasons == {"DOMAIN_VALIDATION_FAILED": 1, "DUPLICATE": 1}
