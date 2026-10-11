"""TASK F -- VALUATION_INPUTS automatic extraction, closing the confirmed
runtime defect: the Gokul Agro Resources Limited financial-table screenshot
(Net Income/PAT/Net Profit, EPS, Total Revenue across Q1 FY27/Jun 2026, FY
2025-26 and TTM columns) previously returned "no safe automatic extraction"
and required every value to be typed in by hand.

Scope, deliberately narrow (see ALLOWED_METRICS_BY_EVIDENCE_TYPE in
app.manual_evidence): only PAT and EPS at a real QUARTERLY/ANNUAL period
become proposed, acceptable VALUATION_INPUTS facts. Total Revenue (a real
figure, but not an allowed VALUATION_INPUTS metric) and any TTM-column value
(a rolling window, not a calendar period this platform's periodType contract
recognizes) are recognized but routed to read-only reference observations,
never silently dropped and never an accepted valuation fact.
"""
from pathlib import Path
import asyncio
from decimal import Decimal
from uuid import UUID

import pytest

from app.evidence_interpretation import interpret_valuation_inputs
from app.fact_precedence import FactSourceTier
from app.manual_evidence import AcceptanceError, ManualEvidenceAcceptor, ManualEvidenceIngestor
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository
from app.research_readiness_runtime import RepositoryResearchReadinessAdapter, ResearchReadinessRuntime
from app.settings import Settings

RELIANCE_ID = UUID("44444444-4444-4444-4444-444444444444")

# The literal confirmed runtime table (Gokul Agro Resources Limited),
# reproduced as plain, column-aligned text -- the same >= 2-whitespace-run
# column convention the header row itself uses.
GOKUL_AGRO_VALUATION_TABLE = (
    "Metric                         Q1 FY27 / Jun 2026   FY 2025-26        TTM" + chr(10) +
    "Net Income / PAT / Net Profit  123 INR crore         369.4 INR crore   420 INR crore" + chr(10) +
    "EPS                            4.16 INR/share        12.54 INR/share   14.25 INR/share" + chr(10) +
    "Total Revenue                  5282 INR crore        24077 INR crore   24435 INR crore" + chr(10)
)


def _settings() -> Settings:
    return Settings(
        research_mcp_gateway_base_url="http://localhost",
        research_mcp_gateway_timeout_seconds=30,
        research_mcp_service_identity="test",
        research_mcp_first_enabled=False,
        research_readiness_ensure_timeout_seconds=30,
        research_opportunity_scheduler_enabled=False,
        country="IN", exchange="XNSE",
    )


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


class TestInterpreterUnit:
    """Direct unit coverage of app.evidence_interpretation.interpret_valuation_inputs,
    independent of the manual-evidence ingest/accept wiring."""

    def test_pat_and_eps_resolve_at_quarterly_and_annual_periods_jun_2026(self):
        from types import SimpleNamespace
        result = interpret_valuation_inputs(SimpleNamespace(text=GOKUL_AGRO_VALUATION_TABLE))
        by_key = {(f.metric, f.period_type): f for f in result.facts}
        assert by_key[("pat", "QUARTERLY")].value == Decimal("123")
        assert by_key[("pat", "QUARTERLY")].period_end.isoformat() == "2026-06-01"
        assert by_key[("eps", "QUARTERLY")].value == Decimal("4.16")
        assert by_key[("eps", "QUARTERLY")].period_end.isoformat() == "2026-06-01"
        assert by_key[("pat", "ANNUAL")].value == Decimal("369.4")
        assert by_key[("pat", "ANNUAL")].period_end.isoformat() == "2026-03-01"
        assert by_key[("eps", "ANNUAL")].value == Decimal("12.54")
        # FINAL VALUATION_INPUTS CLOSURE condition 3 -- units are read from
        # the source table verbatim, never assumed: PAT in INR crore, EPS
        # in INR/share, at both periods.
        assert by_key[("pat", "QUARTERLY")].unit == "INR crore"
        assert by_key[("pat", "ANNUAL")].unit == "INR crore"
        assert by_key[("eps", "QUARTERLY")].unit == "INR/share"
        assert by_key[("eps", "ANNUAL")].unit == "INR/share"
        # Exactly 4 facts -- PAT aliases ('Net Income'/'PAT'/'Net Profit' on
        # ONE combined row) must never fan out into 3 separate facts per
        # period; dedup to the single canonical 'pat' metric.
        assert len(result.facts) == 4

    def test_ttm_column_never_becomes_a_proposed_fact_for_any_metric(self):
        from types import SimpleNamespace
        result = interpret_valuation_inputs(SimpleNamespace(text=GOKUL_AGRO_VALUATION_TABLE))
        assert all(f.period_type != "TRAILING_TWELVE_MONTHS" for f in result.facts)
        ttm_observations = [o for o in result.observations if o.period_type == "TRAILING_TWELVE_MONTHS"]
        assert len(ttm_observations) == 3  # pat, eps, revenue each have a TTM column
        assert {o.metric for o in ttm_observations} == {"pat", "eps", "revenue"}
        assert all("UNSUPPORTED_PERIOD_TYPE_TTM" in o.reason for o in ttm_observations)

    def test_revenue_is_recognized_but_never_a_proposed_fact(self):
        from types import SimpleNamespace
        result = interpret_valuation_inputs(SimpleNamespace(text=GOKUL_AGRO_VALUATION_TABLE))
        assert all(f.metric != "revenue" for f in result.facts)
        revenue_observations = [o for o in result.observations if o.metric == "revenue"]
        assert len(revenue_observations) == 3  # Q1 FY27/Jun 2026, FY 2025-26, TTM
        assert any(o.value == Decimal("5282") for o in revenue_observations)
        assert any("UNSUPPORTED_METRIC_FOR_VALUATION_INPUTS" in o.reason for o in revenue_observations)


class TestManualEvidenceIngestAutoExtraction:
    """app.manual_evidence.ManualEvidenceIngestor.ingest(evidence_type="VALUATION_INPUTS", ...)
    end to end: automatic extraction populates proposedFacts the SAME way
    SHAREHOLDING already does, while requiresManualFields stays True so the
    FinancialFact editor remains the single correction/completion surface."""

    def test_ingest_prefills_proposed_facts_from_a_plain_text_upload(self, ingestor):
        draft = ingestor.ingest(
            evidence_type="VALUATION_INPUTS", file_bytes=GOKUL_AGRO_VALUATION_TABLE.encode("utf-8"),
            filename="gokul_agro_valuation.csv", instrument_id=RELIANCE_ID,
        )
        assert draft.requires_manual_fields is True
        by_key = {(f.field, f.period_type): f for f in draft.proposed_facts}
        assert by_key[("pat", "QUARTERLY")].value == Decimal("123")
        assert by_key[("pat", "QUARTERLY")].unit == "INR crore"
        assert by_key[("pat", "QUARTERLY")].period_end == "2026-06-01"
        assert by_key[("eps", "ANNUAL")].value == Decimal("12.54")
        assert len(draft.proposed_facts) == 4
        # Revenue/TTM are recognized but kept entirely separate from
        # proposedFacts -- read-only reference via field_suggestions, the
        # SAME supplementary-evidence mechanism SHAREHOLDING already uses
        # for shareholderCount.
        assert draft.field_suggestions
        assert any("Revenue" in v for v in draft.field_suggestions.values())
        assert any("TTM" in v for v in draft.field_suggestions.values())
        assert all(f.field != "revenue" for f in draft.proposed_facts)

    def test_png_production_path_matches_plain_text_extraction(self, ingestor):
        """Real PNG -> Tesseract OCR -> DocumentExtractor -> ingest() -- not
        just the plain-text interpreter path the other tests in this file
        exercise."""
        import shutil
        if not shutil.which("tesseract"):
            pytest.skip("tesseract not installed in this environment")
        import io
        from PIL import Image, ImageDraw, ImageFont
        from app.document_extraction import DocumentExtractor, DocumentInput, PNG_MIME

        font = ImageFont.truetype(
            str(Path(__file__).resolve().parent / "fixtures" / "fonts" / "DejaVuSansMono.ttf"), 32)
        col_x = [40, 900, 1400, 1850]
        rows = [
            ["Metric", "Q1 FY27 / Jun 2026", "FY 2025-26", "TTM"],
            ["Net Income / PAT / Net Profit", "123 INR crore", "369.4 INR crore", "420 INR crore"],
            ["EPS", "4.16 INR/share", "12.54 INR/share", "14.25 INR/share"],
            ["Total Revenue", "5282 INR crore", "24077 INR crore", "24435 INR crore"],
        ]
        image = Image.new("RGB", (2250, 60 + len(rows) * 70), color="white")
        draw = ImageDraw.Draw(image)
        for r, row in enumerate(rows):
            for c, cell in enumerate(row):
                draw.text((col_x[c], 20 + r * 70), cell, fill="black", font=font)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")

        draft = ingestor.ingest(
            evidence_type="VALUATION_INPUTS", file_bytes=buffer.getvalue(),
            filename="gokul_agro_valuation.png", instrument_id=RELIANCE_ID,
        )
        by_key = {(f.field, f.period_type): f for f in draft.proposed_facts}
        debug = draft.validation_results
        assert ("pat", "QUARTERLY") in by_key, debug
        assert by_key[("pat", "QUARTERLY")].value == Decimal("123"), debug
        assert by_key[("pat", "QUARTERLY")].period_end == "2026-06-01", debug
        assert by_key[("eps", "QUARTERLY")].value == Decimal("4.16"), debug
        assert by_key[("pat", "ANNUAL")].value == Decimal("369.4"), debug
        assert by_key[("eps", "ANNUAL")].value == Decimal("12.54"), debug


class TestAcceptPreservesExistingInvariants:
    """The automatic prefill is purely a Review convenience -- acceptance
    still runs through the EXACT SAME corrections["facts"] ->
    _validate_financial_fact_rows -> _build_financial_facts pathway every
    other FinancialFact-backed evidence type already uses. No new
    persistence mechanism, no change to USER_UPLOAD authority or explicit-
    acceptance discipline."""

    @pytest.mark.asyncio
    async def test_prefilled_facts_accept_as_real_user_upload_financial_facts(
        self, ingestor, acceptor, repository,
    ):
        draft = ingestor.ingest(
            evidence_type="VALUATION_INPUTS", file_bytes=GOKUL_AGRO_VALUATION_TABLE.encode("utf-8"),
            filename="gokul_agro_valuation.csv", instrument_id=RELIANCE_ID,
        )
        facts_payload = [
            {
                "metric": f.field, "value": str(f.value), "periodEnd": f.period_end,
                "periodType": f.period_type, "unit": f.unit, "sourceUrl": f.source_locator,
            }
            for f in draft.proposed_facts
        ]
        result = await acceptor.accept(draft_id=draft.draft_id, corrections={"facts": facts_payload})
        assert result["persisted"] is True
        assert result["financialFactsSubmitted"] == 4
        persisted_pat = [f for f in repository.financial_facts_for(RELIANCE_ID) if f.key.metric == "pat"]
        assert len(persisted_pat) == 2  # quarterly + annual
        assert all(f.source_tier == FactSourceTier.USER_UPLOAD for f in persisted_pat)
        req = next(r for r in result["readiness"]["requirements"] if r["requirementId"] == "VALUATION_INPUTS")
        assert req["status"] != "READY_FRESH"  # derived ratios still require trusted market data
        assert "EARNINGS_BASIS" in req["coveredInputIds"]

    @pytest.mark.asyncio
    async def test_user_can_still_edit_a_prefilled_value_before_accepting(
        self, ingestor, acceptor, repository,
    ):
        """Prefill is non-binding: the user can correct a value the
        interpreter extracted, same as SHAREHOLDING Review already allows."""
        draft = ingestor.ingest(
            evidence_type="VALUATION_INPUTS", file_bytes=GOKUL_AGRO_VALUATION_TABLE.encode("utf-8"),
            filename="gokul_agro_valuation.csv", instrument_id=RELIANCE_ID,
        )
        facts_payload = [
            {
                "metric": f.field,
                "value": "125" if (f.field, f.period_type) == ("pat", "QUARTERLY") else str(f.value),
                "periodEnd": f.period_end, "periodType": f.period_type, "unit": f.unit,
                "sourceUrl": f.source_locator,
            }
            for f in draft.proposed_facts
        ]
        result = await acceptor.accept(draft_id=draft.draft_id, corrections={"facts": facts_payload})
        assert result["persisted"] is True
        persisted = [
            f for f in repository.financial_facts_for(RELIANCE_ID)
            if f.key.metric == "pat" and f.key.period_type == "QUARTERLY"
        ]
        assert persisted[0].value.value == Decimal("125")

    @pytest.mark.asyncio
    async def test_unsupported_metric_still_rejected_even_if_user_submits_an_observation(
        self, ingestor, acceptor,
    ):
        """A user attempting to submit one of the read-only Revenue
        observations as though it were an acceptable valuation fact is
        still rejected by the EXISTING, unmodified _validate_financial_fact_rows
        check -- this closure never expands VALUATION_INPUTS's allowed-metric
        set."""
        draft = ingestor.ingest(
            evidence_type="VALUATION_INPUTS", file_bytes=GOKUL_AGRO_VALUATION_TABLE.encode("utf-8"),
            filename="gokul_agro_valuation.csv", instrument_id=RELIANCE_ID,
        )
        with pytest.raises(AcceptanceError):
            await acceptor.accept(draft_id=draft.draft_id, corrections={"facts": [
                {"metric": "revenue", "value": "5282", "periodEnd": "2026-06-01",
                 "periodType": "QUARTERLY", "unit": "INR crore", "sourceUrl": "manual-evidence:x"},
            ]})
