"""Stored-document and same-period selection regressions; no provider access."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import test_di12c_financial_sector_balance_sheet as lender
import test_di12c_shareholding_precedence as ownership
import test_official_nse_financial_parsing as official
import test_research_readiness as rr
from app.models import ShareholdingCategory, ShareholdingSnapshotValue
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository
from app.research_applicability import classify_financial_subtype, classify_requirements
from app.research_readiness import ResearchRequirementStatus
from app.research_readiness_runtime import (
    RepositoryResearchReadinessAdapter, _evidence_from_financial_fact, _financial_fact_coverage,
)
from app.settings import Settings
from app.stock_rule_engine import AreaScoreStatus, StockRuleEngineV1
from app.structured_research import official_financial_sector_ratio_facts


# Verbatim forms from the already-stored NSE June-quarter filing, including OCR.
STORED_CRAR_EXCERPT = (
    "Unaudited Standalone and Consolidated Financial Results for the quarter ended June 30, 2026. "
    "Capital Adeauacv Ratio (CRAR) (%) 43.39% "
    "Liquidity Coverage Ratio (LCR) (%) (average of last 90 days) 244.96%"
)


def repository():
    return ResearchRepository(
        settings=Settings(research_live_enabled=False, research_demo_enabled=False),
        persistence=SqliteResearchPersistence(":memory:"),
    )


@pytest.mark.parametrize("industry,expected", [
    ("Mortgage Finance", "FINANCIAL_SECTOR"),
    ("Housing Finance", "FINANCIAL_SECTOR"),
    ("Housing Finance Company", "FINANCIAL_SECTOR"),
    ("Housing Finance Company / HFC", "FINANCIAL_SECTOR"),
    ("HFC", "FINANCIAL_SECTOR"),
    ("Mortgage-Finance", "FINANCIAL_SECTOR"),
    ("Asset Management", "ASSET_MANAGEMENT"),
    ("Auto Components", "CORPORATE"),
    ("SHFC Manufacturing", "CORPORATE"),
])
def test_canonical_industry_subtype(industry, expected):
    assert classify_financial_subtype(industry, "Financial Services") == expected


@pytest.mark.parametrize("label", [
    "Capital Adeauacv Ratio (CRAR) (%)", "CRAR", "Capital Adequacy Ratio",
])
def test_stored_month_day_quarter_and_existing_crar_aliases(label):
    document = official._document(STORED_CRAR_EXCERPT.replace("Capital Adeauacv Ratio (CRAR) (%)", label))
    facts = official_financial_sector_ratio_facts(document)
    assert [(f.key.metric, f.key.period_end, f.value.value) for f in facts] == [
        ("capital_adequacy", "2026-06-30", Decimal("43.39")),
    ]
    assert facts[0].source_identity == str(document.document_id)


def test_ratio_only_stored_document_is_reconciled_persisted_and_used_by_lender():
    repo = repository()
    profile = official._profile()
    repo.profiles = [profile]
    document = official._document(STORED_CRAR_EXCERPT)
    repo.documents[document.document_id] = document
    repo._persistence.upsert_document(document)
    assert asyncio.run(repo._reconcile_incomplete_persisted_official_financial_facts(profile))
    facts = repo._persistence.load_financial_facts({profile.instrument_id})
    assert [(f.key.metric, f.value.value) for f in facts] == [("capital_adequacy", Decimal("43.39"))]
    assert facts[0].source_identity == str(document.document_id)
    assert not asyncio.run(repo._reconcile_incomplete_persisted_official_financial_facts(profile))
    evidence = []
    for fact in facts:
        coverage = _financial_fact_coverage(fact, fact.key.metric, {}, set(), set(), None)
        evidence.append(_evidence_from_financial_fact(fact, "BALANCE_SHEET_FACTS", tuple(coverage["BALANCE_SHEET_FACTS"])))
    snapshot = replace(rr.complete_snapshot(overrides={"BALANCE_SHEET_FACTS": tuple(evidence)}),
        applicability_by_requirement=classify_requirements("Financial Services", "Mortgage Finance", "CANONICAL_REFERENCE"))
    _, readiness, _ = rr.assess(snapshot)
    assert readiness.for_requirement("BALANCE_SHEET_FACTS").status == ResearchRequirementStatus.READY_FRESH
    inputs = replace(lender._financial_inputs(facts=facts), profile=profile, readiness=readiness,
        canonical_metadata={"canonicalSector": "Financial Services", "officialIndustry": "Mortgage Finance"})
    scored = StockRuleEngineV1()._balance_sheet(inputs)
    assert scored.status != AreaScoreStatus.UNSCORABLE
    assert "CAPITAL_ADEQUACY" in {m.metric for m in scored.metrics}


def test_mortgage_finance_corporate_facts_cannot_satisfy_lender_readiness():
    lender.test_balance_sheet_readiness_and_scorer_agree("Mortgage Finance", False)


def test_complete_statement_facts_do_not_hide_unpersisted_crar():
    repo = repository()
    profile = official._profile()
    repo.profiles = [profile]
    document = official._document(official.JUNE)
    repo.documents[document.document_id] = document
    repo._persistence.upsert_document(document)
    repo._reconcile_persisted_official_financial_document(document)
    document = document.model_copy(update={"normalized_text": official.JUNE + " CRAR 43.39%"})
    repo.documents[document.document_id] = document
    repo._persistence.upsert_document(document)
    assert asyncio.run(repo._reconcile_incomplete_persisted_official_financial_facts(profile))
    ratios = [f for f in repo._persistence.load_financial_facts({profile.instrument_id}) if f.key.metric == "capital_adequacy"]
    assert len(ratios) == 1
    assert ratios[0].value.value == Decimal("43.39")


@pytest.mark.parametrize("reverse_insertion", [False, True])
@pytest.mark.parametrize("incomplete_newer", [False, True])
@pytest.mark.parametrize("pledge", [None, Decimal("0"), Decimal("1.25")])
def test_same_period_durable_xbrl_ownership_wins_deterministically(reverse_insertion, incomplete_newer, pledge):
    repo = repository()
    period = datetime(2026, 6, 30, tzinfo=timezone.utc)
    older = rr.NOW - timedelta(days=2)
    newer = rr.NOW - timedelta(days=1)
    values = [ShareholdingSnapshotValue(category=category, percentage=Decimal(value), source_locator=f"nse-xbrl:{category}")
        for category, value in [(ShareholdingCategory.PROMOTER, "60"), (ShareholdingCategory.FII_FPI, "10"),
                               (ShareholdingCategory.DII, "20"), (ShareholdingCategory.PUBLIC_RETAIL, "10")]]
    if pledge is not None:
        values.append(ShareholdingSnapshotValue(category=ShareholdingCategory.PROMOTER_PLEDGE, percentage=pledge,
            metric_basis="PERCENT_OF_PROMOTER_HOLDING", source_locator="nse-xbrl:pledge"))
    qualifying = ownership._shareholding_snapshot().model_copy(update={
        "period_end": period, "research_document_id": None, "source_type": "NSE_SHAREHOLDING_XBRL",
        "source_identity_key": "official-xbrl", "values": values,
        "published_at": older if incomplete_newer else newer, "retrieved_at": older if incomplete_newer else newer,
    })
    incomplete = ownership._shareholding_snapshot().model_copy(update={
        "period_end": period, "research_document_id": None, "source_identity_key": "incomplete-pdf",
        "values": [ShareholdingSnapshotValue(category=ShareholdingCategory.INSURANCE, percentage=Decimal("2"))],
        "published_at": newer if incomplete_newer else older, "retrieved_at": newer if incomplete_newer else older,
    })
    snapshots = [qualifying, incomplete]
    for snapshot in reversed(snapshots) if reverse_insertion else snapshots:
        assert repo.persist_shareholding_snapshot(snapshot)
    # Exercise the durable read, with both possible enumeration orders.
    loaded = repo._persistence.load_shareholding_snapshots({qualifying.instrument_id})
    repo.shareholding_snapshots = {s.id: s for s in (reversed(loaded) if reverse_insertion else loaded)}
    selected = repo.shareholding_for(qualifying.instrument_id)
    assert len(selected) == 1
    assert selected[0].source_identity_key == qualifying.source_identity_key
    assert selected[0].source_url == qualifying.source_url
    assert {v.category: v for v in selected[0].values} == {v.category: v for v in qualifying.values}
    assert not repo._shareholding_snapshot_needs_processing(qualifying)
    assert repo._category_has_qualifying_evidence(qualifying.instrument_id, "SHAREHOLDING_PATTERN")
    evidence = {"SHAREHOLDING": []}
    RepositoryResearchReadinessAdapter._append_shareholding(evidence, selected)
    _, readiness, _ = rr.assess(rr.complete_snapshot(overrides={"SHAREHOLDING": tuple(evidence["SHAREHOLDING"])}))
    area = readiness.for_requirement("SHAREHOLDING")
    assert area.status == ResearchRequirementStatus.READY_FRESH
    assert "PROMOTER_INSTITUTIONAL_PUBLIC_CATEGORIES" not in area.missing_input_ids
    pledge_values = [v.percentage for v in selected[0].values if v.category == ShareholdingCategory.PROMOTER_PLEDGE]
    assert pledge_values == ([] if pledge is None else [pledge])
