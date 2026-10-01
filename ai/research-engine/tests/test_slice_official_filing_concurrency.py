"""Bounded official-filing fetch concurrency (research_official_document_fetch_concurrency).

Scope: _fetch_official_filings() now overlaps up to `concurrency` independent
network fetches for ONE instrument via a fixed worker pool pulling from a
single shared cursor under an asyncio.Lock (see app/repository.py's
_OfficialFilingBatchState / _fetch_official_filings docstring for the design).
All decision-making (budget, reuse, content-type, attempt-budget, host-failure
checks) stays fully serialized under that lock; only the network fetch itself
(_single_flight_official_filing) runs outside it. These tests use event-gated
fake fetchers rather than wall-clock sleeps so overlap/ordering assertions are
deterministic, not timing-dependent.

Provider-free: no real NSE/Yahoo/search calls. No opportunity cycle, no
deploy, no DB reset -- pure unit-level exercise of ResearchRepository with an
in-memory SqliteResearchPersistence() and a fake fetcher.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace as dc_replace
from uuid import UUID

import pytest

from app.deep_investigation import RequirementAcquisitionBudget, _scope
from app.models import CompanyResearchProfile, SourceMode
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository
from app.research_fetching import FetchError, FetchResult, TransportFetchError
from app.settings import Settings
from app.source_discovery import DiscoveryResult
from test_di11c_official_document_budget import _official_source

BODY = "<html><body>Example concurrency-fixture filing body with enough content to parse and persist.</body></html>"


def _profile() -> CompanyResearchProfile:
    return CompanyResearchProfile(
        instrument_id=UUID(int=9010), company_id=UUID(int=9020), company_name="Example Concurrency Limited",
        isin="INE000CC1010", ticker="EXCONC", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        provider_instrument_ids={"NSE": "EXCONC"},
    )


def _repo(*, concurrency: int = 2, max_attempts: int = 10, max_transport_failures_per_host: int = 1) -> ResearchRepository:
    repo = ResearchRepository(
        settings=Settings(
            research_live_enabled=True,
            research_demo_enabled=False,
            research_official_document_fetch_concurrency=concurrency,
            research_official_document_max_attempts_per_refresh=max_attempts,
            research_official_document_max_transport_failures_per_host=max_transport_failures_per_host,
        ),
        persistence=SqliteResearchPersistence(),
    )
    repo.profiles = [_profile()]
    return repo


def _budget(profile, max_documents: int = 4):
    """A RequirementAcquisitionBudget that is never independently 'sufficient'
    (so allow_document()'s only stop condition exercised is the document
    count), registered on the deep_investigation._scope ContextVar the way
    the real acquisition layer does before calling into repository code."""
    async def _never_sufficient():
        return False
    return RequirementAcquisitionBudget(
        profile.instrument_id, "FINANCIAL_RESULTS", None, _never_sufficient,  # type: ignore[arg-type]
        max_documents=max_documents,
    )


class _GatedFetcher:
    """Fake fetcher matching the ResearchRepository._fetcher.fetch(url) contract
    (see tests/test_governance_acquisition.py's _Fetcher for the same shape).
    Tracks in-flight/peak concurrency and can gate specific URLs on asyncio
    Events for deterministic overlap proofs -- no wall-clock sleeps."""

    def __init__(self, gates: dict[str, tuple[asyncio.Event, asyncio.Event]] | None = None,
                 fail_urls: frozenset[str] = frozenset()) -> None:
        self.urls: list[str] = []
        self.gates = gates or {}
        self.fail_urls = fail_urls
        self.in_flight = 0
        self.peak_in_flight = 0

    async def fetch(self, url: str) -> FetchResult:
        self.urls.append(url)
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            gate = self.gates.get(url)
            if gate is not None:
                entered, release = gate
                entered.set()
                await release.wait()
            if url in self.fail_urls:
                raise TransportFetchError("HTTP_FETCH_FAILED")
            return FetchResult(final_url=url, status_code=200, content_type="text/html", text=BODY, bytes_read=len(BODY))
        finally:
            self.in_flight -= 1


def _sources(profile, n: int, category: str = "FINANCIAL_RESULTS"):
    # official_nse_profile_symbol matching the profile's own NSE symbol makes
    # _has_trusted_nse_profile_identity() true, exactly like a real NSE-API
    # discovered filing (see test_governance_acquisition.py's _source()) --
    # this binds the document's identity from the trusted NSE match itself
    # rather than requiring the fixture body text to independently pass
    # entity-resolution company-relevance scoring.
    return [
        dc_replace(_official_source(profile, f"doc-{i}", category),
                  official_nse_profile_symbol=profile.provider_instrument_ids["NSE"])
        for i in range(n)
    ]


def _filings(sources):
    return [DiscoveryResult(s.categories[0], s) for s in sources]


# ---------------------------------------------------------------------------
# 1 -- concurrency never exceeds the configured limit, even with more filings
#      than slots and every fetch deliberately held open.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_concurrency_never_exceeds_configured_limit():
    repo = _repo(concurrency=2)
    profile = _profile()
    sources = _sources(profile, 4)
    gates = {s.url: (asyncio.Event(), asyncio.Event()) for s in sources}
    fetcher = _GatedFetcher(gates=gates)
    repo._fetcher = fetcher

    task = asyncio.create_task(repo._fetch_official_filings(profile, _filings(sources), set()))
    try:
        # Exactly 2 workers -> exactly the first 2 dispatched filings enter.
        await asyncio.wait_for(gates[sources[0].url][0].wait(), 5)
        await asyncio.wait_for(gates[sources[1].url][0].wait(), 5)
        assert fetcher.in_flight == 2
        assert not gates[sources[2].url][0].is_set()
        assert not gates[sources[3].url][0].is_set()

        # Releasing one slot lets exactly one more in -- never a third at once.
        gates[sources[0].url][1].set()
        await asyncio.wait_for(gates[sources[2].url][0].wait(), 5)
        assert fetcher.in_flight <= 2

        gates[sources[1].url][1].set()
        await asyncio.wait_for(gates[sources[3].url][0].wait(), 5)
        assert fetcher.in_flight <= 2

        gates[sources[2].url][1].set()
        gates[sources[3].url][1].set()
        completed_without_failure = await asyncio.wait_for(task, 5)
    finally:
        if not task.done():
            task.cancel()

    assert fetcher.peak_in_flight == 2
    assert sorted(fetcher.urls) == sorted(s.url for s in sources)
    assert completed_without_failure is True


# ---------------------------------------------------------------------------
# 2 -- concurrency=1 preserves the original fully-serial behavior: one fetch
#      completes before the next one is even entered.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_concurrency_one_preserves_serial_behavior():
    repo = _repo(concurrency=1)
    profile = _profile()
    sources = _sources(profile, 3)
    fetcher = _GatedFetcher()
    repo._fetcher = fetcher

    await repo._fetch_official_filings(profile, _filings(sources), set())

    assert fetcher.peak_in_flight == 1
    # Dispatch order is newest-first / _fair_official_filing_order; with a
    # single shared category that's insertion order here.
    assert fetcher.urls == [s.url for s in sources]


# ---------------------------------------------------------------------------
# 3 -- concurrency=2 actually overlaps two slow mocked filing operations
#      (both must be in flight simultaneously before either is released).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_concurrency_two_overlaps_two_slow_operations():
    repo = _repo(concurrency=2)
    profile = _profile()
    sources = _sources(profile, 2)
    gates = {s.url: (asyncio.Event(), asyncio.Event()) for s in sources}
    fetcher = _GatedFetcher(gates=gates)
    repo._fetcher = fetcher

    task = asyncio.create_task(repo._fetch_official_filings(profile, _filings(sources), set()))
    try:
        await asyncio.wait_for(gates[sources[0].url][0].wait(), 5)
        await asyncio.wait_for(gates[sources[1].url][0].wait(), 5)
        # Both slow operations are genuinely in flight at once.
        assert fetcher.in_flight == 2
        gates[sources[0].url][1].set()
        gates[sources[1].url][1].set()
        await asyncio.wait_for(task, 5)
    finally:
        if not task.done():
            task.cancel()
    assert fetcher.peak_in_flight == 2


# ---------------------------------------------------------------------------
# 4 -- the per-refresh attempt budget is never exceeded under concurrency.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_attempt_budget_never_exceeded_under_concurrency():
    repo = _repo(concurrency=2, max_attempts=2)
    profile = _profile()
    sources = _sources(profile, 5)
    fetcher = _GatedFetcher()
    repo._fetcher = fetcher

    completed_without_failure = await repo._fetch_official_filings(profile, _filings(sources), set())

    assert len(fetcher.urls) == 2
    assert completed_without_failure is True  # attempt-budget skips are not failures


# ---------------------------------------------------------------------------
# 5 -- the acquisition document budget is never exceeded under concurrency.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_document_budget_never_exceeded_under_concurrency():
    repo = _repo(concurrency=2, max_attempts=10)
    profile = _profile()
    sources = _sources(profile, 5)
    fetcher = _GatedFetcher()
    repo._fetcher = fetcher

    budget = _budget(profile, max_documents=2)
    token = _scope.set(budget)
    try:
        await repo._fetch_official_filings(profile, _filings(sources), set())
    finally:
        _scope.reset(token)

    assert budget.documents_attempted == 2
    assert len(fetcher.urls) == 2


# ---------------------------------------------------------------------------
# 6 -- a reusable (already-persisted) document never consumes a network slot
#      or a document-budget unit, even interleaved with fresh fetches under
#      concurrency.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_reusable_document_consumes_no_network_slot_or_budget():
    repo = _repo(concurrency=2)
    profile = _profile()
    reused_source, fresh_source = _sources(profile, 2)

    # First pass: persist reused_source for real.
    first_fetcher = _GatedFetcher()
    repo._fetcher = first_fetcher
    await repo._fetch_official_filings(profile, _filings([reused_source]), set())
    assert first_fetcher.urls == [reused_source.url]

    # Second pass: reused_source must be served from durable storage (its
    # fetcher would raise if called), fresh_source is genuinely fetched, and
    # only fresh_source counts against the document budget.
    second_fetcher = _GatedFetcher(fail_urls=frozenset({reused_source.url}))
    repo._fetcher = second_fetcher
    budget = _budget(profile, max_documents=1)
    token = _scope.set(budget)
    try:
        await repo._fetch_official_filings(profile, _filings([reused_source, fresh_source]), set())
    finally:
        _scope.reset(token)

    assert second_fetcher.urls == [fresh_source.url]
    assert budget.documents_attempted == 1


# ---------------------------------------------------------------------------
# 7 -- an unsupported archive/content-type never consumes a network slot or a
#      document-budget unit.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unsupported_content_type_consumes_no_network_slot_or_budget():
    repo = _repo(concurrency=2)
    profile = _profile()
    zip_source = _official_source(profile, "archive", "FINANCIAL_RESULTS", ext=".zip")
    pdf_source = dc_replace(_official_source(profile, "results", "FINANCIAL_RESULTS"),
                            official_nse_profile_symbol=profile.provider_instrument_ids["NSE"])
    fetcher = _GatedFetcher()
    repo._fetcher = fetcher

    budget = _budget(profile, max_documents=1)
    token = _scope.set(budget)
    try:
        await repo._fetch_official_filings(profile, _filings([zip_source, pdf_source]), set())
    finally:
        _scope.reset(token)

    assert zip_source.url not in fetcher.urls
    assert fetcher.urls == [pdf_source.url]
    assert budget.documents_attempted == 1


# ---------------------------------------------------------------------------
# 8 -- transport failures stay retryable (recorded, not swallowed) and the
#      per-host transport-failure limit still halts further same-host attempts
#      once it is reached, even with two concurrent failures in flight.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_transport_failures_retryable_and_host_limit_respected():
    repo = _repo(concurrency=2, max_transport_failures_per_host=1)
    profile = _profile()
    sources = _sources(profile, 3)  # all on the same nsearchives.nseindia.com host
    fetcher = _GatedFetcher(fail_urls=frozenset(s.url for s in sources[:2]))
    repo._fetcher = fetcher

    completed_without_failure = await repo._fetch_official_filings(profile, _filings(sources), set())

    # Both same-host failures were genuinely attempted (retryable, not silently
    # dropped): the failure is recorded as a transport failure, which is what
    # trips the host budget in the first place.
    assert fetcher.urls[:2] == [sources[0].url, sources[1].url]
    # Once 2 failures are on record for this host (>= the configured limit of
    # 1), the third same-host filing must never be attempted.
    assert sources[2].url not in fetcher.urls
    assert completed_without_failure is False


# ---------------------------------------------------------------------------
# 9 -- existing single-flight de-duplication survives the move to bounded
#      concurrency: two dispatch entries for the SAME url must still result
#      in exactly one real network fetch.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_single_flight_still_deduplicates_same_url_under_concurrency():
    repo = _repo(concurrency=2)
    profile = _profile()
    source = _sources(profile, 1)[0]
    entered, release = asyncio.Event(), asyncio.Event()
    fetcher = _GatedFetcher(gates={source.url: (entered, release)})
    repo._fetcher = fetcher

    # Two DiscoveryResults pointing at the identical source/url -- dispatched
    # to two different workers, must still join into ONE real fetch.
    task = asyncio.create_task(repo._fetch_official_filings(profile, _filings([source, source]), set()))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        # Give the second worker a chance to reach _single_flight_official_filing
        # and join the in-flight task before we release it.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(task, 5)
    finally:
        if not task.done():
            task.cancel()

    assert fetcher.urls == [source.url]  # exactly one real network fetch
