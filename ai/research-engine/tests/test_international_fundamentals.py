from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest

from app.fact_precedence import FactSourceTier, FinancialFact, FinancialFactKey, merge_fact
from app.international_fundamentals import EodhdFundamentalProvider, SecEdgarFundamentalProvider, international_provider_for
from app.models import CompanyResearchProfile, ProvenancedValue, SourceMode
from app.settings import Settings
from app.structured_research import financial_result_history_from_facts, financial_statement_history_from_facts


def _profile(**changes):
    values = dict(instrument_id=uuid4(), company_id=uuid4(), company_name="Microsoft Corporation", ticker="MSFT", exchange="NASDAQ", mic="XNAS", country="US", currency="USD")
    values.update(changes)
    return CompanyResearchProfile(**values)


def _client(responses):
    def handler(request):
        for suffix, payload in responses.items():
            if request.url.path.endswith(suffix): return httpx.Response(200, json=payload)
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_sec_resolves_cik_and_normalizes_explicit_companyfacts():
    client = _client({"company_tickers.json": {"0": {"ticker": "MSFT", "cik_str": 789019}}, "CIK0000789019.json": {"facts": {"us-gaap": {
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [{"form": "10-Q", "end": "2026-03-31", "val": 100, "frame": "CY2026Q1", "accn": "a", "filed": "2026-04-25"}]}},
        "NetIncomeLoss": {"units": {"USD": [{"form": "10-Q", "end": "2026-03-31", "val": 25, "frame": "CY2026Q1", "accn": "a"}]}},
        "EarningsPerShareDiluted": {"units": {"USD/shares": [{"form": "10-Q", "end": "2026-03-31", "val": 3, "frame": "CY2026Q1", "accn": "a"}]}},
        "Assets": {"units": {"USD": [{"form": "10-Q", "end": "2026-03-31", "val": 500, "accn": "a"}]}},
        "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [{"form": "10-Q", "end": "2026-03-31", "val": 40, "frame": "CY2026Q1", "accn": "a"}]}},
        "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": [{"form": "10-Q", "end": "2026-03-31", "val": 8, "frame": "CY2026Q1", "accn": "a"}]}},
    }}}})
    result = await SecEdgarFundamentalProvider(Settings(), client=client).collect(_profile())
    assert result.verified_provider_ids == {"SEC_CIK": "0000789019"}
    assert {(f.key.metric, f.key.period_type) for f in result.facts} >= {("revenue", "QUARTERLY"), ("pat", "QUARTERLY"), ("eps", "QUARTERLY"), ("total_assets", "AS_AT"), ("operating_cash_flow", "QUARTERLY"), ("capex", "QUARTERLY")}
    assert all(f.source_tier == FactSourceTier.OFFICIAL_REGULATORY and f.source_mode == SourceMode.REAL for f in result.facts)
    await client.aclose()


@pytest.mark.asyncio
async def test_eodhd_validates_isin_exchange_currency_and_reuses_symbol_mapping():
    profile = _profile(company_name="AIXTRON SE", ticker="AIXA", exchange="XETRA", mic="XFRA", country="DE", currency="EUR", isin="DE000A0WMPJ6")
    payload = {"General": {"ISIN": "DE000A0WMPJ6", "Exchange": "F", "CurrencyCode": "EUR"}, "Financials": {"Income_Statement": {"yearly": {"2025": {"date": "2025-12-31", "Revenue": "100", "NetIncome": "20"}}, "quarterly": {"2026Q1": {"date": "2026-03-31", "Revenue": "30"}}}, "Balance_Sheet": {"yearly": {"2025": {"date": "2025-12-31", "TotalAssets": "200"}}}, "Cash_Flow": {"yearly": {"2025": {"date": "2025-12-31", "CapitalExpenditures": "5"}}}}}
    settings = Settings(eodhd_api_key="test", eodhd_base_url="https://eod.test")
    result = await EodhdFundamentalProvider(settings, client=_client({"fundamentals/AIXA": payload})).collect(profile)
    assert result.verified_provider_ids == {"EODHD": "AIXA"}
    assert {f.key.metric for f in result.facts} >= {"revenue", "pat", "total_assets", "capex"}
    assert any(f.key.metric == "revenue" and f.key.period_type == "QUARTERLY" for f in result.facts)
    bad = {**payload, "General": {**payload["General"], "CurrencyCode": "USD"}}
    assert not (await EodhdFundamentalProvider(settings, client=_client({"fundamentals/AIXA": bad})).collect(profile)).facts


def test_international_precedence_and_read_projection_keep_basis_and_series_limits():
    profile = _profile(); now = datetime.now(timezone.utc)
    def fact(metric, end, kind, tier, value, basis="CONSOLIDATED"):
        return FinancialFact(FinancialFactKey(profile.instrument_id, metric, end, kind, basis), ProvenancedValue(value=value, source_url="https://source", source_name="source", source_type="TEST", retrieved_at=now), tier, "test", end, SourceMode.REAL)
    sec = fact("revenue", "2026-03-31", "QUARTERLY", FactSourceTier.OFFICIAL_REGULATORY, 100)
    assert merge_fact(sec, fact("revenue", "2026-03-31", "QUARTERLY", FactSourceTier.STRUCTURED_FUNDAMENTALS, 1)) is sec
    facts = [fact("revenue", f"202{year}-12-31", "ANNUAL", FactSourceTier.OFFICIAL_REGULATORY, year) for year in range(1, 7)]
    facts += [fact("revenue", f"2026-0{month}-28", "QUARTERLY", FactSourceTier.OFFICIAL_REGULATORY, month) for month in range(1, 6)]
    history = financial_result_history_from_facts(facts)
    assert len([x for x in history if x.period_type == "ANNUAL"]) == 4
    assert len([x for x in history if x.period_type == "QUARTERLY"]) == 4
    mixed = facts + [fact("total_assets", "2026-03-31", "AS_AT", FactSourceTier.OFFICIAL_REGULATORY, 10, "UNKNOWN")]
    assert financial_statement_history_from_facts(mixed, period_type="AS_AT", metrics={"total_assets"})[0].reporting_basis == "UNKNOWN"


def test_provider_routing_keeps_india_outside_international_pipeline():
    assert international_provider_for(_profile(country="IN", exchange="NSE", mic="XNSE"), Settings()) is None
    assert isinstance(international_provider_for(_profile(), Settings()), SecEdgarFundamentalProvider)
    assert isinstance(international_provider_for(_profile(country="DE", exchange="XETRA", mic="XFRA", currency="EUR"), Settings(eodhd_api_key="x")), EodhdFundamentalProvider)
