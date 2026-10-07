"""End-to-end tests for manual-evidence support of the four FinancialFact-backed
Research Readiness requirements: BUSINESS_QUALITY_FACTS, GROWTH_FACTS,
BALANCE_SHEET_FACTS, QUARTERLY_FINANCIALS.

VALUATION_INPUTS is deliberately out of scope here: two of its three
mandatory inputs (LATEST_USABLE_PRICE, PB) are derived exclusively from
StructuredMarketSnapshotRecord, not FinancialFact (see
app.research_readiness_runtime._structured_fact_coverage), and manual
upload of "latest price" was already excluded on market-integrity grounds.
See the final report for the full rationale.

Every test exercises the REAL, unmocked ResearchReadinessRuntime -- no
readiness status is ever asserted without having been derived by it, and no
test sets readiness_status directly.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from app.fact_precedence import FactSourceTier, FinancialFact, FinancialFactKey
from app.manual_evidence import (
    AcceptanceError,
    ALLOWED_METRICS_BY_EVIDENCE_TYPE,
    EvidenceType,
    FINANCIAL_FACT_EVIDENCE_TYPES,
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
AIXTRON_ID = UUID("11111111-1111-1111-1111-111111111111")  # different demo-profile instrument, for isolation checks


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


# Minimal complete fact sets, one per requirement, built strictly from
# ALLOWED_METRICS_BY_EVIDENCE_TYPE / the mandatory inputs confirmed directly
# from ResearchRequirementRegistry.default() and _financial_fact_coverage.
def _balance_sheet_facts(period: str, source: str = "https://example.com/filing") -> list[dict]:
    return [
        {"metric": "total_debt", "value": "12000", "periodEnd": period, "periodType": "ANNUAL", "unit": "INR crore", "sourceUrl": source},
        {"metric": "total_equity", "value": "45000", "periodEnd": period, "periodType": "ANNUAL", "unit": "INR crore", "sourceUrl": source},
        {"metric": "cash", "value": "8000", "periodEnd": period, "periodType": "ANNUAL", "unit": "INR crore", "sourceUrl": source},
    ]


def _business_quality_facts(period_a: str, period_b: str, source: str = "https://example.com/filing") -> list[dict]:
    return [
        {"metric": "pat", "value": "9000", "periodEnd": period_a, "periodType": "ANNUAL", "unit": "INR crore", "sourceUrl": source},
        {"metric": "pat", "value": "9800", "periodEnd": period_b, "periodType": "ANNUAL", "unit": "INR crore", "sourceUrl": source},
        {"metric": "roe", "value": "14.5", "periodEnd": period_b, "periodType": "ANNUAL", "unit": "%", "sourceUrl": source},
        {"metric": "roce", "value": "16.2", "periodEnd": period_b, "periodType": "ANNUAL", "unit": "%", "sourceUrl": source},
        {"metric": "operating_margin", "value": "18.0", "periodEnd": period_b, "periodType": "ANNUAL", "unit": "%", "sourceUrl": source},
        {"metric": "operating_cash_flow", "value": "11000", "periodEnd": period_b, "periodType": "ANNUAL", "unit": "INR crore", "sourceUrl": source},
    ]


def _growth_facts(q1: str, q2: str, a1: str, a2: str, source: str = "https://example.com/filing") -> list[dict]:
    rows = []
    for metric in ("revenue", "pat"):
        for period, ptype, value in (
            (q1, "QUARTERLY", "50000" if metric == "revenue" else "9000"),
            (q2, "QUARTERLY", "52000" if metric == "revenue" else "9500"),
            (a1, "ANNUAL", "190000" if metric == "revenue" else "35000"),
            (a2, "ANNUAL", "205000" if metric == "revenue" else "38000"),
        ):
            rows.append({"metric": metric, "value": value, "periodEnd": period, "periodType": ptype, "unit": "INR crore", "sourceUrl": source})
    return rows


def _quarterly_facts(q1: str, q2: str, source: str = "https://example.com/filing") -> list[dict]:
    return [
        {"metric": "revenue", "value": "50000", "periodEnd": q1, "periodType": "QUARTERLY", "unit": "INR crore", "sourceUrl": source},
        {"metric": "revenue", "value": "52000", "periodEnd": q2, "periodType": "QUARTERLY", "unit": "INR crore", "sourceUrl": source},
        {"metric": "pat", "value": "9000", "periodEnd": q1, "periodType": "QUARTERLY", "unit": "INR crore", "sourceUrl": source},
        {"metric": "pat", "value": "9500", "periodEnd": q2, "periodType": "QUARTERLY", "unit": "INR crore", "sourceUrl": source},
        {"metric": "eps", "value": "14.2", "periodEnd": q2, "periodType": "QUARTERLY", "unit": "INR", "sourceUrl": source},
    ]


class TestCapabilityContract:
    def test_four_financial_fact_types_are_supported_valuation_inputs_is_not(self):
        assert EvidenceType.BUSINESS_QUALITY_FACTS in SUPPORTED_EVIDENCE_TYPES
        assert EvidenceType.GROWTH_FACTS in SUPPORTED_EVIDENCE_TYPES
        assert EvidenceType.BALANCE_SHEET_FACTS in SUPPORTED_EVIDENCE_TYPES
        assert EvidenceType.QUARTERLY_FINANCIALS in SUPPORTED_EVIDENCE_TYPES
        assert not hasattr(EvidenceType, "VALUATION_INPUTS")

    def test_allowed_metrics_are_nonempty_and_disjoint_from_nothing_fabricated(self):
        for etype in FINANCIAL_FACT_EVIDENCE_TYPES:
            assert len(ALLOWED_METRICS_BY_EVIDENCE_TYPE[etype]) > 0


class TestIngestDraft:
    @pytest.mark.parametrize("etype", [
        "BUSINESS_QUALITY_FACTS", "GROWTH_FACTS", "BALANCE_SHEET_FACTS", "QUARTERLY_FINANCIALS",
    ])
    def test_ingest_produces_a_manual_fields_draft_with_no_auto_extracted_facts(self, ingestor, etype):
        draft = ingestor.ingest(evidence_type=etype, file_bytes=b"placeholder", filename="doc.txt", instrument_id=RELIANCE_ID)
        assert draft.requires_manual_fields is True
        assert draft.proposed_facts == []
        assert draft.manual_field_schema == ("facts",)
        # No normalized data is ever mutated by ingest alone.
        assert draft.status.value == "DRAFT"


class TestValidation:
    @pytest.mark.asyncio
    async def test_accept_without_corrections_is_rejected(self, ingestor, acceptor):
        draft = ingestor.ingest(evidence_type="GROWTH_FACTS", file_bytes=b"x", filename="g.txt", instrument_id=RELIANCE_ID)
        with pytest.raises(AcceptanceError, match="requires fact rows"):
            await acceptor.accept(draft_id=draft.draft_id, corrections={})

    @pytest.mark.asyncio
    async def test_unsupported_metric_is_rejected(self, ingestor, acceptor):
        draft = ingestor.ingest(evidence_type="GROWTH_FACTS", file_bytes=b"x", filename="g.txt", instrument_id=RELIANCE_ID)
        with pytest.raises(AcceptanceError, match="UNSUPPORTED_METRIC"):
            await acceptor.accept(draft_id=draft.draft_id, corrections={"facts": [
                {"metric": "made_up_metric", "value": "1", "periodEnd": _days_ago(10), "periodType": "ANNUAL", "unit": "INR", "sourceUrl": "https://x"}
            ]})

    @pytest.mark.asyncio
    async def test_missing_unit_and_source_are_rejected(self, ingestor, acceptor):
        draft = ingestor.ingest(evidence_type="BALANCE_SHEET_FACTS", file_bytes=b"x", filename="b.txt", instrument_id=RELIANCE_ID)
        with pytest.raises(AcceptanceError) as exc:
            await acceptor.accept(draft_id=draft.draft_id, corrections={"facts": [
                {"metric": "cash", "value": "100", "periodEnd": _days_ago(10), "periodType": "ANNUAL"}
            ]})
        assert "MISSING_UNIT" in str(exc.value)
        assert "MISSING_SOURCE" in str(exc.value)

    @pytest.mark.asyncio
    async def test_non_numeric_value_is_rejected(self, ingestor, acceptor):
        draft = ingestor.ingest(evidence_type="BALANCE_SHEET_FACTS", file_bytes=b"x", filename="b.txt", instrument_id=RELIANCE_ID)
        with pytest.raises(AcceptanceError, match="INVALID_VALUE"):
            await acceptor.accept(draft_id=draft.draft_id, corrections={"facts": [
                {"metric": "cash", "value": "not-a-number", "periodEnd": _days_ago(10), "periodType": "ANNUAL", "unit": "INR crore", "sourceUrl": "https://x"}
            ]})

    @pytest.mark.asyncio
    async def test_missing_period_end_is_rejected_never_defaulted_to_upload_time(self, ingestor, acceptor):
        draft = ingestor.ingest(evidence_type="BALANCE_SHEET_FACTS", file_bytes=b"x", filename="b.txt", instrument_id=RELIANCE_ID)
        with pytest.raises(AcceptanceError, match="MISSING_PERIOD_END"):
            await acceptor.accept(draft_id=draft.draft_id, corrections={"facts": [
                {"metric": "cash", "value": "100", "periodType": "ANNUAL", "unit": "INR crore", "sourceUrl": "https://x"}
            ]})

    @pytest.mark.asyncio
    async def test_duplicate_metric_period_within_one_batch_is_rejected(self, ingestor, acceptor):
        draft = ingestor.ingest(evidence_type="BALANCE_SHEET_FACTS", file_bytes=b"x", filename="b.txt", instrument_id=RELIANCE_ID)
        period = _days_ago(10)
        with pytest.raises(AcceptanceError, match="DUPLICATE_FACT_IN_BATCH"):
            await acceptor.accept(draft_id=draft.draft_id, corrections={"facts": [
                {"metric": "cash", "value": "100", "periodEnd": period, "periodType": "ANNUAL", "unit": "INR crore", "sourceUrl": "https://x"},
                {"metric": "cash", "value": "200", "periodEnd": period, "periodType": "ANNUAL", "unit": "INR crore", "sourceUrl": "https://x"},
            ]})

    @pytest.mark.asyncio
    async def test_quarterly_financials_rejects_an_annual_period(self, ingestor, acceptor):
        draft = ingestor.ingest(evidence_type="QUARTERLY_FINANCIALS", file_bytes=b"x", filename="q.txt", instrument_id=RELIANCE_ID)
        with pytest.raises(AcceptanceError, match="QUARTERLY_FINANCIALS_REQUIRES_QUARTERLY_PERIOD"):
            await acceptor.accept(draft_id=draft.draft_id, corrections={"facts": [
                {"metric": "revenue", "value": "100", "periodEnd": _days_ago(10), "periodType": "ANNUAL", "unit": "INR crore", "sourceUrl": "https://x"}
            ]})


class TestBalanceSheetFacts:
    @pytest.mark.asyncio
    async def test_complete_current_upload_reaches_ready_fresh_via_real_runtime(self, ingestor, acceptor, repository):
        period = _days_ago(30)
        draft = ingestor.ingest(evidence_type="BALANCE_SHEET_FACTS", file_bytes=b"x", filename="b.txt", instrument_id=RELIANCE_ID)
        result = await acceptor.accept(draft_id=draft.draft_id, corrections={"facts": _balance_sheet_facts(period)})
        assert result["persisted"] is True
        req = next(r for r in result["readiness"]["requirements"] if r["requirementId"] == "BALANCE_SHEET_FACTS")
        assert req["status"] == "READY_FRESH"
        assert req["sourceProvider"] == "USER_UPLOAD"
        assert set(req["coveredInputIds"]) >= {"DEBT", "EQUITY", "CASH"}

        # Database proof: real FinancialFact rows exist, tagged USER_UPLOAD.
        persisted = repository.financial_facts_for(RELIANCE_ID)
        metrics = {f.key.metric: f for f in persisted}
        assert {"total_debt", "total_equity", "cash"} <= metrics.keys()
        for fact in metrics.values():
            assert fact.source_tier == FactSourceTier.USER_UPLOAD
            assert fact.key.period_end == period

        # Fresh repository/runtime instance, independent read-back proof.
        fresh_repo = ResearchRepository(settings=repository.settings, persistence=repository._persistence)
        fresh_req = await _requirement(fresh_repo, RELIANCE_ID, "BALANCE_SHEET_FACTS")
        assert fresh_req["status"] == "READY_FRESH"

    @pytest.mark.asyncio
    async def test_missing_a_mandatory_fact_never_reaches_ready_fresh(self, ingestor, acceptor):
        period = _days_ago(30)
        draft = ingestor.ingest(evidence_type="BALANCE_SHEET_FACTS", file_bytes=b"x", filename="b.txt", instrument_id=RELIANCE_ID)
        incomplete = [row for row in _balance_sheet_facts(period) if row["metric"] != "cash"]
        result = await acceptor.accept(draft_id=draft.draft_id, corrections={"facts": incomplete})
        req = next(r for r in result["readiness"]["requirements"] if r["requirementId"] == "BALANCE_SHEET_FACTS")
        assert req["status"] != "READY_FRESH"
        assert "CASH" in req["missingInputIds"]

    @pytest.mark.asyncio
    async def test_stale_period_never_reaches_ready_fresh(self, ingestor, acceptor):
        period = _days_ago(2000)
        draft = ingestor.ingest(evidence_type="BALANCE_SHEET_FACTS", file_bytes=b"x", filename="b.txt", instrument_id=AIXTRON_ID)
        result = await acceptor.accept(draft_id=draft.draft_id, corrections={"facts": _balance_sheet_facts(period)})
        req = next(r for r in result["readiness"]["requirements"] if r["requirementId"] == "BALANCE_SHEET_FACTS")
        assert req["status"] != "READY_FRESH"

    @pytest.mark.asyncio
    async def test_wrong_instrument_is_isolated(self, ingestor, acceptor, repository):
        period = _days_ago(30)
        draft = ingestor.ingest(evidence_type="BALANCE_SHEET_FACTS", file_bytes=b"x", filename="b.txt", instrument_id=RELIANCE_ID)
        await acceptor.accept(draft_id=draft.draft_id, corrections={"facts": _balance_sheet_facts(period)})
        other_metrics = {f.key.metric for f in repository.financial_facts_for(AIXTRON_ID)}
        assert "total_debt" not in other_metrics
        other_req = await _requirement(repository, AIXTRON_ID, "BALANCE_SHEET_FACTS")
        assert other_req["status"] != "READY_FRESH"

    @pytest.mark.asyncio
    async def test_duplicate_manual_upload_is_idempotent(self, ingestor, acceptor, repository):
        period = _days_ago(30)
        draft1 = ingestor.ingest(evidence_type="BALANCE_SHEET_FACTS", file_bytes=b"x1", filename="b1.txt", instrument_id=RELIANCE_ID)
        await acceptor.accept(draft_id=draft1.draft_id, corrections={"facts": _balance_sheet_facts(period)})
        before = len(repository.financial_facts_for(RELIANCE_ID))

        draft2 = ingestor.ingest(evidence_type="BALANCE_SHEET_FACTS", file_bytes=b"x2", filename="b2.txt", instrument_id=RELIANCE_ID)
        result2 = await acceptor.accept(draft_id=draft2.draft_id, corrections={"facts": _balance_sheet_facts(period)})
        after = len(repository.financial_facts_for(RELIANCE_ID))

        assert result2["persisted"] is False  # identical resubmit: merge_fact keeps existing, nothing written
        assert after == before  # no duplicate rows

    @pytest.mark.asyncio
    async def test_manual_upload_never_overwrites_stronger_official_evidence(self, ingestor, acceptor, repository):
        """CRITICAL precedence test: an OFFICIAL_NSE fact at an exact key must
        survive a conflicting manual upload at that SAME key untouched --
        merge_fact()'s authority ordering, not a pre-check, enforces this."""
        period = _days_ago(30)
        official = FinancialFact(
            key=FinancialFactKey(RELIANCE_ID, "total_debt", period, "ANNUAL", "UNKNOWN"),
            value=ProvenancedValue(value=Decimal("9999"), unit="INR crore", source_url="https://nse.example/filing",
                                    source_name="NSE", source_type="FILING", retrieved_at=datetime.now(timezone.utc)),
            source_tier=FactSourceTier.OFFICIAL_NSE, source_provider="NSE", source_identity="nse:total_debt",
            source_mode=SourceMode.REAL,
        )
        written = repository.persist_international_financial_facts([official])
        assert written == 1

        conflicting = _balance_sheet_facts(period)
        conflicting[0]["value"] = "1"  # total_debt, deliberately conflicting
        draft = ingestor.ingest(evidence_type="BALANCE_SHEET_FACTS", file_bytes=b"x", filename="b.txt", instrument_id=RELIANCE_ID)
        result = await acceptor.accept(draft_id=draft.draft_id, corrections={"facts": conflicting})

        debt_fact = next(f for f in repository.financial_facts_for(RELIANCE_ID) if f.key.metric == "total_debt")
        assert debt_fact.value.value == Decimal("9999")
        assert debt_fact.source_tier == FactSourceTier.OFFICIAL_NSE
        req = next(r for r in result["readiness"]["requirements"] if r["requirementId"] == "BALANCE_SHEET_FACTS")
        assert req["sourceProvider"] == "NSE"  # not overwritten by the manual attempt

    @pytest.mark.asyncio
    async def test_manual_fact_fills_a_genuine_gap_then_later_official_evidence_supersedes_it(self, ingestor, acceptor, repository):
        """manual vs later official: a manual fact fills the gap first; when
        real official evidence later arrives at the SAME key, it must win."""
        period = _days_ago(30)
        draft = ingestor.ingest(evidence_type="BALANCE_SHEET_FACTS", file_bytes=b"x", filename="b.txt", instrument_id=AIXTRON_ID)
        await acceptor.accept(draft_id=draft.draft_id, corrections={"facts": _balance_sheet_facts(period)})
        manual_fact = next(f for f in repository.financial_facts_for(AIXTRON_ID) if f.key.metric == "total_debt")
        assert manual_fact.source_tier == FactSourceTier.USER_UPLOAD

        official = FinancialFact(
            key=FinancialFactKey(AIXTRON_ID, "total_debt", period, "ANNUAL", "UNKNOWN"),
            value=ProvenancedValue(value=Decimal("5000"), unit="INR crore", source_url="https://nse.example/filing",
                                    source_name="NSE", source_type="FILING", retrieved_at=datetime.now(timezone.utc)),
            source_tier=FactSourceTier.OFFICIAL_NSE, source_provider="NSE", source_identity="nse:total_debt",
            source_mode=SourceMode.REAL,
        )
        written = repository.persist_international_financial_facts([official])
        assert written == 1
        after = next(f for f in repository.financial_facts_for(AIXTRON_ID) if f.key.metric == "total_debt")
        assert after.source_tier == FactSourceTier.OFFICIAL_NSE
        assert after.value.value == Decimal("5000")


class TestBusinessQualityFacts:
    @pytest.mark.asyncio
    async def test_complete_current_upload_reaches_ready_fresh(self, ingestor, acceptor, repository):
        p_a, p_b = _days_ago(400), _days_ago(30)
        draft = ingestor.ingest(evidence_type="BUSINESS_QUALITY_FACTS", file_bytes=b"x", filename="q.txt", instrument_id=RELIANCE_ID)
        result = await acceptor.accept(draft_id=draft.draft_id, corrections={"facts": _business_quality_facts(p_a, p_b)})
        req = next(r for r in result["readiness"]["requirements"] if r["requirementId"] == "BUSINESS_QUALITY_FACTS")
        assert req["status"] == "READY_FRESH"
        assert set(req["coveredInputIds"]) >= {"PROFITABILITY_HISTORY", "ROE", "ROCE", "MARGINS", "CASH_CONVERSION_OR_FCF_QUALITY"}

        fresh_repo = ResearchRepository(settings=repository.settings, persistence=repository._persistence)
        fresh_req = await _requirement(fresh_repo, RELIANCE_ID, "BUSINESS_QUALITY_FACTS")
        assert fresh_req["status"] == "READY_FRESH"

    @pytest.mark.asyncio
    async def test_single_period_profitability_history_stays_incomplete(self, ingestor, acceptor):
        """PROFITABILITY_HISTORY needs 2+ periods -- a single pat row must not
        satisfy it (never fabricate a second period to close the gap)."""
        p = _days_ago(30)
        draft = ingestor.ingest(evidence_type="BUSINESS_QUALITY_FACTS", file_bytes=b"x", filename="q.txt", instrument_id=RELIANCE_ID)
        facts = [row for row in _business_quality_facts(_days_ago(400), p) if not (row["metric"] == "pat" and row["periodEnd"] != p)]
        result = await acceptor.accept(draft_id=draft.draft_id, corrections={"facts": facts})
        req = next(r for r in result["readiness"]["requirements"] if r["requirementId"] == "BUSINESS_QUALITY_FACTS")
        assert req["status"] != "READY_FRESH"
        assert "PROFITABILITY_HISTORY" in req["missingInputIds"]

    @pytest.mark.asyncio
    async def test_stale_periods_never_reach_ready_fresh(self, ingestor, acceptor):
        p_a, p_b = _days_ago(2000), _days_ago(1700)
        draft = ingestor.ingest(evidence_type="BUSINESS_QUALITY_FACTS", file_bytes=b"x", filename="q.txt", instrument_id=AIXTRON_ID)
        result = await acceptor.accept(draft_id=draft.draft_id, corrections={"facts": _business_quality_facts(p_a, p_b)})
        req = next(r for r in result["readiness"]["requirements"] if r["requirementId"] == "BUSINESS_QUALITY_FACTS")
        assert req["status"] != "READY_FRESH"

    @pytest.mark.asyncio
    async def test_wrong_instrument_is_isolated(self, ingestor, acceptor, repository):
        p_a, p_b = _days_ago(400), _days_ago(30)
        draft = ingestor.ingest(evidence_type="BUSINESS_QUALITY_FACTS", file_bytes=b"x", filename="q.txt", instrument_id=RELIANCE_ID)
        await acceptor.accept(draft_id=draft.draft_id, corrections={"facts": _business_quality_facts(p_a, p_b)})
        other_req = await _requirement(repository, AIXTRON_ID, "BUSINESS_QUALITY_FACTS")
        assert other_req["status"] != "READY_FRESH"
        assert "roe" not in {f.key.metric for f in repository.financial_facts_for(AIXTRON_ID)}


class TestGrowthFacts:
    @pytest.mark.asyncio
    async def test_complete_current_upload_reaches_ready_fresh(self, ingestor, acceptor, repository):
        q1, q2 = _days_ago(100), _days_ago(10)
        a1, a2 = _days_ago(400), _days_ago(30)
        draft = ingestor.ingest(evidence_type="GROWTH_FACTS", file_bytes=b"x", filename="g.txt", instrument_id=RELIANCE_ID)
        result = await acceptor.accept(draft_id=draft.draft_id, corrections={"facts": _growth_facts(q1, q2, a1, a2)})
        req = next(r for r in result["readiness"]["requirements"] if r["requirementId"] == "GROWTH_FACTS")
        assert req["status"] == "READY_FRESH"
        assert set(req["coveredInputIds"]) == {
            "REVENUE_HISTORY", "EARNINGS_HISTORY", "QUARTERLY_YOY_QOQ_TRENDS", "ANNUAL_CAGR_INPUTS",
        }

        fresh_repo = ResearchRepository(settings=repository.settings, persistence=repository._persistence)
        fresh_req = await _requirement(fresh_repo, RELIANCE_ID, "GROWTH_FACTS")
        assert fresh_req["status"] == "READY_FRESH"

    @pytest.mark.asyncio
    async def test_missing_annual_periods_blocks_annual_cagr_inputs(self, ingestor, acceptor):
        q1, q2 = _days_ago(100), _days_ago(10)
        a1, a2 = _days_ago(400), _days_ago(30)
        draft = ingestor.ingest(evidence_type="GROWTH_FACTS", file_bytes=b"x", filename="g.txt", instrument_id=RELIANCE_ID)
        quarterly_only = [row for row in _growth_facts(q1, q2, a1, a2) if row["periodType"] == "QUARTERLY"]
        result = await acceptor.accept(draft_id=draft.draft_id, corrections={"facts": quarterly_only})
        req = next(r for r in result["readiness"]["requirements"] if r["requirementId"] == "GROWTH_FACTS")
        assert req["status"] != "READY_FRESH"
        assert "ANNUAL_CAGR_INPUTS" in req["missingInputIds"]

    @pytest.mark.asyncio
    async def test_wrong_instrument_is_isolated(self, ingestor, acceptor, repository):
        q1, q2 = _days_ago(100), _days_ago(10)
        a1, a2 = _days_ago(400), _days_ago(30)
        draft = ingestor.ingest(evidence_type="GROWTH_FACTS", file_bytes=b"x", filename="g.txt", instrument_id=RELIANCE_ID)
        await acceptor.accept(draft_id=draft.draft_id, corrections={"facts": _growth_facts(q1, q2, a1, a2)})
        other_req = await _requirement(repository, AIXTRON_ID, "GROWTH_FACTS")
        assert other_req["status"] != "READY_FRESH"


class TestQuarterlyFinancials:
    @pytest.mark.asyncio
    async def test_complete_current_quarters_reach_ready_fresh(self, ingestor, acceptor, repository):
        q1, q2 = _days_ago(100), _days_ago(10)
        draft = ingestor.ingest(evidence_type="QUARTERLY_FINANCIALS", file_bytes=b"x", filename="qf.txt", instrument_id=RELIANCE_ID)
        result = await acceptor.accept(draft_id=draft.draft_id, corrections={"facts": _quarterly_facts(q1, q2)})
        req = next(r for r in result["readiness"]["requirements"] if r["requirementId"] == "QUARTERLY_FINANCIALS")
        assert req["status"] == "READY_FRESH"
        assert set(req["coveredInputIds"]) >= {
            "LATEST_QUARTERLY_RESULT", "COMPARABLE_QUARTERS", "QUARTERLY_REVENUE", "QUARTERLY_PAT", "QUARTERLY_EPS",
        }

        persisted = repository.financial_facts_for(RELIANCE_ID)
        assert all(f.key.period_type == "QUARTERLY" for f in persisted)  # Q1..Q4 never collapsed into one snapshot
        assert len({f.key.period_end for f in persisted}) == 2  # two distinct quarters preserved

        fresh_repo = ResearchRepository(settings=repository.settings, persistence=repository._persistence)
        fresh_req = await _requirement(fresh_repo, RELIANCE_ID, "QUARTERLY_FINANCIALS")
        assert fresh_req["status"] == "READY_FRESH"

    @pytest.mark.asyncio
    async def test_single_quarter_leaves_comparable_quarters_missing(self, ingestor, acceptor):
        q1 = _days_ago(10)
        draft = ingestor.ingest(evidence_type="QUARTERLY_FINANCIALS", file_bytes=b"x", filename="qf.txt", instrument_id=RELIANCE_ID)
        single_quarter = [
            {"metric": "revenue", "value": "50000", "periodEnd": q1, "periodType": "QUARTERLY", "unit": "INR crore", "sourceUrl": "https://example.com/filing"},
            {"metric": "pat", "value": "9000", "periodEnd": q1, "periodType": "QUARTERLY", "unit": "INR crore", "sourceUrl": "https://example.com/filing"},
            {"metric": "eps", "value": "14.2", "periodEnd": q1, "periodType": "QUARTERLY", "unit": "INR", "sourceUrl": "https://example.com/filing"},
        ]
        result = await acceptor.accept(draft_id=draft.draft_id, corrections={"facts": single_quarter})
        req = next(r for r in result["readiness"]["requirements"] if r["requirementId"] == "QUARTERLY_FINANCIALS")
        assert req["status"] != "READY_FRESH"
        assert "COMPARABLE_QUARTERS" in req["missingInputIds"]

    @pytest.mark.asyncio
    async def test_stale_quarters_never_reach_ready_fresh(self, ingestor, acceptor):
        q1, q2 = _days_ago(900), _days_ago(800)
        draft = ingestor.ingest(evidence_type="QUARTERLY_FINANCIALS", file_bytes=b"x", filename="qf.txt", instrument_id=AIXTRON_ID)
        result = await acceptor.accept(draft_id=draft.draft_id, corrections={"facts": _quarterly_facts(q1, q2)})
        req = next(r for r in result["readiness"]["requirements"] if r["requirementId"] == "QUARTERLY_FINANCIALS")
        assert req["status"] != "READY_FRESH"

    @pytest.mark.asyncio
    async def test_wrong_instrument_is_isolated(self, ingestor, acceptor, repository):
        q1, q2 = _days_ago(100), _days_ago(10)
        draft = ingestor.ingest(evidence_type="QUARTERLY_FINANCIALS", file_bytes=b"x", filename="qf.txt", instrument_id=RELIANCE_ID)
        await acceptor.accept(draft_id=draft.draft_id, corrections={"facts": _quarterly_facts(q1, q2)})
        other_req = await _requirement(repository, AIXTRON_ID, "QUARTERLY_FINANCIALS")
        assert other_req["status"] != "READY_FRESH"
        assert "eps" not in {f.key.metric for f in repository.financial_facts_for(AIXTRON_ID)}
