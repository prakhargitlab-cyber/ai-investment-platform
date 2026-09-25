"""Focused stability tests for the five stabilization items.

* ROE = PAT/Equity*100 structured derivation (item 1)
* ROCE = EBIT/(TotalAssets-CurrentLiabilities)*100 derivation (item 2)
* CURRENT_NEWS candidate relevance/recency ordering + bounded fallback (item 5)

Derivation is read-only at the durable boundary (``financial_facts_for`` /
``financial_facts_for_instruments``) and is never persisted: only the
history/projection consumers filter by metric, so derived ``roe``/``roce`` do
not perturb existing financial histories.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from app.fact_precedence import FactSourceTier, fact_source_authority
from app.models import ProvenancedValue, SourceMode
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository
from app.settings import Settings
from app.fact_precedence import FinancialFact, FinancialFactKey
from app.source_discovery import CandidateSearchResult


def _fact(instrument_id, metric, period_end, period_type, basis, value,
          tier=FactSourceTier.OFFICIAL_NSE, provider="NSE", identity="src"):
    return FinancialFact(
        FinancialFactKey(instrument_id, metric, period_end, period_type, basis),
        ProvenancedValue(
            value=Decimal(str(value)), unit="INR crore",
            source_url="https://nse.example/x", source_name="NSE",
            source_type="EXCHANGE", retrieved_at=datetime.now(timezone.utc)),
        tier, provider, identity, SourceMode.REAL,
    )


def _repo():
    from test_official_nse_financial_parsing import _profile
    repo = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_demo_enabled=False),
        persistence=SqliteResearchPersistence(),
    )
    repo.profiles = [_profile()]
    return repo


def test_roe_derivation_matches_pat_over_equity_and_outranks_yahoo():
    repo = _repo()
    inst = uuid4()
    repo._persistence.upsert_financial_fact(
        _fact(inst, "pat", "2026-03-31", "ANNUAL", "CONSOLIDATED", 20))
    repo._persistence.upsert_financial_fact(
        _fact(inst, "total_equity", "2026-03-31", "AS_AT", "CONSOLIDATED", 200))
    repo._persistence.upsert_financial_fact(
        _fact(inst, "roe", "2026-03-31", "ANNUAL", "CONSOLIDATED", 0.99,
              tier=FactSourceTier.YAHOO, provider="YAHOO_FINANCE"))

    # Derived facts are never persisted -- only the Yahoo summary persists.
    persisted = repo._persistence.load_financial_facts({inst})
    persisted_roes = [f for f in persisted if f.key.metric == "roe"]
    assert persisted_roes and all(f.source_tier == FactSourceTier.YAHOO for f in persisted_roes)

    facts = repo.financial_facts_for(inst)
    roes = [f for f in facts if f.key.metric == "roe"]
    derived = [f for f in roes if f.source_tier == FactSourceTier.OFFICIAL_NSE]
    others = [f for f in roes if f.source_tier != FactSourceTier.OFFICIAL_NSE]
    assert len(derived) == 1
    assert derived[0].value.value == Decimal("10.0")           # 20 / 200 * 100
    assert derived[0].value.unit == "PERCENT"
    assert derived[0].key.period_end == "2026-03-31"
    assert derived[0].key.reporting_basis == "CONSOLIDATED"
    # Derived OFFICIAL_NSE (authority 4) outranks the Yahoo summary (authority 2).
    assert fact_source_authority(derived[0].source_tier) > fact_source_authority(others[0].source_tier)


def test_roce_derivation_matches_ebit_over_capital_employed():
    repo = _repo()
    inst = uuid4()
    repo._persistence.upsert_financial_fact(
        _fact(inst, "ebit", "2026-03-31", "ANNUAL", "STANDALONE", 50))
    repo._persistence.upsert_financial_fact(
        _fact(inst, "total_assets", "2026-03-31", "AS_AT", "STANDALONE", 1000))
    repo._persistence.upsert_financial_fact(
        _fact(inst, "current_liabilities", "2026-03-31", "AS_AT", "STANDALONE", 400))

    facts = repo.financial_facts_for(inst)
    roces = [f for f in facts if f.key.metric == "roce"]
    assert len(roces) == 1
    # 50 / (1000 - 400) * 100 = 8.333...
    assert abs(roces[0].value.value - Decimal("8.333333333333333333333333333")) < Decimal("0.001")
    assert roces[0].value.unit == "PERCENT"
    # Not persisted.
    assert not [f for f in repo._persistence.load_financial_facts({inst}) if f.key.metric == "roce"]


def test_no_derivation_without_matching_period_or_basis():
    repo = _repo()
    # Mismatched period_end: pat 2026 + equity 2025 -> different groups -> no ROE.
    inst = uuid4()
    repo._persistence.upsert_financial_fact(
        _fact(inst, "pat", "2026-03-31", "ANNUAL", "STANDALONE", 20))
    repo._persistence.upsert_financial_fact(
        _fact(inst, "total_equity", "2025-03-31", "AS_AT", "STANDALONE", 200))
    assert not [f for f in repo.financial_facts_for(inst) if f.key.metric == "roe"]
    # Mismatched basis: pat CONSOLIDATED + equity STANDALONE -> no ROE.
    inst2 = uuid4()
    repo._persistence.upsert_financial_fact(
        _fact(inst2, "pat", "2026-03-31", "ANNUAL", "CONSOLIDATED", 20))
    repo._persistence.upsert_financial_fact(
        _fact(inst2, "total_equity", "2026-03-31", "AS_AT", "STANDALONE", 200))
    assert not [f for f in repo.financial_facts_for(inst2) if f.key.metric == "roe"]


def test_zero_denominator_skips_derivation():
    repo = _repo()
    inst = uuid4()
    repo._persistence.upsert_financial_fact(
        _fact(inst, "pat", "2026-03-31", "ANNUAL", "STANDALONE", 5))
    repo._persistence.upsert_financial_fact(
        _fact(inst, "total_equity", "2026-03-31", "AS_AT", "STANDALONE", 0))
    assert not [f for f in repo.financial_facts_for(inst) if f.key.metric == "roe"]
    assert not [f for f in repo.financial_facts_for(inst) if f.key.metric == "roce"]


def test_async_batch_path_derives_ratios():
    repo = _repo()
    inst = uuid4()
    repo._persistence.upsert_financial_fact(
        _fact(inst, "pat", "2026-03-31", "ANNUAL", "CONSOLIDATED", 20))
    repo._persistence.upsert_financial_fact(
        _fact(inst, "total_equity", "2026-03-31", "AS_AT", "CONSOLIDATED", 200))
    grouped = asyncio.run(repo.financial_facts_for_instruments({inst}))
    assert "roe" in {f.key.metric for f in grouped[inst]}
    # Empty instrument set yields no derived facts.
    assert asyncio.run(repo.financial_facts_for_instruments(set())) == {}


@pytest.mark.asyncio
async def test_current_news_candidates_ranked_by_recency_not_url():
    from test_news_acquisition_v2 import Repository, Provider, company, NOW, no_sleep
    from app.news_acquisition import acquire_news
    from types import SimpleNamespace
    repo = Repository()

    # Distinct titles but the same issuer-relevant body that test_worker_exposure
    # proves extracts to an event under industry='Wires and cables'. The shared
    # stub otherwise returns identical HTML, which the test double cannot
    # attribute to two different documents.
    async def fetch(url):
        repo.fetches.append(url)
        text = ("<html><title>Copper reaches record high</title>"
                "<body>Pressure builds on cable makers.</body></html>")
        return SimpleNamespace(text=text, content_type="text/html", final_url=url)
    repo._fetcher.fetch = fetch

    # stale URL sorts BEFORE fresh URL lexicographically ('aaa' < 'zzz'), but
    # recency ranking must fetch the fresher candidate first regardless of URL.
    stale = CandidateSearchResult(
        "Old", "https://aaa.publisher.test/old", "snippet", NOW - timedelta(days=5),
        "web", "id", "q", "CURRENT_NEWS")
    fresh = CandidateSearchResult(
        "New", "https://zzz.publisher.test/new", "snippet", NOW,
        "web", "id", "q", "CURRENT_NEWS")
    provider = Provider(results=[stale, fresh])
    run, features = await acquire_news(
        repo, company(), providers=[provider], industry="Wires and cables", now=NOW, sleep=no_sleep)
    # Fresh (discovered_at=NOW) ranks above stale (NOW-5d) -> fetched first,
    # even though 'aaa' sorts before 'zzz' by URL string. (Outcome is not
    # asserted here because the shared stub returns identical HTML for both
    # URLs, which the test double cannot attribute to two documents.)
    assert repo.fetches[0] == fresh.url, repo.fetches
    assert len(repo.fetches) == 2


@pytest.mark.asyncio
async def test_current_news_failing_top_candidate_falls_back_to_next_ranked():
    from test_news_acquisition_v2 import Repository, Provider, candidate, company, NOW, no_sleep
    from app.news_acquisition import acquire_news
    repo = Repository()
    broken_calls = {"n": 0}

    async def broken(url):
        broken_calls["n"] += 1
        raise RuntimeError("transient")
    repo._fetcher.fetch = broken
    # Two candidates; max_documents=2 means both are processed (bounded). The
    # first-ranked candidate's failure must NOT abort the run -- the next
    # ranked candidate is still attempted, and the run stays PARTIAL (never
    # certifies no-events).
    provider = Provider(results=[candidate(), candidate("https://publisher.test/second")])
    run, features = await acquire_news(
        repo, company(), providers=[provider], now=NOW, sleep=no_sleep, max_documents=2)
    assert run.outcome == "SEARCH_PARTIAL"
    assert broken_calls["n"] == 2  # both ranked candidates were attempted (bounded fallback)


def test_derived_ratio_facts_are_timezone_aware_from_naive_period_end():
    """Regression for the DEV Stage-2 failure ``as_of must be timezone-aware``
    (cycle 3ab5f3a1-f437-4c73-9fb1-54e6e144e4dd, ~11s, before any
    acquisition/readiness work).

    Root cause: production PostgreSQL persists financial-facts ``period_end``
    as DATE-only strings / naïve timestamps.  ``_ratio_fact`` built the derived
    ROE/ROCE ``as_of_date`` via ``_period_datetime`` which called
    ``datetime.fromisoformat`` *without* localizing, so a date-only
    ``period_end`` (e.g. ``"2026-03-31"``) produced a **naïve** datetime.
    Stage 2 (``ResearchEvidence.__post_init__`` -> ``_require_aware``,
    ``research_readiness.py:1567``) rejects non-aware ``as_of`` with exactly
    ``ValueError("as_of must be timezone-aware")``, yielding
    ``STAGE2_INTERNAL_ERROR`` for every financial issuer.

    This test exercises the exact durable-read boundary
    (``financial_facts_for`` -> ``load_financial_facts`` -> derived facts) with
    persisted-style DATE-only periods and asserts the Stage-2 invariant
    holds on the derived facts' ``as_of_date``.
    """
    from app.research_readiness import _require_aware

    repo = _repo()
    inst = uuid4()
    # DATE-only period_end, exactly as PostgreSQL/SQLite hydrates it.
    repo._persistence.upsert_financial_fact(
        _fact(inst, "pat", "2026-03-31", "ANNUAL", "CONSOLIDATED", 20))
    repo._persistence.upsert_financial_fact(
        _fact(inst, "total_equity", "2026-03-31", "AS_AT", "CONSOLIDATED", 200))
    repo._persistence.upsert_financial_fact(
        _fact(inst, "ebit", "2026-03-31", "ANNUAL", "CONSOLIDATED", 50))
    repo._persistence.upsert_financial_fact(
        _fact(inst, "total_assets", "2026-03-31", "AS_AT", "CONSOLIDATED", 1000))
    repo._persistence.upsert_financial_fact(
        _fact(inst, "current_liabilities", "2026-03-31", "AS_AT", "CONSOLIDATED", 400))

    facts = repo.financial_facts_for(inst)
    derived = {f.key.metric: f for f in facts}
    assert "roe" in derived and "roce" in derived

    # The exact invariant Stage 2 enforces on a derived fact's as_of. On the
    # broken code this raised ValueError("as_of must be timezone-aware").
    for metric in ("roe", "roce"):
        as_of = derived[metric].value.as_of_date
        _require_aware(as_of, "as_of")
        assert as_of.tzinfo is not None
        assert as_of.utcoffset() is not None
