"""Bounded ETF acquisition adapters over capabilities already in the repository.

No scheduler, opportunity dispatch, readiness, automatic universe crawl, or
provider fallback. Callers explicitly choose a source and requested metric.
"""
import csv
from datetime import datetime, timezone
from decimal import Decimal
import io
import re
from urllib.parse import urlparse
from uuid import UUID

from app.etf_evidence import (
    EtfAcquisitionAttempt, EtfAcquisitionOutcome as Outcome, EtfAuthority, EtfFact,
    EtfListing, EtfMetric, EtfProvenance, evidence_id, etf_freshness,
)
from app.models import EtfResearchProfile, ResearchDocument, StructuredMarketSnapshot


NSE_ETF_LIST_URL = "https://nsearchives.nseindia.com/content/equities/eq_etfseclist.csv"


def parse_nse_etf_list(text: str, provenance: EtfProvenance, master_rows=()) -> list[EtfListing]:
    """Match the existing Java official-list headers and normalized-ISIN identity.

    Underlying is preserved verbatim: 'Gold', for example, is not necessarily
    an index name. DateofListing is never treated as fund inception.
    """
    if provenance.authority != EtfAuthority.OFFICIAL_EXCHANGE or provenance.provider != "NSE":
        raise ValueError("Official NSE provenance required")
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")), skipinitialspace=True, strict=True)
    if not reader.fieldnames or not {"Symbol", "SecurityName", "ISINNumber"} <= set(reader.fieldnames):
        raise ValueError("NSE_ETF_LIST_REQUIRED_HEADERS_MISSING")
    listings = {}
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise ValueError("NSE_ETF_LIST_MALFORMED_ROW")
        isin = re.sub(r"\s+", "", row["ISINNumber"]).upper()
        symbol = row["Symbol"].strip().upper()
        listing = EtfListing(isin=isin, symbol=symbol, name=row["SecurityName"].strip(),
            underlying_reference=(row.get("Underlying") or "").strip() or None, provenance=provenance)
        if isin in listings and listings[isin] != listing:
            raise ValueError("NSE_ETF_LIST_AMBIGUOUS_ISIN")
        listings[isin] = listing
    for isin, listing in listings.items():
        matches = [row for row in master_rows
                   if re.sub(r"\s+", "", str(row.get("isin") or "")).upper() == isin
                   and row.get("assetType") == "ETF"
                   and str(row.get("primaryExchange") or row.get("exchange") or "").upper() in {"NSE", "XNSE"}]
        ids = {UUID(str(row.get("globalInstrumentId") or row.get("instrumentId"))) for row in matches}
        if len(ids) > 1:
            raise ValueError("AMBIGUOUS_CANONICAL_ETF_IDENTITY")
        if ids:
            listing.instrument_id = next(iter(ids))
    return [listings[key] for key in sorted(listings)]


class EtfAcquisitionService:
    def __init__(self, persistence, *, clock=None):
        self.persistence = persistence
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def reusable_evidence(self, instrument_id, metric: EtfMetric, *, allow_secondary=False):
        """DB-only reuse; never advances an attempt or an evidence timestamp."""
        if metric == EtfMetric.NAV:
            rows = self.persistence.etf_navs(instrument_id)
        elif metric == EtfMetric.UNDERLYING_HOLDINGS:
            rows = self.persistence.etf_holdings(instrument_id)
        else:
            rows = self.persistence.etf_facts(instrument_id, metric)
        selected = rows[0] if rows else None
        if selected is None or etf_freshness(selected, metric, now=self.clock()) != "FRESH":
            return None
        if selected.provenance.authority == EtfAuthority.SECONDARY and not allow_secondary:
            return None
        return selected

    def _record(self, instrument_id, metric, provider, evidence=(), *, outcome=None, reason="EXPLICIT_SOURCE_EVIDENCE",
                unavailability_evidence=None):
        result = EtfAcquisitionAttempt(instrument_id=instrument_id, metric=str(metric), provider=provider,
            attempted_at=self.clock(), outcome=outcome or (Outcome.SUCCESS_WITH_DATA if evidence else Outcome.SUCCESS_EMPTY),
            reason=reason, evidence_ids=[evidence_id(item) for item in evidence], unavailability_evidence=unavailability_evidence)
        self.persistence.save_etf_acquisition(evidence, result)
        return result

    async def discover_nse(self, client, *, master_rows=()):
        """One explicit list request; never loops over fund/provider endpoints."""
        try:
            response = await client.get(NSE_ETF_LIST_URL, headers={"Accept": "text/csv", "Referer": "https://www.nseindia.com/"})
            response.raise_for_status()
            provenance = EtfProvenance(provider="NSE", source_type="NSE_OFFICIAL_ETF_SECURITY_LIST",
                source_identity=NSE_ETF_LIST_URL, source_url=NSE_ETF_LIST_URL,
                retrieved_at=self.clock(), authority=EtfAuthority.OFFICIAL_EXCHANGE, reliability_level="LEVEL_A")
            evidence = parse_nse_etf_list(response.text, provenance, master_rows)
        except Exception as exc:
            return self._record(None, "DISCOVERY", "NSE", outcome=Outcome.TECHNICAL_FAILURE,
                reason=f"NSE_ETF_DISCOVERY_FAILED:{type(exc).__name__}:{str(exc)[:160]}")
        return self._record(None, "DISCOVERY", "NSE", evidence)

    async def acquire_secondary_quote(self, profile: EtfResearchProfile, metric: EtfMetric, provider, instrument: dict):
        """Reuse Yahoo's proven quote facts, not its undated navPrice mapping."""
        mapping = {EtfMetric.MARKET_PRICE: "latestPrice", EtfMetric.TRADING_VOLUME: "volume",
                   EtfMetric.BID: "bid", EtfMetric.ASK: "ask"}
        if metric not in mapping:
            return self._record(profile.instrument_id, metric, "YAHOO_FINANCE", outcome=Outcome.NOT_IMPLEMENTED,
                reason="NO_PROVEN_FIELD_DATE_CONTRACT" if metric == EtfMetric.NAV else "NO_PROVEN_ETF_ADAPTER")
        try:
            if provider.provider_name != "YAHOO_FINANCE":
                raise ValueError("Unverified structured provider")
            if (instrument.get("assetType") != "ETF"
                    or UUID(str(instrument.get("instrumentId") or instrument.get("globalInstrumentId"))) != profile.instrument_id):
                raise ValueError("ETF_CANONICAL_IDENTITY_MISMATCH")
            snapshot = await provider.collect(instrument)
            if not isinstance(snapshot, StructuredMarketSnapshot) or snapshot.resolution.quote_type != "ETF":
                raise ValueError("MALFORMED_OR_NON_ETF_SNAPSHOT")
            if snapshot.resolution.instrument_id != profile.instrument_id:
                raise ValueError("ETF_PROVIDER_IDENTITY_MISMATCH")
            if snapshot.status != "STRUCTURED_PROVIDER_AVAILABLE" or snapshot.safe_error_code:
                raise ValueError(f"STRUCTURED_STATUS:{snapshot.status}")
            value = snapshot.facts.get(mapping[metric])
            if value is None:
                return self._record(profile.instrument_id, metric, provider.provider_name, reason="VALID_RESPONSE_FIELD_ABSENT")
            if value.as_of_date is None:
                raise ValueError("QUOTE_AS_OF_DATE_MISSING")
            fact = EtfFact(instrument_id=profile.instrument_id, metric=metric, value=Decimal(str(value.value)),
                unit=value.unit, as_of_date=value.as_of_date.date(), provenance=EtfProvenance(
                    provider=provider.provider_name, source_type="STRUCTURED_MARKET_PROVIDER",
                    source_identity=f"{snapshot.resolution.provider_ticker}:{mapping[metric]}:{value.as_of_date.isoformat()}",
                    source_url=value.source_url, source_locator=mapping[metric], retrieved_at=value.retrieved_at,
                    published_at=value.published_at, authority=EtfAuthority.SECONDARY,
                    confidence=value.confidence, reliability_level="LEVEL_C"))
        except Exception as exc:
            return self._record(profile.instrument_id, metric, "YAHOO_FINANCE", outcome=Outcome.TECHNICAL_FAILURE,
                reason=f"STRUCTURED_ACQUISITION_FAILED:{type(exc).__name__}:{str(exc)[:160]}")
        return self._record(profile.instrument_id, metric, provider.provider_name, [fact])

    def acquire_official_document(self, profile: EtfResearchProfile, document: ResearchDocument, metric: EtfMetric):
        """Parse only demonstrated legacy TER/AUM labels from a validated document.

        The caller uses the existing document fetcher. No new website discovery,
        guessed AMC URL, legacy inferred provider/index, or named holdings list.
        Missing effective dates remain unknown; publication is not an as-of date.
        """
        if metric not in {EtfMetric.EXPENSE_RATIO, EtfMetric.AUM}:
            return self._record(profile.instrument_id, metric, "OFFICIAL_DOCUMENT", outcome=Outcome.NOT_IMPLEMENTED,
                reason="NO_PROVEN_OFFICIAL_FIELD_PARSER")
        try:
            host = (urlparse(document.canonical_url).hostname or "").lower()
            nse = host in {"nsearchives.nseindia.com", "www.nseindia.com", "nseindia.com"}
            amc = host in {domain.lower() for domain in profile.known_domains}
            if (document.instrument_id != profile.instrument_id or document.source_mode != "REAL"
                    or document.status != "PROCESSED" or document.reliability_level not in {"LEVEL_A", "LEVEL_B"}
                    or not ((nse and document.source_classification == "EXCHANGE")
                            or (amc and document.source_classification == "OFFICIAL_COMPANY"))):
                raise ValueError("UNVERIFIED_ETF_OFFICIAL_DOCUMENT")
            text = document.normalized_text or ""
            if not text.strip() or re.search(r"captcha|access denied|too many requests", text, re.I):
                raise ValueError("EMPTY_OR_BLOCKED_DOCUMENT")
            if metric == EtfMetric.EXPENSE_RATIO:
                label = r"\b(?:total expense ratio|expense ratio|TER)\b"
                matches = re.findall(label + r"\s*[:=-]?\s*(\d+(?:\.\d+)?)\s*%", text, re.I)
                values = {(Decimal(number), "percent") for number in matches}
            else:
                label = r"\b(?:AUM|assets under management|fund size)\b"
                values = set()
                # EUR/USD/GBP with billion/million scales
                matches = re.findall(label + r"\s*[:=-]?\s*(EUR|USD|GBP)\s*(\d+(?:[,.]\d+)?)\s*(bn|billion|mn|million)\b", text, re.I)
                for currency, number, scale in matches:
                    num = Decimal(number.replace(",", ""))
                    factor = Decimal("1000000000") if scale.lower() in {"bn", "billion"} else Decimal("1000000")
                    values.add((num * factor, currency.upper()))
                # INR with crore/lakh scales (₹, INR, Rs, Rs.)
                # Handles Indian comma notation: 1,00,00,000 or international: 10,000,000
                matches_inr = re.findall(r"(?:₹|INR|Rs\.?)\s*(\d{1,3}(?:,\d{2})*(?:,\d{3})?|[\d.,]+)\s*(crore|crores|lakh|lakhs)\b", text, re.I)
                for number, scale in matches_inr:
                    # Handle Indian comma notation: 1,00,00,000 -> remove commas
                    num = Decimal(number.replace(",", "").replace(" ", ""))
                    factor = Decimal("10000000") if scale.lower() in {"crore", "crores"} else Decimal("100000")
                    values.add((num * factor, "INR"))
                # INR with million (not crore/lakh)
                matches_inr_mn = re.findall(r"(?:₹|INR|Rs\.?)\s*(\d+(?:[,.]\d+)?)\s*(million|mn)\b", text, re.I)
                for number, _ in matches_inr_mn:
                    num = Decimal(number.replace(",", ""))
                    values.add((num * Decimal("1000000"), "INR"))
                # Explicit INR with bn/mn (same as other currencies)
                matches_inr2 = re.findall(label + r"\s*[:=-]?\s*INR\s*(\d+(?:[,.]\d+)?)\s*(bn|billion|mn|million)\b", text, re.I)
                for number, scale in matches_inr2:
                    num = Decimal(number.replace(",", ""))
                    factor = Decimal("1000000000") if scale.lower() in {"bn", "billion"} else Decimal("1000000")
                    values.add((num * factor, "INR"))
            provenance = EtfProvenance(provider="NSE" if nse else host, source_type=str(document.source_type),
                source_identity=str(document.document_id), source_url=document.canonical_url,
                source_locator=f"normalized_text:{metric}", published_at=document.published_at,
                retrieved_at=document.retrieved_at, reliability_level=document.reliability_level,
                authority=EtfAuthority.OFFICIAL_EXCHANGE if nse else EtfAuthority.OFFICIAL_FUND)
            declaration = re.search(label + r"\s*:\s*(not published|not disclosed)\b", text, re.I)
            if not values and declaration:
                return self._record(profile.instrument_id, metric, "OFFICIAL_DOCUMENT", outcome=Outcome.GENUINELY_UNAVAILABLE,
                    reason=f"SOURCE_DECLARATION:{declaration.group(0)}", unavailability_evidence=provenance)
            if len(values) > 1 or (not values and re.search(label, text, re.I)):
                raise ValueError("AMBIGUOUS_OR_UNPARSEABLE_METRIC")
            if not values:
                return self._record(profile.instrument_id, metric, "OFFICIAL_DOCUMENT", reason="VALID_DOCUMENT_FIELD_ABSENT")
            value, unit = next(iter(values))
            fact = EtfFact(instrument_id=profile.instrument_id, metric=metric, value=value, unit=unit,
                provenance=provenance)
        except Exception as exc:
            return self._record(profile.instrument_id, metric, "OFFICIAL_DOCUMENT", outcome=Outcome.TECHNICAL_FAILURE,
                reason=f"OFFICIAL_DOCUMENT_FAILED:{type(exc).__name__}:{str(exc)[:160]}")
        return self._record(profile.instrument_id, metric, "OFFICIAL_DOCUMENT", [fact])
