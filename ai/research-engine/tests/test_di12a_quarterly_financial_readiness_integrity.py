"""DI-12A — Quarterly financial readiness evidence integrity.

Proven defect: ``RepositoryResearchReadinessAdapter._append_documents`` added a
``QUARTERLY_FINANCIALS`` evidence item covering the *mandatory*
``LATEST_QUARTERLY_RESULT`` input whenever a parsed/processed document's text
contained a loose keyword ("financial result" / "quarterly result" / "earnings")
and ``latest_fact_period is None or document_at >= latest_fact_period``. The
``latest_fact_period is None`` clause let a keyword-only document *independently*
satisfy ``LATEST_QUARTERLY_RESULT`` even when the document's parse produced zero
persisted ``FinancialFact`` rows (Castrol production: ``OFFICIAL_NSE_FACTS=0`` yet
``QUARTERLY_FINANCIALS`` reported ``READY_FRESH`` with ``sourceProvider=NSE`` off
an NSE June press-release PDF).

Fix: documents must not cover numerical ``QUARTERLY_FINANCIALS`` inputs from
loose keywords. Authoritative numerical coverage already comes from persisted
``FinancialFact`` rows in ``_append_financial_evidence``/``_financial_fact_coverage``
(LATEST_QUARTERLY_RESULT, COMPARABLE_QUARTERS, QUARTERLY_REVENUE, QUARTERLY_PAT,
QUARTERLY_EBITDA_OR_OPERATING_PROFIT, QUARTERLY_EPS, QUARTERLY_MARGINS). The
document-only branch is removed; documents retain their legitimate non-financial
roles (GOVERNANCE_HISTORY / SECTOR_MACRO).

``_append_events`` has NO equivalent branch (it only feeds CURRENT_NEWS /
ORDER_BOOK_CAPEX_GUIDANCE / GOVERNANCE_HISTORY), so an EARNINGS_RELEASE event
cannot fabricate QUARTERLY_FINANCIALS coverage — test 8 proves the negative.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.fact_precedence import FactSourceTier, FinancialFact, FinancialFactKey
from app.models import (
    CompanyResearchProfile,
    DocumentStatus,
    DocumentType,
    EventImpact,
    ProvenancedValue,
    ReliabilityLevel,
    ResearchDocument,
    ResearchEvent,
    ResearchEventType,
    ResearchLifecycleStatus,
    SourceClassification,
    SourceMode,
    SourceType,
    TimeHorizon,
)
from app.research_readiness import (
    ResearchReadinessService,
    ResearchRequirementRegistry,
    ResearchRequirementStatus,
    ResearchSourceTier,
)
from app.research_readiness_runtime import RepositoryResearchReadinessAdapter


INSTRUMENT_ID = UUID("99999999-9999-9999-9999-999999999999")
COMPANY_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
QUARTERLY_INPUTS = (
    "LATEST_QUARTERLY_RESULT",
    "COMPARABLE_QUARTERS",
    "QUARTERLY_REVENUE",
    "QUARTERLY_PAT",
    "QUARTERLY_EBITDA_OR_OPERATING_PROFIT",
    "QUARTERLY_EPS",
    "QUARTERLY_MARGINS",
)


def _profile(instrument_id: UUID = INSTRUMENT_ID) -> CompanyResearchProfile:
    return CompanyResearchProfile(
        instrument_id=instrument_id,
        company_id=COMPANY_ID,
        company_name="Castrol India Ltd.",
        ticker="CASTROLIND",
        exchange="NSE",
        mic="XNSE",
        country="IN",
        currency="INR",
        isin="INE172A01027",
        provider_instrument_ids={"NSE": "CASTROLIND", "YAHOO_FINANCE": "CASTROLIND.NS"},
    )


def _fact(profile, metric, value, period_end, period_type) -> FinancialFact:
    return FinancialFact(
        FinancialFactKey(profile.instrument_id, metric, period_end, period_type, "CONSOLIDATED"),
        ProvenancedValue(
            value=Decimal(value),
            unit="INR crore",
            as_of_date=datetime.fromisoformat(period_end).replace(tzinfo=timezone.utc),
            source_url="https://nsearchives.nseindia.com/corporate/quarterly-result.pdf",
            source_name="NSE",
            source_type="EXCHANGE_ANNOUNCEMENT",
            published_at=NOW - timedelta(days=2),
            retrieved_at=NOW - timedelta(hours=1),
            confidence=0.98,
        ),
        FactSourceTier.OFFICIAL_NSE,
        "NSE",
        f"nse-{metric}-{period_end}-{period_type}",
        SourceMode.REAL,
    )


def _yahoo_fact(profile, metric, value, period_end, period_type) -> FinancialFact:
    return FinancialFact(
        FinancialFactKey(profile.instrument_id, metric, period_end, period_type, "CONSOLIDATED"),
        ProvenancedValue(
            value=Decimal(value),
            unit="INR crore",
            as_of_date=datetime.fromisoformat(period_end).replace(tzinfo=timezone.utc),
            source_url="https://finance.yahoo.com/quote/CASTROLIND.NS",
            source_name="Yahoo Finance",
            source_type="STRUCTURED_MARKET_PROVIDER",
            published_at=NOW - timedelta(days=2),
            retrieved_at=NOW - timedelta(hours=1),
            confidence=0.9,
        ),
        FactSourceTier.YAHOO,
        "YAHOO_FINANCE",
        f"yahoo-{metric}-{period_end}-{period_type}",
        SourceMode.REAL,
    )


def _nse_press_release_doc(profile, *, text, title="Quarterly Results",
                           published_at=NOW - timedelta(days=42)) -> ResearchDocument:
    return ResearchDocument(
        instrument_id=profile.instrument_id,
        company_id=profile.company_id,
        canonical_url="https://www.nseindia.com/corporate/castrol-quarterly-results.pdf",
        original_url="https://www.nseindia.com/corporate/castrol-quarterly-results.pdf",
        title=title,
        source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
        source_classification=SourceClassification.EXCHANGE,
        source_name="NSE",
        content_type="application/pdf",
        document_type=DocumentType.PDF_REFERENCE,
        content_hash="fixture-" + title.lower().replace(" ", "-"),
        status=DocumentStatus.PROCESSED,
        reliability_level=ReliabilityLevel.LEVEL_A,
        entity_resolution_confidence=0.99,
        source_mode=SourceMode.REAL,
        normalized_text=text,
        published_at=published_at,
        retrieved_at=published_at,
    )


def _earnings_release_event(profile) -> ResearchEvent:
    event_at = NOW - timedelta(days=2)
    return ResearchEvent(
        instrument_id=profile.instrument_id,
        company_id=profile.company_id,
        event_type=ResearchEventType.EARNINGS_RELEASE,
        event_date=event_at,
        detected_at=event_at,
        title="Castrol June quarterly results",
        summary="Castrol June quarterly results",
        source_document_id=uuid4(),
        source_url="https://www.nseindia.com/corporate/castrol-quarterly-results.pdf",
        source_type=SourceType.NEWS,
        source_classification=SourceClassification.REPUTABLE_NEWS,
        reliability=ReliabilityLevel.LEVEL_B,
        source_mode=SourceMode.REAL,
        confidence=0.85,
        impact=EventImpact.NEUTRAL,
        time_horizon=TimeHorizon.SHORT_TERM,
        status=ResearchLifecycleStatus.VALIDATED,
        raw_evidence_reference="Castrol June quarterly results",
        published_at=event_at,
        retrieved_at=event_at,
    )


class _Repo:
    """Minimal read-only stand-in for ResearchRepository (no network/DB)."""

    def __init__(self, profile, *, facts=None, documents=None, events=None,
                 structured=None, observations=None, shareholding=None) -> None:
        self._profile = profile
        self.facts = list(facts or [])
        self.structured = list(structured or [])
        self.observations = list(observations or [])
        self.documents = list(documents or [])
        self.events = list(events or [])
        self.shareholding = list(shareholding or [])

    def profile(self, instrument_id):
        return self._profile

    def financial_facts_for(self, instrument_id):
        return [f for f in self.facts if f.key.instrument_id == instrument_id]

    def structured_market_snapshots_for(self, instrument_ids):
        return {i: [x for x in self.structured if x.instrument_id == i] for i in instrument_ids}

    def market_price_observations_for(self, instrument_ids):
        return {i: [x for x in self.observations if x.instrument_id == i] for i in instrument_ids}

    def documents_for(self, instrument_id, source_mode=None):
        return [d for d in self.documents if d.instrument_id == instrument_id]

    def events_for(self, instrument_id, source_mode=None):
        return [e for e in self.events if e.instrument_id == instrument_id]

    def shareholding_for(self, instrument_id, limit=4):
        return [s for s in self.shareholding if s.instrument_id == instrument_id][:limit]


def _nse_quarterly_facts(profile):
    return [
        _fact(profile, "revenue", "100", "2026-08-31", "QUARTERLY"),
        _fact(profile, "pat", "12", "2026-08-31", "QUARTERLY"),
        _fact(profile, "eps", "5", "2026-08-31", "QUARTERLY"),
        _fact(profile, "revenue", "90", "2026-05-31", "QUARTERLY"),
        _fact(profile, "pat", "10", "2026-05-31", "QUARTERLY"),
        _fact(profile, "revenue", "360", "2026-03-31", "ANNUAL"),
        _fact(profile, "pat", "40", "2026-03-31", "ANNUAL"),
    ]


def _yahoo_quarterly_facts(profile):
    return [
        _yahoo_fact(profile, "revenue", "100", "2026-08-31", "QUARTERLY"),
        _yahoo_fact(profile, "pat", "12", "2026-08-31", "QUARTERLY"),
        _yahoo_fact(profile, "eps", "5", "2026-08-31", "QUARTERLY"),
        _yahoo_fact(profile, "revenue", "90", "2026-05-31", "QUARTERLY"),
        _yahoo_fact(profile, "pat", "10", "2026-05-31", "QUARTERLY"),
    ]


def _adapter(repo):
    adapter = RepositoryResearchReadinessAdapter(repo)
    adapter.remember_canonical_metadata(
        repo._profile.instrument_id,
        {"canonicalSector": "Industrials", "updatedAt": NOW.isoformat()},
    )
    return adapter


def _has_latest_doc_evidence(snapshot) -> bool:
    """True iff any QUARTERLY_FINANCIALS evidence item is a document-only
    LATEST_QUARTERLY_RESULT (the DI-12A fabrication pattern)."""
    for item in snapshot.evidence_for("QUARTERLY_FINANCIALS"):
        if "LATEST_QUARTERLY_RESULT" in item.covered_input_ids and item.evidence_id.startswith("document:"):
            return True
    return False


# --------------------------------------------------------------------------------------
# C1: NSE press-release PDF with generic "earnings" + zero persisted facts must NOT
#     add LATEST_QUARTERLY_RESULT.
# --------------------------------------------------------------------------------------
def test_di12a_bare_earnings_document_without_facts_does_not_cover_latest() -> None:
    profile = _profile()
    repo = _Repo(profile, facts=[], documents=[
        _nse_press_release_doc(profile, text="Castrol recycling press release: "
                                            "earnings from collected oil and recyclers."),
    ])
    snapshot = _adapter(repo).load_by_global_instrument_id(
        profile.instrument_id, ResearchRequirementRegistry.default().requirements
    )
    quarterly = snapshot.evidence_for("QUARTERLY_FINANCIALS")
    assert not any("LATEST_QUARTERLY_RESULT" in item.covered_input_ids for item in quarterly)
    assert not _has_latest_doc_evidence(snapshot)
    result = ResearchReadinessService(_adapter(repo)).assess(
        profile.instrument_id, jurisdiction="INDIA", now=NOW
    )
    assert result.for_requirement("QUARTERLY_FINANCIALS").status != ResearchRequirementStatus.READY_FRESH


# --------------------------------------------------------------------------------------
# C2: "financial result" text alone + zero facts must NOT satisfy QUARTERLY_FINANCIALS.
# --------------------------------------------------------------------------------------
def test_di12a_financial_result_text_without_facts_does_not_cover_latest() -> None:
    profile = _profile()
    repo = _Repo(profile, facts=[], documents=[
        _nse_press_release_doc(profile, text="Financial result announcement for the quarter."),
    ])
    snapshot = _adapter(repo).load_by_global_instrument_id(
        profile.instrument_id, ResearchRequirementRegistry.default().requirements
    )
    quarterly = snapshot.evidence_for("QUARTERLY_FINANCIALS")
    assert not any("LATEST_QUARTERLY_RESULT" in item.covered_input_ids for item in quarterly)


# --------------------------------------------------------------------------------------
# C3: "quarterly result" text alone + zero facts must NOT satisfy QUARTERLY_FINANCIALS.
# --------------------------------------------------------------------------------------
def test_di12a_quarterly_result_text_without_facts_does_not_cover_latest() -> None:
    profile = _profile()
    repo = _Repo(profile, facts=[], documents=[
        _nse_press_release_doc(profile, text="Standalone quarterly result for the quarter ended June 30."),
    ])
    snapshot = _adapter(repo).load_by_global_instrument_id(
        profile.instrument_id, ResearchRequirementRegistry.default().requirements
    )
    quarterly = snapshot.evidence_for("QUARTERLY_FINANCIALS")
    assert not any("LATEST_QUARTERLY_RESULT" in item.covered_input_ids for item in quarterly)


# --------------------------------------------------------------------------------------
# C4: real persisted Yahoo quarterly facts still satisfy their QUARTERLY_FINANCIALS
#     inputs (source provenance preserved as APPROVED_SECONDARY).
# --------------------------------------------------------------------------------------
def test_di12a_yahoo_facts_satisfy_quarterly_inputs() -> None:
    profile = _profile()
    repo = _Repo(profile, facts=_yahoo_quarterly_facts(profile))
    adapter = _adapter(repo)
    snapshot = adapter.load_by_global_instrument_id(
        profile.instrument_id, ResearchRequirementRegistry.default().requirements
    )
    quarterly = snapshot.evidence_for("QUARTERLY_FINANCIALS")
    assert quarterly, "expected Yahoo facts to populate QUARTERLY_FINANCIALS"
    covered = {input_id for item in quarterly for input_id in item.covered_input_ids}
    for required in ("LATEST_QUARTERLY_RESULT", "COMPARABLE_QUARTERS",
                     "QUARTERLY_REVENUE", "QUARTERLY_PAT", "QUARTERLY_EPS"):
        assert required in covered, required
    assert all(item.evidence_id.startswith("financial:") for item in quarterly)
    assert all(item.source == "YAHOO_FINANCE" for item in quarterly)
    assert all(item.source_tier == ResearchSourceTier.APPROVED_SECONDARY for item in quarterly)
    result = ResearchReadinessService(adapter).assess(
        profile.instrument_id, jurisdiction="INDIA", now=NOW
    )
    assert result.for_requirement("QUARTERLY_FINANCIALS").status == ResearchRequirementStatus.READY_FRESH
    assert result.for_requirement("QUARTERLY_FINANCIALS").source == "YAHOO_FINANCE"


# --------------------------------------------------------------------------------------
# C5: real persisted OFFICIAL_NSE quarterly facts still satisfy QUARTERLY_FINANCIALS
#     inputs and preserve authoritative (OFFICIAL) source provenance.
# --------------------------------------------------------------------------------------
def test_di12a_official_nse_facts_satisfy_quarterly_inputs_with_authoritative_source() -> None:
    profile = _profile()
    repo = _Repo(profile, facts=_nse_quarterly_facts(profile))
    adapter = _adapter(repo)
    snapshot = adapter.load_by_global_instrument_id(
        profile.instrument_id, ResearchRequirementRegistry.default().requirements
    )
    quarterly = snapshot.evidence_for("QUARTERLY_FINANCIALS")
    assert quarterly
    covered = {input_id for item in quarterly for input_id in item.covered_input_ids}
    assert "LATEST_QUARTERLY_RESULT" in covered
    assert all(item.source == "NSE" for item in quarterly)
    assert all(item.source_tier == ResearchSourceTier.OFFICIAL for item in quarterly)
    result = ResearchReadinessService(adapter).assess(
        profile.instrument_id, jurisdiction="INDIA", now=NOW
    )
    readiness = result.for_requirement("QUARTERLY_FINANCIALS")
    assert readiness.status == ResearchRequirementStatus.READY_FRESH
    assert readiness.source == "NSE"
    assert readiness.source_tier == ResearchSourceTier.OFFICIAL


# --------------------------------------------------------------------------------------
# C6: equivalent Yahoo AND OFFICIAL_NSE facts for the latest quarter — authoritative
#     precedence (NSE > Yahoo) remains unchanged.
# --------------------------------------------------------------------------------------
def test_di12a_official_nse_outranks_yahoo_when_both_cover_latest_quarter() -> None:
    profile = _profile()
    facts = _nse_quarterly_facts(profile) + [
        # Equivalent Yahoo fact for the SAME latest quarter/metric as the NSE revenue:
        _yahoo_fact(profile, "revenue", "99", "2026-08-31", "QUARTERLY"),
    ]
    repo = _Repo(profile, facts=facts)
    adapter = _adapter(repo)
    result = ResearchReadinessService(adapter).assess(
        profile.instrument_id, jurisdiction="INDIA", now=NOW
    )
    readiness = result.for_requirement("QUARTERLY_FINANCIALS")
    assert readiness.status == ResearchRequirementStatus.READY_FRESH
    # NSE (OFFICIAL, authority rank 0 for QUARTERLY_FINANCIALS/INDIA) must outrank
    # Yahoo (APPROVED_SECONDARY). The selected source is the NSE fact, not Yahoo.
    assert readiness.source == "NSE"
    assert readiness.source_tier == ResearchSourceTier.OFFICIAL


# --------------------------------------------------------------------------------------
# C7: a document may still contribute to non-financial requirements it legitimately
#     covers; the fix does not globally suppress documents.
# --------------------------------------------------------------------------------------
def test_di12a_document_still_contributes_to_governance_history_not_quarterly() -> None:
    profile = _profile()
    repo = _Repo(profile, facts=[], documents=[
        _nse_press_release_doc(
            profile,
            title="Regulatory Litigation Update",
            text="The board announced a regulatory fraud investigation into "
                 "earlier earnings recognition and auditor review.",
            published_at=NOW - timedelta(days=10),
        ),
    ])
    snapshot = _adapter(repo).load_by_global_instrument_id(
        profile.instrument_id, ResearchRequirementRegistry.default().requirements
    )
    governance = snapshot.evidence_for("GOVERNANCE_HISTORY")
    assert governance, "governance document must still be recorded"
    assert any("GOVERNANCE_EVIDENCE" in item.covered_input_ids for item in governance)
    assert not _has_latest_doc_evidence(snapshot)
    assert not any("LATEST_QUARTERLY_RESULT" in item.covered_input_ids
                   for item in snapshot.evidence_for("QUARTERLY_FINANCIALS"))


# --------------------------------------------------------------------------------------
# C8: an EARNINGS_RELEASE event (with zero facts / no qualifying document) cannot
#     fabricate QUARTERLY_FINANCIALS/LATEST_QUARTERLY_RESULT coverage.
#     _append_events only feeds CURRENT_NEWS / ORDER_BOOK_CAPEX_GUIDANCE /
#     GOVERNANCE_HISTORY -- confirmed by reading the code -- so this is a
#     proof-of-negative guard test.
# --------------------------------------------------------------------------------------
def test_di12a_earnings_release_event_cannot_fabricate_quarterly_coverage() -> None:
    profile = _profile()
    repo = _Repo(profile, facts=[], events=[_earnings_release_event(profile)])
    snapshot = _adapter(repo).load_by_global_instrument_id(
        profile.instrument_id, ResearchRequirementRegistry.default().requirements
    )
    quarterly = snapshot.evidence_for("QUARTERLY_FINANCIALS")
    assert not any("LATEST_QUARTERLY_RESULT" in item.covered_input_ids for item in quarterly)
    news = snapshot.evidence_for("CURRENT_NEWS")
    assert any(str(item.event_date or "") for item in news)  # the event is still recorded as news
    assert any(item.evidence_id.startswith("event:") for item in news)
