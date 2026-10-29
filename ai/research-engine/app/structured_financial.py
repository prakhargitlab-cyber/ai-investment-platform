"""Official NSE financial XBRL, using the existing discovery client and fetcher.

The endpoint and 2026 taxonomy were verified against NSE's own site and filings;
see docs/nse-structured-financial-contract.md. Unsupported forms fall back to the
existing document path. This module neither writes evidence nor parses PDFs.
"""
import asyncio
import calendar
import logging
import time
from collections import OrderedDict, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Protocol
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

from app.fact_precedence import FinancialFact, FinancialFactKey, FactSourceTier, merge_fact
from app.financial_evidence_plan import filing_rank, period_date, potential_coverage
from app.research_fetching import DocumentSizeLimitExceeded
from app.models import CompanyResearchProfile, ProvenancedValue, SourceMode
from app.source_discovery import _is_indian_nse_profile

logger = logging.getLogger(__name__)


class OfficialFinancialProvider(Protocol):
    provider_name: str

    async def collect(self, profile: CompanyResearchProfile) -> list[FinancialFact]: ...


_XBRLI = "http://www.xbrl.org/2003/instance"
_TAXONOMY = "http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt"
_SUPPORTED_TAXONOMIES = {_TAXONOMY, "http://www.sebi.gov.in/xbrl/2025-01-31/in-capmkt"}
_PARSER_VERSION = "nse-integrated-2025-2026-v2"
_MAX_DISCOVERY_PAGES = 5
_MAX_XML_DOCUMENTS = 4
_EXTRA_PERIOD_DOCUMENTS = 2
_ACQUISITION_SECONDS = 20.0
_IST = timezone(timedelta(hours=5, minutes=30))
_DURATION = {
    "RevenueFromOperations": "revenue", "ProfitLossForPeriod": "pat",
    "ProfitBeforeTax": "pbt", "FinanceCosts": "finance_costs",
    "CashFlowsFromUsedInOperatingActivities": "cash_flow_from_operating_activities",
    "CashFlowsFromUsedInInvestingActivities": "cash_flow_from_investing_activities",
    "CashFlowsFromUsedInFinancingActivities": "cash_flow_from_financing_activities",
}
_INSTANT = {"Assets": "total_assets", "Equity": "total_equity", "Liabilities": "total_liabilities",
            "CurrentAssets": "current_assets", "CurrentLiabilities": "current_liabilities",
            "CashAndCashEquivalents": "cash_and_cash_equivalents",
            "BorrowingsCurrent": "current_borrowings", "BorrowingsNoncurrent": "noncurrent_borrowings"}
_EPS = ("DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
        "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations")


def _financial_log_url(value):
    """Log source/namespace identity without credentials, query tokens or fragments."""
    try:
        parsed = urlparse(str(value))
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            return f"{parsed.scheme}://{parsed.hostname}{parsed.path}"
    except ValueError:
        pass
    return "REDACTED_NON_HTTP_URI"


def _official_xml_url(url):
    parsed = urlparse(str(url))
    return (parsed.scheme == "https" and parsed.hostname == "nsearchives.nseindia.com"
            and not parsed.username and not parsed.password and parsed.port in {None, 443}
            and parsed.path.startswith("/corporate/xbrl/") and parsed.path.endswith(".xml"))


def _filing_log_metadata(row):
    try:
        period = datetime.strptime(row.get("qe_Date"), "%d-%b-%Y").date().isoformat()
    except (ValueError, TypeError):
        period = "UNKNOWN"
    basis = str(row.get("consolidated", "")).upper()
    return {"source_url": _financial_log_url(row.get("xbrl")), "reported_period": period,
            "reporting_basis": basis if basis in {"CONSOLIDATED", "STANDALONE"} else "UNKNOWN"}


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
    rejected = defaultdict(int)

    def report(facts, reason):
        metadata = _filing_log_metadata(row)
        logger.info(
            "official_structured_financial_parse instrument_id=%s source_url=%s "
            "reported_period=%s reporting_basis=%s namespaces=%s parsed_fact_count=%d "
            "persisted_fact_count=%s reason=%s rejected=%s",
            profile.instrument_id, metadata["source_url"], metadata["reported_period"],
            metadata["reporting_basis"], sorted({_financial_log_url(uri) for uri in namespaces.values()}),
            len(facts), "UNKNOWN" if facts else 0, reason, dict(rejected),
        )
        return facts

    # Support both 2025 and 2026 taxonomies
    taxonomy_nodes = []
    for taxonomy in _SUPPORTED_TAXONOMIES:
        taxonomy_nodes.extend([n for n in root if n.tag.startswith("{" + taxonomy + "}")])
    if not taxonomy_nodes:
        return report([], "UNSUPPORTED_TAXONOMY")
    present_taxonomies = sorted(t for t in _SUPPORTED_TAXONOMIES
                                if any(n.tag.startswith("{" + t + "}") for n in root))
    chosen_taxonomy = present_taxonomies[0] if len(present_taxonomies) == 1 else None
    if not chosen_taxonomy:
        raise ValueError("NSE_STRUCTURED_FINANCIAL_AMBIGUOUS_TAXONOMY")

    def values(name, context=None):
        return {str(n.text or "").strip() for n in taxonomy_nodes if n.tag == f"{{{chosen_taxonomy}}}{name}"
                and (context is None or n.get("contextRef") == context)}
    symbol = profile.provider_instrument_ids.get("NSE", "").strip().upper()
    if (not symbol or not profile.isin or values("Symbol") != {symbol}
            or values("ISIN") != {profile.isin.strip().upper()} or row.get("symbol") != symbol
            or row.get("isin") and str(row["isin"]).strip().upper() != profile.isin.strip().upper()):
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
    expected_scrip = row.get("scripCode") or profile.provider_instrument_ids.get("BSE")
    if len(scrips) != 1 or (expected_scrip and scrips != {str(expected_scrip).strip()}):
        raise ValueError("NSE_STRUCTURED_FINANCIAL_SCRIP_MISMATCH")
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
                identifier.get("scheme") in {"http://www.sebi.gov.in/in-capmkt/ScripCode",
                                              "http://www.nseindia.com/in-capmkt/ScripCode",
                                              "http://www.bseindia.com/in-capmkt/ScripCode"}
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
            if kind is None:
                rejected["CONTEXT_DURATION_UNSUPPORTED"] += 1
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
    for node in taxonomy_nodes:
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
            rejected["FACT_UNIT_UNSUPPORTED"] += 1
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
            calculation_basis=f"NSE_XBRL:{chosen_taxonomy};tag={tag};context={context}"),
            FactSourceTier.OFFICIAL_NSE, "NSE", row["xbrl"], SourceMode.REAL))
    return report(sorted(facts, key=lambda f: (f.key.period_end, f.key.period_type, f.key.metric)),
                  "PARSED" if facts else "NO_ELIGIBLE_FACTS" if contexts else "NO_ELIGIBLE_CONTEXTS")


class NseOfficialFinancialProvider:
    provider_name = "NSE"
    RESULTS_URL = "https://www.nseindia.com/api/integrated-filing-results"

    def __init__(self, client, fetcher, *, clock=None):
        # Borrow existing clients; their lifecycle, timeouts and retries remain
        # owned by discovery/repository. No new HTTP client or resource setting.
        self.client, self.fetcher = client, fetcher
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        # Receipts only: never retain XML bodies or an unbounded fact cache.
        self._planned_receipts = OrderedDict()

    def _remember_receipt(self, key):
        self._planned_receipts[key] = None
        self._planned_receipts.move_to_end(key)
        while len(self._planned_receipts) > 256:
            self._planned_receipts.popitem(last=False)

    async def collect_planned(self, profile, *, existing_facts, plan_factory, accept,
                              document_failed=None, record_failure=None, completed=None,
                              document_known=None, record_document=None):
        """Acquire exact missing statement keys within independent XML budgets.

        Discovery metadata estimates coverage; only accepted facts close gaps.
        The extra two slots require distinct unmet periods, never a blanket
        extension of the default four-document allowance.
        """
        if (not self.fetcher.settings.research_live_enabled or not _is_indian_nse_profile(profile)
                or not profile.isin or not profile.provider_instrument_ids.get("NSE")):
            return []
        facts = {fact.key: fact for fact in existing_facts}
        plan = plan_factory(list(facts.values()))
        if not plan.missing(facts.values()) and not plan.unresolved:
            return []
        symbol = profile.provider_instrument_ids["NSE"].strip().upper()
        rows, seen_rows, attempted, extension_periods = [], set(), set(), set()
        fetched_urls = set()
        accepted, pages, downloads, bytes_received = [], 0, 0, 0
        reason = "NO_ELIGIBLE_FILINGS"
        started = time.monotonic()
        max_bytes = self.fetcher.settings.research_max_content_bytes

        def receipt(row):
            return (str(profile.instrument_id), symbol, profile.isin, _PARSER_VERSION,
                    row["xbrl"], row["qe_Date"], row["broadcast_Date"], row["consolidated"], max_bytes)

        def replan(values):
            basis = plan.reporting_basis
            if not any(f.key.reporting_basis in {"CONSOLIDATED", "STANDALONE"} for f in values):
                bases = {str(r["consolidated"]).upper() for r in rows}
                basis = "CONSOLIDATED" if "CONSOLIDATED" in bases else "STANDALONE"
            published = [datetime.strptime(r["qe_Date"], "%d-%b-%Y").date().isoformat()
                         for r in rows if str(r["consolidated"]).upper() == basis]
            return plan_factory(values, published_periods=published, preferred_basis=basis)

        try:
            async with asyncio.timeout(_ACQUISITION_SECONDS):
                for page in range(1, _MAX_DISCOVERY_PAGES + 1):
                    response = await self.client.get(self.RESULTS_URL, params={
                        "symbol": symbol, "type": "Integrated Filing- Financials", "page": page, "size": 20})
                    pages += 1
                    response.raise_for_status()
                    if len(response.content) > max_bytes:
                        raise ValueError("NSE_STRUCTURED_FINANCIAL_INVALID_RESPONSE")
                    payload = response.json()
                    if (not isinstance(payload, dict) or not isinstance(payload.get("data"), list)
                            or len(payload["data"]) > 20):
                        raise ValueError("NSE_STRUCTURED_FINANCIAL_INVALID_RESPONSE")
                    before = len(seen_rows)
                    for row in payload["data"]:
                        try:
                            if (not isinstance(row, dict) or row.get("symbol") != symbol
                                    or row.get("type") != "Integrated Filing- Financials"
                                    or not _official_xml_url(row.get("xbrl"))
                                    or str(row.get("consolidated", "")).upper() not in {"CONSOLIDATED", "STANDALONE"}):
                                continue
                            end = datetime.strptime(row["qe_Date"], "%d-%b-%Y").date()
                            published = datetime.strptime(row["broadcast_Date"], "%d-%b-%Y %H:%M:%S").replace(tzinfo=_IST)
                            if end > published.date() or published > self.clock():
                                continue
                            key = receipt(row)
                        except (KeyError, ValueError, TypeError):
                            continue
                        if key not in seen_rows:
                            seen_rows.add(key)
                            rows.append(row)
                    # A repeated page is not historical pagination. Never spin
                    # on providers that ignore page or advertise stale totals.
                    if not payload["data"] or len(seen_rows) == before:
                        break
                    discovered = replan(list(facts.values()))
                    outstanding = discovered.missing(facts.values())
                    if (not discovered.unresolved and outstanding and all(any(
                            str(r["consolidated"]).upper() == need.reporting_basis
                            and datetime.strptime(r["qe_Date"], "%d-%b-%Y").date() == period_date(need.period_end)
                            for r in rows) for need in outstanding)):
                        break
                    total = payload.get("totalCount")
                    if isinstance(total, int) and page * 20 >= total:
                        break
                plan = replan(list(facts.values()))
                # A revision cannot be replaced by its older original merely
                # because the current parser has no revision policy.
                revised = {(r["qe_Date"].upper(), str(r["consolidated"]).upper()) for r in rows
                           if r.get("type_Sub") != "Original" or r.get("revised_Date")}
                while True:
                    missing = plan.missing(facts.values())
                    if not missing and not plan.unresolved:
                        reason = "SATISFIED"
                        break
                    if downloads >= _MAX_XML_DOCUMENTS + _EXTRA_PERIOD_DOCUMENTS:
                        reason = "DOCUMENT_LIMIT"
                        break
                    fiscal_end = period_date(plan.annual_periods[0]) if plan.annual_periods else None
                    candidates = []
                    for row in rows:
                        key = receipt(row)
                        if (key in attempted or key in self._planned_receipts or row["xbrl"] in fetched_urls
                                or (row["qe_Date"].upper(), str(row["consolidated"]).upper()) in revised
                                or str(row["consolidated"]).upper() != plan.reporting_basis):
                            continue
                        rank = filing_rank(row, missing, fiscal_end=fiscal_end)
                        if rank is None and any(x.startswith("PUBLISHED_") for x in plan.unresolved):
                            # Bootstrap calendar identity from a real statement.
                            # An audited filing is a useful hint, never a fact.
                            rank = (0, int(row.get("audited") == "Audited"), 0,
                                    datetime.strptime(row["broadcast_Date"], "%d-%b-%Y %H:%M:%S"), row["xbrl"])
                        if rank is not None:
                            candidates.append((rank, key, row))
                    if not candidates:
                        reason = "PAGE_LIMIT" if pages == _MAX_DISCOVERY_PAGES else "NO_ELIGIBLE_FILINGS"
                        break
                    _, key, row = max(candidates, key=lambda item: item[0])
                    attempted.add(key)
                    if document_known is not None and await document_known(key):
                        continue
                    if document_failed is not None and await document_failed(row["xbrl"], max_bytes):
                        continue
                    if downloads >= _MAX_XML_DOCUMENTS:
                        periods = {n.period_end for n in potential_coverage(row, missing, fiscal_end=fiscal_end)}
                        distinct = periods - extension_periods
                        if not distinct:
                            reason = "DOCUMENT_LIMIT"
                            continue
                        extension_periods.add(min(distinct))
                    downloads += 1
                    fetched_urls.add(row["xbrl"])
                    try:
                        network = await self.fetcher.fetch_network(row["xbrl"], headers={
                            "User-Agent": "Mozilla/5.0 (compatible; AIInvestmentResearch/1.0)",
                            "Accept": "application/xml,text/xml,*/*", "Referer": "https://www.nseindia.com/"},
                            max_bytes=max_bytes)
                        bytes_received += len(network.content)
                        if len(network.content) > max_bytes:
                            raise DocumentSizeLimitExceeded(len(network.content), max_bytes)
                        if (not _official_xml_url(network.final_url) or network.status_code != 200
                                or network.content_type.split(";", 1)[0] not in {"application/xml", "text/xml"}):
                            raise ValueError("NSE_STRUCTURED_FINANCIAL_INVALID_RESPONSE")
                        parsed = await asyncio.to_thread(parse_nse_financial_xbrl, network.content, profile,
                                                         {**row, "xbrl": network.final_url}, retrieved_at=self.clock())
                        del network
                    except DocumentSizeLimitExceeded:
                        if record_failure is not None:
                            await record_failure(row["xbrl"], max_bytes)
                        self._remember_receipt(key)
                        continue
                    except (ValueError, ET.ParseError) as exc:
                        logger.info("official_structured_document_rejected source_url=%s error_type=%s",
                                    _financial_log_url(row["xbrl"]), type(exc).__name__)
                        parsed = []
                    prospective = dict(facts)
                    for fact in parsed:
                        prospective[fact.key] = merge_fact(prospective.get(fact.key), fact)
                    candidate_plan = replan(list(prospective.values()))
                    selected = [f for f in parsed if candidate_plan.accepts_period(f)]
                    if selected:
                        persisted = await accept(selected)
                        # Production returns the durable reload. Simple callers
                        # may return an upsert count; use the same precedence.
                        if isinstance(persisted, (list, tuple)):
                            facts = {f.key: f for f in persisted}
                        else:
                            for fact in selected:
                                facts[fact.key] = merge_fact(facts.get(fact.key), fact)
                        accepted.extend(selected)
                    plan = replan(list(facts.values()))
                    if record_document is not None:
                        await record_document(key, row["xbrl"], len(selected))
                    self._remember_receipt(key)
        except asyncio.CancelledError:
            reason = "CANCELLED"
            raise
        except TimeoutError:
            reason = "TIME_LIMIT"
            raise
        except Exception:
            reason = "PROVIDER_BACKOFF"
            raise
        finally:
            unmet = plan.missing(facts.values())
            logger.info("official_financial_plan instrument_id=%s reason=%s pages=%d downloads=%d "
                        "bytes_received=%d elapsed_ms=%d unmet_targets=%s missing_keys=%s unresolved=%s",
                        profile.instrument_id, reason, pages, downloads, bytes_received,
                        round((time.monotonic() - started) * 1000), sorted({n.period_end for n in unmet}),
                        sorted({(n.metric, n.frequency, n.period_end, n.reporting_basis) for n in unmet}), plan.unresolved)
            if completed is not None:
                await completed(reason)
        return accepted

    async def collect(self, profile):
        if (not self.fetcher.settings.research_live_enabled
                or not _is_indian_nse_profile(profile) or not profile.isin
                or not profile.provider_instrument_ids.get("NSE")):
            return []
        symbol = profile.provider_instrument_ids["NSE"].strip().upper()
        all_rows, discovered = [], set()
        # Paginate through all results to discover historical filings
        page = 1
        max_pages = _MAX_DISCOVERY_PAGES
        logger.info("official_structured_financial_discovery instrument_id=%s page_limit=%d",
                    profile.instrument_id, max_pages)
        stop_reason = "PAGE_LIMIT"
        while page <= max_pages:
            response = await self.client.get(self.RESULTS_URL, params={
                "symbol": symbol, "type": "Integrated Filing- Financials", "page": page, "size": 20})
            response.raise_for_status()
            payload = response.json()
            if (not isinstance(payload, dict) or not isinstance(payload.get("data"), list)
                    or len(payload["data"]) > 20):
                raise ValueError("NSE_STRUCTURED_FINANCIAL_INVALID_RESPONSE")
            before = len(discovered)
            for row in payload["data"]:
                if (not isinstance(row, dict) or row.get("symbol") != symbol
                        or row.get("type") != "Integrated Filing- Financials"
                        or not _official_xml_url(row.get("xbrl"))
                        or not row.get("qe_Date") or not row.get("broadcast_Date")):
                    continue
                try:
                    end = datetime.strptime(row["qe_Date"], "%d-%b-%Y")
                    published = datetime.strptime(row["broadcast_Date"], "%d-%b-%Y %H:%M:%S")
                except (ValueError, TypeError):
                    continue
                identity = (row["xbrl"], end, published, row.get("consolidated"))
                if identity in discovered:
                    continue
                discovered.add(identity)
                all_rows.append((end, published, row))
            logger.info("official_structured_financial_page instrument_id=%s page=%d raw_rows=%d "
                        "valid_rows=%d discovered_periods=%s empty_page=%s", profile.instrument_id,
                        page, len(payload["data"]), len(discovered) - before,
                        sorted({end.date().isoformat() for end, _, _ in all_rows}), not payload["data"])
            if not payload["data"]:
                stop_reason = "EMPTY_PAGE"
                break
            # Metadata discovery has its own page bound, independent of XML work.
            if len(discovered) == before:
                stop_reason = "NO_NEW_FILINGS"
                break
            page += 1
        logger.info("official_structured_financial_pagination_stop instrument_id=%s page=%d reason=%s",
                    profile.instrument_id, min(page, max_pages), stop_reason)
        all_rows.sort(key=lambda item: (item[0], item[1], item[2]["xbrl"]), reverse=True)
        # Bounded XML work; does not consume or enlarge the existing PDF budget.
        selected, identities, skipped = [], set(), []
        for _, _, row in all_rows:
            identity = (row["qe_Date"], row.get("consolidated"))
            if identity in identities:
                continue
            identities.add(identity)
            if len(selected) >= _MAX_XML_DOCUMENTS:
                if row.get("type_Sub") == "Original" and not row.get("revised_Date"):
                    skipped.append(_filing_log_metadata(row))
                continue
            if row.get("type_Sub") != "Original" or row.get("revised_Date"):
                # Revision semantics are not inferred from timestamps alone.
                # Unsupported is not a transient provider exception: let the
                # existing official-document result determine repair eligibility.
                return []
            selected.append(row)
        logger.info("official_structured_financial_selection instrument_id=%s selected=%s "
                    "document_limit_skipped=%s reason=%s", profile.instrument_id,
                    [_filing_log_metadata(row) for row in selected], skipped,
                    "DOCUMENT_LIMIT" if skipped else "SELECTION_COMPLETE")
        facts, bytes_received, invalid_response = [], 0, False
        for row in selected:
            try:
                network = await self.fetcher.fetch_network(row["xbrl"], headers={
                    "User-Agent": "Mozilla/5.0 (compatible; AIInvestmentResearch/1.0)",
                    "Accept": "application/xml,text/xml,*/*", "Referer": "https://www.nseindia.com/"},
                    max_bytes=self.fetcher.settings.research_max_content_bytes)
            except Exception as exc:
                logger.info("official_structured_financial_filing_failed instrument_id=%s filing=%s "
                            "reason=FETCH_OR_PARSE_ERROR error_type=%s", profile.instrument_id,
                            _filing_log_metadata(row), type(exc).__name__)
                raise
            bytes_received += len(network.content)
            if (not _official_xml_url(network.final_url) or network.status_code != 200
                    or network.content_type.split(";", 1)[0] not in {"application/xml", "text/xml"}):
                invalid_response = True
                continue
            try:
                row = {**row, "xbrl": network.final_url}
                facts.extend(await asyncio.to_thread(parse_nse_financial_xbrl, network.content, profile, row,
                                                      retrieved_at=self.clock()))
            except (ValueError, ET.ParseError) as exc:
                logger.info("official_structured_document_rejected source_url=%s error_type=%s",
                            _financial_log_url(row["xbrl"]), type(exc).__name__)
        if invalid_response and not facts:
            raise ValueError("NSE_STRUCTURED_FINANCIAL_INVALID_RESPONSE")
        logger.info("bytes_received=%d facts_collected=%d", bytes_received, len(facts))
        return facts
