"""Bounded ETF acquisition adapters over capabilities already in the repository.

No scheduler, opportunity dispatch, readiness, automatic universe crawl, or
provider fallback. Callers explicitly choose a source and requested metric.
"""
import csv
from datetime import datetime, timezone
from decimal import Decimal
import io
import re
from uuid import UUID

from app.etf_evidence import (
    EtfAcquisitionAttempt, EtfAcquisitionOutcome as Outcome, EtfAuthority, EtfFact,
    EtfListing, EtfMetric, EtfProvenance, evidence_id, etf_freshness,
)
from app.models import EtfResearchProfile, ResearchDocument, StructuredMarketSnapshot


NSE_ETF_LIST_URL = "https://nsearchives.nseindia.com/content/equities/eq_etfseclist.csv"

# NSE's archive host silently stalls (ReadTimeout) a request whose headers do
# not identify a browser, rather than returning an explicit 403/429 -- shared
# by every caller of NSE_ETF_LIST_URL (both the single-instrument discovery
# below and the Full ETF Radar's universe discovery in etf_full_universe.py)
# so the two acquisition paths never drift out of sync on this requirement.
NSE_ETF_LIST_HEADERS = {
    "Accept": "text/csv", "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
}


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
    def __init__(self, persistence, *, clock=None, run_blocking=None, fence=None, document_registry=None):
        self.persistence = persistence
        self.document_registry = document_registry
        self.fence = fence
        self.run_blocking = run_blocking
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
        self.persistence.save_etf_acquisition(evidence, result, **({"fence": self.fence} if self.fence else {}))
        return result

    async def _record_async(self, *args, **kwargs):
        if self.run_blocking is not None:
            return await self.run_blocking(lambda: self._record(*args, **kwargs))
        return self._record(*args, **kwargs)

    async def discover_nse(self, client, *, master_rows=(), instrument_ids=None):
        """One explicit list request; never loops over fund/provider endpoints.

        NSE's archive host silently stalls (ReadTimeout) requests whose
        User-Agent identifies a non-browser HTTP client, rather than returning
        an explicit 403/429 -- the same browser-identity headers are already
        required for every other NSE call in this codebase (see nse_etf_market
        .HEADERS and nse_historical_daily.HEADERS) and for the equivalent,
        already-working portfolio-service Java client against this identical
        URL (OfficialNseEtfSecurityListClient).
        """
        try:
            response = await client.get(NSE_ETF_LIST_URL, headers=NSE_ETF_LIST_HEADERS)
            response.raise_for_status()
            provenance = EtfProvenance(provider="NSE", source_type="NSE_OFFICIAL_ETF_SECURITY_LIST",
                source_identity=NSE_ETF_LIST_URL, source_url=NSE_ETF_LIST_URL,
                retrieved_at=self.clock(), authority=EtfAuthority.OFFICIAL_EXCHANGE, reliability_level="LEVEL_A")
            if instrument_ids is not None:
                # A fresh successful discovery is a dated identity observation;
                # repeat requests on the same day retain the same evidence key.
                provenance.source_identity += "#observed=" + provenance.retrieved_at.date().isoformat()
            evidence = parse_nse_etf_list(response.text, provenance, master_rows)
            self.discovered_listings = evidence
            if instrument_ids is not None:
                requested = set(instrument_ids)
                evidence = [item for item in evidence if item.instrument_id in requested]
                if {item.instrument_id for item in evidence} != requested:
                    raise ValueError("CANONICAL_ETF_NOT_IN_OFFICIAL_LIST")
                expected = {UUID(str(row.get("globalInstrumentId") or row.get("instrumentId"))):
                            str(row.get("primarySymbol") or row.get("ticker") or row.get("symbol"))
                            for row in master_rows}
                if any(item.symbol != expected[item.instrument_id] for item in evidence):
                    raise ValueError("OFFICIAL_ETF_SYMBOL_CONFLICT")
        except Exception as exc:
            return await self._record_async(None, "DISCOVERY", "NSE", outcome=Outcome.TECHNICAL_FAILURE,
                reason=f"NSE_ETF_DISCOVERY_FAILED:{type(exc).__name__}:{str(exc)[:160]}")
        return await self._record_async(None, "DISCOVERY", "NSE", evidence)

    async def acquire_secondary_quote(self, profile: EtfResearchProfile, metric: EtfMetric, provider, instrument: dict):
        """Reuse Yahoo's proven quote facts, not its undated navPrice mapping."""
        mapping = {EtfMetric.MARKET_PRICE: "latestPrice", EtfMetric.TRADING_VOLUME: "volume",
                   EtfMetric.BID: "bid", EtfMetric.ASK: "ask"}
        if metric not in mapping:
            return await self._record_async(profile.instrument_id, metric, "YAHOO_FINANCE", outcome=Outcome.NOT_IMPLEMENTED,
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
                return await self._record_async(profile.instrument_id, metric, provider.provider_name, reason="VALID_RESPONSE_FIELD_ABSENT")
            if value.as_of_date is None:
                raise ValueError("QUOTE_AS_OF_DATE_MISSING")
            numeric = Decimal(str(value.value))
            if not numeric.is_finite() or numeric < 0 or (metric == EtfMetric.MARKET_PRICE and numeric == 0):
                raise ValueError("INVALID_QUOTE_VALUE")
            if metric == EtfMetric.TRADING_VOLUME and (value.unit != "shares" or numeric != numeric.to_integral_value()):
                raise ValueError("INVALID_VOLUME_UNIT_OR_VALUE")
            if metric != EtfMetric.TRADING_VOLUME and profile.currency and value.unit != profile.currency:
                raise ValueError("QUOTE_CURRENCY_MISMATCH")
            if value.as_of_date.date() > self.clock().date():
                raise ValueError("FUTURE_DATED_QUOTE")
            fact = EtfFact(instrument_id=profile.instrument_id, metric=metric, value=Decimal(str(value.value)),
                unit=value.unit, as_of_date=value.as_of_date.date(), provenance=EtfProvenance(
                    provider=provider.provider_name, source_type="STRUCTURED_MARKET_PROVIDER",
                    source_identity=f"{snapshot.resolution.provider_ticker}:{mapping[metric]}:{value.as_of_date.isoformat()}",
                    source_url=value.source_url, source_locator=mapping[metric], retrieved_at=value.retrieved_at,
                    published_at=value.published_at, authority=EtfAuthority.SECONDARY,
                    confidence=value.confidence, reliability_level="LEVEL_C"))
        except Exception as exc:
            return await self._record_async(profile.instrument_id, metric, "YAHOO_FINANCE", outcome=Outcome.TECHNICAL_FAILURE,
                reason=f"STRUCTURED_ACQUISITION_FAILED:{type(exc).__name__}:{str(exc)[:160]}")
        return await self._record_async(profile.instrument_id, metric, provider.provider_name, [fact])

    def acquire_official_document(self, profile: EtfResearchProfile, document: ResearchDocument, metric: EtfMetric):
        """Acquire one authorized metric; unsupported or unverified inputs fail closed."""
        if metric not in {EtfMetric.EXPENSE_RATIO, EtfMetric.AUM}:
            return self._record(profile.instrument_id, metric, "OFFICIAL_DOCUMENT", outcome=Outcome.NOT_IMPLEMENTED,
                reason="NO_PROVEN_OFFICIAL_FIELD_PARSER")
        from app.etf_document_parser import verified_scheme_section, parse_metric
        try:
            source = (self.document_registry.source_for(profile.instrument_id, document.original_url)
                      if self.document_registry is not None else None)
            section, host, authority, metadata = verified_scheme_section(
                self.persistence, profile, document, source, now=self.clock())
            value, unit, as_of, lines, reason = parse_metric(
                section, metric, source, now=self.clock(), retrieved_at=document.retrieved_at)
            metadata.update(metric_lines=lines, metric_as_of_date=as_of.isoformat() if as_of else None)
            provenance = EtfProvenance(provider="NSE" if authority == EtfAuthority.OFFICIAL_EXCHANGE else host,
                source_type=str(document.source_type),
                source_identity=f"{document.canonical_url}#sha256={source.normalized_text_sha256}",
                source_url=document.canonical_url, source_metadata=metadata,
                source_locator=f"normalized_text:{metadata['section_span']}:{metric}",
                published_at=document.published_at, retrieved_at=document.retrieved_at,
                reliability_level=document.reliability_level, authority=authority)
            if reason == "SOURCE_DECLARATION":
                return self._record(profile.instrument_id, metric, "OFFICIAL_DOCUMENT",
                    outcome=Outcome.GENUINELY_UNAVAILABLE, reason=reason, unavailability_evidence=provenance)
            if value is None:
                return self._record(profile.instrument_id, metric, "OFFICIAL_DOCUMENT", reason=reason)
            fact = EtfFact(instrument_id=profile.instrument_id, metric=metric, value=value, unit=unit,
                as_of_date=as_of, provenance=provenance)
            # Same-period primary-source corrections need an explicit reconciliation
            # workflow. Do not silently choose one by publication/retrieval time.
            if any(old.as_of_date == as_of and old.provenance.authority in {
                    EtfAuthority.OFFICIAL_EXCHANGE, EtfAuthority.OFFICIAL_FUND}
                    and (old.value, old.unit) != (value, unit)
                    for old in self.persistence.etf_facts(profile.instrument_id, metric)):
                raise ValueError("CONFLICTING_OFFICIAL_METRIC_FOR_PERIOD")
        except ValueError as exc:
            return self._record(profile.instrument_id, metric, "OFFICIAL_DOCUMENT", outcome=Outcome.TECHNICAL_FAILURE,
                reason=f"OFFICIAL_DOCUMENT_FAILED:{str(exc)[:160]}")
        return self._record(profile.instrument_id, metric, "OFFICIAL_DOCUMENT", [fact])
