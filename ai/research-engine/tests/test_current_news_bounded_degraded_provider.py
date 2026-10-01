"""Guardian Review (STAGE2_LIVE_RUN_DEFECTS_20260930.md, Issue 5) -- CURRENT_NEWS
bounded degraded-provider work.

The CURRENT_NEWS completion-semantics fix made each explicit_queries call
bypass SearxngSearchDiscoveryProvider's own cross-call degradation backoff on
purpose (a provider merely slow on ONE query must get a fair, fresh attempt
on the next). But nothing then stopped acquire_news() from issuing every
planned query (up to max_queries, default 14) to a provider that has been
degraded/empty for EVERY one of them within that SAME call -- real wasted
work against a proven-unresponsive engine, evidenced in the production run.

The fix: a per-call-only circuit breaker (_MAX_CONSECUTIVE_DEGRADED_QUERIES)
stops issuing further queries to a provider after that many CONSECUTIVE
degraded/empty responses IN THIS CALL. It never persists across calls (a
future freshness retry gets a full fresh attempt), never changes which
candidates get ranked/selected (only degraded/empty responses trigger it,
which contribute no candidates either way), and never changes failure
classification (SEARCH_PROVIDER_DEGRADED is already recorded per degraded
query and dedupes identically whether the loop stops early or runs to
completion).
"""
from __future__ import annotations

import pytest

from app.news_acquisition import _MAX_CONSECUTIVE_DEGRADED_QUERIES, acquire_news

from test_news_acquisition_v2 import NOW, Provider, Repository, company, no_sleep


@pytest.mark.asyncio
async def test_consecutive_degraded_queries_stop_early_bounding_wasted_work():
    provider = Provider(degraded=True)  # every call reports degraded + empty rows
    run, _ = await acquire_news(Repository(), company(), providers=[provider], now=NOW,
                                 sleep=no_sleep, max_queries=14)

    # The real point of the fix: far fewer than 14 queries were actually
    # issued against a provider proven degraded for every one of them.
    assert len(provider.calls) == _MAX_CONSECUTIVE_DEGRADED_QUERIES
    assert len(provider.calls) < 14

    # Failure classification is byte-for-byte unchanged from running the
    # full 14 queries: still FAILED, still exactly SEARCH_PROVIDER_DEGRADED
    # (deduped), never silently upgraded to a false success.
    outcome = run.providers[0]
    assert outcome.outcome == "FAILED"
    assert outcome.failure_code == "SEARCH_PROVIDER_DEGRADED"
    assert outcome.queries_completed == 0
    assert run.outcome == "SEARCH_FAILED"


@pytest.mark.asyncio
async def test_a_later_successful_query_resets_the_consecutive_counter():
    """Two degraded queries followed by a real success, followed by two more
    degraded queries must NOT trip the breaker -- only a genuinely unbroken
    run of consecutive degraded/empty responses should, since a provider
    that just produced a real result a moment ago is not proven stuck."""
    class FlakyProvider:
        provider_name = "flaky"

        def __init__(self):
            self.calls = []

        async def discover(self, company, category, window):
            self.calls.append(window.explicit_queries)
            index = len(self.calls) - 1
            self.last_query_degraded = index != 2  # every call except the 3rd is degraded
            if index == 2:
                from app.source_discovery import CandidateSearchResult
                return [CandidateSearchResult(
                    "Real result", "https://publisher.test/real", "", NOW, "flaky",
                    "q", "q", "CURRENT_NEWS",
                )]
            return []

    provider = FlakyProvider()
    run, _ = await acquire_news(Repository(), company(), providers=[provider], now=NOW,
                                 sleep=no_sleep, max_queries=14)

    # The counter reset on the 3rd (successful) query, so two more degraded
    # queries afterward (well under the threshold) must not trip the
    # breaker -- every planned query the provider actually has must still
    # run.
    assert len(provider.calls) >= 5
    outcome = run.providers[0]
    assert outcome.queries_completed == 1  # the one real success
    assert outcome.candidate_count == 1


@pytest.mark.asyncio
async def test_future_call_gets_a_fully_fresh_attempt_not_a_persisted_bound():
    """The circuit breaker must be scoped to ONE acquire_news() call only --
    a second, later call (the next readiness cycle's freshness retry) must
    issue its own full run of queries, proving nothing persists across
    calls."""
    provider = Provider(degraded=True)
    repo = Repository()
    await acquire_news(repo, company(), providers=[provider], now=NOW, sleep=no_sleep, max_queries=14)
    first_call_count = len(provider.calls)
    assert first_call_count == _MAX_CONSECUTIVE_DEGRADED_QUERIES

    await acquire_news(repo, company(), providers=[provider], now=NOW, sleep=no_sleep, max_queries=14)
    second_call_count = len(provider.calls) - first_call_count
    assert second_call_count == _MAX_CONSECUTIVE_DEGRADED_QUERIES  # fresh, not suppressed to zero
