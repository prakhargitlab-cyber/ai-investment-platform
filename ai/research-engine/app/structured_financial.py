"""Official NSE financial XBRL, using the existing discovery client and fetcher.

The endpoint and 2026 taxonomy were verified against NSE's own site and filings;
see docs/nse-structured-financial-contract.md. Unsupported forms fall back to the
existing document path. This module neither writes evidence nor parses PDFs.
"""
import asyncio
import calendar
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Protocol
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

from app.fact_precedence import FinancialFact, FinancialFactKey, FactSourceTier
from app.models import CompanyResearchProfile, ProvenancedValue, SourceMode
from app.source_discovery import _is_indian_nse_profile


class OfficialFinancialProvider(Protocol):
    provider_name: str

    async def collect(self, profile: CompanyResearchProfile) -> list[FinancialFact]: ...


_XBRLI = "http://www.xbrl.org/2003/instance"
_TAXONOMY = "http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt"
_IST = timezone(timedelta(hours=5, minutes=30))
_DURATION = {
    "RevenueFromOperations": "revenue", "ProfitLossForPeriod": "pat",
    "FinanceCosts": "finance_costs",
    "CashFlowsFromUsedInOperatingActivities": "cash_flow_from_operating_activities",
    "CashFlowsFromUsedInInvestingActivities": "cash_flow_from_investing_activities",
    "CashFlowsFromUsedInFinancingActivities": "cash_flow_from_financing_activities",
}
_INSTANT = {"Assets": "total_assets", "Equity": "total_equity", "Liabilities": "total_liabilities",
            "CurrentAssets": "current_assets", "CurrentLiabilities": "current_liabilities",
            "CashAndCashEquivalents": "cash_and_cash_equivalents"}
_EPS = ("DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
        "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations")


def _official_xml_url(url):
    parsed = urlparse(str(url))
    return (parsed.scheme == "https" and parsed.hostname == "nsearchives.nseindia.com"
            and not parsed.username and not parsed.password and parsed.port in {None, 443}
            and parsed.path.startswith("/corporate/xbrl/") and parsed.path.endswith(".xml"))


def parse_nse_financial_xbrl(content: bytes, profile, row, *, retrieved_at) -> list[FinancialFact]:
    """Project only explicit, unambiguous company totals into existing fact keys.

    XBRL decimals describes accuracy, not a multiplier. Dimensional/segment facts,
    cumulative interim durations, nils and conflicting duplicate keys are omitted.
    No totals, EBITDA, debt, annualization or quarter subtraction are inferred.
    """
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise ValueError("NSE_STRUCTURED_FINANCIAL_UNSAFE_XML")
    namespaces = {}
    for _, (prefix, uri) in ET.iterparse(BytesIO(content), events=("start-ns",)):
        if prefix in namespaces and namespaces[prefix] != uri:
            raise ValueError("NSE_STRUCTURED_FINANCIAL_NAMESPACE_REBOUND")
        namespaces[prefix] = uri
    root = ET.fromstring(content)
    if root.tag != f"{{{_XBRLI}}}xbrl":
        raise ValueError("NSE_STRUCTURED_FINANCIAL_INVALID_XML")
    nodes = [n for n in root if n.tag.startswith("{" + _TAXONOMY + "}")]
    if not nodes:
        return []
    def values(name, context=None):
        return {str(n.text or "").strip() for n in nodes if n.tag == f"{{{_TAXONOMY}}}{name}"
                and (context is None or n.get("contextRef") == context)}
    symbol = profile.provider_instrument_ids.get("NSE", "").strip().upper()
    if (not symbol or not profile.isin or values("Symbol") != {symbol}
            or values("ISIN") != {profile.isin.strip().upper()} or row.get("symbol") != symbol):
        raise ValueError("NSE_STRUCTURED_FINANCIAL_IDENTITY_MISMATCH")
    basis = str(row.get("consolidated", "")).upper()
    if basis not in {"STANDALONE", "CONSOLIDATED"} or {
            value.upper() for value in values("NatureOfReportStandaloneConsolidated")} != {basis}:
        raise ValueError("NSE_STRUCTURED_FINANCIAL_BASIS_MISMATCH")
    if not row.get("qe_Date") or not row.get("broadcast_Date"):
        raise ValueError("NSE_STRUCTURED_FINANCIAL_INVALID_DATE")
    reported_end = datetime.strptime(row["qe_Date"], "%d-%b-%Y").date()
    published = datetime.strptime(row["broadcast_Date"], "%d-%b-%Y %H:%M:%S").replace(tzinfo=_IST)
    if not reported_end <= published.date() or not published <= retrieved_at:
        raise ValueError("NSE_STRUCTURED_FINANCIAL_INVALID_DATE")
    if not _official_xml_url(row["xbrl"]):
        raise ValueError("NSE_STRUCTURED_FINANCIAL_SOURCE_INVALID")
    # Context entity must also agree with the explicit document identity.
    scrips = values("ScripCode")
    contexts = {}
    context_ids = set()
    for context in root.findall(f"{{{_XBRLI}}}context"):
        if not context.get("id") or context.get("id") in context_ids:
            raise ValueError("NSE_STRUCTURED_FINANCIAL_DUPLICATE_CONTEXT")
        context_ids.add(context.get("id"))
        if any(n.tag.rsplit("}", 1)[-1] in {"scenario", "segment"} for n in context.iter()):
            continue
        identifier = context.find(f"{{{_XBRLI}}}entity/{{{_XBRLI}}}identifier")
        if identifier is None or not (
                identifier.get("scheme") == "http://www.sebi.gov.in/in-capmkt/ScripCode"
                and scrips == {identifier.text}):
            continue
        period = context.find(f"{{{_XBRLI}}}period")
        if period is None:
            continue
        parts = {n.tag.rsplit("}", 1)[-1]: date.fromisoformat(n.text) for n in period}
        end = parts.get("instant", parts.get("endDate"))
        if end is None or end > reported_end:
            continue
        kind = None
        if set(parts) == {"instant"}:
            kind = "AS_AT"
        elif set(parts) == {"startDate", "endDate"}:
            start = parts["startDate"]
            if end.day == calendar.monthrange(end.year, end.month)[1] and start.day == 1:
                months = (end.year - start.year) * 12 + end.month - start.month + 1
                kind = {3: "QUARTERLY", 12: "ANNUAL"}.get(months)
            # The explicit duration facts must agree with the XBRL context.
            key = context.get("id")
            if (values("DateOfStartOfReportingPeriod", key) != {start.isoformat()}
                    or values("DateOfEndOfReportingPeriod", key) != {end.isoformat()}):
                continue
        if kind:
            key = context.get("id")
            contexts[key] = (kind, end.isoformat())
    def qualified(text):
        prefix, sep, local = str(text or "").partition(":")
        return (namespaces.get(prefix), local) if sep else (namespaces.get(""), prefix)
    units = {}
    for unit in root.findall(f"{{{_XBRLI}}}unit"):
        measures = [qualified(n.text) for n in unit.findall(f"{{{_XBRLI}}}measure")]
        numerator = [qualified(n.text) for n in unit.findall(f"{{{_XBRLI}}}divide/{{{_XBRLI}}}unitNumerator/{{{_XBRLI}}}measure")]
        denominator = [qualified(n.text) for n in unit.findall(f"{{{_XBRLI}}}divide/{{{_XBRLI}}}unitDenominator/{{{_XBRLI}}}measure")]
        currency = [("http://www.xbrl.org/2003/iso4217", "INR")]
        value = ("INR" if measures == currency and not numerator and not denominator else
                 "INR per share" if not measures and numerator == currency and denominator == [(_XBRLI, "shares")] else None)
        if unit.get("id") in units:
            raise ValueError("NSE_STRUCTURED_FINANCIAL_DUPLICATE_UNIT")
        units[unit.get("id")] = value
    candidates = defaultdict(list)
    for node in nodes:
        name = node.tag.rsplit("}", 1)[-1]
        context = contexts.get(node.get("contextRef"))
        if context is None or node.get("{http://www.w3.org/2001/XMLSchema-instance}nil") in {"true", "1"}:
            continue
        kind, period_end = context
        metric = (_INSTANT if kind == "AS_AT" else _DURATION).get(name)
        if kind != "AS_AT" and name in _EPS:
            metric = "eps"
        if metric is None:
            continue
        unit = units.get(node.get("unitRef"))
        if unit != ("INR per share" if metric == "eps" else "INR"):
            continue
        try:
            value = Decimal(node.text or "")
        except InvalidOperation:
            continue
        if not value.is_finite():
            continue
        key = FinancialFactKey(profile.instrument_id, metric, period_end, kind, basis)
        candidates[key].append((name, node.get("contextRef"), value, unit))
    facts = []
    for key, alternatives in candidates.items():
        # Match the existing DI-20E diluted-then-basic total EPS projection.
        if key.metric == "eps":
            chosen = next(tag for tag in _EPS if any(a[0] == tag for a in alternatives))
            alternatives = [a for a in alternatives if a[0] == chosen]
        if len({(a[2], a[3]) for a in alternatives}) != 1:
            continue
        tag, context, value, unit = alternatives[0]
        facts.append(FinancialFact(key, ProvenancedValue(
            value=value, unit=unit, period=key.period_end, source_url=row["xbrl"],
            source_name="NSE integrated financial XBRL", source_type="EXCHANGE_ANNOUNCEMENT",
            published_at=published, retrieved_at=retrieved_at, confidence=.95,
            calculation_basis=f"NSE_XBRL:{_TAXONOMY};tag={tag};context={context}"),
            FactSourceTier.OFFICIAL_NSE, "NSE", row["xbrl"], SourceMode.REAL))
    return sorted(facts, key=lambda f: (f.key.period_end, f.key.period_type, f.key.metric))


class NseOfficialFinancialProvider:
    provider_name = "NSE"
    RESULTS_URL = "https://www.nseindia.com/api/integrated-filing-results"

    def __init__(self, client, fetcher, *, clock=None):
        # Borrow existing clients; their lifecycle, timeouts and retries remain
        # owned by discovery/repository. No new HTTP client or resource setting.
        self.client, self.fetcher = client, fetcher
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def collect(self, profile):
        if (not self.fetcher.settings.research_live_enabled
                or not _is_indian_nse_profile(profile) or not profile.isin
                or not profile.provider_instrument_ids.get("NSE")):
            return []
        symbol = profile.provider_instrument_ids["NSE"].strip().upper()
        response = await self.client.get(self.RESULTS_URL, params={
            "symbol": symbol, "type": "Integrated Filing- Financials", "page": 1, "size": 20})
        response.raise_for_status()
        payload = response.json()
        if (not isinstance(payload, dict) or not isinstance(payload.get("data"), list)
                or len(payload["data"]) > 20):
            raise ValueError("NSE_STRUCTURED_FINANCIAL_INVALID_RESPONSE")
        rows = []
        for row in payload["data"]:
            if (not isinstance(row, dict) or row.get("symbol") != symbol
                    or row.get("type") != "Integrated Filing- Financials"
                    or not _official_xml_url(row.get("xbrl"))
                    or not row.get("qe_Date") or not row.get("broadcast_Date")):
                # Incomplete filing-results metadata (NSE sometimes returns a
                # provisional/pending row with a null quarter-end or broadcast
                # timestamp). Treat as unsupported for this row only -- never
                # guess a date -- and let discovery continue past it.
                continue
            end = datetime.strptime(row["qe_Date"], "%d-%b-%Y")
            published = datetime.strptime(row["broadcast_Date"], "%d-%b-%Y %H:%M:%S")
            rows.append((end, published, row))
        rows.sort(key=lambda item: (item[0], item[1], item[2]["xbrl"]), reverse=True)
        # Bounded XML work; does not consume or enlarge the existing PDF budget.
        from app.deep_investigation import _SINGLETON_MAX_DOCUMENTS
        selected, identities = [], set()
        for _, _, row in rows:
            identity = (row["qe_Date"], row.get("consolidated"))
            if identity in identities:
                continue
            identities.add(identity)
            if row.get("type_Sub") != "Original" or row.get("revised_Date"):
                # Revision semantics are not inferred from timestamps alone.
                # Unsupported is not a transient provider exception: let the
                # existing official-document result determine repair eligibility.
                return []
            selected.append(row)
            if len(selected) == _SINGLETON_MAX_DOCUMENTS:
                break
        facts = []
        for row in selected:
            network = await self.fetcher.fetch_network(row["xbrl"], headers={
                "User-Agent": "Mozilla/5.0 (compatible; AIInvestmentResearch/1.0)",
                "Accept": "application/xml,text/xml,*/*", "Referer": "https://www.nseindia.com/"},
                max_bytes=self.fetcher.settings.research_max_content_bytes)
            if (not _official_xml_url(network.final_url) or network.status_code != 200
                    or network.content_type.split(";", 1)[0] not in {"application/xml", "text/xml"}):
                raise ValueError("NSE_STRUCTURED_FINANCIAL_INVALID_RESPONSE")
            row = {**row, "xbrl": network.final_url}
            facts.extend(await asyncio.to_thread(parse_nse_financial_xbrl, network.content, profile, row,
                                                  retrieved_at=self.clock()))
        return facts
