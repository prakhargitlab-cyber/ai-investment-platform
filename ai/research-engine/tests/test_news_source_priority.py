"""Focused coverage for the CAPTCHA-free news source foundation iteration.

Proves, without redoing broad investigation and without touching CURRENT_NEWS
readiness semantics:
  1. an already-persisted NSE/exchange filing is preferred/reused as
     CURRENT_NEWS evidence before generic search is even needed;
  2. no duplicate acquisition happens when that authoritative evidence
     already exists (no re-fetch);
  3. generic search fallback still works when no persisted official evidence
     exists;
  4. CAPTCHA/403/429/timeout/502 never become SUCCESS_EMPTY;
  5. the minimal global-provider cache hook is fetched once and reused by
     many concurrent callers instead of once per instrument.
"""
import asyncio
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from app.news_acquisition import acquire_news, persisted_official_discovery
from app.source_discovery import CandidateSearchResult, SearchProviderError, _safe_search_get
from app.persistence import SqliteResearchPersistence
from app.global_data_provider import SingleFlightTTLCache, GLOBAL_CACHE_KEY
from test_news_intelligence_v2 import NOW, KEY, document
from test_news_acquisition_v2 import Repository, Provider, company, candidate, no_sleep


def exchange_document(text, url='https://www.nseindia.com/archives/ann1.pdf'):
    doc = document(text, 'EXCHANGE')
    doc.canonical_url = url
    doc.discovery_provider = 'NSE_OFFICIAL_API'
    return doc


@pytest.mark.asyncio
async def test_persisted_exchange_filing_satisfies_news_without_new_fetch():
    repo = Repository()
    doc = exchange_document('Generic Cable Limited announced a large order win for copper cables.')
    doc.document_id = UUID(int=90)
    repo.documents[doc.document_id] = doc
    repo._persistence.upsert_document(doc)

    run, features = await acquire_news(repo, company(), providers=[Provider(results=[])], now=NOW, sleep=no_sleep)

    assert run.outcome == 'SEARCH_COMPLETE_WITH_EVENTS'
    assert features
    assert repo.fetches == []  # already-persisted filing was reused, never re-downloaded
    reuse = {p.provider: p for p in run.providers}['OFFICIAL_FILING_REUSE']
    assert reuse.outcome == 'SUCCESS_WITH_RESULTS' and reuse.candidate_count == 1


@pytest.mark.asyncio
async def test_official_evidence_preferred_over_generic_search_result():
    repo = Repository()
    doc = exchange_document('Generic Cable Limited announced a large order win for copper cables.')
    doc.document_id = UUID(int=91)
    repo.documents[doc.document_id] = doc
    repo._persistence.upsert_document(doc)
    # A generic search candidate that would also be usable evidence, ranked
    # lower than the exchange-hosted filing by _candidate_rank.
    provider = Provider(results=[candidate('https://publisher.test/also-news')])

    run, features = await acquire_news(repo, company(), providers=[provider], now=NOW, sleep=no_sleep,
        max_documents=1)

    assert run.outcome == 'SEARCH_COMPLETE_WITH_EVENTS'
    assert features
    # The single usable slot was filled by the reused, already-persisted
    # exchange filing; the generic search candidate was never fetched.
    assert repo.fetches == []


def test_persisted_official_discovery_ignores_non_exchange_documents():
    # The default Repository fixture carries an OFFICIAL_COMPANY document
    # (persisted for an unrelated purpose) -- not an EXCHANGE filing -- and
    # must not be repurposed as CURRENT_NEWS evidence just because it exists.
    repo = Repository()
    outcomes, candidates = persisted_official_discovery(repo, company())
    assert outcomes == [] and candidates == []


@pytest.mark.asyncio
async def test_no_persisted_official_evidence_falls_back_to_generic_search():
    repo = Repository()  # no EXCHANGE document persisted
    provider = Provider(results=[candidate()])
    run, features = await acquire_news(repo, company(), providers=[provider], industry='Wires and cables',
        now=NOW, sleep=no_sleep)
    assert run.outcome == 'SEARCH_COMPLETE_WITH_EVENTS'
    assert features
    assert len(repo.fetches) == 1  # fallback fetched the generic search candidate


@pytest.mark.parametrize('status,code', [
    (401, 'SEARCH_PROVIDER_FORBIDDEN'),
    (403, 'SEARCH_PROVIDER_FORBIDDEN'),
    (429, 'SEARCH_PROVIDER_RATE_LIMITED'),
    (502, 'SEARCH_PROVIDER_UNAVAILABLE'),
    (500, 'SEARCH_PROVIDER_UNAVAILABLE'),
])
@pytest.mark.asyncio
async def test_provider_failure_statuses_never_become_success_empty(status, code):
    def handler(request):
        return httpx.Response(status)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(SearchProviderError) as excinfo:
        await _safe_search_get(client, 'https://searx.test/search', params={'q': 'x'})
    assert str(excinfo.value).startswith(code)
    await client.aclose()


@pytest.mark.asyncio
async def test_provider_timeout_never_becomes_success_empty():
    def handler(request):
        raise httpx.ReadTimeout('timed out', request=request)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(SearchProviderError) as excinfo:
        await _safe_search_get(client, 'https://searx.test/search', params={'q': 'x'})
    assert str(excinfo.value) == 'SEARCH_PROVIDER_TIMEOUT'
    await client.aclose()


@pytest.mark.asyncio
async def test_global_provider_hook_fetched_once_and_reused_across_many_callers():
    # Simulates a future global provider (Fed/RBI/CPI/etc.) being asked for by
    # many concurrently-evaluated instruments: it must be fetched once and
    # reused, never once per instrument.
    cache = SingleFlightTTLCache(ttl_seconds=60)
    calls = []
    async def fetch():
        calls.append(1)
        await asyncio.sleep(0.01)
        return {'snapshot': 'global-macro-data'}

    results = await asyncio.gather(*[cache.get_or_fetch(GLOBAL_CACHE_KEY, fetch) for _ in range(25)])

    assert len(calls) == 1  # fetched exactly once for all 25 simulated instruments
    assert all(r == {'snapshot': 'global-macro-data'} for r in results)

    # Still within TTL: a later caller reuses the cached value, no new fetch.
    again = await cache.get_or_fetch(GLOBAL_CACHE_KEY, fetch)
    assert len(calls) == 1 and again == {'snapshot': 'global-macro-data'}
