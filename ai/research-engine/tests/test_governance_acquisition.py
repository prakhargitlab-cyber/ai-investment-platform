"""Option B governance acquisition (no readiness/Rule-Engine semantic change).

NSE announcement -> structured metadata / semantic concept classification ->
official filing fetch/persist -> GOVERNANCE_EVIDENCE -> readiness.
SUCCESS_EMPTY stays a distinct outcome and never satisfies GOVERNANCE_EVIDENCE.
The titles below are REGRESSION EXAMPLES of generic governance concepts, not
production mappings; the structured category value is test-only configuration.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import replace as dc_replace
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
import pytest

from app.deep_investigation import RequirementAcquisitionBudget, _scope
from app.extraction import governance_concepts, governance_disclosure
from app.failure_taxonomy import EVIDENCE_UNAVAILABLE, TECHNICAL_RETRYABLE, classify_reason
from app.models import (CompanyResearchProfile, DocumentStatus, ReliabilityLevel, SourceClassification,
                        SourceMode, SourceType)
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository
from app.research_fetching import FetchError, FetchResult
from app.research_readiness import ResearchRequirementStatus
from app.research_readiness_runtime import ResearchReadinessRuntime
from app.settings import Settings
from app.source_discovery import DiscoveryResult, OfficialFilingDiscovery
from test_di11c_official_document_budget import _official_source

TEST_ONLY_CATEGORY = "TEST-ONLY STRUCTURED GOVERNANCE CATEGORY"
GOVERNANCE_TITLES = ["Change in Directorate", "Change in Auditors", "Compliance Report on Corporate Governance",
                     "Resignation of Company Secretary and Compliance Officer",
                     "Appointment of Mr. A. Kumar as Additional Director",
                     "Show Cause Notice received from SEBI"]
UNRELATED_TITLES = ["Financial Results", "Board meeting notice", "Outcome of Board Meeting", "Trading Window closure",
                    "Newspaper Publication", "Order worth Rs 500 crore", "Investor Presentation"]


def _profile():
    return CompanyResearchProfile(
        instrument_id=UUID(int=501), company_id=UUID(int=502), company_name="Example Governance Limited",
        isin="INE000G01010", ticker="EXGOV", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        provider_instrument_ids={"NSE": "EXGOV"})


def _row(title, url, **extra):
    return {"an_dt": "10-Sep-2026 10:00:00", "desc": title, "attchmntText": title, "attchmntFile": url,
            "symbol": "EXGOV", "isin": "INE000G01010", **extra}


async def _discover(rows, categories=("REGULATORY", "MANAGEMENT", "RISKS"), **kwargs):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=rows, request=r)))
    return await OfficialFilingDiscovery(client, **kwargs).discover(_profile(), set(categories), set())


# 1 structured authoritative metadata (infrastructure; empty by default) ------
@pytest.mark.asyncio
async def test_01_structured_governance_metadata_is_classified_when_configured():
    rows = [_row("Intimation under Regulation 30", "https://nsearchives.nseindia.com/corporate/g1.pdf",
                 category=TEST_ONLY_CATEGORY)]
    assert await _discover(rows) == []  # no production mapping invented: empty by default
    found = await _discover(rows, governance_categories=[TEST_ONLY_CATEGORY])
    assert [r.category for r in found] == ["REGULATORY"]


# 2 generic concept-based title fallback --------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("title", GOVERNANCE_TITLES)
async def test_02_generic_governance_title_fallback(title):
    assert governance_disclosure(title) and governance_concepts(title)
    found = await _discover([_row(title, "https://nsearchives.nseindia.com/corporate/g2.pdf")])
    assert len(found) == 1 and found[0].category == "REGULATORY"


# 3 unrelated announcements are not governance --------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("title", UNRELATED_TITLES)
async def test_03_unrelated_announcement_is_not_governance(title):
    assert not governance_disclosure(title)
    found = await _discover([_row(title, "https://nsearchives.nseindia.com/corporate/u.pdf")])
    assert not {r.category for r in found} & {"REGULATORY", "MANAGEMENT", "RISKS"}


# 4 company relevance stays enforced -------------------------------------------
@pytest.mark.asyncio
async def test_04_company_relevance_rejection_remains_enforced():
    other = _row("Change in Directorate", "https://nsearchives.nseindia.com/corporate/other.pdf",
                 isin="INE999Z01010", symbol="OTHERCO")
    assert await _discover([other]) == []


# 5 duplicate filing deduplicated before download ------------------------------
@pytest.mark.asyncio
async def test_05_duplicate_governance_filing_is_deduplicated_before_download():
    rows = [_row("Change in Directorate", "https://nsearchives.nseindia.com/corporate/dup.pdf?b=2&a=1"),
            _row("Change in Directorate", "HTTPS://nsearchives.nseindia.com/corporate/dup.pdf?a=1&b=2")]
    assert len(await _discover(rows)) == 1


def _repo():
    repo = ResearchRepository(settings=Settings(research_live_enabled=True, research_demo_enabled=False),
                              persistence=SqliteResearchPersistence())
    repo.profiles = [_profile()]
    return repo


def _source(suffix, title):
    profile = _profile()
    return dc_replace(_official_source(profile, suffix, "REGULATORY"), official_title=title,
                      official_nse_profile_symbol="EXGOV", official_published_at=datetime(2026, 9, 10, tzinfo=timezone.utc))


GOVERNANCE_BODY = ("<html><body><p>Example Governance Limited informs the Exchange of the appointment of "
                   "Mr. A. Kumar as Additional Director (Independent) with effect from 10 September 2026, "
                   "and the resignation of the Company Secretary and Compliance Officer.</p></body></html>")


class _Fetcher:
    def __init__(self, fail_urls=()):
        self.urls, self.fail_urls = [], set(fail_urls)

    async def fetch(self, url):
        self.urls.append(url)
        if url in self.fail_urls:
            raise FetchError("PDF_EXTRACTION_TIMEOUT")
        return FetchResult(final_url=url, status_code=200, content_type="text/html", text=GOVERNANCE_BODY,
                           bytes_read=len(GOVERNANCE_BODY))

    async def fetch_official_document(self, url, **kwargs):
        return await self.fetch(url)


# 6 persisted governance filing is reused (no download/parse) -----------------
def test_06_persisted_governance_filing_is_reused(monkeypatch):
    repo = _repo()
    source = _source("gov_reuse", "Change in Directorate")
    first = _Fetcher()
    repo._fetcher = first
    monkeypatch.setattr(repo._fetcher, "fetch_official_document", first.fetch_official_document, raising=False)
    asyncio.run(repo._fetch_official_filings(_profile(), [DiscoveryResult("REGULATORY", source)], set()))
    assert first.urls == [source.url]
    assert repo.document_count_for(_profile().instrument_id) == 1
    second = _Fetcher(fail_urls={source.url})
    repo._fetcher = second
    asyncio.run(repo._fetch_official_filings(_profile(), [DiscoveryResult("REGULATORY", source)], set()))
    assert second.urls == []  # reused from durable storage, never re-downloaded
    assert repo.document_count_for(_profile().instrument_id) == 1


# 12 one PDF/parser failure -> next best governance candidate attempted --------
def test_12_failed_governance_candidate_falls_through_to_next():
    repo = _repo()
    bad, good = _source("gov_bad", "Change in Auditors"), _source("gov_good", "Change in Directorate")
    repo._fetcher = _Fetcher(fail_urls={bad.url})
    asyncio.run(repo._fetch_official_filings(
        _profile(), [DiscoveryResult("REGULATORY", bad), DiscoveryResult("REGULATORY", good)], set()))
    assert repo._fetcher.urls == [bad.url, good.url]
    documents = repo.documents_for(_profile().instrument_id, source_mode=SourceMode.REAL)
    assert [d.canonical_url for d in documents] == [good.url]
    assert governance_disclosure(f"{documents[0].title} {documents[0].normalized_text}")


# 7/8 structured evidence suppresses search when sufficient; bounded otherwise --
def _with_budget(sufficient_value):
    async def sufficient():
        return sufficient_value
    return RequirementAcquisitionBudget(_profile().instrument_id, "GOVERNANCE_HISTORY",
                                        datetime.now(timezone.utc), sufficient)


@pytest.mark.parametrize("sufficient, expect_search", [(True, False), (False, True)])
def test_07_08_search_fallback_only_when_authoritative_evidence_insufficient(sufficient, expect_search):
    repo = _repo()
    calls = []

    async def search(profile, missing, seen):
        calls.append(set(missing))
        return []
    repo._search_discovery.discover = search
    budget = _with_budget(sufficient)

    async def run():
        token = _scope.set(budget)
        try:
            return await repo._refresh_search_discovery(_profile(), {"REGULATORY"}, set())
        finally:
            _scope.reset(token)
    asyncio.run(run())
    assert bool(calls) is expect_search
    assert budget.queries_reserved <= budget.max_queries  # fallback stays bounded


# 9 query budget exhaustion is technical --------------------------------------
def test_09_query_budget_exhaustion_is_technical_retryable():
    assert classify_reason("DISCOVERY_QUERY_BUDGET_EXHAUSTED") == TECHNICAL_RETRYABLE


# 10 SUCCESS_EMPTY vs technical failure: distinct durable states --------------
@pytest.mark.asyncio
@pytest.mark.parametrize("outcome, reason, expected_class", [
    ("SUCCESS_EMPTY", None, EVIDENCE_UNAVAILABLE),
    ("FAILED", "NETWORK_TIMEOUT", TECHNICAL_RETRYABLE),
])
async def test_10_success_empty_is_distinct_from_technical_failure(outcome, reason, expected_class):
    from app.deep_investigation import investigate
    from app.research_readiness_runtime import TargetedEnsureResult
    from test_research_readiness_runtime import StateDataSource, RuntimeRepository, UpdatingExecutor, INSTRUMENT_ID

    class Repo(RuntimeRepository):
        def acquisition_observations_for(self, instrument_id):
            return [{"requirement_id": "GOVERNANCE_HISTORY", "provider": "NSE", "outcome": outcome,
                     "observed_at": datetime.now(timezone.utc).isoformat(), "failure_reason": reason}]

    source = StateDataSource({"GOVERNANCE_HISTORY"})
    runtime = ResearchReadinessRuntime(Repo(), source, UpdatingExecutor(source, update_primary=False))

    async def noop(*a, **k):
        return TargetedEnsureResult(await runtime.read(INSTRUMENT_ID, jurisdiction="INDIA"), (), ())
    runtime.ensure = noop
    result, _, _ = await investigate(runtime, INSTRUMENT_ID, jurisdiction="INDIA")
    recorded = result.failures["GOVERNANCE_HISTORY"]
    assert classify_reason(recorded) == expected_class
    if outcome == "SUCCESS_EMPTY":
        assert recorded == "ACQUISITION_NOT_DUE"  # checked & empty: not retried
    else:
        assert recorded == "ACQUISITION_BACKOFF|NETWORK_TIMEOUT"  # technical: exact reason retained


# 11 SUCCESS_EMPTY does not satisfy GOVERNANCE_EVIDENCE -------------------------
@pytest.mark.asyncio
async def test_11_success_empty_never_satisfies_governance_evidence():
    from app.research_readiness import DurableResearchSnapshot
    from test_research_readiness_runtime import StateDataSource, RuntimeRepository, UpdatingExecutor, INSTRUMENT_ID

    class EmptyCheckedSource(StateDataSource):
        def load_by_global_instrument_id(self, instrument_id, requirements):
            snapshot = super().load_by_global_instrument_id(instrument_id, requirements)
            return DurableResearchSnapshot(
                instrument_id, snapshot.evidence_by_requirement, failure_reasons=snapshot.failure_reasons,
                acquisition_observations={"GOVERNANCE_HISTORY": {"outcome": "SUCCESS_EMPTY", "history": [
                    {"outcome": "SUCCESS_EMPTY", "observed_at": datetime.now(timezone.utc).isoformat()}]}})

    source = EmptyCheckedSource({"GOVERNANCE_HISTORY"})
    runtime = ResearchReadinessRuntime(RuntimeRepository(), source, UpdatingExecutor(source))
    row = (await runtime.read(INSTRUMENT_ID, jurisdiction="INDIA")).for_requirement("GOVERNANCE_HISTORY")
    assert row.status not in {ResearchRequirementStatus.READY_FRESH, ResearchRequirementStatus.READY_STALE}
    assert "GOVERNANCE_EVIDENCE" in row.missing_input_ids


def test_11b_concept_governance_document_is_governance_evidence():
    repo = _repo()
    repo._fetcher = _Fetcher()
    source = _source("gov_ev", "Change in Directorate")
    asyncio.run(repo._fetch_official_filings(_profile(), [DiscoveryResult("REGULATORY", source)], set()))
    evidence = defaultdict(list)
    from app.research_readiness_runtime import RepositoryResearchReadinessAdapter
    RepositoryResearchReadinessAdapter._append_documents(evidence, repo.documents_for(_profile().instrument_id))
    assert evidence["GOVERNANCE_HISTORY"], "a persisted authoritative governance filing yields GOVERNANCE_EVIDENCE"


# 13 bounded repair never loops ------------------------------------------------
@pytest.mark.asyncio
async def test_13_governance_repair_is_bounded(monkeypatch):
    from app.cycle_checkpoint import CandidateState, CycleCheckpoint, PHASE_DEEP
    from app.research_readiness_runtime import TargetedEnsureResult
    from test_global_opportunity_acquisition import acquisition_service
    from test_global_scanner import NOW, instrument, persisted
    from test_stock_rule_engine import _readiness

    service, runtime, store = acquisition_service(monkeypatch)
    blocked = _readiness(overrides={"GOVERNANCE_HISTORY": ResearchRequirementStatus.MISSING}, critical_pct=50)
    calls = []

    async def ensure(key, **kwargs):
        persisted(store, instrument(key.int))
        if kwargs.get("requirement_ids") is not None:
            return TargetedEnsureResult(None, (), ())
        calls.append(key)
        return TargetedEnsureResult(blocked, (), (), failures={"GOVERNANCE_HISTORY": "DISCOVERY_QUERY_BUDGET_EXHAUSTED"})
    runtime.ensure.side_effect = ensure
    store.create_cycle_run("gov", {})
    for owner in ("a", "b", "c", "d"):
        store._connection.execute("UPDATE global_opportunity_cycle_run SET owner_id=? WHERE cycle_id='gov'", (owner,))
        result = await service.run([instrument(1)], as_of=NOW, shortlist_limit=1,
                                   checkpoint=await CycleCheckpoint(store, "gov", owner).load())
        assert result.diagnostics[0].failure_class == TECHNICAL_RETRYABLE
        assert result.diagnostics[0].acquisition_failures == {"GOVERNANCE_HISTORY": "DISCOVERY_QUERY_BUDGET_EXHAUSTED"}
    assert len(calls) == 3  # 2 in the first run (main + repair), 1 on restart, then never again
    row = store.cycle_progress("gov")[(PHASE_DEEP, str(UUID(int=1)))]
    assert row["state"] == CandidateState.RETRYABLE_FAILURE and row["attempts"] == 3
