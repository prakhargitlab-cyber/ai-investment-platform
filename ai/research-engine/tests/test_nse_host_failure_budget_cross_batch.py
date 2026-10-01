"""Guardian Review (STAGE2_LIVE_RUN_DEFECTS_20260930.md, Issue 3) -- NSE
host transport-failure budget must survive across separate
_fetch_official_filings() batch calls for the SAME instrument.

One readiness cycle calls _fetch_official_filings() once per capability
group needing official filings (e.g. once for FINANCIAL_RESULTS, again
moments later for GOVERNANCE/SHAREHOLDING) for the same instrument.
_OfficialFilingBatchState.host_transport_failures is freshly created on
EVERY call, so before this fix a host that had just tripped the transport-
failure budget in one group's batch was retried again immediately, with a
fresh counter, in the very next group's batch -- defeating the budget's
purpose (proven in the evidence doc for instrument 42b59185).

The fix adds a repository-instance-level (not per-batch)
`_official_host_cooldowns: dict[(instrument_id, host), float]` map,
consulted and refreshed alongside the existing per-batch counter, so the
budget survives across batches for the same (instrument, host) pair while
still allowing a genuine retry once research_official_document_host_cooldown_seconds
elapses.
"""
from __future__ import annotations

import asyncio

import pytest

from app.deep_investigation import _scope
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository
from app.settings import Settings
from app.source_discovery import DiscoveryResult

from test_slice_official_filing_concurrency import (
    _GatedFetcher,
    _budget,
    _filings,
    _profile,
)
from dataclasses import replace as dc_replace
from test_di11c_official_document_budget import _official_source


def _sources(profile, n: int, *, start: int = 0, category: str = "FINANCIAL_RESULTS"):
    """Like test_slice_official_filing_concurrency._sources but with a
    `start` offset so two separate batches for the same profile/category
    never collide on the same fixture URL."""
    return [
        dc_replace(
            _official_source(profile, f"doc-{start + i}", category),
            official_nse_profile_symbol=profile.provider_instrument_ids["NSE"],
        )
        for i in range(n)
    ]


def _repo(*, max_transport_failures_per_host: int = 1, host_cooldown_seconds: float = 60.0) -> ResearchRepository:
    repo = ResearchRepository(
        settings=Settings(
            research_live_enabled=True,
            research_demo_enabled=False,
            research_official_document_fetch_concurrency=1,
            research_official_document_max_transport_failures_per_host=max_transport_failures_per_host,
            research_official_document_host_cooldown_seconds=host_cooldown_seconds,
        ),
        persistence=SqliteResearchPersistence(),
    )
    repo.profiles = [_profile()]
    return repo


@pytest.mark.asyncio
async def test_host_failure_budget_survives_into_a_later_batch_for_same_instrument():
    repo = _repo(max_transport_failures_per_host=1, host_cooldown_seconds=60.0)
    profile = _profile()
    # Batch A: one filing, its host transport-fetch fails -- trips the budget.
    batch_a_sources = _sources(profile, 1)
    fetcher = _GatedFetcher(fail_urls=frozenset({batch_a_sources[0].url}))
    repo._fetcher = fetcher

    budget_a = _budget(profile)
    token = _scope.set(budget_a)
    try:
        completed_a = await repo._fetch_official_filings(profile, _filings(batch_a_sources), set())
    finally:
        _scope.reset(token)
    assert completed_a is False
    assert fetcher.urls == [batch_a_sources[0].url]

    # Batch B: called moments later (e.g. the next capability group's own
    # _fetch_official_filings call) for the SAME instrument, a DIFFERENT
    # filing but the SAME host. Without the cross-batch cooldown, this
    # would retry immediately since _OfficialFilingBatchState is fresh here.
    batch_b_sources = _sources(profile, 1, start=1)
    assert batch_b_sources[0].url != batch_a_sources[0].url
    budget_b = _budget(profile)
    token = _scope.set(budget_b)
    try:
        completed_b = await repo._fetch_official_filings(profile, _filings(batch_b_sources), set())
    finally:
        _scope.reset(token)

    # The second batch's filing must be skipped on the cross-batch cooldown
    # -- no new network attempt for this host so soon after the first
    # batch's failure.
    assert batch_b_sources[0].url not in fetcher.urls
    assert fetcher.urls == [batch_a_sources[0].url]  # still just the one call


@pytest.mark.asyncio
async def test_host_cooldown_expires_and_allows_a_later_legitimate_retry():
    repo = _repo(max_transport_failures_per_host=1, host_cooldown_seconds=0.05)
    profile = _profile()
    batch_a_sources = _sources(profile, 1)
    fetcher = _GatedFetcher(fail_urls=frozenset({batch_a_sources[0].url}))
    repo._fetcher = fetcher

    budget_a = _budget(profile)
    token = _scope.set(budget_a)
    try:
        await repo._fetch_official_filings(profile, _filings(batch_a_sources), set())
    finally:
        _scope.reset(token)
    assert fetcher.urls == [batch_a_sources[0].url]

    # Let the short cooldown elapse.
    await asyncio.sleep(0.08)

    batch_b_sources = _sources(profile, 1, start=1)
    budget_b = _budget(profile)
    token = _scope.set(budget_b)
    try:
        completed_b = await repo._fetch_official_filings(profile, _filings(batch_b_sources), set())
    finally:
        _scope.reset(token)

    # Cooldown has elapsed: the later batch's filing on the same host must
    # be genuinely retried (a fresh attempt, not permanently blacklisted).
    assert batch_b_sources[0].url in fetcher.urls
    assert completed_b is True


@pytest.mark.asyncio
async def test_host_cooldown_is_scoped_per_instrument_not_global():
    """A different instrument sharing the same NSE host must NOT be
    affected by another instrument's cooldown -- the cooldown key is
    (instrument_id, host), not just host."""
    repo = _repo(max_transport_failures_per_host=1, host_cooldown_seconds=60.0)
    profile_a = _profile()
    from uuid import UUID
    profile_b = profile_a.model_copy(update={"instrument_id": UUID(int=9999), "company_id": UUID(int=8888)})
    repo.profiles = [profile_a, profile_b]

    source_a = _sources(profile_a, 1)[0]
    fetcher = _GatedFetcher(fail_urls=frozenset({source_a.url}))
    repo._fetcher = fetcher

    budget_a = _budget(profile_a)
    token = _scope.set(budget_a)
    try:
        await repo._fetch_official_filings(profile_a, _filings([source_a]), set())
    finally:
        _scope.reset(token)

    source_b = _sources(profile_b, 1, start=1)[0]  # distinct doc suffix -> distinct URL, not in fail_urls
    budget_b = _budget(profile_b)
    token = _scope.set(budget_b)
    try:
        completed_b = await repo._fetch_official_filings(profile_b, _filings([source_b]), set())
    finally:
        _scope.reset(token)

    # Instrument B's own filing on the same host must still be attempted --
    # instrument A's cooldown must not leak across instruments.
    assert source_b.url in fetcher.urls
    assert completed_b is True
