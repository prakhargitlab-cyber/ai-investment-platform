"""Provider-neutral international fundamentals collection.

This module deliberately consumes the global instrument-backed research profile
and emits ordinary ``FinancialFact`` values.  It never creates an instrument
master or participates in the India/NSE document pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import httpx

from app.fact_precedence import FactSourceTier, FinancialFact, FinancialFactKey
from app.models import CompanyResearchProfile, ProvenancedValue, SourceMode
from app.settings import Settings


@dataclass(frozen=True)
class InternationalFundamentalsResult:
    facts: list[FinancialFact]
    verified_provider_ids: dict[str, str]


class InternationalFundamentalProvider(Protocol):
    async def collect(self, profile: CompanyResearchProfile) -> InternationalFundamentalsResult: ...


def international_provider_for(profile: CompanyResearchProfile, settings: Settings, *, client: httpx.AsyncClient | None = None) -> InternationalFundamentalProvider | None:
    if str(profile.country).upper() in {"IN", "IND", "INDIA"} or str(profile.mic).upper() in {"NSE", "XNSE"}:
        return None
    if str(profile.country).upper() in {"US", "USA"} or str(profile.mic).upper() in {"XNYS", "XNAS", "ARCX", "BATS"}:
        return SecEdgarFundamentalProvider(settings, client=client)
    if _is_european(profile):
        return EodhdFundamentalProvider(settings, client=client)
    return None


class SecEdgarFundamentalProvider:
    provider_name = "SEC_EDGAR"
    _CONCEPTS = {
        "revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"),
        "operating_income": ("OperatingIncomeLoss",), "ebit": ("OperatingIncomeLoss",),
        "pat": ("NetIncomeLoss",), "eps": ("EarningsPerShareDiluted", "EarningsPerShareBasic"),
        "total_assets": ("Assets",), "total_liabilities": ("Liabilities",),
        "equity": ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
        "cash_and_cash_equivalents": ("CashAndCashEquivalentsAtCarryingValue",),
        "total_debt": ("LongTermDebtAndFinanceLeaseObligationsCurrent", "LongTermDebtCurrent", "LongTermDebtNoncurrent"),
        "current_assets": ("AssetsCurrent",), "current_liabilities": ("LiabilitiesCurrent",),
        "shares_outstanding": ("CommonStocksIncludingAdditionalPaidInCapitalMember", "EntityCommonStockSharesOutstanding"),
        "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",),
        "capex": ("PaymentsToAcquirePropertyPlantAndEquipment",),
        "investing_cash_flow": ("NetCashProvidedByUsedInInvestingActivities",),
        "financing_cash_flow": ("NetCashProvidedByUsedInFinancingActivities",),
    }

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None):
        self.settings, self.client = settings, client

    async def collect(self, profile: CompanyResearchProfile) -> InternationalFundamentalsResult:
        client, owned = self._client()
        try:
            cik = profile.provider_instrument_ids.get("SEC_CIK") or await self._resolve_cik(client, profile.ticker)
            if not cik:
                return InternationalFundamentalsResult([], {})
            response = await client.get(self.settings.sec_edgar_companyfacts_endpoint.format(cik=str(cik).zfill(10)))
            response.raise_for_status()
            return InternationalFundamentalsResult(self._facts(profile, str(cik).zfill(10), response.json()), {"SEC_CIK": str(cik).zfill(10)})
        finally:
            if owned: await client.aclose()

    def _client(self) -> tuple[httpx.AsyncClient, bool]:
        return self.client or httpx.AsyncClient(timeout=self.settings.sec_edgar_timeout_seconds, headers={"User-Agent": self.settings.sec_edgar_user_agent, "Accept-Encoding": "gzip, deflate"}), self.client is None

    async def _resolve_cik(self, client: httpx.AsyncClient, ticker: str) -> str | None:
        response = await client.get(self.settings.sec_edgar_ticker_endpoint)
        response.raise_for_status()
        wanted = ticker.upper()
        for item in response.json().values() if isinstance(response.json(), dict) else response.json():
            if str(item.get("ticker", "")).upper() == wanted:
                return str(item.get("cik_str"))
        return None

    def _facts(self, profile: CompanyResearchProfile, cik: str, payload: dict[str, Any]) -> list[FinancialFact]:
        facts: list[FinancialFact] = []
        us_gaap = payload.get("facts", {}).get("us-gaap", {})
        for metric, concepts in self._CONCEPTS.items():
            concept = next((us_gaap.get(name) for name in concepts if us_gaap.get(name)), None)
            if not concept: continue
            units = concept.get("units", {})
            entries = next(iter(units.values()), [])
            selected: dict[tuple[str, str], dict[str, Any]] = {}
            for entry in entries:
                form, end, value = entry.get("form"), entry.get("end"), _decimal(entry.get("val"))
                if form not in {"10-Q", "10-K"} or not end or value is None: continue
                kind = "ANNUAL" if form == "10-K" else "QUARTERLY"
                # Balance facts are point-in-time; flow facts must have an explicit frame.
                if metric in {"total_assets", "total_liabilities", "equity", "cash_and_cash_equivalents", "total_debt", "current_assets", "current_liabilities", "shares_outstanding"}:
                    kind = "AS_AT"
                elif not entry.get("frame"):
                    continue
                selected[(kind, end)] = entry
            for (kind, end), entry in sorted(selected.items(), reverse=True)[:4]:
                value = _decimal(entry.get("val"))
                facts.append(_fact(profile, metric, end, kind, value, "UNKNOWN", "SEC_EDGAR", f"SEC:{cik}:{entry.get('accn', end)}", self.settings.sec_edgar_companyfacts_endpoint.format(cik=cik), "SEC EDGAR", "OFFICIAL_REGULATORY_FILING", FactSourceTier.OFFICIAL_REGULATORY, entry.get("filed")))
        return facts


class EodhdFundamentalProvider:
    provider_name = "EODHD"
    _METRICS = {"Revenue": "revenue", "OperatingIncome": "operating_income", "EBIT": "ebit", "EBITDA": "ebitda", "NetIncome": "pat", "EarningsPerShare": "eps", "TotalAssets": "total_assets", "TotalLiab": "total_liabilities", "TotalStockholderEquity": "equity", "CashAndEquivalents": "cash_and_cash_equivalents", "ShortLongTermDebtTotal": "total_debt", "TotalCurrentAssets": "current_assets", "TotalCurrentLiabilities": "current_liabilities", "CommonSharesOutstanding": "shares_outstanding", "TotalCashFromOperatingActivities": "operating_cash_flow", "TotalCashflowsFromInvestingActivities": "investing_cash_flow", "TotalCashFromFinancingActivities": "financing_cash_flow", "CapitalExpenditures": "capex"}

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None): self.settings, self.client = settings, client

    async def collect(self, profile: CompanyResearchProfile) -> InternationalFundamentalsResult:
        if not self.settings.eodhd_api_key: return InternationalFundamentalsResult([], {})
        symbol = profile.provider_instrument_ids.get("EODHD") or profile.ticker
        client, owned = self.client or httpx.AsyncClient(timeout=self.settings.eodhd_timeout_seconds), self.client is None
        try:
            response = await client.get(f"{self.settings.eodhd_base_url.rstrip('/')}/fundamentals/{symbol}", params={"api_token": self.settings.eodhd_api_key, "fmt": "json"})
            response.raise_for_status(); payload = response.json()
            if not _eodhd_identity_matches(profile, payload): return InternationalFundamentalsResult([], {})
            return InternationalFundamentalsResult(self._facts(profile, symbol, payload), {"EODHD": symbol})
        finally:
            if owned: await client.aclose()

    def _facts(self, profile: CompanyResearchProfile, symbol: str, payload: dict[str, Any]) -> list[FinancialFact]:
        out: list[FinancialFact] = []
        for statement_key, annual_kind in (("Income_Statement", "ANNUAL"), ("Balance_Sheet", "AS_AT"), ("Cash_Flow", "ANNUAL")):
            statement = payload.get("Financials", {}).get(statement_key, {})
            for frequency, rows in (("yearly", statement.get("yearly", {})), ("quarterly", statement.get("quarterly", {}))):
                kind = annual_kind if frequency == "yearly" or annual_kind == "AS_AT" else "QUARTERLY"
                for row in rows.values():
                    end = row.get("date")
                    if not end: continue
                    for raw, metric in self._METRICS.items():
                        value = _decimal(row.get(raw))
                        if value is not None: out.append(_fact(profile, metric, end, kind, value, "UNKNOWN", "EODHD", f"EODHD:{symbol}:{kind}:{end}", f"{self.settings.eodhd_base_url.rstrip('/')}/fundamentals/{symbol}", "EODHD", "STRUCTURED_FUNDAMENTALS", FactSourceTier.STRUCTURED_FUNDAMENTALS, row.get("filing_date")))
        return _latest_four(out)


def _fact(profile, metric, end, kind, value, basis, provider, identity, url, name, source_type, tier, published):
    return FinancialFact(FinancialFactKey(profile.instrument_id, metric, str(end), kind, basis), ProvenancedValue(value=value, unit=None, source_url=url, source_name=name, source_type=source_type, published_at=_date(published), retrieved_at=datetime.now(timezone.utc), confidence=.9), tier, provider, identity, SourceMode.REAL)

def _latest_four(facts):
    keep = {}; [keep.setdefault((f.key.metric, f.key.period_type, f.key.period_end), f) for f in sorted(facts, key=lambda f: f.key.period_end or "", reverse=True) if sum(1 for x in keep.values() if x.key.metric == f.key.metric and x.key.period_type == f.key.period_type) < 4]; return list(keep.values())
def _decimal(value):
    try: return Decimal(str(value)) if value not in (None, "") else None
    except (InvalidOperation, ValueError): return None
def _date(value):
    try: return datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc) if value else None
    except ValueError: return None
def _is_european(profile): return str(profile.country).upper() in {"DE", "GERMANY", "FR", "FRANCE", "GB", "UK", "NL", "ES", "IT", "SE", "CH", "BE", "AT", "DK", "FI", "NO", "IE", "PT"}
def _eodhd_identity_matches(profile, payload):
    general = payload.get("General", payload); isin = str(general.get("ISIN") or "").upper(); exchange = str(general.get("Exchange") or general.get("ExchangeCode") or "").upper(); currency = str(general.get("CurrencyCode") or general.get("Currency") or "").upper()
    if profile.isin and isin and profile.isin.upper() != isin: return False
    if profile.currency and currency and profile.currency.upper() != currency: return False
    expected = {str(profile.exchange).upper(), str(profile.mic).upper()}; aliases = {"XETRA", "XFRA", "F", "FRANKFURT"}
    if exchange and not (exchange in expected or (expected & aliases and exchange in aliases)): return False
    return bool(profile.isin and isin or exchange or not profile.exchange)
