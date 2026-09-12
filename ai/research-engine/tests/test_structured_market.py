from decimal import Decimal

import httpx
import pandas as pd
import pytest

from app.settings import Settings
from app.research_fetching import FetchError, HttpResearchFetcher
from app.structured_market import StructuredProviderError, YahooFinanceProvider, strongest_company_identity, _resolution_queries, _candidate_score


def provider(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return YahooFinanceProvider(Settings(), client)


def response(data, status=200):
    return httpx.Response(status, json=data)


class FakeTicker:
    def __init__(self, _symbol, info, news=None, income_stmt=None, balance_sheet=None, **statements):
        self.info = info
        self.news = news or []
        self.income_stmt = income_stmt
        self.balance_sheet = balance_sheet
        for name, value in statements.items(): setattr(self, name, value)


def yfinance_provider(info, news=None, income_stmt=None, balance_sheet=None, **statements):
    return YahooFinanceProvider(Settings(), httpx.AsyncClient(),
        ticker_factory=lambda symbol: FakeTicker(symbol, info, news, income_stmt, balance_sheet, **statements))


def instrument(**updates):
    value = {
        "canonicalName": "ZEN TECHNOLOGIES LTD", "isin": "INE251B01027",
        "canonicalSymbol": "ZENTEC", "brokerSymbol": "ZENTEC", "country": "IN",
        "tradingCurrency": "INR", "assetType": "EQUITY",
    }
    value.update(updates)
    return value


@pytest.mark.asyncio
async def test_resolves_nse_equity_and_normalizes_partial_fundamentals_with_provenance():
    def handler(request):
        if "/finance/search" in request.url.path:
            return response({"quotes": [{"symbol": "ZENTEC.NS", "quoteType": "EQUITY", "longname": "Zen Technologies Limited", "exchange": "NSI", "currency": "INR"}]})
        if request.url.path.endswith("/finance/quote"):
            return response({"quoteResponse": {"result": [{"regularMarketPrice": 1825.6, "currency": "INR", "regularMarketTime": 1788076800, "trailingPE": 42.6}]}})
        return response({"quoteSummary": {"result": [{"financialData": {"returnOnEquity": {"raw": .21}, "totalDebt": {"raw": 1000}}, "assetProfile": {"sector": "Industrials"}}]}})

    snapshot = await provider(handler).collect(instrument())
    assert snapshot.resolution.provider_ticker == "ZENTEC.NS"
    assert snapshot.resolution.currency == "INR"
    assert snapshot.facts["latestPrice"].value == Decimal("1825.6")
    assert snapshot.facts["trailingPE"].value == Decimal("42.6")
    assert snapshot.facts["roe"].value == Decimal("21.00")
    assert snapshot.facts["latestPrice"].source_type == "STRUCTURED_MARKET_PROVIDER"


@pytest.mark.asyncio
async def test_yahoo_statement_adapter_requires_explicit_period_and_marks_unknown_basis():
    period = pd.Timestamp("2026-06-30")
    quarterly = pd.DataFrame({period: {"Total Revenue": 1000, "Net Income": 120, "EBITDA": 200, "Diluted EPS": 4.5}})
    snapshot = await yfinance_provider({"currency": "INR", "quoteType": "EQUITY", "regularMarketPrice": 10},
                                       quarterly_income_stmt=quarterly).collect(instrument(structuredProviderTicker="ZENTEC.NS", structuredProviderStatus="VERIFIED"))
    values = {(item["metric"], item["periodType"], item["periodEnd"], item["reportingBasis"]): item["value"] for item in snapshot.statement_facts}
    assert values[("revenue", "QUARTERLY", "2026-06-30T00:00:00", "UNKNOWN")] == Decimal("1000")
    assert values[("pat", "QUARTERLY", "2026-06-30T00:00:00", "UNKNOWN")] == Decimal("120")
    assert next(item for item in snapshot.statement_facts if item["metric"] == "revenue")["rawFieldOrigin"] == "Total Revenue"
    assert "totalDebt" not in {item["metric"] for item in snapshot.statement_facts}


@pytest.mark.asyncio
async def test_resolves_eu_listing_and_rejects_wrong_currency_candidate():
    def handler(request):
        if "/finance/search" in request.url.path:
            return response({"quotes": [
                {"symbol": "BESI.AS", "quoteType": "EQUITY", "longname": "BE Semiconductor Industries N.V.", "exchange": "AMS", "currency": "EUR"},
                {"symbol": "BESI", "quoteType": "EQUITY", "longname": "BE Semiconductor Industries", "exchange": "NMS", "currency": "USD"},
            ]})
        if request.url.path.endswith("/finance/quote"):
            return response({"quoteResponse": {"result": [{"regularMarketPrice": 125, "currency": "EUR"}]}})
        return response({"quoteSummary": {"result": []}})
    snapshot = await provider(handler).collect(instrument(canonicalName="BE SEMICONDUCTOR INDUSTRIES", canonicalSymbol="BESI", brokerSymbol="BESI", country="NL", tradingCurrency="EUR"))
    assert snapshot.resolution.provider_ticker == "BESI.AS"


@pytest.mark.asyncio
async def test_rejects_ambiguous_or_wrong_instrument_instead_of_attaching_data():
    def handler(request):
        return response({"quotes": [
            {"symbol": "RENK.DE", "quoteType": "EQUITY", "longname": "RENK Group AG", "exchange": "GER", "currency": "EUR"},
            {"symbol": "R3NK.DE", "quoteType": "EQUITY", "longname": "RENK Group AG", "exchange": "GER", "currency": "EUR"},
        ]})
    with pytest.raises(StructuredProviderError, match="RESOLUTION_AMBIGUOUS"):
        await provider(handler).resolve_instrument(instrument(canonicalName="RENK GROUP AG", canonicalSymbol="", brokerSymbol="",
                                                              brokerExchange="IBIS2", country="DE", tradingCurrency="EUR"))


@pytest.mark.asyncio
async def test_provider_failure_is_typed_and_does_not_create_fake_fields():
    async_provider = provider(lambda request: response({}, 503))
    with pytest.raises(StructuredProviderError, match="STRUCTURED_PROVIDER_UNAVAILABLE"):
        await async_provider.collect(instrument(canonicalName="ARCADIS NV", country="NL", tradingCurrency="EUR"))


def test_identity_priority_and_overview_fallback_trim_nbsp_and_separator():
    assert strongest_company_identity({"canonicalName": "RENK", "overview": "RENK GROUP AG · R3NK"}) == "RENK GROUP AG"
    assert strongest_company_identity({"overview": "  AALBERTS N.V.\u00a0·\u00a0AALB  "}) == "AALBERTS N.V."
    assert strongest_company_identity({"overview": "BE Semiconductor Industries\u00c2\u00b7 BESI"}) == "BE Semiconductor Industries"


def test_full_overview_company_precedes_symbol_and_preserves_symbol_as_separate_evidence():
    value = instrument(canonicalName="", companyName="", overview="BE Semiconductor Industries N.V. · BESI",
                       brokerSymbol="BESI", ticker="BESI")
    queries = _resolution_queries(value, strongest_company_identity(value))
    assert queries[0] == "BE Semiconductor Industries N.V."
    assert queries == ["BE Semiconductor Industries N.V."]
    assert value["brokerSymbol"] == "BESI"
    assert all(not query.endswith((".AS", ".DE")) for query in queries)


def test_symbol_is_discovery_fallback_only_without_usable_company_name():
    value = instrument(canonicalName="", companyName="", brokerDescription="", overview="", isin="",
                       canonicalSymbol="", brokerSymbol="BESI", ticker="BESI")
    queries = _resolution_queries(value, strongest_company_identity(value))
    assert queries[0] == "BESI"


@pytest.mark.asyncio
async def test_yahoo_first_discovery_request_uses_full_overview_company_not_symbol():
    searches = []
    def handler(request):
        searches.append(request.url.params.get("q"))
        return response({"quotes": []})
    value = instrument(canonicalName="BESI", companyName="", brokerDescription="",
                       overview="BE Semiconductor Industries N.V. · BESI", isin="", canonicalSymbol="",
                       brokerSymbol="BESI", ticker="BESI")
    with pytest.raises(StructuredProviderError, match="COMPANY_NOT_RESOLVED"):
        await provider(handler).resolve_instrument(value)
    assert searches[0] == "BE Semiconductor Industries N.V."
    assert searches == ["BE Semiconductor Industries N.V."]


def test_listing_evidence_beats_higher_company_name_similarity_for_multi_listed_company():
    held = instrument(canonicalName="Acme Industries N.V.", brokerSymbol="ACME", ticker="ACME",
                      brokerExchange="IBIS2", canonicalExchange="SMART", tradingCurrency="EUR")
    wrong_listing = {"symbol": "ACME.AS", "quoteType": "EQUITY", "longname": "Acme Industries N.V.",
                     "exchange": "AMS", "currency": "EUR"}
    held_listing = {"symbol": "ACM.DE", "quoteType": "EQUITY", "longname": "Acme Industries",
                    "exchange": "GER", "currency": "EUR"}
    assert _candidate_score(held, held["canonicalName"], wrong_listing) == 0
    assert _candidate_score(held, held["canonicalName"], held_listing) > 0.70


def test_amsterdam_contract_selects_amsterdam_listing_for_same_company():
    held = instrument(canonicalName="Acme Industries N.V.", brokerSymbol="ACME", ticker="ACME",
                      brokerExchange="AEB", canonicalExchange="SMART", tradingCurrency="EUR")
    amsterdam = {"symbol": "ACME.AS", "quoteType": "EQUITY", "longname": "Acme Industries N.V.",
                 "exchange": "AMS", "currency": "EUR"}
    german = {"symbol": "ACM.DE", "quoteType": "EQUITY", "longname": "Acme Industries N.V.",
              "exchange": "GER", "currency": "EUR"}
    assert _candidate_score(held, held["canonicalName"], amsterdam) > 0.70
    assert _candidate_score(held, held["canonicalName"], german) == 0


def test_currency_security_type_and_isin_are_listing_validation_evidence():
    held = instrument(canonicalName="Acme Industries", isin="NL0000000001", brokerExchange="AEB",
                      tradingCurrency="EUR", securityType="COMMON")
    matching = {"symbol": "ACME.AS", "quoteType": "EQUITY", "longname": "Acme Industries",
                "exchange": "AMS", "currency": "EUR", "isin": "NL0000000001"}
    wrong_currency = dict(matching, currency="USD")
    wrong_type = dict(matching, quoteType="ETF")
    wrong_isin = dict(matching, isin="NL0000000002")
    assert _candidate_score(held, held["canonicalName"], matching) == 1.0
    assert _candidate_score(held, held["canonicalName"], wrong_currency) == 0
    assert _candidate_score(held, held["canonicalName"], wrong_type) == 0
    assert _candidate_score(held, held["canonicalName"], wrong_isin) == 0


@pytest.mark.asyncio
async def test_full_name_discovers_multiple_listings_then_held_exchange_selects_one():
    searches = []
    candidates = [
        {"symbol": "ACME.AS", "quoteType": "EQUITY", "longname": "Acme Industries N.V.", "exchange": "AMS", "currency": "EUR"},
        {"symbol": "ACM.DE", "quoteType": "EQUITY", "longname": "Acme Industries", "exchange": "GER", "currency": "EUR"},
    ]
    def handler(request):
        searches.append(request.url.params.get("q"))
        return response({"quotes": candidates})
    held = instrument(canonicalName="Acme Industries N.V.", overview="Acme Industries N.V. · ACME",
                      canonicalSymbol="ACME", brokerSymbol="ACME", ticker="ACME",
                      brokerExchange="IBIS2", canonicalExchange="SMART", tradingCurrency="EUR")
    result = await provider(handler).resolve_instrument(held)
    assert searches == ["Acme Industries N.V."]
    assert result.provider_ticker == "ACM.DE"


@pytest.mark.asyncio
async def test_resolution_and_snapshot_are_reused_within_configured_freshness():
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        if "/finance/search" in request.url.path:
            return response({"quotes": [{"symbol": "ZENTEC.NS", "quoteType": "EQUITY", "longname": "Zen Technologies Limited", "exchange": "NSE", "currency": "INR"}]})
        if request.url.path.endswith("/finance/quote"):
            return response({"quoteResponse": {"result": [{"regularMarketPrice": 1800, "currency": "INR"}]}})
        return response({"quoteSummary": {"result": []}})
    market = provider(handler)
    first = await market.collect(instrument())
    second = await market.collect(instrument())
    assert first is second
    assert calls > 0
    first_call_count = calls
    await market.collect(instrument())
    assert calls == first_call_count


@pytest.mark.asyncio
async def test_durable_verified_mapping_survives_local_cache_loss_without_fuzzy_search():
    fuzzy_searches = 0
    def handler(request):
        nonlocal fuzzy_searches
        if "/finance/search" in request.url.path:
            if request.url.params.get("quotesCount") != "0":
                fuzzy_searches += 1
            return response({}, 503)
        if request.url.path.endswith("/finance/quote"):
            return response({"quoteResponse": {"result": [{"regularMarketPrice": 1800, "currency": "INR", "quoteType": "EQUITY"}]}})
        return response({"quoteSummary": {"result": []}})
    durable = instrument(structuredProviderTicker="ZENTEC.NS", structuredProviderExchange="NSE",
                         structuredProviderCurrency="INR", structuredProviderStatus="VERIFIED")
    snapshot = await provider(handler).collect(durable)
    assert snapshot.resolution.provider_ticker == "ZENTEC.NS"
    assert snapshot.resolution.status == "VERIFIED_REUSED"
    assert fuzzy_searches == 0


@pytest.mark.asyncio
async def test_known_nse_listing_rejects_bse_candidate_without_guessing_suffix():
    async_provider = provider(lambda request: response({"quotes": [
        {"symbol": "ZENTEC.BO", "quoteType": "EQUITY", "longname": "Zen Technologies Limited", "exchange": "BSE", "currency": "INR"}
    ]}))
    with pytest.raises(StructuredProviderError, match="COMPANY_NOT_RESOLVED"):
        await async_provider.resolve_instrument(instrument(canonicalExchange="NSE"))


@pytest.mark.asyncio
async def test_verified_nse_candidate_is_validated_before_yahoo_mapping_can_be_persisted():
    searches = []
    def handler(request):
        if "/finance/search" in request.url.path:
            if request.url.params.get("quotesCount") != "0":
                searches.append(request.url.params.get("q"))
            return response({"quotes": [{"symbol": "OFFICIAL.NS", "quoteType": "EQUITY",
                "longname": "Generic Components Limited", "exchange": "NSI", "currency": "INR",
                "isin": "INE000A01010"}]})
        if request.url.path.endswith("/finance/quote"):
            return response({"quoteResponse": {"result": [{"regularMarketPrice": 100, "currency": "INR"}]}})
        return response({"quoteSummary": {"result": []}})
    held = instrument(canonicalName="Generic Components Limited", isin="INE000A01010", canonicalSymbol="BROKER_ALIAS",
                      brokerSymbol="BROKER_ALIAS", ticker="BROKER_ALIAS", canonicalExchange="NSE",
                      structuredNseCandidateTicker="OFFICIAL.NS", structuredNseCandidateSource="VERIFIED_NSE")
    snapshot = await provider(handler).collect(held)
    assert searches == ["OFFICIAL.NS"]
    assert snapshot.resolution.provider_ticker == "OFFICIAL.NS"
    assert snapshot.resolution.status == "VERIFIED_NSE_CANDIDATE"


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate", [
    {"symbol": "OFFICIAL.BO", "quoteType": "EQUITY", "longname": "Generic Components Limited", "exchange": "BSE", "currency": "INR"},
    {"symbol": "OFFICIAL.NS", "quoteType": "EQUITY", "longname": "Different Company Limited", "exchange": "NSE", "currency": "INR"},
    {"symbol": "OFFICIAL.NS", "quoteType": "ETF", "longname": "Generic Components Limited", "exchange": "NSE", "currency": "INR"},
])
async def test_verified_nse_candidate_rejects_identity_or_security_conflicts(candidate):
    async_provider = provider(lambda request: response({"quotes": [candidate]}))
    held = instrument(canonicalName="Generic Components Limited", isin="INE000A01010", canonicalExchange="NSE",
                      structuredNseCandidateTicker="OFFICIAL.NS", structuredNseCandidateSource="VERIFIED_NSE")
    with pytest.raises(StructuredProviderError, match="COMPANY_NOT_RESOLVED"):
        await async_provider.resolve_instrument(held)


@pytest.mark.asyncio
async def test_untrusted_or_invalid_nse_hint_does_not_become_candidate_proof():
    searches = []
    def handler(request):
        searches.append(request.url.params.get("q"))
        return response({"quotes": []})
    held = instrument(structuredNseCandidateTicker="BROKER_ALIAS.NS", structuredNseCandidateSource="BROKER_IMPORT_IDENTITY")
    with pytest.raises(StructuredProviderError, match="COMPANY_NOT_RESOLVED"):
        await provider(handler).resolve_instrument(held)
    assert searches == ["ZEN TECHNOLOGIES LTD"]


@pytest.mark.asyncio
async def test_yfinance_current_price_and_full_structured_fields_are_normalized_without_raw_leak():
    info = {"currentPrice": 1826.8, "regularMarketPrice": 1800, "currency": "INR", "quoteType": "EQUITY",
        "exchange": "NSI", "longName": "Zen Technologies Limited", "sector": "Industrials",
        "industry": "Aerospace & Defense", "marketCap": 100, "enterpriseValue": 90, "trailingPE": 42,
        "forwardPE": 30, "priceToSalesTrailing12Months": 8, "priceToBook": 7, "enterpriseToRevenue": 6,
        "enterpriseToEbitda": 20, "trailingEps": 10, "forwardEps": 12, "bid": 0, "ask": 0,
        "targetLowPrice": 1500, "targetMedianPrice": 1800, "targetMeanPrice": 1850, "targetHighPrice": 2100,
        "numberOfAnalystOpinions": 6, "recommendationKey": "buy", "regularMarketVolume": 1234}
    market = yfinance_provider(info, [{"title": "Older update", "publisher": "Publisher", "link": "https://example.com/older", "providerPublishTime": 1788076700}, {"title": "Public update", "publisher": "Publisher", "link": "https://example.com/news", "providerPublishTime": 1788076800}])
    snapshot = await market.collect(instrument(structuredProviderTicker="ZENTEC.NS", structuredProviderStatus="VERIFIED",
                                               structuredProviderCurrency="INR", assetType="EQUITY"))
    assert snapshot.facts["latestPrice"].value == Decimal("1826.8")
    assert "bid" not in snapshot.facts and "ask" not in snapshot.facts
    assert snapshot.facts["priceToSales"].value == Decimal("8")
    assert snapshot.facts["publicAnalystTargetMedianPrice"].value == Decimal("1800")
    assert snapshot.facts["sector"].value == "Industrials"
    assert snapshot.news[0]["headline"] == "Public update"
    assert snapshot.news[0]["publishedAt"].year == 2026
    assert snapshot.news[0]["url"] == "https://example.com/news"
    assert "currentPrice" not in snapshot.model_dump_json()


@pytest.mark.asyncio
async def test_yfinance_regular_market_fallback_missing_price_and_etf_type():
    durable = instrument(structuredProviderTicker="TEST.NS", structuredProviderStatus="VERIFIED",
                         structuredProviderCurrency="INR", assetType="ETF")
    snapshot = await yfinance_provider({"regularMarketPrice": 55, "currency": "INR", "quoteType": "ETF",
                                        "exchange": "NSI", "navPrice": 54.8}).collect(durable)
    assert snapshot.facts["latestPrice"].value == Decimal("55")
    assert snapshot.facts["navPrice"].value == Decimal("54.8")
    assert snapshot.resolution.quote_type == "ETF"
    mislabeled = await yfinance_provider({"regularMarketPrice": 130.96, "currency": "INR", "quoteType": "EQUITY",
                                          "longName": "Nippon India ETF Gold BeES"}).collect(durable)
    assert mislabeled.resolution.quote_type == "ETF"
    with pytest.raises(StructuredProviderError, match="STRUCTURED_PRICE_UNAVAILABLE"):
        await yfinance_provider({"currency": "INR", "quoteType": "ETF"}).collect(durable)
    with pytest.raises(StructuredProviderError, match="STRUCTURED_PRICE_UNAVAILABLE"):
        await yfinance_provider({"regularMarketPrice": 0, "currency": "INR", "quoteType": "ETF"}).collect(durable)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("symbol", "WRONG.NS", "SYMBOL"),
        ("exchange", "NYQ", "EXCHANGE"),
        ("currency", "USD", "CURRENCY"),
    ],
)
async def test_verified_yahoo_mapping_rejects_returned_identity_conflicts(field, value, error):
    info = {
        "symbol": "ZENTEC.NS",
        "currentPrice": 10,
        "currency": "INR",
        "quoteType": "EQUITY",
        "exchange": "NSI",
        field: value,
    }
    durable = instrument(
        structuredProviderTicker="ZENTEC.NS",
        structuredProviderStatus="VERIFIED",
        structuredProviderCurrency="INR",
        structuredProviderExchange="XNSE",
    )
    with pytest.raises(StructuredProviderError, match=f"PERSISTED_MAPPING_CONFLICT:{error}"):
        await yfinance_provider(info).collect(durable)


@pytest.mark.asyncio
async def test_yfinance_roe_normalization_and_missing_value_remains_absent():
    durable = instrument(structuredProviderTicker="ZENTEC.NS", structuredProviderStatus="VERIFIED",
                         structuredProviderCurrency="INR")
    present = await yfinance_provider({"currentPrice": 10, "currency": "INR", "quoteType": "EQUITY",
                                       "returnOnEquity": 0.2145}).collect(durable)
    assert present.facts["roe"].value == Decimal("21.4500")
    assert present.facts["roe"].source_name == "Yahoo Finance"
    missing = await yfinance_provider({"currentPrice": 10, "currency": "INR", "quoteType": "EQUITY"}).collect(durable)
    assert "roe" not in missing.facts


@pytest.mark.asyncio
async def test_roce_uses_latest_common_annual_statement_period_and_requires_all_inputs():
    period = pd.Timestamp("2026-03-31")
    income = pd.DataFrame({period: {"EBIT": 250}})
    balance = pd.DataFrame({period: {"Total Assets": 2000, "Current Liabilities": 750}})
    durable = instrument(structuredProviderTicker="ZENTEC.NS", structuredProviderStatus="VERIFIED",
                         structuredProviderCurrency="INR")
    snapshot = await yfinance_provider({"currentPrice": 10, "currency": "INR", "quoteType": "EQUITY"},
                                       income_stmt=income, balance_sheet=balance).collect(durable)
    assert snapshot.facts["roce"].value == Decimal("20.0")
    assert "total assets - current liabilities" in snapshot.facts["roce"].calculation_basis
    incomplete = await yfinance_provider({"currentPrice": 10, "currency": "INR", "quoteType": "EQUITY"},
                                         income_stmt=income,
                                         balance_sheet=pd.DataFrame({period: {"Total Assets": 2000}})).collect(durable)
    assert "roce" not in incomplete.facts


@pytest.mark.asyncio
async def test_bank_does_not_receive_industrial_roce_even_when_statement_rows_exist():
    period = pd.Timestamp("2026-03-31")
    income = pd.DataFrame({period: {"EBIT": 250}})
    balance = pd.DataFrame({period: {"Total Assets": 2000, "Current Liabilities": 750}})
    durable = instrument(structuredProviderTicker="BANK.NS", structuredProviderStatus="VERIFIED",
                         structuredProviderCurrency="INR")
    snapshot = await yfinance_provider({"currentPrice": 10, "currency": "INR", "quoteType": "EQUITY",
                                        "sector": "Financial Services", "industry": "Banks - Regional",
                                        "returnOnEquity": .15, "returnOnAssets": .01},
                                       income_stmt=income, balance_sheet=balance).collect(durable)
    assert snapshot.facts["roe"].value == Decimal("15.00")
    assert snapshot.facts["roa"].value == Decimal("1.00")
    assert "roce" not in snapshot.facts


@pytest.mark.asyncio
async def test_public_pdf_uses_normal_page_text_extraction_with_page_markers(monkeypatch):
    class Page:
        def __init__(self, text): self.text = text
        def extract_text(self): return self.text
    class Reader:
        def __init__(self, _stream): self.pages = [Page("ZEN TECHNOLOGIES quarterly results"), Page("PAT and EPS details")]
    monkeypatch.setattr("pypdf.PdfReader", Reader)
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-public-pdf", request=request)
    ))
    result = await HttpResearchFetcher(Settings(research_max_retries=0), client).fetch("https://example.com/results.pdf")
    assert "[PDF_PAGE 1]" in result.text
    assert "PAT and EPS details" in result.text


@pytest.mark.asyncio
async def test_scanned_public_pdf_reports_ocr_required(monkeypatch):
    class Page:
        def extract_text(self): return ""
    class Reader:
        def __init__(self, _stream): self.pages = [Page()]
    monkeypatch.setattr("pypdf.PdfReader", Reader)
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "application/pdf"},
                                       content=b"%PDF-scanned-pdf", request=request)
    ))
    result = await HttpResearchFetcher(Settings(research_max_retries=0), client).fetch("https://example.com/scanned.pdf")
    assert result.extraction_status == "PDF_SCANNED_OCR_REQUIRED"
    assert result.text == "[PDF_PAGE 1]\n"


@pytest.mark.asyncio
async def test_financial_issuer_reported_statements_are_preserved_without_industrial_roce():
    income = pd.DataFrame({pd.Timestamp("2026-06-30"): {"Total Revenue": 100, "Net Income": 20},
                           pd.Timestamp("2026-03-31"): {"Total Revenue": 90, "Net Income": 18}})
    snapshot = await yfinance_provider({"currentPrice": 10, "currency": "INR", "quoteType": "EQUITY",
        "sector": "Financial Services", "industry": "Financial Data & Stock Exchanges"},
        income_stmt=income, quarterly_income_stmt=income).collect(instrument(
        structuredProviderTicker="EXCHANGE.NS", structuredProviderStatus="VERIFIED", structuredProviderCurrency="INR"))
    assert {row["metric"] for row in snapshot.statement_facts} >= {"revenue", "pat"}
    assert {row["periodType"] for row in snapshot.statement_facts} == {"ANNUAL", "QUARTERLY"}
    assert all(row["sourceUrl"].endswith("EXCHANGE.NS") for row in snapshot.statement_facts)
    assert "roce" not in snapshot.facts
