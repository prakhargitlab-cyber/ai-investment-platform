"""Offline requirement admission through real readiness and authority gates."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from app import deep_investigation, repository as repository_module
from app.deep_investigation import acquisition_budget, investigate
from app.fact_precedence import FinancialFactKey
from app.failure_taxonomy import EVIDENCE_UNAVAILABLE, classify_reason
from app.financial_authority import annual_income_history_gaps, authoritative_financial_upgrade_required
from app.financial_projection import project_semantic_financial_facts
from app.research_readiness import ResearchRequirementStatus as Status, ResearchSourceTier
from app.research_readiness_runtime import (
    ExistingResearchCapabilityExecutor, RepositoryResearchReadinessAdapter, ResearchReadinessRuntime,
)
from test_radar_financial_retry_fixes import (
    INSTRUMENT, NOW, _document, _fact, _receipt, _repository, original,
)


EMPTY = "PRIMARY_FINANCIAL_PROVIDER_RETURNED_NO_FACTS"
FINANCIAL = frozenset({"GROWTH_FACTS", "BUSINESS_QUALITY_FACTS"})
pytestmark = pytest.mark.usefixtures("financial_fixture_clock")


@pytest_asyncio.fixture(autouse=True)
async def no_network_or_database(monkeypatch):
    import psycopg
    import sqlite3

    def forbidden(*args, **kwargs):
        pytest.fail("This regression must use only memory and local fixture bytes")

    # Install after asyncio creates its Windows loopback wakeup socket, and
    # restore before teardown creates another event loop.
    with monkeypatch.context() as guard:
        guard.setattr(socket.socket, "connect", forbidden)
        guard.setattr(psycopg, "connect", forbidden)
        guard.setattr(sqlite3, "connect", forbidden)
        yield


class FinancialRuntime(ResearchReadinessRuntime):
    """Use actual financial coverage; hold unrelated requirements ready."""
    def __init__(self, repository, executor, financial_requirements):
        super().__init__(repository, RepositoryResearchReadinessAdapter(repository), executor)
        self.financial_requirements = financial_requirements

    async def read(self, instrument_id, **kwargs):
        readiness = await super().read(instrument_id, **kwargs)
        return replace(readiness, requirements=tuple(
            row if row.requirement_id in self.financial_requirements else replace(
                row, status=Status.READY_FRESH, source="NSE", source_tier=ResearchSourceTier.OFFICIAL,
                missing_input_ids=(), missing_reason=None)
            for row in readiness.requirements))


def _income_facts(years=(2025, 2026)):
    facts = [_fact(metric, f"{year}-03-31") for year in years for metric in ("revenue", "pat")]
    return facts + [replace(_fact(metric, period), key=FinancialFactKey(
        INSTRUMENT, metric, period, "QUARTERLY", "CONSOLIDATED"))
        for period in ("2026-03-31", "2026-06-30", "2025-09-30", "2025-06-30") for metric in ("revenue", "pat")]


def _case(*, requirements=FINANCIAL, facts=None, documents=(), reason=EMPTY):
    repo, store, profile = _repository(documents, _income_facts() if facts is None else facts)
    repo.market_session_data = AsyncMock(return_value=(None, None))
    for requirement in requirements:
        store.upsert_acquisition_observation(INSTRUMENT, requirement, "READINESS_EXECUTOR", "FAILED",
            NOW - timedelta(days=1), failure_reason=reason)
    prior = list(store.observations)
    calls, downloads = [], []

    async def acquire(instrument_id, categories, **kwargs):
        assert instrument_id == INSTRUMENT and categories == {"FINANCIAL_RESULTS"}
        budget = acquisition_budget(instrument_id)
        assert budget is not None
        calls.append(tuple(budget.member_requirement_ids or (budget.requirement_id,)))
        if not budget.reserve_queries(1) or not await budget.allow_document():
            assert budget.exhausted
            return
        downloads.append("offline-authoritative-response")
        # Simulate a bounded, still-empty authoritative attempt at the existing
        # provider boundary, retaining facts and recording its actual context.
        for document in documents:
            if repo._financial_document_parse_unchanged(document) is None:
                repo._record_financial_document_parse(document, "UNSUPPORTED")
        now = datetime.now(timezone.utc)
        repo._financial_authority_attempts[INSTRUMENT] = now
        await repo.record_acquisition_observation(INSTRUMENT, "QUARTERLY_FINANCIALS", "NSE",
            "SUCCESS_EMPTY", now, source_url=repo._financial_parser_retry_context(profile))
        budget.requirement_failures.update({key: EMPTY for key in calls[-1]})

    repo.refresh_targeted_categories = acquire
    orchestrator = SimpleNamespace(ensure_structured_market=AsyncMock(
        return_value=SimpleNamespace(error=EMPTY)))
    executor = ExistingResearchCapabilityExecutor(repo, SimpleNamespace(), orchestrator)
    runtime = FinancialRuntime(repo, executor, requirements)
    return runtime, repo, store, profile, prior, calls, downloads


def _authority_receipt(repo, store, profile, *, age=timedelta(), outcome="SUCCESS_EMPTY", failure=None):
    store.upsert_acquisition_observation(INSTRUMENT, "QUARTERLY_FINANCIALS", "NSE", outcome,
        NOW - age, failure_reason=failure, source_url=repo._financial_parser_retry_context(profile))


@pytest.mark.asyncio
async def test_empty_secondary_provider_does_not_block_new_authoritative_source():
    runtime, repo, store, _, prior, calls, downloads = _case()
    store.upsert_acquisition_observation(INSTRUMENT, "GROWTH_FACTS", "YAHOO_FINANCE_MCP",
        "SUCCESS_EMPTY", NOW, evidence_count=0)
    before = dict(store.facts)
    result, _, _ = await investigate(runtime, INSTRUMENT, jurisdiction="INDIA")
    assert len(calls) == len(downloads) == 1
    assert set(calls[0]) == FINANCIAL  # One shared capability, not two downloads.
    assert set(result.planned_requirement_ids) == FINANCIAL
    assert store.facts == before and all(row in store.observations for row in prior)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["capability", "version", "content", "requirements"])
async def test_changed_recovery_context_reopens_old_empty_verdict_once(change, monkeypatch):
    document = _document()
    runtime, repo, store, profile, prior, calls, downloads = _case(documents=[document])
    _receipt(repo, document)
    _authority_receipt(repo, store, profile)
    if change == "capability":
        monkeypatch.setattr(repository_module, "FINANCIAL_PARSER_CAPABILITY", "NEW_COORDINATE_CAPABILITY")
    elif change == "version":
        monkeypatch.setattr(repository_module, "FINANCIAL_PARSER_VERSION", "new-parser-version")
    elif change == "content":
        document.normalized_text += " Amended statement"
    else:
        fact = _fact("total_equity", amount="500")
        store.facts[fact.key] = fact
    before = dict(store.facts)
    await investigate(runtime, INSTRUMENT, jurisdiction="INDIA")
    assert len(calls) == len(downloads) == 1
    assert repo._financial_document_parse_unchanged(document) == "UNSUPPORTED"
    for _ in range(2):
        await investigate(runtime, INSTRUMENT, jurisdiction="INDIA")
    assert len(calls) == len(downloads) == 1  # No identical repair-loop acquisition.
    assert store.facts == before and all(row in store.observations for row in prior)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,failure,age", [
    ("SUCCESS_EMPTY", None, timedelta(hours=1)),
    ("FAILED", "HTTP_FETCH_FAILED", timedelta(seconds=1)),
])
async def test_unchanged_empty_condition_respects_authority_backoff(outcome, failure, age):
    document = _document()
    runtime, repo, store, profile, _, calls, downloads = _case(documents=[document])
    _receipt(repo, document)
    _authority_receipt(repo, store, profile, age=age, outcome=outcome, failure=failure)
    observations = list(store.observations)
    result, _, _ = await investigate(runtime, INSTRUMENT, jurisdiction="INDIA")
    assert not calls and not downloads
    assert all(result.failures[key] == EMPTY for key in FINANCIAL)
    assert store.observations == observations


@pytest.mark.asyncio
async def test_in_flight_authority_attempt_prevents_duplicate_recovery(monkeypatch):
    document = _document()
    runtime, repo, store, profile, _, calls, _ = _case(documents=[document])
    _receipt(repo, document)
    _authority_receipt(repo, store, profile, age=timedelta(minutes=5))
    repo._financial_authority_attempts[INSTRUMENT] = NOW
    monkeypatch.setattr(repository_module, "FINANCIAL_PARSER_CAPABILITY", "NEW_CAPABILITY")
    await investigate(runtime, INSTRUMENT, jurisdiction="INDIA")
    assert not calls


@pytest.mark.asyncio
async def test_missing_fy2024_reaches_acquisition_and_preserves_original_pdf_facts(original):
    document = original[0]
    facts = project_semantic_financial_facts(document).facts
    runtime, repo, store, profile, _, calls, downloads = _case(
        requirements={"GROWTH_FACTS"}, facts=facts, documents=[document])
    _receipt(repo, document, "PARSED")
    _authority_receipt(repo, store, profile, age=timedelta(days=4))
    before = dict(store.facts)
    missing = annual_income_history_gaps(facts)
    assert {(key.metric, key.period_end) for key in missing} == {
        ("revenue", "2024-03-31"), ("pat", "2024-03-31")}
    result, _, _ = await investigate(runtime, INSTRUMENT, jurisdiction="INDIA")
    assert calls == [("GROWTH_FACTS",)] and len(downloads) == 1
    assert "ANNUAL_CAGR_INPUTS" in result.readiness.for_requirement("GROWTH_FACTS").missing_input_ids
    assert store.facts == before and not store.reconciliations
    assert repo._financial_document_parse_unchanged(document) == "PARSED"


@pytest.mark.asyncio
async def test_missing_ebit_roce_retries_even_with_complete_income_history():
    facts = _income_facts((2024, 2025, 2026)) + [
        _fact("roe", amount="15", unit="%"), _fact("profit_margin", amount="10", unit="%"),
        _fact("operating_cash_flow", amount="90"), _fact("total_assets", amount="1000"),
        _fact("current_liabilities", amount="300"),
    ]
    runtime, repo, store, profile, _, calls, _ = _case(requirements={"BUSINESS_QUALITY_FACTS"}, facts=facts)
    assert not authoritative_financial_upgrade_required(profile, facts)
    readiness = await runtime.read(INSTRUMENT, jurisdiction="INDIA")
    quality = readiness.for_requirement("BUSINESS_QUALITY_FACTS")
    assert quality.status == Status.PARTIAL and quality.missing_input_ids == ("ROCE",)
    assert not any(f.key.metric == "ebit" for f in facts)
    _authority_receipt(repo, store, profile, age=timedelta(days=4))
    await investigate(runtime, INSTRUMENT, jurisdiction="INDIA")
    assert calls == [("BUSINESS_QUALITY_FACTS",)]
    assert not any(f.key.metric in {"ebit", "roce"} for f in store.facts.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("requirement", sorted(FINANCIAL))
async def test_ready_requirement_does_not_even_check_retry_eligibility(requirement, monkeypatch):
    facts = _income_facts((2024, 2025, 2026)) + [
        _fact("roe", amount="15", unit="%"), _fact("roce", amount="12", unit="%"),
        _fact("profit_margin", amount="10", unit="%"), _fact("operating_cash_flow", amount="90"),
    ]
    runtime, repo, store, _, _, calls, _ = _case(requirements={requirement}, facts=facts)
    monkeypatch.setattr(repo, "financial_authority_upgrade_due", lambda *args: pytest.fail("READY must be reused"))
    observations = list(store.observations)
    result, _, _ = await investigate(runtime, INSTRUMENT, jurisdiction="INDIA")
    assert result.readiness.for_requirement(requirement).status == Status.READY_FRESH
    assert not calls and store.observations == observations


@pytest.mark.asyncio
@pytest.mark.parametrize("requirement", sorted(FINANCIAL))
async def test_missing_financial_requirement_can_reach_eligible_source(requirement):
    runtime, _, _, _, _, calls, downloads = _case(requirements={requirement}, facts=[], reason=None)
    readiness = await runtime.read(INSTRUMENT, jurisdiction="INDIA")
    assert readiness.for_requirement(requirement).status == Status.MISSING
    await investigate(runtime, INSTRUMENT, jurisdiction="INDIA")
    assert calls == [(requirement,)] and len(downloads) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["ROBOTS_OR_ACCESS_BLOCKED", "COMPANY_RELEVANCE_FAILED",
    "EVIDENCE_INSUFFICIENT_WITHIN_PLAN", EMPTY + "|DOMAIN_BLOCKED"])
async def test_genuine_terminal_verdicts_remain_blocked(reason, monkeypatch):
    runtime, repo, store, _, _, calls, _ = _case(reason=reason)
    monkeypatch.setattr(repo, "financial_authority_upgrade_due", lambda *args: pytest.fail("Terminal failure"))
    observations = list(store.observations)
    result, _, _ = await investigate(runtime, INSTRUMENT, jurisdiction="INDIA")
    assert not calls and store.observations == observations
    assert all(result.failures[key] == reason for key in FINANCIAL)
    assert classify_reason(reason) == classify_reason(EMPTY) == EVIDENCE_UNAVAILABLE


@pytest.mark.asyncio
async def test_no_verified_authoritative_source_remains_blocked():
    runtime, repo, _, profile, _, calls, _ = _case()
    repo.profile = lambda *_: profile.model_copy(update={"provider_instrument_ids": {}})
    await investigate(runtime, INSTRUMENT, jurisdiction="INDIA")
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["_SINGLETON_MAX_DOCUMENTS", "_SINGLETON_MAX_QUERIES"])
async def test_reopened_requirement_still_obeys_existing_budget(limit, monkeypatch):
    runtime, _, store, _, _, calls, downloads = _case()
    before = dict(store.facts)
    monkeypatch.setattr(deep_investigation, limit, 0)
    result, _, _ = await investigate(runtime, INSTRUMENT, jurisdiction="INDIA")
    assert len(calls) == 1 and not downloads
    assert all(result.readiness.for_requirement(key).status == Status.PARTIAL for key in FINANCIAL)
    assert store.facts == before


@pytest.mark.asyncio
async def test_oversized_pdf_verdict_allows_other_financial_sources_without_erasing_receipt():
    runtime, repo, store, _, prior, calls, downloads = _case(reason="DOCUMENT_SIZE_LIMIT_EXCEEDED")
    url = "https://nsearchives.nseindia.com/corporate/oversized.pdf"
    await repo.record_document_size_failure(INSTRUMENT, url, 1_500_000)
    receipt = store.observations[-1]
    await investigate(runtime, INSTRUMENT, jurisdiction="INDIA")
    assert len(calls) == len(downloads) == 1
    assert repo.document_size_failure_known(INSTRUMENT, url, 1_500_000)
    assert not repo.document_size_failure_known(INSTRUMENT, url.replace("oversized", "alternative"), 1_500_000)
    assert all(row in store.observations for row in [*prior, receipt])
    await investigate(runtime, INSTRUMENT, jurisdiction="INDIA")
    assert len(calls) == 1  # Ordinary provider backoff still applies.
