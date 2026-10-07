"""End-to-end tests for manual-evidence support of the four requirements
added under the "CONTINUE FROM CURRENT STATE -- FIX MANUAL EVIDENCE RUNTIME
DEFECTS" task: LATEST_PRICE, HISTORICAL_PRICE_SERIES, VALUATION_INPUTS,
SECTOR_MACRO.

These four were previously excluded from manual evidence on market-integrity
grounds; the task's authoritative product rule (status != READY_FRESH =>
Upload Evidence must be offered, for EVERY requirement) supersedes that
restriction, but ONLY once each type has a genuine, validated,
provenance-tracked, lower-authority manual contract -- never a frontend
button with no backend behind it. Every test here exercises the REAL,
unmocked ResearchReadinessRuntime: no readiness status is ever asserted
without having been derived by it, and no test sets a status directly.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from app.fact_precedence import FactSourceTier, FinancialFact, FinancialFactKey
from app.manual_evidence import (
    AcceptanceError,
    EvidenceType,
    ManualEvidenceAcceptor,
    ManualEvidenceIngestor,
    SUPPORTED_EVIDENCE_TYPES,
)
from app.models import ProvenancedValue, SourceMode
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository
from app.research_readiness_runtime import (
    ResearchReadinessRuntime,
    RepositoryResearchReadinessAdapter,
    jurisdiction_for_profile,
    readiness_response,
)
from app.settings import Settings

RELIANCE_ID = UUID("44444444-4444-4444-4444-444444444444")
AIXTRON_ID = UUID("11111111-1111-1111-1111-111111111111")  # different demo-profile instrument, for isolation


def _settings() -> Settings:
    return Settings(
        research_mcp_gateway_base_url="http://localhost",
        research_mcp_gateway_timeout_seconds=30,
        research_mcp_service_identity="test",
        research_mcp_first_enabled=False,
        research_readiness_ensure_timeout_seconds=30,
        research_opportunity_scheduler_enabled=False,
        country="IN",
        exchange="XNSE",
    )


def _days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


@pytest.fixture
def repository():
    return ResearchRepository(settings=_settings(), persistence=SqliteResearchPersistence())


@pytest.fixture
def ingestor(repository):
    from app import manual_evidence
    manual_evidence._drafts.clear()
    return ManualEvidenceIngestor(repository=repository)


@pytest.fixture
def acceptor(repository):
    from app import manual_evidence
    manual_evidence._drafts.clear()
    adapter = RepositoryResearchReadinessAdapter(repository)
    runtime = ResearchReadinessRuntime(repository, adapter, None, ensure_timeout_seconds=30)
    return ManualEvidenceAcceptor(repository=repository, runtime=runtime)


async def _requirement(repository, instrument_id, requirement_id):
    """Read current readiness for one requirement through the REAL runtime."""
    adapter = RepositoryResearchReadinessAdapter(repository)
    runtime = ResearchReadinessRuntime(repository, adapter, None, ensure_timeout_seconds=30)
    profile = repository.profile(instrument_id)
    result = await runtime.read(instrument_id, jurisdiction=jurisdiction_for_profile(profile))
    resp = readiness_response(result, runtime.requirement_registry)
    return next(r for r in resp["requirements"] if r["requirementId"] == requirement_id)


class TestCapabilityContract:
    def test_all_twelve_evidence_types_are_supported(self):
        for etype in (
            "SHAREHOLDING", "CURRENT_NEWS", "ORDER_BOOK_CAPEX_GUIDANCE", "GOVERNANCE_HISTORY",
            "BUSINESS_QUALITY_FACTS", "GROWTH_FACTS", "BALANCE_SHEET_FACTS", "QUARTERLY_FINANCIALS",
            "VALUATION_INPUTS", "LATEST_PRICE", "HISTORICAL_PRICE_SERIES", "SECTOR_MACRO",
        ):
            assert EvidenceType(etype) in SUPPORTED_EVIDENCE_TYPES
        assert len(SUPPORTED_EVIDENCE_TYPES) == 12


class TestLatestPrice:
    @pytest.mark.asyncio
    async def test_reject_accept_without_corrections(self, ingestor, acceptor):
        draft = ingestor.ingest(evidence_type="LATEST_PRICE", file_bytes=b"x", filename="p.txt", instrument_id=RELIANCE_ID)
        with pytest.raises(AcceptanceError):
            await acceptor.accept(draft_id=draft.draft_id, corrections={})

    @pytest.mark.asyncio
    async def test_malformed_price_rejected(self, ingestor, acceptor):
        draft = ingestor.ingest(evidence_type="LATEST_PRICE", file_bytes=b"x", filename="p.txt", instrument_id=RELIANCE_ID)
        with pytest.raises(AcceptanceError, match="INVALID_PRICE"):
            await acceptor.accept(draft_id=draft.draft_id, corrections={
                "price": "not-a-number", "currency": "INR", "asOf": _iso_days_ago(1), "source": "manual note",
            })

    @pytest.mark.asyncio
    async def test_negative_price_rejected(self, ingestor, acceptor):
        draft = ingestor.ingest(evidence_type="LATEST_PRICE", file_bytes=b"x", filename="p.txt", instrument_id=RELIANCE_ID)
        with pytest.raises(AcceptanceError, match="INVALID_PRICE"):
            await acceptor.accept(draft_id=draft.draft_id, corrections={
                "price": "-100", "currency": "INR", "asOf": _iso_days_ago(1), "source": "manual note",
            })

    @pytest.mark.asyncio
    async def test_future_as_of_rejected(self, ingestor, acceptor):
        draft = ingestor.ingest(evidence_type="LATEST_PRICE", file_bytes=b"x", filename="p.txt", instrument_id=RELIANCE_ID)
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        with pytest.raises(AcceptanceError, match="AS_OF_IN_FUTURE"):
            await acceptor.accept(draft_id=draft.draft_id, corrections={
                "price": "1500", "currency": "INR", "asOf": future, "source": "manual note",
            })

    @pytest.mark.asyncio
    async def test_invalid_currency_rejected(self, ingestor, acceptor):
        draft = ingestor.ingest(evidence_type="LATEST_PRICE", file_bytes=b"x", filename="p.txt", instrument_id=RELIANCE_ID)
        with pytest.raises(AcceptanceError, match="INVALID_CURRENCY"):
            await acceptor.accept(draft_id=draft.draft_id, corrections={
                "price": "1500", "currency": "rupees", "asOf": _iso_days_ago(1), "source": "manual note",
            })

    @pytest.mark.asyncio
    async def test_missing_source_rejected(self, ingestor, acceptor):
        draft = ingestor.ingest(evidence_type="LATEST_PRICE", file_bytes=b"x", filename="p.txt", instrument_id=RELIANCE_ID)
        with pytest.raises(AcceptanceError, match="MISSING_SOURCE"):
            await acceptor.accept(draft_id=draft.draft_id, corrections={
                "price": "1500", "currency": "INR", "asOf": _iso_days_ago(1), "source": "",
            })

    @pytest.mark.asyncio
    async def test_valid_upload_fills_gap_with_user_upload_provenance_never_fabricates_ready_fresh(
        self, ingestor, acceptor, repository,
    ):
        before = await _requirement(repository, RELIANCE_ID, "LATEST_PRICE")
        assert before["status"] != "READY_FRESH"

        draft = ingestor.ingest(evidence_type="LATEST_PRICE", file_bytes=b"x", filename="p.txt", instrument_id=RELIANCE_ID)
        result = await acceptor.accept(draft_id=draft.draft_id, corrections={
            "price": "1482.50", "currency": "INR", "asOf": _iso_days_ago(1), "source": "manual close reference",
        })
        assert result["persisted"] is True
        req = next(r for r in result["readiness"]["requirements"] if r["requirementId"] == "LATEST_PRICE")
        # The readiness engine remains authoritative: a single manual price
        # point is evidence, but must never be silently promoted to
        # READY_FRESH by the accept path itself.
        assert req["status"] != "READY_FRESH"
        assert req["sourceProvider"] == "USER_UPLOAD"
        assert req["sourceTier"] == "USER_UPLOAD"

        persisted = [f for f in repository.financial_facts_for(RELIANCE_ID) if f.key.metric == "manual_latest_price"]
        assert len(persisted) == 1
        assert persisted[0].source_tier == FactSourceTier.USER_UPLOAD
        assert persisted[0].value.value == Decimal("1482.50")

    @pytest.mark.asyncio
    async def test_manual_latest_price_never_outranks_a_trusted_provider_observation(
        self, ingestor, acceptor, repository,
    ):
        """CRITICAL precedence test, mirroring
        test_manual_upload_never_overwrites_stronger_official_evidence in
        test_manual_evidence_financial_facts.py: a trusted tier at the exact
        same key must survive a conflicting manual upload untouched."""
        as_of = datetime.now(timezone.utc) - timedelta(hours=2)
        period_end = as_of.date().isoformat()
        trusted = FinancialFact(
            key=FinancialFactKey(RELIANCE_ID, "manual_latest_price", period_end, "POINT_IN_TIME", "UNKNOWN"),
            value=ProvenancedValue(value=Decimal("1500.00"), unit="INR", source_url="https://yahoo.example/quote",
                                    source_name="YAHOO", source_type="MARKET_DATA", retrieved_at=datetime.now(timezone.utc)),
            source_tier=FactSourceTier.YAHOO, source_provider="YAHOO", source_identity="yahoo:latest_price",
            source_mode=SourceMode.REAL,
        )
        written = repository.persist_international_financial_facts([trusted])
        assert written == 1

        draft = ingestor.ingest(evidence_type="LATEST_PRICE", file_bytes=b"x", filename="p.txt", instrument_id=RELIANCE_ID)
        await acceptor.accept(draft_id=draft.draft_id, corrections={
            "price": "1.00", "currency": "INR", "asOf": as_of.isoformat(), "source": "deliberately conflicting manual price",
        })

        fact = next(f for f in repository.financial_facts_for(RELIANCE_ID) if f.key.metric == "manual_latest_price")
        assert fact.value.value == Decimal("1500.00")
        assert fact.source_tier == FactSourceTier.YAHOO

    @pytest.mark.asyncio
    async def test_later_trusted_observation_supersedes_an_earlier_manual_one(
        self, ingestor, acceptor, repository,
    ):
        as_of = datetime.now(timezone.utc) - timedelta(hours=2)
        period_end = as_of.date().isoformat()
        draft = ingestor.ingest(evidence_type="LATEST_PRICE", file_bytes=b"x", filename="p.txt", instrument_id=AIXTRON_ID)
        await acceptor.accept(draft_id=draft.draft_id, corrections={
            "price": "250.00", "currency": "USD", "asOf": as_of.isoformat(), "source": "manual close reference",
        })
        manual_fact = next(f for f in repository.financial_facts_for(AIXTRON_ID) if f.key.metric == "manual_latest_price")
        assert manual_fact.source_tier == FactSourceTier.USER_UPLOAD

        trusted = FinancialFact(
            key=FinancialFactKey(AIXTRON_ID, "manual_latest_price", period_end, "POINT_IN_TIME", "UNKNOWN"),
            value=ProvenancedValue(value=Decimal("255.00"), unit="USD", source_url="https://yahoo.example/quote",
                                    source_name="YAHOO", source_type="MARKET_DATA", retrieved_at=datetime.now(timezone.utc)),
            source_tier=FactSourceTier.YAHOO, source_provider="YAHOO", source_identity="yahoo:latest_price",
            source_mode=SourceMode.REAL,
        )
        written = repository.persist_international_financial_facts([trusted])
        assert written == 1
        after = next(f for f in repository.financial_facts_for(AIXTRON_ID) if f.key.metric == "manual_latest_price")
        assert after.source_tier == FactSourceTier.YAHOO
        assert after.value.value == Decimal("255.00")


class TestHistoricalPriceSeries:
    @pytest.mark.asyncio
    async def test_duplicate_dates_rejected(self, ingestor, acceptor):
        draft = ingestor.ingest(evidence_type="HISTORICAL_PRICE_SERIES", file_bytes=b"x", filename="h.txt", instrument_id=RELIANCE_ID)
        with pytest.raises(AcceptanceError, match="DUPLICATE_OBSERVATION_DATE"):
            await acceptor.accept(draft_id=draft.draft_id, corrections={
                "currency": "INR", "source": "manual series",
                "observations": f"{_days_ago(2)},100\n{_days_ago(2)},101",
            })

    @pytest.mark.asyncio
    async def test_future_observation_date_rejected(self, ingestor, acceptor):
        draft = ingestor.ingest(evidence_type="HISTORICAL_PRICE_SERIES", file_bytes=b"x", filename="h.txt", instrument_id=RELIANCE_ID)
        future = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%d")
        with pytest.raises(AcceptanceError, match="OBSERVATION_DATE_IN_FUTURE"):
            await acceptor.accept(draft_id=draft.draft_id, corrections={
                "currency": "INR", "source": "manual series", "observations": f"{future},100",
            })

    @pytest.mark.asyncio
    async def test_non_numeric_close_rejected(self, ingestor, acceptor):
        draft = ingestor.ingest(evidence_type="HISTORICAL_PRICE_SERIES", file_bytes=b"x", filename="h.txt", instrument_id=RELIANCE_ID)
        with pytest.raises(AcceptanceError, match="INVALID_OBSERVATION_CLOSE"):
            await acceptor.accept(draft_id=draft.draft_id, corrections={
                "currency": "INR", "source": "manual series", "observations": f"{_days_ago(2)},not-a-number",
            })

    @pytest.mark.asyncio
    async def test_empty_observations_rejected(self, ingestor, acceptor):
        draft = ingestor.ingest(evidence_type="HISTORICAL_PRICE_SERIES", file_bytes=b"x", filename="h.txt", instrument_id=RELIANCE_ID)
        with pytest.raises(AcceptanceError, match="MISSING_OBSERVATIONS"):
            await acceptor.accept(draft_id=draft.draft_id, corrections={
                "currency": "INR", "source": "manual series", "observations": "   ",
            })

    @pytest.mark.asyncio
    async def test_valid_upload_persists_one_fact_per_observation_never_fabricates_ready_fresh(
        self, ingestor, acceptor, repository,
    ):
        before = await _requirement(repository, RELIANCE_ID, "HISTORICAL_PRICE_SERIES")
        assert before["status"] != "READY_FRESH"

        rows = "\n".join(f"{_days_ago(d)},{1400 + d}" for d in range(1, 11))
        draft = ingestor.ingest(evidence_type="HISTORICAL_PRICE_SERIES", file_bytes=b"x", filename="h.txt", instrument_id=RELIANCE_ID)
        result = await acceptor.accept(draft_id=draft.draft_id, corrections={
            "currency": "INR", "source": "manual price history", "observations": rows,
        })
        assert result["persisted"] is True
        req = next(r for r in result["readiness"]["requirements"] if r["requirementId"] == "HISTORICAL_PRICE_SERIES")
        assert req["status"] != "READY_FRESH"
        assert req["sourceProvider"] == "USER_UPLOAD"

        persisted = [f for f in repository.financial_facts_for(RELIANCE_ID) if f.key.metric == "manual_historical_close"]
        assert len(persisted) == 10
        assert all(f.source_tier == FactSourceTier.USER_UPLOAD for f in persisted)


class TestValuationInputs:
    @pytest.mark.asyncio
    async def test_unsupported_metric_rejected(self, ingestor, acceptor):
        draft = ingestor.ingest(evidence_type="VALUATION_INPUTS", file_bytes=b"x", filename="v.txt", instrument_id=RELIANCE_ID)
        with pytest.raises(AcceptanceError):
            await acceptor.accept(draft_id=draft.draft_id, corrections={"facts": [
                {"metric": "pe_ratio", "value": "20", "periodEnd": _days_ago(30), "periodType": "ANNUAL",
                 "unit": "x", "sourceUrl": "https://example.com"},
            ]})

    @pytest.mark.asyncio
    async def test_eps_fact_fills_earnings_basis_input_never_calculates_ratios(
        self, ingestor, acceptor, repository,
    ):
        period = _days_ago(30)
        draft = ingestor.ingest(evidence_type="VALUATION_INPUTS", file_bytes=b"x", filename="v.txt", instrument_id=RELIANCE_ID)
        result = await acceptor.accept(draft_id=draft.draft_id, corrections={"facts": [
            {"metric": "eps", "value": "45.2", "periodEnd": period, "periodType": "ANNUAL",
             "unit": "INR", "sourceUrl": "https://example.com/filing"},
        ]})
        assert result["persisted"] is True
        req = next(r for r in result["readiness"]["requirements"] if r["requirementId"] == "VALUATION_INPUTS")
        # PE/PB/EV_EBITDA/FCF_YIELD/LATEST_USABLE_PRICE are derived ratios
        # from trusted structured market data -- a manual EPS fact alone
        # must never be enough to reach READY_FRESH.
        assert req["status"] != "READY_FRESH"
        assert "EARNINGS_BASIS" in req["coveredInputIds"]

        persisted = [f for f in repository.financial_facts_for(RELIANCE_ID) if f.key.metric == "eps"]
        assert len(persisted) == 1
        assert persisted[0].source_tier == FactSourceTier.USER_UPLOAD


class TestSectorMacro:
    @pytest.mark.asyncio
    async def test_reject_accept_without_corrections(self, ingestor, acceptor):
        draft = ingestor.ingest(evidence_type="SECTOR_MACRO", file_bytes=b"x", filename="s.txt", instrument_id=RELIANCE_ID)
        with pytest.raises(AcceptanceError):
            await acceptor.accept(draft_id=draft.draft_id, corrections={})

    @pytest.mark.asyncio
    async def test_future_as_of_rejected(self, ingestor, acceptor):
        draft = ingestor.ingest(evidence_type="SECTOR_MACRO", file_bytes=b"x", filename="s.txt", instrument_id=RELIANCE_ID)
        future = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")
        with pytest.raises(AcceptanceError, match="AS_OF_IN_FUTURE"):
            await acceptor.accept(draft_id=draft.draft_id, corrections={
                "summary": "Exposure to crude oil feedstock volatility.", "asOf": future, "source": "internal note",
            })

    @pytest.mark.asyncio
    async def test_missing_summary_rejected(self, ingestor, acceptor):
        draft = ingestor.ingest(evidence_type="SECTOR_MACRO", file_bytes=b"x", filename="s.txt", instrument_id=RELIANCE_ID)
        with pytest.raises(AcceptanceError, match="MISSING_SUMMARY"):
            await acceptor.accept(draft_id=draft.draft_id, corrections={
                "summary": "", "asOf": _days_ago(1), "source": "internal note",
            })

    @pytest.mark.asyncio
    async def test_valid_upload_fills_macro_exposure_input_never_mutates_canonical_sector(
        self, ingestor, acceptor, repository,
    ):
        before = await _requirement(repository, RELIANCE_ID, "SECTOR_MACRO")
        assert before["status"] != "READY_FRESH"
        before_classification = before.get("businessClassification")

        draft = ingestor.ingest(evidence_type="SECTOR_MACRO", file_bytes=b"x", filename="s.txt", instrument_id=RELIANCE_ID)
        result = await acceptor.accept(draft_id=draft.draft_id, corrections={
            "summary": "Significant exposure to crude oil feedstock price volatility and refining margins.",
            "asOf": _days_ago(1), "source": "internal sector note",
        })
        assert result["persisted"] is True
        req = next(r for r in result["readiness"]["requirements"] if r["requirementId"] == "SECTOR_MACRO")
        assert req["status"] != "READY_FRESH"
        assert req["sourceProvider"] == "USER_UPLOAD"
        assert req["sourceTier"] == "USER_UPLOAD"
        assert "RELEVANT_MACRO_EVENT_EXPOSURE" in req["coveredInputIds"]
        # Manual evidence must never mutate canonical instrument
        # classification just because a user uploaded a document.
        assert req.get("businessClassification") == before_classification

        docs = repository.documents_for(RELIANCE_ID, source_mode=SourceMode.REAL)
        manual_docs = [d for d in docs if d.canonical_url.startswith("manual-evidence:sector-macro:")]
        assert len(manual_docs) == 1
        assert manual_docs[0].source_name == "USER_UPLOAD"

    @pytest.mark.asyncio
    async def test_wrong_instrument_is_isolated(self, ingestor, acceptor, repository):
        draft = ingestor.ingest(evidence_type="SECTOR_MACRO", file_bytes=b"x", filename="s.txt", instrument_id=AIXTRON_ID)
        await acceptor.accept(draft_id=draft.draft_id, corrections={
            "summary": "Exposure specific to Aixtron's semiconductor equipment sector.",
            "asOf": _days_ago(1), "source": "internal note",
        })
        reliance_docs = repository.documents_for(RELIANCE_ID, source_mode=SourceMode.REAL)
        assert not any(d.canonical_url.startswith("manual-evidence:sector-macro:") for d in reliance_docs)


class TestNoDirectReadinessStatusWrites:
    """No accept path may ever write a readiness status directly -- the
    runtime must be the one and only source of the status returned."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "evidence_type,corrections",
        [
            ("LATEST_PRICE", {"price": "1500", "currency": "INR", "asOf": None, "source": "manual"}),
            ("SECTOR_MACRO", {"summary": "exposure", "asOf": None, "source": "manual"}),
        ],
    )
    async def test_accept_result_status_matches_an_independent_runtime_read(
        self, ingestor, acceptor, repository, evidence_type, corrections,
    ):
        corrections = dict(corrections)
        corrections["asOf"] = _iso_days_ago(1) if evidence_type == "LATEST_PRICE" else _days_ago(1)
        draft = ingestor.ingest(evidence_type=evidence_type, file_bytes=b"x", filename="x.txt", instrument_id=RELIANCE_ID)
        result = await acceptor.accept(draft_id=draft.draft_id, corrections=corrections)
        req_id = evidence_type
        from_accept = next(r for r in result["readiness"]["requirements"] if r["requirementId"] == req_id)

        fresh_repo = ResearchRepository(settings=repository.settings, persistence=repository._persistence)
        from_fresh_read = await _requirement(fresh_repo, RELIANCE_ID, req_id)
        assert from_accept["status"] == from_fresh_read["status"]
