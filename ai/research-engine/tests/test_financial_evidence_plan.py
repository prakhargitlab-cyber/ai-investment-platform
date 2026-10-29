"""Offline requirements, captured NSE structures and bounded acquisition tests."""
import asyncio
import json
import logging
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.fact_precedence import FinancialFact, FinancialFactKey, FactSourceTier
from app.financial_evidence_plan import (HistoryDemand, build_financial_evidence_plan,
    official_amount, potential_coverage, shift_months)
from app.models import ProvenancedValue, SourceMode
from app.repository import _derive_ratio_facts
from app.research_fetching import DocumentSizeLimitExceeded, HttpResearchFetcher, TransportFetchError
from app.settings import Settings
from app.structured_financial import NseOfficialFinancialProvider, parse_nse_financial_xbrl
from test_di15_financial_authority_upgrade import _repo

NOW = datetime(2026, 10, 19, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures" / "nse_financial_xbrl"
CAPTURES = json.loads((FIXTURES / "20microns_filings.json").read_text())


@pytest.fixture(autouse=True)
def capture_financial_telemetry(monkeypatch, caplog):
    # app.main configures a non-propagating WARNING-level application logger
    # during collection. Capture the real INFO events without changing that
    # production configuration or weakening the telemetry assertions.
    monkeypatch.setattr(logging.getLogger("app"), "propagate", True)
    caplog.set_level(logging.INFO, logger="app.structured_financial")


def case():
    repo, profile = _repo()
    profile = profile.model_copy(update={"isin": "INE144J01027", "ticker": "20MICRONS",
        "provider_instrument_ids": {"NSE": "20MICRONS"}})
    repo.profiles = [profile]
    return repo, profile


def fact(profile, metric="revenue", period="2026-03-31", frequency="ANNUAL", value="100", unit="INR", basis="CONSOLIDATED"):
    return FinancialFact(FinancialFactKey(profile.instrument_id, metric, period, frequency, basis),
        ProvenancedValue(value=Decimal(value), unit=unit, source_name="NSE fixture", source_url="https://nsearchives.nseindia.com/corporate/verified.xml",
            retrieved_at=NOW, published_at=NOW, confidence=.95),
        FactSourceTier.OFFICIAL_NSE, "NSE", "fixture:"+metric+period, SourceMode.REAL)


def history(profile):
    return [fact(profile, m, p) for p in ("2026-03-31", "2025-03-31") for m in ("revenue", "pat")] + [
        fact(profile, m, "2026-06-30", "QUARTERLY") for m in ("revenue", "pat")]


def test_default_three_years_and_exact_yoy_quarters():
    _, p = case()
    plan = build_financial_evidence_plan(p, history(p), {"GROWTH_FACTS": {"ANNUAL_CAGR_INPUTS", "QUARTERLY_YOY_QOQ_TRENDS"}})
    assert plan.annual_periods == ("2026-03-31", "2025-03-31", "2024-03-31")
    assert plan.quarterly_periods == ("2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30")
    assert {n.period_end for n in plan.missing(history(p)) if n.frequency == "ANNUAL"} == {"2024-03-31"}


@pytest.mark.parametrize("count", [4, 5])
def test_justified_annual_extension(count):
    _, p = case()
    plan = build_financial_evidence_plan(p, history(p), {"GROWTH_FACTS": {"ANNUAL_CAGR_INPUTS"}},
        history_demands=[HistoryDemand("GROWTH_CONSISTENCY", "ANNUAL", count)])
    assert len(plan.annual_periods) == count
    assert len({n.period_end for n in plan.needs}) == count


@pytest.mark.parametrize("demand", [HistoryDemand("ROCE", "ANNUAL", 5), HistoryDemand("REVENUE_CAGR", "ANNUAL", 10),
                                   HistoryDemand("TTM", "QUARTERLY", 9), HistoryDemand("TTM", "QUARTERLY", 0)])
def test_unjustified_or_unbounded_extension_rejected(demand):
    _, p = case()
    with pytest.raises(ValueError, match="UNJUSTIFIED"):
        build_financial_evidence_plan(p, history(p), {}, history_demands=[demand])


def test_fiscal_boundary_publication_and_eight_quarter_cap():
    _, p = case()
    inputs = [fact(p, period="2025-09-30"), fact(p, period="2025-12-31", frequency="QUARTERLY")]
    plan = build_financial_evidence_plan(p, inputs, {"GROWTH_FACTS": {"ANNUAL_CAGR_INPUTS", "QUARTERLY_YOY_QOQ_TRENDS"}},
        history_demands=[HistoryDemand("TTM", "QUARTERLY", 8)])
    assert plan.annual_periods == ("2025-09-30", "2024-09-30", "2023-09-30")
    assert len(plan.quarterly_periods) == 8 and "2026-09-30" not in plan.annual_periods
    assert shift_months(datetime(2024, 2, 29).date(), -12).isoformat() == "2023-02-28"


def test_roce_targets_only_matching_inputs_and_unknown_never_covers_official():
    _, p = case()
    facts = history(p)
    plan = build_financial_evidence_plan(p, facts, {"BUSINESS_QUALITY_FACTS": {"ROCE"}})
    assert {(n.metric, n.period_end) for n in plan.needs} == {
        ("ebit", "2026-03-31"), ("total_assets", "2026-03-31"), ("current_liabilities", "2026-03-31")}
    yahoo = replace(fact(p, "ebit", basis="UNKNOWN"), source_tier=FactSourceTier.YAHOO, source_provider="YAHOO_FINANCE_MCP")
    assert official_amount(yahoo) is None
    assert not plan.needs[0].covered([yahoo])


@pytest.mark.parametrize("capture", CAPTURES, ids=lambda c: c["fixture"])
def test_captured_2025_taxonomy_identity_basis_units_and_ytd(capture):
    _, p = case()
    facts = parse_nse_financial_xbrl((FIXTURES/capture["fixture"]).read_bytes(), p, capture["metadata"], retrieved_at=NOW)
    assert facts and all(f.key.reporting_basis == "CONSOLIDATED" for f in facts)
    assert all(f.source_provider == "NSE" and "2025-01-31" in f.value.calculation_basis for f in facts)
    assert {f.key.period_type for f in facts} <= {"ANNUAL", "QUARTERLY", "AS_AT"}
    if "20251231" in capture["fixture"]:
        pat = next(f for f in facts if f.key.metric == "pat")
        assert pat.key.period_type == "QUARTERLY" and pat.value.value == Decimal("148684000")
        assert not any(f.key.period_type == "ANNUAL" for f in facts)  # nine months is not annual/quarterly
    if "20250331" in capture["fixture"]:
        assert any(f.key.metric == "pbt" and f.key.period_type == "ANNUAL" for f in facts)
        # PY_I is a cash-flow opening-cash context, not a comparative income
        # statement or a complete prior-year balance sheet.
        assert not any(f.key.period_end == "2024-03-31" and f.key.metric == "revenue" for f in facts)


def test_unverified_identity_and_basis_still_rejected():
    _, p = case()
    capture = CAPTURES[0]
    xml = (FIXTURES/capture["fixture"]).read_bytes()
    for row in ({**capture["metadata"], "consolidated": "Standalone"}, {**capture["metadata"], "symbol": "OTHER"}):
        with pytest.raises(ValueError):
            parse_nse_financial_xbrl(xml, p, row, retrieved_at=NOW)


def test_roce_deterministic_matching_units_basis_and_authority():
    _, p = case()
    facts = [fact(p, "pbt", value="10", unit="INR crore"), fact(p, "finance_costs", value="2", unit="INR crore"),
             fact(p, "total_assets", frequency="AS_AT", value="100", unit="INR crore"),
             fact(p, "current_liabilities", frequency="AS_AT", value="20", unit="INR crore")]
    derived = _derive_ratio_facts(p.instrument_id, facts)
    assert next(f for f in derived if f.key.metric == "roce").value.value == Decimal("15")
    assert next(f for f in derived if f.key.metric == "ebit").value.value == Decimal("120000000")
    assert not _derive_ratio_facts(p.instrument_id, [replace(f, source_provider="YAHOO_FINANCE_MCP", source_tier=FactSourceTier.YAHOO) for f in facts])
    assert not any(f.key.metric == "roce" for f in _derive_ratio_facts(p.instrument_id, facts[:-1]))
    bad = replace(facts[-1], value=facts[-1].value.model_copy(update={"unit": "USD"}))
    assert not any(f.key.metric == "roce" for f in _derive_ratio_facts(p.instrument_id, facts[:-1]+[bad]))
    quarterly = [replace(f, key=replace(f.key, period_type="QUARTERLY")) if f.key.period_type == "ANNUAL" else f for f in facts]
    assert not _derive_ratio_facts(p.instrument_id, quarterly)


def provider_for(repo, rows, parsed=None):
    client = SimpleNamespace(get=AsyncMock(return_value=httpx.Response(200, json={"data": rows, "totalCount": len(rows)},
        request=httpx.Request("GET", NseOfficialFinancialProvider.RESULTS_URL))))
    blobs = {c["metadata"]["xbrl"]: (FIXTURES/c["fixture"]).read_bytes() for c in CAPTURES}
    async def fetch(url, **kwargs):
        return SimpleNamespace(final_url=url, status_code=200, content_type="application/xml", content=blobs.get(url, b"fixture"))
    fetcher = SimpleNamespace(settings=repo.settings, fetch_network=AsyncMock(side_effect=fetch))
    return NseOfficialFinancialProvider(client, fetcher, clock=lambda: NOW)


@pytest.mark.asyncio
async def test_no_network_when_existing_trusted_evidence_complete():
    repo, p = case()
    facts = [fact(p, m, period) for period in ("2026-03-31", "2025-03-31", "2024-03-31") for m in ("revenue", "pat")]
    provider = provider_for(repo, [])
    accept = AsyncMock()
    await provider.collect_planned(p, existing_facts=facts, accept=accept,
        plan_factory=lambda fs, **kw: build_financial_evidence_plan(p, fs, {"GROWTH_FACTS": {"ANNUAL_CAGR_INPUTS"}}, **kw))
    provider.client.get.assert_not_awaited()
    provider.fetcher.fetch_network.assert_not_awaited()
    accept.assert_not_awaited()


@pytest.mark.asyncio
async def test_actual_quarterly_gaps_persist_idempotently_and_cache_avoids_redownload(caplog):
    repo, p = case()
    facts = history(p)
    await repo.persist_international_financial_facts_async(facts)
    provider = provider_for(repo, [c["metadata"] for c in CAPTURES])
    planner = lambda fs, **kw: build_financial_evidence_plan(p, fs, {"GROWTH_FACTS": {"ANNUAL_CAGR_INPUTS", "QUARTERLY_YOY_QOQ_TRENDS"}}, **kw)
    with caplog.at_level("INFO"):
        await provider.collect_planned(p, existing_facts=facts, plan_factory=planner, accept=repo.persist_international_financial_facts_async)
    current = repo.financial_facts_for(p.instrument_id)
    assert {"2025-09-30", "2025-12-31"} <= {f.key.period_end for f in current if f.key.period_type == "QUARTERLY" and f.key.metric == "pat"}
    assert not any(f.key.period_end == "2024-03-31" and f.key.metric == "pat" for f in current)
    count = len(current)
    calls = provider.fetcher.fetch_network.await_count
    await provider.collect_planned(p, existing_facts=current, plan_factory=planner, accept=repo.persist_international_financial_facts_async)
    assert len(repo.financial_facts_for(p.instrument_id)) == count
    assert provider.fetcher.fetch_network.await_count == calls
    assert "bytes_received=" in caplog.text and "unmet_targets=['2024-03-31'" in caplog.text


@pytest.mark.asyncio
async def test_integrated_multi_period_coverage_stops_after_one(monkeypatch):
    repo, p = case()
    seed = [fact(p)]
    provider = provider_for(repo, [CAPTURES[0]["metadata"], {**CAPTURES[0]["metadata"], "qe_Date": "31-Mar-2026"}])
    integrated = [fact(p, m, period) for m in ("revenue", "pat") for period in ("2026-03-31", "2025-03-31", "2024-03-31")]
    monkeypatch.setattr("app.structured_financial.parse_nse_financial_xbrl", lambda *a, **kw: integrated)
    accept = AsyncMock(return_value=6)
    await provider.collect_planned(p, existing_facts=seed, accept=accept,
        plan_factory=lambda fs, **kw: build_financial_evidence_plan(p, fs, {"GROWTH_FACTS": {"ANNUAL_CAGR_INPUTS"}}, **kw))
    assert provider.fetcher.fetch_network.await_count == 1
    assert accept.await_count == 1


@pytest.mark.asyncio
async def test_document_failure_isolated_from_alternative_and_durable_receipt():
    repo, p = case()
    row = CAPTURES[2]["metadata"]
    oversized = {**row, "xbrl": row["xbrl"].replace(".xml", "_oversized.xml"), "xbrlFileSize": "1 Bytes"}
    provider = provider_for(repo, [oversized, row])
    original = provider.fetcher.fetch_network.side_effect
    async def fetch(url, **kw):
        if "oversized" in url:
            raise DocumentSizeLimitExceeded(2_000_000, 1_500_000)
        return await original(url, **kw)
    provider.fetcher.fetch_network.side_effect = fetch
    async def failed(url, limit):
        return repo.document_size_failure_known(p.instrument_id, url, limit)
    async def record(url, limit):
        await repo.record_document_size_failure(p.instrument_id, url, limit)
    planner = lambda fs, **kw: build_financial_evidence_plan(p, fs, {"QUARTERLY_FINANCIALS": {"LATEST_QUARTERLY_RESULT"}}, **kw)
    result = await provider.collect_planned(p, existing_facts=[fact(p, "eps", "2025-12-31", "QUARTERLY")],
        plan_factory=planner, accept=repo.persist_international_financial_facts_async, document_failed=failed, record_failure=record)
    assert result and provider.fetcher.fetch_network.await_count == 2
    assert repo.document_size_failure_known(p.instrument_id, oversized["xbrl"], 1_500_000)
    assert not repo.document_size_failure_known(p.instrument_id, row["xbrl"], 1_500_000)
    assert not repo.document_size_failure_known(p.instrument_id, oversized["xbrl"], 3_000_000)


@pytest.mark.asyncio
async def test_finite_adaptive_budget_never_silently_discards_older_year(monkeypatch, caplog):
    repo, p = case()
    row = CAPTURES[0]["metadata"]
    periods = ("31-Mar-2026", "31-Mar-2025", "31-Mar-2024", "30-Jun-2026", "31-Dec-2025", "30-Sep-2025", "30-Jun-2025")
    rows = [{**row, "qe_Date": period, "broadcast_Date": "01-Aug-2026 12:00:00", "xbrl": row["xbrl"].replace(".xml", f"_{i}.xml")}
            for i, period in enumerate(periods)]
    provider = provider_for(repo, rows)
    monkeypatch.setattr("app.structured_financial.parse_nse_financial_xbrl", lambda *a, **kw: [])
    with caplog.at_level("INFO"):
        await provider.collect_planned(p, existing_facts=history(p), accept=AsyncMock(),
            plan_factory=lambda fs, **kw: build_financial_evidence_plan(p, fs, {"GROWTH_FACTS": {"ANNUAL_CAGR_INPUTS", "QUARTERLY_YOY_QOQ_TRENDS"}}, **kw))
    assert provider.fetcher.fetch_network.await_count == 6
    urls = [call.args[0] for call in provider.fetcher.fetch_network.await_args_list]
    assert rows[2]["xbrl"] in urls  # FY2024 did not lose all slots to newer quarters
    assert "reason=DOCUMENT_LIMIT" in caplog.text


@pytest.mark.asyncio
async def test_provider_timeout_terminates_without_retrying_other_documents():
    repo, p = case()
    provider = provider_for(repo, [c["metadata"] for c in CAPTURES])
    provider.fetcher.fetch_network.side_effect = TransportFetchError("timeout")
    completed = AsyncMock()
    with pytest.raises(TransportFetchError):
        await provider.collect_planned(p, existing_facts=history(p), accept=AsyncMock(), completed=completed,
            plan_factory=lambda fs, **kw: build_financial_evidence_plan(p, fs, {"GROWTH_FACTS": {"ANNUAL_CAGR_INPUTS"}}, **kw))
    assert provider.fetcher.fetch_network.await_count == 1
    assert completed.call_args.args[0] == "PROVIDER_BACKOFF"


@pytest.mark.asyncio
@pytest.mark.parametrize("size,accepted", [(100, True), (101, False)])
async def test_streaming_xml_size_limit_preserved(size, accepted):
    settings = Settings(research_max_content_bytes=100, research_max_retries=0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, content=b"x"*size, headers={"content-type": "application/xml"}, request=request))) as client:
        fetcher = HttpResearchFetcher(settings, client)
        url = CAPTURES[0]["metadata"]["xbrl"]
        if accepted:
            result = await fetcher.fetch_network(url, max_bytes=100)
            assert len(result.content) == 100
        else:
            with pytest.raises(DocumentSizeLimitExceeded):
                await fetcher.fetch_network(url, max_bytes=100)


@pytest.mark.asyncio
async def test_discovery_page_cap_leaves_missing_history_explicit(caplog):
    repo, p = case()
    provider = provider_for(repo, [])
    row = CAPTURES[0]["metadata"]
    async def page(url, params):
        older = {**row, "qe_Date": f"31-Mar-{2010 + params['page']}",
                 "xbrl": row["xbrl"].replace(".xml", f"_{params['page']}.xml")}
        return httpx.Response(200, json={"data": [older]}, request=httpx.Request("GET", url))
    provider.client.get.side_effect = page
    await provider.collect_planned(p, existing_facts=history(p), accept=AsyncMock(),
        plan_factory=lambda fs, **kw: build_financial_evidence_plan(p, fs, {"GROWTH_FACTS": {"ANNUAL_CAGR_INPUTS"}}, **kw))
    assert provider.client.get.await_count == 5
    provider.fetcher.fetch_network.assert_not_awaited()
    assert "reason=PAGE_LIMIT" in caplog.text and "2024-03-31" in caplog.text


@pytest.mark.asyncio
async def test_acquisition_deadline_cancels_network_and_records_reason(monkeypatch):
    repo, p = case()
    provider = provider_for(repo, [])
    monkeypatch.setattr("app.structured_financial._ACQUISITION_SECONDS", .01)
    async def slow(*args, **kwargs):
        await asyncio.sleep(10)
    provider.client.get.side_effect = slow
    completed = AsyncMock()
    with pytest.raises(TimeoutError):
        await provider.collect_planned(p, existing_facts=history(p), accept=AsyncMock(), completed=completed,
            plan_factory=lambda fs, **kw: build_financial_evidence_plan(p, fs, {"GROWTH_FACTS": {"ANNUAL_CAGR_INPUTS"}}, **kw))
    assert provider.client.get.await_count == 1
    provider.fetcher.fetch_network.assert_not_awaited()
    completed.assert_awaited_once_with("TIME_LIMIT")


@pytest.mark.asyncio
async def test_durable_xml_receipts_survive_new_provider_and_preserve_authoritative_values():
    repo, p = case()
    await repo.persist_international_financial_facts_async(history(p))
    receipts = set()
    async def known(identity):
        return identity in receipts
    async def record(identity, url, count):
        receipts.add(identity)
    planner = lambda fs, **kw: build_financial_evidence_plan(p, fs,
        {"GROWTH_FACTS": {"ANNUAL_CAGR_INPUTS", "QUARTERLY_YOY_QOQ_TRENDS"}}, **kw)
    for attempt in range(2):
        provider = provider_for(repo, [c["metadata"] for c in CAPTURES])
        await provider.collect_planned(p, existing_facts=repo.financial_facts_for(p.instrument_id),
            plan_factory=planner, accept=repo.persist_international_financial_facts_async,
            document_known=known, record_document=record)
        if attempt:
            provider.fetcher.fetch_network.assert_not_awaited()
    facts = repo.financial_facts_for(p.instrument_id)
    assert all(next(f for f in facts if f.key == old.key).value.value == old.value.value for old in history(p))
    assert not any(f.key.period_end == "2024-03-31" and f.key.metric in {"revenue", "pat", "eps"} for f in facts)


def test_scrip_metadata_mismatch_fails_closed():
    _, p = case()
    captured = CAPTURES[0]
    with pytest.raises(ValueError, match="SCRIP_MISMATCH"):
        parse_nse_financial_xbrl((FIXTURES / captured["fixture"]).read_bytes(), p,
            {**captured["metadata"], "scripCode": "999999"}, retrieved_at=NOW)
