"""CURRENT_NEWS search completion semantics (query-plan completion, not engine health).

Uses the real SearXNG adapter against scripted HTTP responses so the provider's
own degraded/CAPTCHA handling is exercised together with acquire_news().
"""
from __future__ import annotations

import httpx
import pytest

from app.failure_taxonomy import TECHNICAL_RETRYABLE, classify_reason, classify_requirement_failures
from app.news_acquisition import acquire_news
from app.news_intelligence import search_state
from app.research_readiness_runtime import _incomplete_news_search_reason
from app.source_discovery import SearxngSearchDiscoveryProvider

from test_news_acquisition_v2 import NOW, Provider, Repository, company, no_sleep

QUERIES = 4
COMPLETE = {"SEARCH_COMPLETE_WITH_EVENTS", "SEARCH_COMPLETE_NO_EVENTS"}


class ScriptedClient:
    """One scripted response (or exception) per planned query, in order."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def get(self, url, params, headers=None):
        item = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


def ok(*, unresponsive=()):
    return httpx.Response(200, json={
        "results": [
            {"title": "Generic Cable Limited order", "url": "https://publisher.test/a", "content": "x", "engine": "duckduckgo"},
            {"title": "Copper prices", "url": "https://publisher.test/b", "content": "y", "engine": "brave"},
        ],
        "unresponsive_engines": [list(item) for item in unresponsive],
    })


def empty(*, unresponsive=()):
    return httpx.Response(200, json={"results": [], "unresponsive_engines": [list(item) for item in unresponsive]})


CAPTCHA = (("google", "CAPTCHA"), ("bing", "too many requests"))


async def run_with(script, **kwargs):
    client = ScriptedClient(script)
    provider = SearxngSearchDiscoveryProvider("http://searxng/search", client=client)
    repo = Repository()
    run, features = await acquire_news(repo, company(), providers=[provider], industry="Wires and cables",
                                       now=NOW, sleep=no_sleep, max_queries=QUERIES, **kwargs)
    searx = next(p for p in run.providers if p.provider == "searxng")
    return run, searx, client, repo


@pytest.mark.asyncio
async def test_all_queries_complete_with_results_while_some_engines_degraded_is_complete():
    run, searx, client, _ = await run_with([ok(unresponsive=CAPTCHA)] * QUERIES)
    assert client.calls == QUERIES
    assert searx.outcome == "SUCCESS_WITH_RESULTS" and searx.failure_code is None
    assert searx.queries_completed == searx.queries_planned == QUERIES
    assert searx.degraded_queries == QUERIES  # diagnostic preserved, not a failure
    assert run.outcome in COMPLETE and run.coverage == 1
    assert search_state(run, NOW) in {"READY_WITH_EVENTS", "READY_NO_EVENTS"}
    assert _incomplete_news_search_reason(run) is None


@pytest.mark.asyncio
async def test_all_queries_complete_with_genuine_zero_results_is_success_empty():
    run, searx, _, repo = await run_with([empty()] * QUERIES)
    assert searx.outcome == "SUCCESS_EMPTY" and searx.failure_code is None and searx.degraded_queries == 0
    assert run.outcome == "SEARCH_COMPLETE_NO_EVENTS" and run.qualifying_events == 0
    assert repo.fetches == []  # no fabricated candidate/document


@pytest.mark.asyncio
async def test_one_query_throwing_after_successes_is_partial_with_specific_timeout_code():
    run, searx, _, _ = await run_with([ok(), ok(), ok(), httpx.ReadTimeout("slow")])
    assert searx.outcome == "PARTIAL" and searx.queries_completed == QUERIES - 1
    assert searx.failure_code == "SEARCH_PROVIDER_TIMEOUT"
    assert run.outcome == "SEARCH_PARTIAL" and search_state(run, NOW) == "PARTIAL_SEARCH"
    reason = _incomplete_news_search_reason(run)
    assert reason == "SEARCH_PROVIDER_TIMEOUT"
    assert classify_requirement_failures({"CURRENT_NEWS": reason}) == TECHNICAL_RETRYABLE


@pytest.mark.asyncio
async def test_every_query_failing_is_failed_with_rate_limit_code_retained():
    run, searx, _, _ = await run_with([httpx.Response(429, json={})] * QUERIES)
    assert searx.outcome == "FAILED" and searx.queries_completed == 0
    assert searx.failure_code == "SEARCH_PROVIDER_RATE_LIMITED"
    assert run.outcome == "SEARCH_FAILED" and search_state(run, NOW) == "FAILED_SEARCH"
    assert classify_reason(_incomplete_news_search_reason(run)) == TECHNICAL_RETRYABLE


@pytest.mark.asyncio
async def test_all_engines_captcha_or_rate_limited_is_never_success_empty():
    run, searx, _, _ = await run_with([empty(unresponsive=CAPTCHA)] * QUERIES)
    assert searx.outcome == "FAILED" and searx.queries_completed == 0
    assert searx.failure_code.startswith("SEARCH_PROVIDER_DEGRADED")
    assert run.outcome == "SEARCH_FAILED"
    # Mixed: genuine empties plus a query answered by no engine -> still not empty.
    run, searx, _, _ = await run_with([empty(), empty(), empty(unresponsive=CAPTCHA), empty()])
    assert searx.outcome == "PARTIAL" and searx.queries_completed == QUERIES - 1
    assert run.outcome == "SEARCH_PARTIAL"


@pytest.mark.asyncio
async def test_degraded_flag_with_no_rows_from_any_adapter_is_a_failed_query():
    provider = Provider(degraded=True)  # adapter that reports degradation but does not raise
    run, _ = await acquire_news(Repository(), company(), providers=[provider], now=NOW, sleep=no_sleep)
    outcome = run.providers[0]
    assert outcome.outcome == "FAILED" and outcome.failure_code == "SEARCH_PROVIDER_DEGRADED"
    assert run.outcome == "SEARCH_FAILED"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,code", [("fetch", "DOCUMENT_FETCH_FAILED"),
                                          ("extract", "EXTRACTION_FAILED"),
                                          ("budget", "DOCUMENT_BUDGET_EXHAUSTED")])
async def test_document_incompleteness_is_partial_with_specific_code(failure, code):
    client = ScriptedClient([ok()] * QUERIES)
    provider = SearxngSearchDiscoveryProvider("http://searxng/search", client=client)
    repo = Repository()
    if failure == "fetch":
        async def broken(url):
            raise RuntimeError("private upstream error")
        repo._fetcher.fetch = broken
    elif failure == "extract":
        async def blank(url):
            from types import SimpleNamespace
            return SimpleNamespace(text="<html><body></body></html>", content_type="text/html", final_url=url)
        repo._fetcher.fetch = blank
    run, _ = await acquire_news(repo, company(), providers=[provider], industry="Wires and cables", now=NOW,
                                sleep=no_sleep, max_queries=QUERIES, max_documents=1 if failure == "budget" else 6)
    searx = next(p for p in run.providers if p.provider == "searxng")
    assert searx.outcome == "PARTIAL" and code in searx.failure_code.split("|")
    assert run.outcome == "SEARCH_PARTIAL"
    assert "private upstream error" not in run.model_dump_json()
    assert classify_reason(_incomplete_news_search_reason(run)) == TECHNICAL_RETRYABLE


@pytest.mark.asyncio
async def test_unexpected_exception_keeps_class_name_but_never_message():
    provider = Provider(fail=True)
    run, _ = await acquire_news(Repository(), company(), providers=[provider], now=NOW, sleep=no_sleep)
    assert run.providers[0].failure_code == "SEARCH_QUERY_FAILED:RuntimeError"
    assert "SECRET" not in run.model_dump_json()
    assert classify_reason(run.providers[0].failure_code) == TECHNICAL_RETRYABLE
