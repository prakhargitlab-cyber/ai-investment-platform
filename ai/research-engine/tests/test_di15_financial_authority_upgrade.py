"""Generic authority upgrades use canonical identity, never company heuristics."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.fact_precedence import FactSourceTier
from app.financial_authority import authoritative_financial_upgrade_required
from app.persistence import SqliteResearchPersistence
from app.portfolio_orchestration import _refresh_profile_from_global_instrument
from app.repository import ResearchRepository, _InstrumentRefreshGate
from app.research_readiness import ResearchRequirementStatus
from app.research_readiness_runtime import (
    ExistingResearchCapabilityExecutor,
    RepositoryResearchReadinessAdapter, ResearchReadinessRuntime,
)
from app.settings import Settings
from app.stock_rule_engine import _fact_series
from app.structured_research import latest_quarterly_result_from_facts
from test_official_nse_financial_parsing import _profile, _document, JUNE


NOW = datetime.now(timezone.utc)


def _repo():
    repo = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_demo_enabled=False),
        persistence=SqliteResearchPersistence(),
    )
    profile = _profile()
    repo.profiles = [profile]
    return repo, profile


def _facts(repo, *, official=False):
    facts = repo._official_financial_fact_candidates(_document(JUNE))
    if official:
        return facts
    return [replace(fact, source_tier=FactSourceTier.YAHOO, source_provider="YAHOO_FINANCE",
                    source_identity="secondary:" + fact.key.metric,
                    value=fact.value.model_copy(update={
                        "source_name": "Yahoo Finance", "source_url": "https://finance.yahoo.com/quote/EXAMPLE.NS",
                        "retrieved_at": NOW,
                    })) for fact in facts]


def _stored(facts):
    return {f.key: replace(f, value=f.value.model_copy(update={"as_of_date": None, "period": None, "calculation_basis": None})) for f in facts}


def _seed(repo, facts):
    for fact in facts:
        repo._persistence.upsert_financial_fact(fact)


def test_fresh_secondary_requires_upgrade_but_complete_official_does_not():
    repo, profile = _repo()
    assert authoritative_financial_upgrade_required(profile, _facts(repo))
    assert not authoritative_financial_upgrade_required(profile, _facts(repo, official=True))


def test_single_authoritative_quarter_is_not_comparable_history():
    repo, profile = _repo()
    facts = [f for f in _facts(repo, official=True)
             if f.key.period_type == "QUARTERLY" and f.key.period_end == "2026-06-30"]
    assert authoritative_financial_upgrade_required(profile, facts)


@pytest.mark.parametrize("status", ["RESOLVED", "UNVERIFIED", "REJECTED", "PENDING"])
def test_canonical_adapter_removes_unverified_identity(status):
    repo, profile = _repo()
    _refresh_profile_from_global_instrument(profile, {
        "canonicalName": "Synthetic Issuer", "primarySymbol": "SYNTHETIC",
        "primaryExchange": "NSE", "country": "IN", "currency": "INR",
        "providerMappings": [{"provider": "NSE", "providerSymbol": "SYNTHETIC", "status": status}],
    })
    assert "NSE" not in profile.provider_instrument_ids
    assert not authoritative_financial_upgrade_required(profile, _facts(repo))


@pytest.mark.parametrize("change", [{"country": "US"}, {"exchange": "NASDAQ"}, {"provider_instrument_ids": {}}])
def test_ineligible_identity_never_upgrades(change):
    repo, profile = _repo()
    assert not authoritative_financial_upgrade_required(profile.model_copy(update=change), _facts(repo))


def test_inapplicable_requirement_never_upgrades():
    repo, profile = _repo()
    assert not authoritative_financial_upgrade_required(profile, [], SimpleNamespace(applicability="NOT_APPLICABLE"))


@pytest.mark.parametrize("fail", [False, True])
def test_fresh_gate_allows_only_official_upgrade_and_retains_fallback(monkeypatch, fail):
    repo, profile = _repo()
    secondary = _facts(repo)
    _seed(repo, secondary)
    monkeypatch.setattr(repo, "_instrument_refresh_gate", lambda *a, **k: _InstrumentRefreshGate(set(), False, False, "FRESH_AND_COMPLETE"))
    calls = []

    async def discover(_profile, categories, seen):
        calls.append(categories)
        if fail:
            raise RuntimeError("official unavailable")
        return []

    async def forbidden(*a, **k):
        pytest.fail("authority upgrade must not invoke search or registered sources")

    monkeypatch.setattr(repo._official_filing_discovery, "discover", discover)
    monkeypatch.setattr(repo._discovery, "discover", forbidden)
    monkeypatch.setattr(repo, "_fetch_registered_source", forbidden)
    asyncio.run(repo._refresh_live(profile.instrument_id, {"FINANCIAL_RESULTS"}))
    assert calls == [{"FINANCIAL_RESULTS"}]
    assert _stored(repo.financial_facts_for(profile.instrument_id)) == _stored(secondary)
    assert latest_quarterly_result_from_facts(secondary) is not None
    asyncio.run(repo._refresh_live(profile.instrument_id, {"FINANCIAL_RESULTS"}))
    assert len(calls) == 1
    assert repo.financial_authority_upgrade_due(profile, NOW + timedelta(days=4))


def test_persisted_document_repairs_values_and_is_idempotent_without_fetch():
    repo, profile = _repo()
    document = _document(JUNE)
    repo.documents[document.document_id] = document
    candidates = repo._official_financial_fact_candidates(document)
    _seed(repo, candidates)
    original = candidates[0]
    poisoned = replace(original, value=original.value.model_copy(update={"value": Decimal("999999")}))
    repo._persistence.upsert_financial_fact(poisoned, allow_same_tier_correction=True)
    assert asyncio.run(repo._incomplete_persisted_official_financial_documents(profile)) == [document]
    asyncio.run(repo._reconcile_incomplete_persisted_official_financial_facts(profile))
    assert _stored(repo.financial_facts_for(profile.instrument_id)) == _stored(candidates)
    assert asyncio.run(repo._incomplete_persisted_official_financial_documents(profile)) == []
    assert repo._reconcile_persisted_official_financial_document(document) == (True, 0)


def test_missing_facts_reuse_document_and_complete_facts_do_not_reconcile():
    repo, profile = _repo()
    document = _document(JUNE)
    repo.documents[document.document_id] = document
    assert asyncio.run(repo._reconcile_incomplete_persisted_official_financial_facts(profile))
    first = repo.financial_facts_for(profile.instrument_id)
    assert first and not asyncio.run(repo._reconcile_incomplete_persisted_official_financial_facts(profile))
    assert repo.financial_facts_for(profile.instrument_id) == first


def test_automatic_repair_preserves_absent_keys_without_complete_scope():
    repo, profile = _repo()
    document = _document(JUNE)
    repo.documents[document.document_id] = document
    candidates = repo._official_financial_fact_candidates(document)
    original = next(f for f in candidates if f.key.period_end == "2026-06-30" and f.key.metric == "revenue")
    obsolete = replace(original, key=replace(original.key, period_type="ANNUAL"))
    unrelated = replace(original, key=replace(original.key, period_end="2020-06-30"), source_identity="other-document")
    _seed(repo, candidates + [obsolete, unrelated])
    assert not asyncio.run(repo._reconcile_incomplete_persisted_official_financial_facts(profile))
    stored = _stored(repo.financial_facts_for(profile.instrument_id))
    assert obsolete.key in stored
    assert stored[unrelated.key].source_identity == "other-document"
    assert len(stored) == len(candidates) + 2
    assert not asyncio.run(repo._reconcile_incomplete_persisted_official_financial_facts(profile))


def test_document_without_valid_facts_never_claims_authoritative_readiness():
    repo, profile = _repo()
    document = _document("Official financial results announcement, but no supported numerical table.")
    repo.documents[document.document_id] = document
    assert not asyncio.run(repo._reconcile_incomplete_persisted_official_financial_facts(profile))
    adapter = RepositoryResearchReadinessAdapter(repo)
    runtime = ResearchReadinessRuntime(repo, adapter, SimpleNamespace())
    result = asyncio.run(runtime.read(profile.instrument_id, jurisdiction="INDIA"))
    quarterly = result.for_requirement("QUARTERLY_FINANCIALS")
    assert quarterly.status == ResearchRequirementStatus.MISSING
    assert quarterly.source is None
    _seed(repo, _facts(repo))
    quarterly = asyncio.run(runtime.read(profile.instrument_id, jurisdiction="INDIA")).for_requirement("QUARTERLY_FINANCIALS")
    assert quarterly.status == ResearchRequirementStatus.READY_FRESH
    assert quarterly.source != "NSE"


def test_actual_ensure_executor_reuses_document_without_network(monkeypatch):
    repo, profile = _repo()
    _seed(repo, _facts(repo))
    document = _document(JUNE)
    repo.documents[document.document_id] = document

    async def forbidden(*a, **k):
        pytest.fail("persisted document must satisfy the upgrade locally")

    monkeypatch.setattr(repo._official_filing_discovery, "discover", forbidden)
    monkeypatch.setattr(repo._discovery, "discover", forbidden)
    monkeypatch.setattr(repo, "_fetch_registered_source", forbidden)
    adapter = RepositoryResearchReadinessAdapter(repo)
    runtime = ResearchReadinessRuntime(repo, adapter, ExistingResearchCapabilityExecutor(repo, SimpleNamespace(), None))
    result = asyncio.run(runtime.ensure(profile.instrument_id, jurisdiction="INDIA", requirement_ids=["QUARTERLY_FINANCIALS"]))
    assert result.readiness.for_requirement("QUARTERLY_FINANCIALS").source == "NSE"
    assert all(f.source_tier == FactSourceTier.OFFICIAL_NSE for f in repo.financial_facts_for(profile.instrument_id))


def test_official_wins_persistence_presentation_and_rule_series():
    repo, profile = _repo()
    official = _facts(repo, official=True)
    yahoo = [replace(fact, value=fact.value.model_copy(update={"value": Decimal("1")})) for fact in _facts(repo)]
    _seed(repo, yahoo)
    _seed(repo, official)
    _seed(repo, yahoo)
    assert _stored(repo.financial_facts_for(profile.instrument_id)) == _stored(official)
    for combined in [list(_stored(yahoo).values()) + list(_stored(official).values()), list(_stored(official).values()) + list(_stored(yahoo).values())]:
        result = latest_quarterly_result_from_facts(combined)
        assert result.revenue.value == Decimal("8261.11")
        series = _fact_series(SimpleNamespace(financial_facts=combined), ["revenue"], "QUARTERLY")
        assert all(item.source == "NSE" for item in series)
        assert series[-1].value == Decimal("82611100000")
    assert series[-1].unit == "INR"


@pytest.mark.parametrize("key_change", [{"reporting_basis": "CONSOLIDATED"}, {"period_type": "ANNUAL"}])
def test_basis_and_period_type_do_not_satisfy_other_series(key_change):
    repo, profile = _repo()
    quarterly = [f for f in _facts(repo) if f.key.period_type == "QUARTERLY"]
    official = [replace(f, key=replace(f.key, **key_change)) for f in _facts(repo, official=True)
                if f.key.period_type == "QUARTERLY"]
    assert authoritative_financial_upgrade_required(profile, quarterly + official)
    _seed(repo, quarterly + official)
    assert len(repo.financial_facts_for(profile.instrument_id)) == len(quarterly + official)


def test_newer_secondary_period_cannot_be_satisfied_by_stale_official_window():
    repo, profile = _repo()
    official = _facts(repo, official=True)
    newer = [replace(f, key=replace(f.key, period_end="2026-09-30")) for f in _facts(repo)
             if f.key.period_type == "QUARTERLY"]
    assert authoritative_financial_upgrade_required(profile, official + newer)


def test_only_latest_four_observed_periods_require_authority():
    repo, profile = _repo()
    templates = [f for f in _facts(repo, official=True)
                 if f.key.period_type == "QUARTERLY" and f.key.period_end == "2026-06-30"]
    official = [replace(f, key=replace(f.key, period_end=period))
                for period in ("2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30") for f in templates]
    historical_secondary = [replace(f, key=replace(f.key, period_end="2025-06-30"),
                                    source_tier=FactSourceTier.YAHOO, source_provider="YAHOO_FINANCE") for f in templates]
    assert not authoritative_financial_upgrade_required(profile, official + historical_secondary)
    missing_pat = [f for f in official if not (f.key.period_end == "2025-09-30" and f.key.metric == "pat")]
    assert authoritative_financial_upgrade_required(profile, missing_pat + historical_secondary)


@pytest.mark.parametrize("symbol", ["SYNTHALPHA", "SYNTHBETA", "SYNTHGAMMA"])
def test_no_symbol_or_company_dependency(symbol):
    repo, profile = _repo()
    profile = profile.model_copy(update={"instrument_id": uuid4(), "company_name": symbol + " Limited",
                                        "ticker": symbol, "provider_instrument_ids": {"NSE": symbol}})
    facts = [replace(f, key=replace(f.key, instrument_id=profile.instrument_id)) for f in _facts(repo)]
    assert authoritative_financial_upgrade_required(profile, facts)


def test_explicit_ensure_upgrades_fresh_facts_but_read_does_not(monkeypatch):
    repo, profile = _repo()
    _seed(repo, _facts(repo))
    adapter = RepositoryResearchReadinessAdapter(repo)
    calls = []

    async def discover(_profile, categories, seen):
        assert categories == {"FINANCIAL_RESULTS"}
        calls.append("QUARTERLY_FINANCIALS")
        return []

    monkeypatch.setattr(repo._official_filing_discovery, "discover", discover)
    runtime = ResearchReadinessRuntime(
        repo, adapter, ExistingResearchCapabilityExecutor(repo, SimpleNamespace(), None),
    )

    async def run():
        before = await runtime.read(profile.instrument_id, jurisdiction="INDIA")
        assert before.for_requirement("QUARTERLY_FINANCIALS").status == ResearchRequirementStatus.READY_FRESH
        assert not calls
        result = await runtime.ensure(profile.instrument_id, jurisdiction="INDIA", requirement_ids=["QUARTERLY_FINANCIALS"])
        assert calls == ["QUARTERLY_FINANCIALS"]
        assert profile.instrument_id in repo._financial_authority_attempts
        after = result.readiness.for_requirement("QUARTERLY_FINANCIALS")
        assert after.status == ResearchRequirementStatus.READY_FRESH
        assert after.source != "NSE"
        await runtime.ensure(profile.instrument_id, jurisdiction="INDIA", requirement_ids=["QUARTERLY_FINANCIALS"])
        assert calls == ["QUARTERLY_FINANCIALS"]

    asyncio.run(run())
