"""Focused regression tests for the DI-7C event-loop / liveness starvation fix.

Root cause: the ready-path financial-facts read used by the sync readiness gate
``_instrument_refresh_gate`` -> ``_financial_result_documents_with_extracted_facts``
called ``load_financial_facts()`` with *no* instrument filter, performing a full
``SELECT * FROM global_financial_facts`` on the event loop.  With 8 concurrent
baseline ensures (2577 eligible NSE equities) this blocked ``/health`` past the
3s liveness threshold and triggered Kubernetes restarts.

These tests pin the smallest semantics-preserving fix: every financial-facts
read on the readiness/persistence boundary must be bounded to a single
instrument id (cheap indexed lookup) rather than a full-table scan.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.fact_precedence import FactSourceTier, FinancialFact, FinancialFactKey
from app.models import (
    CompanyResearchProfile,
    ProvenancedValue,
    ReliabilityLevel,
    ResearchDocument,
    SourceClassification,
    SourceMode,
    SourceType,
)
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository
from app.settings import Settings

# Minimal parseable NSE quarterly results fixture.  It carries income-statement
# columns (Revenue / PAT / EPS) so the repository's NSE parser yields at least
# one FinancialFact, which is required for the gate and persist paths to reach
# the financial-facts read boundary under test.
_DI7C_JUNE_RESULT_TEXT = (
    "Amounts in Rs. Crore Extract of Statement of Unaudited Financial Results for "
    "the quarter ended 30th June 2026 "
    "Particulars Quarter Ended Year Ended 30th June 31st March 30th June 31st March "
    "2026 2026 2025 2026 "
    "Revenue From Operations 8,261.11 7,335.75 6,915.38 27,284.15 "
    "Net Profit for the period after Tax 1,927.21 1,684.31 1,745.69 6,427.00 "
    "Earning Per Share (of Rs 10 each) Basic (Rs.) 1.47 1.29 1.34 5.36 "
    "Diluted (Rs.) 1.47 1.29 1.34 5.36"
)


def _reliance_profile(repository: ResearchRepository) -> CompanyResearchProfile:
    profile = next(profile for profile in repository.profiles if profile.ticker == "RELIANCE")
    profile.provider_instrument_ids["NSE"] = "RELIANCE"
    return profile


def _build_repo(persistence: SqliteResearchPersistence) -> tuple[ResearchRepository, CompanyResearchProfile]:
    repository = ResearchRepository(
        settings=Settings(research_live_enabled=True),
        persistence=persistence,
    )
    return repository, _reliance_profile(repository)


def _ingest_nse_financial_results(
    repository: ResearchRepository,
    profile: CompanyResearchProfile,
    *,
    suffix: str,
    text: str,
    company_line: str = "Reliance Industries Limited RELIANCE INE002A01018",
) -> ResearchDocument:
    return repository.ingest_fixture(
        original_url=f"https://nsearchives.nseindia.com/corporate/di7c-{suffix}.pdf",
        source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
        source_name="NSE corporate announcements",
        publisher="NSE",
        content_type="text/html",
        body=(
            f"<html><title>{company_line} Financial Results</title>"
            f"<main>{company_line} {text}</main></html>"
        ),
        reliability=ReliabilityLevel.LEVEL_A,
        source_mode=SourceMode.REAL,
        source_classification=SourceClassification.EXCHANGE,
        expected_profile=profile,
    )


def _persist_nse_revenue_fact(
    repository: ResearchRepository,
    profile: CompanyResearchProfile,
    document: ResearchDocument,
) -> FinancialFact:
    key = FinancialFactKey(profile.instrument_id, "revenue", "2026-06-30", "QUARTERLY", None)
    fact = FinancialFact(
        key,
        ProvenancedValue(
            value=Decimal("90000"),
            unit="INR crore",
            source_url="https://nsearchives.nseindia.com/corporate/di7c-revenue.pdf",
            source_name="NSE",
            source_type="EXCHANGE_ANNOUNCEMENT",
            retrieved_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
            confidence=None,
        ),
        FactSourceTier.OFFICIAL_NSE,
        "NSE",
        str(document.document_id),
        SourceMode.REAL,
    )
    repository._persistence.upsert_financial_fact(fact)
    return fact


def _seed_gate_ready_state(
    repository: ResearchRepository,
    profile: CompanyResearchProfile,
) -> ResearchDocument:
    """Ingest a NSE financial-results document + an OFFICIAL_NSE revenue fact.

    This mirrors the minimal state that drives the sync readiness gate to reach
    the financial-facts read boundary at repository.py:2118.
    """
    document = _ingest_nse_financial_results(
        repository, profile, suffix="seed", text=_DI7C_JUNE_RESULT_TEXT
    )
    _persist_nse_revenue_fact(repository, profile, document)
    return document


class _SpyPersistence(SqliteResearchPersistence):
    """Sqlite persistence that records every ``load_financial_facts`` call arg.

    Subclasses ``SqliteResearchPersistence`` so ``ResearchRepository._run_blocking_persistence``
    keeps the in-process (non-offloading) SQLite path, letting assertions observe
    the exact instrument-id argument passed to the bounded read.
    """

    def __init__(self, database_path: str = ":memory:") -> None:
        super().__init__(database_path)
        self.load_financial_facts_calls: list = []

    def load_financial_facts(self, instrument_ids=None):
        self.load_financial_facts_calls.append(instrument_ids)
        return super().load_financial_facts(instrument_ids)


class _SlowScanPersistence(SqliteResearchPersistence):
    """Reproduces the pre-fix full-table scan: an unbounded (``None``) read sleeps."""

    def load_financial_facts(self, instrument_ids=None):
        if instrument_ids is None:
            time.sleep(0.3)
        return super().load_financial_facts(instrument_ids)


def test_financial_result_documents_reads_bounded_to_instrument():
    persistence = _SpyPersistence()
    repository, profile = _build_repo(persistence)
    document = _seed_gate_ready_state(repository, profile)

    docs = repository._financial_result_documents_with_extracted_facts(profile.instrument_id)

    calls = persistence.load_financial_facts_calls
    assert calls, "gate path did not consult financial facts"
    assert all(call == {profile.instrument_id} for call in calls), calls
    assert all(call is not None for call in calls), calls
    assert document.document_id in {doc.document_id for doc in docs}


def test_instrument_refresh_gate_never_unbounded_scan():
    persistence = _SpyPersistence()
    repository, profile = _build_repo(persistence)
    _seed_gate_ready_state(repository, profile)

    now = datetime.now(timezone.utc)
    gate = repository._instrument_refresh_gate(profile, set(), now, force=False)

    calls = persistence.load_financial_facts_calls
    assert calls, "gate did not consult financial facts"
    assert all(call is not None for call in calls), calls
    assert all(call == {profile.instrument_id} for call in calls), calls
    assert gate.state


@pytest.mark.asyncio
async def test_concurrent_baseline_gates_keep_event_loop_responsive():
    """Eight concurrent sync readiness gates must not starve the event loop.

    The readiness gate runs ON the event loop (that is the production starvation
    boundary).  With the fix each gate performs only a bounded indexed read, so
    an ``/health``-style liveness sentinel sleeps 0.1s and still completes well
    inside the budget.  A regressing (unbounded) read would block the loop for
    ~0.3s per gate and trip the deadline.
    """
    persistence = _SlowScanPersistence()
    repository, profile = _build_repo(persistence)
    _seed_gate_ready_state(repository, profile)

    now = datetime.now(timezone.utc)

    async def gate_once() -> None:
        repository._instrument_refresh_gate(profile, set(), now, force=False)
        await asyncio.sleep(0)

    async def liveness_sentinel() -> None:
        await asyncio.sleep(0.1)

    gate_tasks = [asyncio.create_task(gate_once()) for _ in range(8)]
    sentinel = asyncio.create_task(liveness_sentinel())

    await asyncio.wait_for(
        asyncio.gather(*gate_tasks, sentinel),
        timeout=1.0,
    )
    assert sentinel.done(), "liveness sentinel was starved by a blocking financial-facts scan"


@pytest.mark.asyncio
async def test_persist_official_financial_facts_reads_bounded():
    persistence = _SpyPersistence()
    repository, profile = _build_repo(persistence)
    document = _ingest_nse_financial_results(
        repository, profile, suffix="persist", text=_DI7C_JUNE_RESULT_TEXT
    )
    # This read-boundary test models a verified discovery result. A generic
    # fixture ingestion alone no longer grants NSE authority (DI-20E).
    document = document.model_copy(update={"discovery_provider": "NSE_OFFICIAL_API",
                                           "entity_resolution_confidence": 0.99})

    result = await repository._run_blocking_persistence(
        repository._persist_official_financial_facts, document
    )

    calls = persistence.load_financial_facts_calls
    assert calls, "persist path did not consult financial facts"
    assert all(call == {document.instrument_id} for call in calls), calls
    assert all(call is not None for call in calls), calls
    assert result[0] is True
    assert result[1] >= 1, "parsed official facts were not persisted"


@pytest.mark.asyncio
async def test_incomplete_window_documents_reads_bounded():
    persistence = _SpyPersistence()
    repository, profile = _build_repo(persistence)
    _seed_gate_ready_state(repository, profile)

    await repository._incomplete_persisted_official_financial_documents(profile)

    calls = persistence.load_financial_facts_calls
    assert calls, "incomplete-window path did not consult financial facts"
    assert all(call == {profile.instrument_id} for call in calls), calls
    assert all(call is not None for call in calls), calls
