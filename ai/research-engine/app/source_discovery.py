from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Protocol
from urllib.parse import urlparse

import httpx

from app.models import (
    CompanyResearchProfile,
    DocumentSubtype,
    EtfResearchProfile,
    ReliabilityLevel,
    ShareholdingCategory,
    ShareholdingSnapshot,
    ShareholdingSnapshotValue,
    SourceClassification,
    SourceMode,
    SourceType,
)
from app.entity_resolution import _contains_identity
from app.normalization import canonicalize_url
from app.source_registry import RegisteredResearchSource, approved_sources_for_categories
from app.url_security import UnsafeUrlError, validate_public_http_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscoveryResult:
    category: str
    source: RegisteredResearchSource


@dataclass(frozen=True)
class SearchDateWindow:
    months: int = 12
    year: int | None = None
    query_limit: int | None = None
    explicit_queries: tuple[str, ...] | None = None


@dataclass(frozen=True)
class CandidateSearchResult:
    title: str
    url: str
    snippet: str
    discovered_at: datetime
    provider: str
    query_id: str
    query: str
    category: str


@dataclass(frozen=True)
class RejectedSearchCandidate:
    url: str
    reason: str
    category: str
    provider: str


@dataclass
class SearchDiscoveryStats:
    candidate_count: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    categories_attempted: int = 0
    documents_fetched: int = 0
    events_extracted: int = 0
    provider_failure_count: int = 0
    zero_result_query_count: int = 0
    rejected_reasons: dict[str, int] = field(default_factory=dict)

    def reject(self, reason: str) -> None:
        self.rejected_count += 1
        self.rejected_reasons[reason] = self.rejected_reasons.get(reason, 0) + 1


class SearchDiscoveryProvider(Protocol):
    provider_name: str

    async def discover(self, company: CompanyResearchProfile | EtfResearchProfile, category: str, date_window: SearchDateWindow) -> list[CandidateSearchResult]:
        """Return candidate publisher URLs only. Search snippets are never investment evidence."""


class DisabledSearchDiscoveryProvider:
    provider_name = "disabled"

    async def discover(self, company: CompanyResearchProfile | EtfResearchProfile, category: str, date_window: SearchDateWindow) -> list[CandidateSearchResult]:
        return []


class SearchProviderConfigurationError(RuntimeError):
    pass


class SearchProviderError(RuntimeError):
    pass


class BraveCompatibleSearchDiscoveryProvider:
    provider_name = "brave-compatible"

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        *,
        max_results_per_query: int = 5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not endpoint:
            raise SearchProviderConfigurationError("SEARCH_PROVIDER_NOT_CONFIGURED:endpoint")
        if not api_key:
            raise SearchProviderConfigurationError("SEARCH_PROVIDER_NOT_CONFIGURED:api_key")
        self.endpoint = endpoint
        self.api_key = api_key
        self.max_results_per_query = min(max(max_results_per_query, 1), 20)
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0))

    async def discover(self, company: CompanyResearchProfile | EtfResearchProfile, category: str, date_window: SearchDateWindow) -> list[CandidateSearchResult]:
        results: list[CandidateSearchResult] = []
        for query_id, query in enumerate(_bounded_search_queries(company, category, date_window), start=1):
            response = await _safe_search_get(
                self._client,
                self.endpoint,
                params={"q": query, "count": self.max_results_per_query},
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "X-Subscription-Token": self.api_key,
                },
            )
            payload = _safe_json(response)
            web = payload.get("web", {})
            web_results = web.get("results", []) if isinstance(web, dict) else []
            if not isinstance(web_results, list):
                raise SearchProviderError("SEARCH_PROVIDER_UNAVAILABLE:invalid_response")
            for item in web_results[: self.max_results_per_query]:
                if not isinstance(item, dict):
                    raise SearchProviderError("SEARCH_PROVIDER_UNAVAILABLE:invalid_response")
                url = item.get("url")
                if not url:
                    continue
                results.append(
                    CandidateSearchResult(
                        title=str(item.get("title") or ""),
                        url=str(url),
                        snippet=str(item.get("description") or ""),
                        discovered_at=datetime.now(timezone.utc),
                        provider=self.provider_name,
                        query_id=f"{category}:{query_id}",
                        query=query,
                        category=category,
                    )
                )
        return results


class GoogleCompatibleSearchDiscoveryProvider:
    provider_name = "google-compatible"

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        engine_id: str,
        *,
        max_results_per_query: int = 5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not endpoint:
            raise SearchProviderConfigurationError("SEARCH_PROVIDER_NOT_CONFIGURED:endpoint")
        if not api_key:
            raise SearchProviderConfigurationError("SEARCH_PROVIDER_NOT_CONFIGURED:api_key")
        if not engine_id:
            raise SearchProviderConfigurationError("SEARCH_PROVIDER_NOT_CONFIGURED:engine_id")
        self.endpoint = endpoint
        self.api_key = api_key
        self.engine_id = engine_id
        self.max_results_per_query = max_results_per_query
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0))

    async def discover(self, company: CompanyResearchProfile | EtfResearchProfile, category: str, date_window: SearchDateWindow) -> list[CandidateSearchResult]:
        results: list[CandidateSearchResult] = []
        for query_id, query in enumerate(_bounded_search_queries(company, category, date_window), start=1):
            try:
                response = await _safe_search_get(
                    self._client,
                    self.endpoint,
                    params={"q": query, "key": self.api_key, "cx": self.engine_id, "num": self.max_results_per_query},
                )
            except SearchProviderError as exc:
                if str(exc) == "SEARCH_PROVIDER_FORBIDDEN":
                    raise SearchProviderError("GOOGLE_PROVIDER_UNAVAILABLE") from exc
                raise
            payload = _safe_json(response)
            for item in payload.get("items", [])[: self.max_results_per_query]:
                link = item.get("link") or item.get("url")
                if not link:
                    continue
                results.append(
                    CandidateSearchResult(
                        title=str(item.get("title") or ""),
                        url=str(link),
                        snippet=str(item.get("snippet") or ""),
                        discovered_at=datetime.now(timezone.utc),
                        provider=self.provider_name,
                        query_id=f"{category}:{query_id}",
                        query=query,
                        category=category,
                    )
                )
        return results


class SearxngSearchDiscoveryProvider:
    provider_name = "searxng"

    def __init__(
        self,
        endpoint: str,
        *,
        max_results_per_query: int = 5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not endpoint:
            raise SearchProviderConfigurationError("SEARCH_PROVIDER_NOT_CONFIGURED:endpoint")
        self.endpoint = endpoint
        self.max_results_per_query = min(max(max_results_per_query, 1), 20)
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0))

    async def discover(self, company: CompanyResearchProfile | EtfResearchProfile, category: str, date_window: SearchDateWindow) -> list[CandidateSearchResult]:
        results: list[CandidateSearchResult] = []
        failures: list[str] = []
        self.last_query_degraded = False
        for query_id, query in enumerate(_bounded_search_queries(company, category, date_window), start=1):
            try:
                response = await _safe_search_get(
                    self._client,
                    self.endpoint,
                    params={
                        "q": query,
                        "format": "json",
                        "categories": "general",
                        "language": "en-US",
                        "safesearch": 0,
                    },
                )
            except SearchProviderError as exc:
                failures.append(str(exc))
                logger.warning("search_query_failed company=%s category=%s provider=%s query_id=%s reason=%s",
                    _profile_display_name(company), category, self.provider_name, query_id, str(exc))
                continue
            payload = _safe_json(response)
            web_results = payload.get("results", [])
            if not isinstance(web_results, list):
                raise SearchProviderError("SEARCH_PROVIDER_UNAVAILABLE:invalid_response")
            for item in web_results[: self.max_results_per_query]:
                if not isinstance(item, dict):
                    raise SearchProviderError("SEARCH_PROVIDER_UNAVAILABLE:invalid_response")
                url = item.get("url")
                if not url:
                    continue
                results.append(
                    CandidateSearchResult(
                        title=str(item.get("title") or ""),
                        url=str(url),
                        snippet=str(item.get("content") or item.get("snippet") or ""),
                        discovered_at=datetime.now(timezone.utc),
                        provider=self.provider_name,
                        query_id=f"{category}:{query_id}",
                        query=query,
                        category=category,
                    )
                )
            result_engines = sorted({
                str(engine)
                for item in web_results if isinstance(item, dict)
                for engine in ([item.get("engine")] if item.get("engine") else item.get("engines", []))
                if engine
            })
            unresponsive_count = len(payload.get("unresponsive_engines", []))
            self.last_query_degraded = self.last_query_degraded or bool(unresponsive_count)
            # An empty result set from engines that did not answer is not
            # evidence that the company had no matching public information.
            # Let the aggregate service treat this as retryable provider
            # degradation rather than a successful zero-result check.
            if not result_engines and unresponsive_count:
                failures.append(f"SEARCH_PROVIDER_DEGRADED:unresponsive_engines={unresponsive_count}")
                logger.warning(
                    "search_query_degraded company=%s category=%s provider=%s query_id=%s unresponsive_engine_count=%s",
                    _profile_display_name(company), category, self.provider_name, query_id, unresponsive_count,
                )
                continue
            logger.info(
                "search_query_complete company=%s category=%s provider=%s query_id=%s query=%s result_count=%s engines=%s unresponsive_engine_count=%s",
                _profile_display_name(company), category, self.provider_name, query_id, query,
                len(web_results), result_engines, unresponsive_count,
            )
        if failures and not results:
            raise SearchProviderError(f"SEARCH_PROVIDER_UNAVAILABLE:{failures[-1]}")
        return results


class ApprovedSourceDiscovery:
    def discover(
        self,
        profile: CompanyResearchProfile,
        missing_categories: set[str],
        already_seen_urls: set[str],
    ) -> list[DiscoveryResult]:
        results: list[DiscoveryResult] = []
        for source in approved_sources_for_categories(profile.instrument_id, missing_categories):
            if canonicalize_url(source.url) in already_seen_urls:
                continue
            validate_public_http_url(source.url)
            host = source.host
            if not host or not any(host == domain.lower() or host.endswith(f".{domain.lower()}") for domain in profile.known_domains):
                continue
            for category in sorted(set(source.categories) & missing_categories):
                results.append(DiscoveryResult(category=category, source=source))
        return results


class OfficialFilingDiscovery:
    """Exchange-first public filing discovery; search remains a fallback."""
    NSE_ANNOUNCEMENTS_URL = "https://www.nseindia.com/api/corporate-announcements"

    def __init__(self, client: httpx.AsyncClient | None = None, announcements_url: str | None = None) -> None:
        self.announcements_url = announcements_url or self.NSE_ANNOUNCEMENTS_URL
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=4.0), headers={
            "User-Agent": "Mozilla/5.0 (compatible; AIInvestmentResearch/1.0)",
            "Accept": "application/json", "Referer": "https://www.nseindia.com/",
        })

    async def discover(self, profile: CompanyResearchProfile, categories: set[str], seen_urls: set[str]) -> list[DiscoveryResult]:
        requested = {category for category in categories if category in {"FINANCIAL_RESULTS", "SHAREHOLDING_PATTERN"}}
        if not _is_indian_nse_profile(profile) or not categories:
            logger.info("official_discovery_result provider=NSE globalInstrumentId=%s status=SKIPPED exchange=%s country=%s reason=INELIGIBLE_PROFILE_OR_CATEGORY", profile.instrument_id, profile.exchange, profile.country)
            return []
        # Profile mappings are hydrated from the global master with VERIFIED/
        # RESOLVED status only. Never turn a portfolio or broker display ticker
        # into exchange identity here.
        symbol = profile.provider_instrument_ids.get("NSE")
        if not symbol:
            logger.info("shareholding_identity_resolved provider=NSE globalInstrumentId=%s outcome=SKIPPED reason=TRUSTED_NSE_MAPPING_UNAVAILABLE", profile.instrument_id)
            return []
        symbol_source = "VERIFIED_NSE_MAPPING"
        logger.info(
            "official_discovery_start provider=NSE globalInstrumentId=%s exchange=%s country=%s "
            "mappingAvailable=%s symbolSource=%s symbol=%r",
            profile.instrument_id,
            profile.exchange,
            profile.country,
            bool(profile.provider_instrument_ids.get("NSE")),
            symbol_source,
            symbol,
        )
        try:
            response = await self.client.get(self.announcements_url, params={"index": "equities", "symbol": symbol})
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise SearchProviderError("NSE_OFFICIAL_INVALID_RESPONSE")
            candidates: list[tuple[int, datetime, DiscoveryResult]] = []
            candidate_urls = set(seen_urls)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                title = " ".join(str(row.get(key) or "") for key in ("desc", "attchmntText"))
                url = str(row.get("attchmntFile") or "")
                subtype = classify_nse_document_subtype(
                    desc=row.get("desc"),
                    attachment_text=row.get("attchmntText"),
                    attachment_file=row.get("attchmntFile"),
                )
                category = "FINANCIAL_RESULTS" if _is_financial_result_announcement(title) and "FINANCIAL_RESULTS" in requested else (
                    "SHAREHOLDING_PATTERN" if _is_shareholding_announcement(title) and "SHAREHOLDING_PATTERN" in requested else None
                )
                # Preserve required filing selection and add only attachments
                # whose *pre-download NSE metadata* carries a high-confidence
                # subtype.  Generic announcements remain excluded.
                if category is None and subtype is not None:
                    category = subtype.value
                if not url or category is None:
                    continue
                try:
                    canonical = canonicalize_url(url)
                    validate_public_http_url(canonical)
                except ValueError as exc:
                    logger.warning(
                        "official_candidate_rejected provider=NSE globalInstrumentId=%s reason=%s host=%s",
                        profile.instrument_id,
                        "UNSAFE_URL" if isinstance(exc, UnsafeUrlError) else "MALFORMED_URL",
                        _safe_attachment_host(url),
                    )
                    continue
                if canonical in candidate_urls:
                    continue
                candidate_urls.add(canonical)
                published = _nse_datetime(row.get("an_dt"))
                source = RegisteredResearchSource(
                    source_id=f"nse:{category.lower()}:{profile.instrument_id}:{content_hash_key(canonical)}",
                    instrument_id=profile.instrument_id, url=canonical, source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
                    source_classification=SourceClassification.EXCHANGE, source_name="NSE corporate announcements",
                    publisher="NSE", reliability_level=ReliabilityLevel.LEVEL_A, domain=urlparse(canonical).hostname or "",
                    company_id=profile.company_id, allowed=True, discovery_method="NSE_OFFICIAL_API",
                    priority=_nse_discovery_priority(subtype, category), categories=(category,),
                    document_subtype=subtype, official_nse_profile_symbol=symbol,
                )
                candidates.append((_nse_discovery_priority(subtype, category), published, DiscoveryResult(category=category, source=source)))
            accepted = [item for _, _, item in sorted(candidates, key=lambda value: (value[0], -value[1].timestamp()))]
            logger.info("official_filing_discovery provider=NSE globalInstrumentId=%s status=%s candidateCount=%s acceptedCount=%s reason=%s", profile.instrument_id, "SUCCESS" if accepted else "ZERO_CANDIDATES", len(rows), len(accepted), "NONE" if accepted else "NO_QUALIFYING_ATTACHMENT")
            return accepted
        except Exception as exc:
            logger.warning(
                "official_discovery_result provider=NSE globalInstrumentId=%s status=FAILED candidateCount=0 acceptedCount=0 reason=%s",
                profile.instrument_id,
                type(exc).__name__,
            )
            raise


class OfficialNseShareholdingDiscovery:
    """Read NSE's dedicated quarterly shareholding-pattern feed.

    This is intentionally separate from corporate announcements: NSE publishes
    Regulation 31 shareholding data as a dedicated corporate-filings product,
    with an official XBRL artifact per reported period.
    """

    NSE_SHAREHOLDINGS_URL = "https://www.nseindia.com/api/corporate-share-holdings-master"

    def __init__(self, client: httpx.AsyncClient | None = None, shareholdings_url: str | None = None) -> None:
        self.shareholdings_url = shareholdings_url or self.NSE_SHAREHOLDINGS_URL
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=4.0), headers={
            "User-Agent": "Mozilla/5.0 (compatible; AIInvestmentResearch/1.0)",
            "Accept": "application/json", "Referer": "https://www.nseindia.com/",
        })

    async def discover(self, profile: CompanyResearchProfile) -> list[ShareholdingSnapshot]:
        if not _is_indian_nse_profile(profile):
            logger.info("shareholding_identity_resolved provider=NSE globalInstrumentId=%s outcome=SKIPPED reason=INELIGIBLE_PROFILE", profile.instrument_id)
            return []
        # This mapping is hydrated only after the global-provider mapping
        # layer has excluded broker-import identities.  There is deliberately
        # no profile-ticker or portfolio-alias fallback here.
        symbol = profile.provider_instrument_ids.get("NSE")
        if not symbol:
            logger.info("shareholding_identity_resolved provider=NSE globalInstrumentId=%s outcome=SKIPPED reason=TRUSTED_NSE_MAPPING_UNAVAILABLE", profile.instrument_id)
            return []

        logger.info("shareholding_official_discovery provider=NSE globalInstrumentId=%s symbol=%r outcome=START", profile.instrument_id, symbol)
        try:
            response = await self.client.get(self.shareholdings_url, params={"index": "equities", "symbol": symbol})
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise SearchProviderError("NSE_SHAREHOLDING_OFFICIAL_INVALID_RESPONSE")
        except httpx.HTTPError as exc:
            logger.warning("shareholding_official_discovery provider=NSE globalInstrumentId=%s outcome=UNAVAILABLE reason=%s", profile.instrument_id, type(exc).__name__)
            raise SearchProviderError("NSE_SHAREHOLDING_OFFICIAL_UNAVAILABLE") from exc

        candidates = [snapshot for row in rows if isinstance(row, dict)
                      if (snapshot := _nse_shareholding_snapshot(profile, symbol, row)) is not None]
        candidates.sort(key=lambda snapshot: (snapshot.period_end, snapshot.published_at or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
        # Keep the latest four distinct reporting periods, but retain every
        # official record selected for those periods. A revised filing has its
        # own recordId and must remain durably distinguishable from the filing
        # it supersedes; the read model selects the latest publication.
        periods: set[datetime] = set()
        for candidate in candidates:
            periods.add(candidate.period_end)
            if len(periods) == 4:
                break
        snapshots = [candidate for candidate in candidates if candidate.period_end in periods]
        logger.info("shareholding_official_discovery provider=NSE globalInstrumentId=%s outcome=%s rowCount=%s snapshotCount=%s", profile.instrument_id, "SUCCESS" if snapshots else "ZERO_RESULTS", len(rows), len(snapshots))
        return snapshots


def _is_indian_nse_profile(profile: CompanyResearchProfile) -> bool:
    return profile.country.upper() in {"IN", "IND", "INDIA"} and profile.exchange.upper() in {"NSE", "XNSE"}


def _is_financial_result_announcement(value: str) -> bool:
    lowered = value.lower()
    return any(term in lowered for term in ("financial results", "financial result", "unaudited financial", "audited financial", "results for the period ended"))


def _is_shareholding_announcement(value: str) -> bool:
    # NSE announcements carry many ordinary ownership references.  Only the
    # explicit quarterly filing name is a SHAREHOLDING_PATTERN artifact.
    normalized = re.sub(r"\s+", " ", value.casefold()).strip()
    return bool(re.search(r"\bshare(?:holding|\s+holder)\s+pattern\b", normalized))


def classify_nse_document_subtype(
    *,
    desc: object = None,
    attachment_text: object = None,
    attachment_file: object = None,
) -> DocumentSubtype | None:
    """Classify only explicit, pre-download NSE announcement metadata.

    A subtype is an optional selection hint, never a substitute for document
    identity or post-download evidence extraction.  The deliberately narrow
    rules fail closed for notices and isolated keywords.
    """
    normalized = _nse_metadata_text(desc, attachment_text, attachment_file)
    if not normalized:
        return None
    # Single deterministic precedence for metadata containing more than one
    # signal.  A substantive transcript/presentation outranks a notice.
    if _has_any_phrase(normalized, (
        "conference call transcript", "earnings call transcript", "analyst call transcript",
        "concall transcript", "conference call presentation", "earnings call presentation",
    )):
        return DocumentSubtype.CONFERENCE_CALL_MATERIAL
    if _has_any_phrase(normalized, ("agm presentation", "egm presentation", "shareholder meeting presentation")):
        return None
    if _has_any_phrase(normalized, (
        "investor presentation", "investors presentation", "corporate presentation",
        "earnings presentation", "results presentation", "analyst presentation",
    )):
        return DocumentSubtype.INVESTOR_PRESENTATION
    if _has_any_phrase(normalized, ("statutory notice", "compliance notice", "compliance certificate", "trading window")):
        return None
    if _has_any_phrase(normalized, ("investor release", "press release", "media release", "earnings release", "business update")):
        return DocumentSubtype.INVESTOR_RELEASE
    if _has_any_phrase(normalized, (
        "receipt of order", "receipt of orders", "received an order", "order received",
        "order win", "letter of award", "work order", "contract awarded", "award of contract",
    )):
        return DocumentSubtype.ORDER_CONTRACT_DISCLOSURE
    if _has_any_phrase(normalized, (
        "capacity expansion", "commissioning of", "new manufacturing facility", "new plant",
        "greenfield expansion", "brownfield expansion", "expansion project",
    )):
        return DocumentSubtype.CAPEX_CAPACITY_DISCLOSURE
    return None


def _nse_metadata_text(*values: object) -> str:
    joined = " ".join(str(value or "") for value in values)
    joined = re.sub(r"[_./\\-]+", " ", joined.casefold())
    return re.sub(r"\s+", " ", joined).strip()


def _has_any_phrase(value: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in value for phrase in phrases)


def _nse_discovery_priority(subtype: DocumentSubtype | None, category: str) -> int:
    return {
        DocumentSubtype.CONFERENCE_CALL_MATERIAL: 0,
        DocumentSubtype.INVESTOR_PRESENTATION: 1,
        DocumentSubtype.INVESTOR_RELEASE: 2,
        DocumentSubtype.ORDER_CONTRACT_DISCLOSURE: 3,
        DocumentSubtype.CAPEX_CAPACITY_DISCLOSURE: 4,
    }.get(subtype, 5 if category == "FINANCIAL_RESULTS" else 6)


def _nse_datetime(value: object) -> datetime:
    try:
        return datetime.strptime(str(value), "%d-%b-%Y %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _nse_shareholding_snapshot(
    profile: CompanyResearchProfile,
    trusted_symbol: str,
    row: dict[object, object],
) -> ShareholdingSnapshot | None:
    # A server response must still identify the requested trusted NSE symbol.
    # The verified mapping, not a broker alias or a fuzzy company-name match,
    # is the identity proof for this dedicated NSE feed.
    if str(row.get("symbol") or "").strip().upper() != trusted_symbol.strip().upper():
        return None
    # NSE calls this field "As on Date" in the dedicated Shareholding Pattern
    # table. This quarterly feature must not treat an arbitrary special-event
    # as-on date as a Regulation 31 quarter. The feed exposes no separate
    # reporting-period field for such rows, so do not round or infer one.
    period_end = _nse_shareholding_date(row.get("date"))
    if period_end is not None and not _is_nse_quarter_end(period_end):
        logger.info(
            "shareholding_official_discovery provider=NSE globalInstrumentId=%s outcome=REJECTED reason=NON_QUARTER_REPORTING_DATE recordId=%s asOnDate=%s",
            profile.instrument_id,
            str(row.get("recordId") or "NONE"),
            period_end.date().isoformat(),
        )
        return None
    xbrl_url = str(row.get("xbrl") or "").strip()
    if period_end is None or not xbrl_url:
        return None
    try:
        canonical_url = canonicalize_url(xbrl_url)
        validate_public_http_url(canonical_url)
    except ValueError:
        return None
    values = _nse_shareholding_values(row)
    if not values:
        return None
    identity = str(row.get("recordId") or "").strip() or canonical_url
    published_at = _nse_shareholding_date(row.get("submissionDate")) or _nse_datetime(row.get("broadcastDate"))
    return ShareholdingSnapshot(
        instrument_id=profile.instrument_id,
        period_end=period_end,
        filing_basis=str(row.get("typeOfSubmission") or "").strip() or None,
        source_provider="NSE",
        source_type="NSE_SHAREHOLDING_XBRL",
        source_identity_key=f"NSE_SHAREHOLDING:{identity}",
        source_url=canonical_url,
        published_at=published_at,
        confidence=Decimal("0.95"),
        reliability_level=ReliabilityLevel.LEVEL_A,
        source_mode=SourceMode.REAL,
        values=values,
    )


def _nse_shareholding_values(row: dict[object, object]) -> list[ShareholdingSnapshotValue]:
    # Only the promoter-and-promoter-group field has a matching durable
    # ownership category. NSE's aggregate public shareholding includes several
    # possible institutional and non-retail classes, so it must not be coerced
    # into PUBLIC_RETAIL (or any other detailed category).
    fields = (
        ("pr_and_prgrp", "Promoter and Promoter Group", ShareholdingCategory.PROMOTER),
    )
    values: list[ShareholdingSnapshotValue] = []
    for field, label, category in fields:
        percentage = _nse_percentage(row.get(field))
        if percentage is None:
            continue
        values.append(ShareholdingSnapshotValue(
            category=category,
            percentage=percentage,
            raw_source_label=label,
            source_locator=f"nse-shareholdings-master:{field}",
            evidence_text=f"{label}: {percentage}%",
        ))
    return values


def _nse_percentage(value: object) -> Decimal | None:
    try:
        percentage = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return percentage if Decimal("0") <= percentage <= Decimal("100") else None


def _nse_shareholding_date(value: object) -> datetime | None:
    raw = str(value or "").strip()
    for fmt in ("%d-%b-%Y", "%d-%b-%Y %H:%M:%S", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _is_nse_quarter_end(value: datetime) -> bool:
    return (value.month, value.day) in {(3, 31), (6, 30), (9, 30), (12, 31)}


def content_hash_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())[-32:]


def _safe_attachment_host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


class SearchDiscoveryService:
    def __init__(
        self,
        provider: SearchDiscoveryProvider,
        *,
        max_queries_per_category: int = 6,
        max_results_per_query: int = 5,
        max_documents_per_refresh: int = 6,
        allowed_domains: list[str] | None = None,
    ) -> None:
        self.provider = provider
        self.max_queries_per_category = max_queries_per_category
        self.max_results_per_query = max_results_per_query
        self.max_documents_per_refresh = max_documents_per_refresh
        self.allowed_domains = {domain.lower() for domain in (allowed_domains or []) if domain}
        self.last_stats = SearchDiscoveryStats()
        self.rejected_candidates: list[RejectedSearchCandidate] = []

    async def discover(self, profile: CompanyResearchProfile | EtfResearchProfile, missing_categories: set[str], already_seen_urls: set[str]) -> list[DiscoveryResult]:
        stats = SearchDiscoveryStats()
        rejected: list[RejectedSearchCandidate] = []
        accepted: list[DiscoveryResult] = []
        seen = set(already_seen_urls)
        window = SearchDateWindow(year=datetime.now(timezone.utc).year, query_limit=self.max_queries_per_category)
        per_category_limit = max(1, self.max_documents_per_refresh // max(len(missing_categories), 1))
        provider_errors: list[str] = []
        for category in sorted(missing_categories):
            accepted_for_category = 0
            stats.categories_attempted += 1
            try:
                candidates = await self.provider.discover(profile, category, window)
            except SearchProviderError as exc:
                stats.provider_failure_count += 1
                if not provider_errors:
                    stats.reject(str(exc))
                provider_errors.append(str(exc))
                logger.warning("search_category_failed company=%s category=%s provider=%s reason=%s",
                    _profile_display_name(profile), category, self.provider.provider_name, str(exc))
                continue
            if not candidates:
                stats.zero_result_query_count += 1
            # Search ranking is not filing selection.  Prefer an actual result
            # document from an exchange/company over a generic IR landing page,
            # then let the report parser choose the latest fiscal period.
            ranked_candidates = sorted(
                candidates[: self.max_queries_per_category * self.max_results_per_query],
                key=lambda candidate: _candidate_rank(profile, candidate), reverse=True,
            )
            for candidate in ranked_candidates:
                stats.candidate_count += 1
                try:
                    source = _candidate_to_source(self, profile, candidate)
                except ValueError as exc:
                    reason = _discovery_rejection_reason(str(exc))
                    stats.reject(reason)
                    rejected.append(RejectedSearchCandidate(candidate.url, reason, category, candidate.provider))
                    continue
                canonical = canonicalize_url(source.url)
                if canonical in seen:
                    stats.reject("DUPLICATE")
                    rejected.append(RejectedSearchCandidate(candidate.url, "DUPLICATE", category, candidate.provider))
                    continue
                seen.add(canonical)
                accepted.append(DiscoveryResult(category=category, source=source))
                stats.accepted_count += 1
                accepted_for_category += 1
                if len(accepted) >= self.max_documents_per_refresh:
                    break
                if accepted_for_category >= per_category_limit:
                    break
            if len(accepted) >= self.max_documents_per_refresh:
                break
        accepted.sort(key=lambda result: _source_rank(result.source.source_classification))
        self.last_stats = stats
        self.rejected_candidates = rejected
        logger.info(
            "search_discovery_complete company=%s provider=%s categories=%s candidate_count=%s accepted_count=%s rejected_count=%s provider_failures=%s zero_result_categories=%s rejected_reasons=%s",
            _profile_display_name(profile),
            self.provider.provider_name,
            stats.categories_attempted,
            stats.candidate_count,
            stats.accepted_count,
            stats.rejected_count,
            stats.provider_failure_count,
            stats.zero_result_query_count,
            stats.rejected_reasons,
        )
        if not accepted and provider_errors and stats.provider_failure_count == stats.categories_attempted:
            raise SearchProviderError(f"SEARCH_PROVIDER_UNAVAILABLE:{provider_errors[-1]}")
        return accepted


def _candidate_rank(profile: CompanyResearchProfile | EtfResearchProfile, candidate: CandidateSearchResult) -> tuple[int, int, int, datetime]:
    haystack = f"{candidate.title} {candidate.snippet} {candidate.url}".lower()
    host = (urlparse(candidate.url).hostname or "").lower()
    official = any(host == domain.lower() or host.endswith(f".{domain.lower()}") for domain in profile.known_domains)
    exchange = any(token in host for token in ("nseindia", "bseindia", "euronext", "deutsche-boerse", "londonstockexchange", "sec.gov"))
    filing_terms = ("quarterly result", "financial result", "unaudited result", "audited result", "earnings release", "results for quarter", "quarter ended", "financial statement")
    ownership_terms = ("shareholding pattern", "shareholder pattern", "promoter", "fii", "fpi", "dii")
    is_pdf = ".pdf" in candidate.url.lower() or " pdf" in haystack
    relevant = any(term in haystack for term in filing_terms if candidate.category == "FINANCIAL_RESULTS") or any(
        term in haystack for term in ownership_terms if candidate.category in {"Ownership", "INSTITUTIONAL_ACTIVITY"}
    )
    return (3 if exchange else 2 if official else 0, int(relevant), int(is_pdf), candidate.discovered_at)


def _candidate_to_source(service: SearchDiscoveryService, profile: CompanyResearchProfile | EtfResearchProfile, candidate: CandidateSearchResult) -> RegisteredResearchSource:
    try:
        validate_public_http_url(candidate.url)
    except ValueError as exc:
        raise ValueError("DOMAIN_VALIDATION_FAILED") from exc
    canonical = canonicalize_url(candidate.url)
    host = (urlparse(canonical).hostname or "").lower()
    if service.allowed_domains and not any(host == domain or host.endswith(f".{domain}") for domain in service.allowed_domains):
        raise ValueError("DOMAIN_VALIDATION_FAILED")
    classification = classify_source(host, profile, candidate)
    if isinstance(profile, CompanyResearchProfile) and _candidate_issuer_relevance(profile, candidate, host) == "NEGATIVE":
        raise ValueError("COMPANY_RELEVANCE_FAILED")
    reliability = reliability_for_classification(classification)
    source_type = source_type_for_classification(classification)
    priority = 2 if classification in {SourceClassification.OFFICIAL_COMPANY, SourceClassification.REGULATORY, SourceClassification.EXCHANGE} else 4
    if classification == SourceClassification.OFFICIAL_COMPANY and host not in {domain.lower() for domain in profile.known_domains}:
        profile.known_domains.append(host)
    return RegisteredResearchSource(
        source_id=f"search:{candidate.provider}:{candidate.query_id}:{host}", instrument_id=profile.instrument_id,
        url=canonical, source_type=source_type, source_classification=classification,
        source_name=f"{candidate.provider} discovered publisher page", publisher=publisher_from_host(host),
        reliability_level=reliability, domain=host, company_id=getattr(profile, "company_id", getattr(profile, "fund_id", None)),
        allowed=True, discovery_method="SEARCH_DISCOVERY", priority=priority, categories=(candidate.category,),
    )


def _candidate_issuer_relevance(profile: CompanyResearchProfile, candidate: CandidateSearchResult, host: str) -> str:
    """Return positive, neutral, or contradictory issuer evidence from search metadata.

    Search metadata is not document evidence.  It is only used to stop a
    clearly named different issuer from being registered under this profile.
    Exchange and regulatory hosts are deliberately neutral: they publish for
    many issuers and therefore cannot establish company relevance by domain.
    """
    if any(host == domain.lower() or host.endswith(f".{domain.lower()}") for domain in profile.known_domains):
        return "POSITIVE"

    evidence = f"{candidate.title} {candidate.snippet} {candidate.url}"
    if _candidate_has_positive_issuer_evidence(profile, evidence):
        return "POSITIVE"
    if _candidate_names_other_issuer(evidence):
        return "NEGATIVE"
    return "NEUTRAL"


def _candidate_has_positive_issuer_evidence(profile: CompanyResearchProfile, evidence: str) -> bool:
    normalized_isin = _normalized_issuer_token(profile.isin)
    normalized_evidence = _normalized_issuer_token(evidence)
    if normalized_isin and normalized_isin in normalized_evidence:
        return True

    identities = [profile.company_name, *profile.aliases, *profile.provider_instrument_ids.values()]
    return any(
        identity and len(identity.strip()) >= 4 and _contains_identity(evidence.casefold(), identity.casefold())
        for identity in identities
    )


def _candidate_names_other_issuer(evidence: str) -> bool:
    """Recognize an explicitly named corporate issuer without guessing one."""
    return re.search(
        r"\b[A-Z][A-Z&.' -]{3,}?\s+(?:LIMITED|LTD\.?|INC\.?|CORPORATION|CORP\.?|PLC|P\.L\.C\.)\b",
        evidence,
        flags=re.IGNORECASE,
    ) is not None


def _normalized_issuer_token(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def generate_search_queries(profile: CompanyResearchProfile | EtfResearchProfile, category: str, date_window: SearchDateWindow) -> list[str]:
    if isinstance(profile, EtfResearchProfile):
        return generate_etf_search_queries(profile, category, date_window)
    year = date_window.year or datetime.now(timezone.utc).year
    names = [profile.company_name, *profile.aliases, profile.ticker]
    identities = _dedupe_identity_terms(names)[:3]
    broad_patterns = [
        "investor relations",
        "annual report",
        "quarterly results",
        "earnings",
        "guidance",
        "orders backlog",
        "contracts",
        "capex",
        "acquisition",
        "clients",
        "analyst rating",
        "target price",
        "institutional ownership",
        "valuation",
        "news",
    ]
    category_patterns = {
        "FINANCIAL_RESULTS": ["financial results", "quarterly results", "earnings", "annual report"],
        "GUIDANCE": ["guidance", "outlook", "guidance raised", "guidance cut"],
        "ORDERS_BACKLOG": ["order intake", "order backlog", "orders", "backlog"],
        "CONTRACTS": ["contracts", "contract award", "framework agreement"],
        "CAPEX": ["capex", "capital expenditure", "investment"],
        "NEW_FACILITIES": ["new facility", "new plant", "capacity expansion", "factory investment"],
        "ACQUISITIONS": ["acquisition", "merger", "divestment"],
        "CLIENTS": ["clients", "customers", "customer contract", "customer win"],
        "PRODUCTS": ["products", "product launch"],
        "MANAGEMENT": ["management", "executive board", "CEO", "CFO"],
        "ANALYST_OPINION": ["analyst rating", "analyst opinion", "broker rating"],
        "ANALYST_TARGETS": ["analyst target price", "target price", "price target"],
        "INSTITUTIONAL_ACTIVITY": ["institutional ownership", "shareholder structure", "major shareholders"],
        "VALUATION": ["valuation", "multiples", "market capitalization"],
        "RISKS": ["risks", "risk factors"],
        "CATALYSTS": ["catalysts", "news", "contracts", "guidance"],
        "Customers": [
            "new customer",
            "customer order",
            "customer win",
            "customer contract",
            "customer qualification",
            "strategic customer",
            "customer loss",
        ],
        "Growth": ["earnings results", "revenue growth", "profit margins", "order growth", "market expansion", "product ramp"],
        "Orders & Backlog": ["order wins", "order intake", "contract", "backlog", "order cancellation"],
        "CAPEX & Capacity": ["CAPEX", "new plant investment", "capacity expansion", "factory investment"],
        "Guidance": ["management guidance", "guidance", "outlook", "guidance raised", "guidance cut"],
        "Ownership": ["institutional ownership", "FII DII ownership", "shareholding changes"],
        "Analyst": ["analyst target", "analyst ratings"],
        "M&A": ["acquisition", "merger", "divestment"],
        "Regulatory": ["regulatory announcement", "stock exchange announcement"],
    }
    patterns = category_patterns.get(category, [category])
    mapped_category = category in category_patterns
    queries: list[str] = []
    indian_market = profile.country.upper() in {"IN", "IND", "INDIA"} or profile.exchange.upper() in {
        "NSE", "BSE", "XNSE", "XBOM"
    }
    if indian_market:
        nse_symbol = profile.provider_instrument_ids.get("NSE") or profile.ticker
        bse_symbol = profile.provider_instrument_ids.get("BSE") or profile.provider_instrument_ids.get("NSE") or profile.ticker
        india_patterns = {
            "FINANCIAL_RESULTS": ["quarterly financial results", "corporate financial results"],
            "Ownership": ["shareholding pattern", "promoter FII DII holding", "promoter pledge"],
            "INSTITUTIONAL_ACTIVITY": ["shareholding pattern", "promoter FII DII holding"],
            "ORDERS_BACKLOG": ["order win corporate announcement", "contract award"],
            "CAPEX": ["capex new plant capacity expansion"],
            "NEW_FACILITIES": ["new plant capacity expansion"],
            "Regulatory": ["corporate announcement"],
        }.get(category, patterns)
        primary_pattern = india_patterns[0]
        queries.extend([
            f'"{profile.company_name}" {primary_pattern} site:nseindia.com',
            f'"{profile.company_name}" {primary_pattern} site:bseindia.com',
            f'"{profile.company_name}" {primary_pattern}',
        ])
        if profile.isin:
            queries.append(f'"{profile.isin}"')
        queries.extend([f'"{nse_symbol}" NSE', f'"{bse_symbol}" BSE'])
        for pattern in india_patterns[1:]:
            queries.extend([
                f'"{profile.company_name}" {pattern} site:nseindia.com',
                f'"{profile.company_name}" {pattern} site:bseindia.com',
                f'"{profile.company_name}" {pattern}',
            ])
    if not mapped_category:
        return [f"{identity} {category} {year}" for identity in identities]
    queries.extend(f"{profile.company_name} {pattern}" for pattern in broad_patterns)
    queries.extend(f"{profile.company_name} {pattern} {profile.ticker}" for pattern in broad_patterns if profile.ticker)
    for identity in identities:
        for pattern in patterns:
            queries.append(f"{identity} {pattern} {year}")
            if mapped_category:
                queries.append(f"{identity} {pattern}")
    if profile.company_name and profile.ticker and category in {"Analyst", "ANALYST_OPINION", "ANALYST_TARGETS"}:
        ticker_patterns = ["analyst", "target price", "earnings", "investor relations"]
        for pattern in ticker_patterns:
            queries.append(f"{profile.company_name} {profile.ticker} {pattern}")
    return queries


def generate_etf_search_queries(profile: EtfResearchProfile, category: str, date_window: SearchDateWindow) -> list[str]:
    name = profile.fund_name
    index = profile.underlying_index or _infer_underlying_index(name)
    category_patterns = {
        "ETF_PROFILE": ["factsheet", "holdings", "expense ratio", "AUM", "distribution policy", "NAV"],
        "ETF_PERFORMANCE": ["performance", "tracking difference", "premium discount", "dividend yield"],
        "INDEX_OUTLOOK": ["outlook", "valuation", "earnings outlook", "analyst outlook", "macro environment"],
        "ETF_RISK": ["concentration risk", "sector concentration", "drawdown", "rate sensitivity", "liquidity"],
    }
    patterns = category_patterns.get(category, [category])
    queries = [f"{name} {pattern}" for pattern in patterns]
    if profile.ticker:
        queries.extend(f"{name} {profile.ticker} {pattern}" for pattern in patterns)
    if index:
        queries.extend(
            [
                f"{index} outlook",
                f"{index} valuation",
                f"{index} earnings outlook",
                f"{index} analyst outlook",
            ]
        )
    return _dedupe_identity_terms(queries)


def _bounded_search_queries(profile: CompanyResearchProfile, category: str, date_window: SearchDateWindow) -> list[str]:
    queries = list(date_window.explicit_queries[:10]) if date_window.explicit_queries is not None else generate_search_queries(profile, category, date_window)
    if date_window.query_limit is None:
        return queries
    return queries[: max(date_window.query_limit, 0)]


def classify_source(host: str, profile: CompanyResearchProfile | EtfResearchProfile, candidate: CandidateSearchResult | None = None) -> SourceClassification:
    if any(host == domain.lower() or host.endswith(f".{domain.lower()}") for domain in profile.known_domains):
        return SourceClassification.OFFICIAL_COMPANY
    if candidate and _candidate_matches_company_domain(host, profile, candidate):
        return SourceClassification.OFFICIAL_COMPANY
    if host in {
        "sec.gov",
        "www.sec.gov",
        "sebi.gov.in",
        "www.sebi.gov.in",
        "afm.nl",
        "www.afm.nl",
        "bundesanzeiger.de",
        "www.bundesanzeiger.de",
    }:
        return SourceClassification.REGULATORY
    if (
        host.endswith("deutsche-boerse.com")
        or host.endswith("deutsche-boerse-cash-market.com")
        or host.endswith("euronext.com")
        or host.endswith("nseindia.com")
        or host.endswith("bseindia.com")
        or host.endswith("lse.co.uk")
        or host.endswith("nasdaq.com")
    ):
        return SourceClassification.EXCHANGE
    if host.endswith("infineon.com") or host.endswith("onsemi.com") or host.endswith("stmicroelectronics.com"):
        return SourceClassification.CUSTOMER
    if (
        host.endswith("eqs-news.com")
        or host.endswith("reuters.com")
        or host.endswith("bloomberg.com")
        or host.endswith("finance.yahoo.com")
        or host.endswith("ft.com")
        or host.endswith("wsj.com")
        or host.endswith("cnbc.com")
        or host.endswith("marketwatch.com")
    ):
        return SourceClassification.REPUTABLE_NEWS
    if (
        host.endswith("marketscreener.com")
        or host.endswith("moneycontrol.com")
        or host.endswith("trendlyne.com")
        or host.endswith("screener.in")
        or host.endswith("morningstar.com")
        or host.endswith("investing.com")
    ):
        return SourceClassification.INVESTMENT_RESEARCH
    return SourceClassification.OTHER


def _candidate_matches_company_domain(host: str, profile: CompanyResearchProfile | EtfResearchProfile, candidate: CandidateSearchResult) -> bool:
    domain_label = _registrable_label(host)
    display_name = _profile_display_name(profile)
    company_tokens = _company_domain_tokens(display_name)
    if isinstance(profile, EtfResearchProfile) and profile.fund_provider:
        company_tokens |= _company_domain_tokens(profile.fund_provider)
    if not domain_label or not company_tokens:
        return False
    if domain_label not in company_tokens and not any(token in domain_label or domain_label in token for token in company_tokens):
        return False
    evidence = f"{candidate.title} {candidate.snippet} {candidate.query}".lower()
    company_name = display_name.lower()
    if company_name in evidence:
        return True
    matched_tokens = [token for token in company_tokens if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", evidence)]
    official_terms = ["investor", "annual report", "quarterly", "results", "earnings", "guidance", "press", "news", "factsheet", "holdings", "fund"]
    return bool(matched_tokens) and any(term in evidence for term in official_terms)


def _company_domain_tokens(company_name: str) -> set[str]:
    legal_suffixes = {
        "ag",
        "asa",
        "corp",
        "corporation",
        "gmbh",
        "group",
        "holding",
        "holdings",
        "inc",
        "limited",
        "ltd",
        "nv",
        "n.v",
        "plc",
        "sa",
        "se",
        "the",
    }
    tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", company_name.lower())
        if len(token) >= 4 and token not in legal_suffixes
    ]
    compact = "".join(tokens)
    result = set(tokens)
    if len(compact) >= 4:
        result.add(compact)
    return result


def _registrable_label(host: str) -> str:
    parts = [part for part in host.lower().split(".") if part and part != "www"]
    if len(parts) < 2:
        return parts[0] if parts else ""
    return parts[-2]


def _source_rank(classification: SourceClassification) -> int:
    return {
        SourceClassification.OFFICIAL_COMPANY: 1,
        SourceClassification.REGULATORY: 2,
        SourceClassification.EXCHANGE: 3,
        SourceClassification.INVESTMENT_RESEARCH: 5,
        SourceClassification.REPUTABLE_NEWS: 6,
        SourceClassification.CUSTOMER: 8,
        SourceClassification.PARTNER: 8,
        SourceClassification.SUPPLIER: 8,
        SourceClassification.OTHER: 9,
    }[classification]


def reliability_for_classification(classification: SourceClassification) -> ReliabilityLevel:
    return {
        SourceClassification.OFFICIAL_COMPANY: ReliabilityLevel.LEVEL_B,
        SourceClassification.REGULATORY: ReliabilityLevel.LEVEL_A,
        SourceClassification.EXCHANGE: ReliabilityLevel.LEVEL_A,
        SourceClassification.CUSTOMER: ReliabilityLevel.LEVEL_B,
        SourceClassification.PARTNER: ReliabilityLevel.LEVEL_B,
        SourceClassification.SUPPLIER: ReliabilityLevel.LEVEL_C,
        SourceClassification.REPUTABLE_NEWS: ReliabilityLevel.LEVEL_C,
        SourceClassification.INVESTMENT_RESEARCH: ReliabilityLevel.LEVEL_D,
        SourceClassification.OTHER: ReliabilityLevel.LEVEL_E,
    }[classification]


def source_type_for_classification(classification: SourceClassification) -> SourceType:
    if classification == SourceClassification.OFFICIAL_COMPANY:
        return SourceType.INVESTOR_RELATIONS
    if classification == SourceClassification.REGULATORY:
        return SourceType.REGULATORY_FILING
    if classification == SourceClassification.EXCHANGE:
        return SourceType.EXCHANGE_ANNOUNCEMENT
    if classification == SourceClassification.REPUTABLE_NEWS:
        return SourceType.NEWS
    return SourceType.SEARCH_DISCOVERY


def publisher_from_host(host: str) -> str:
    parts = host.split(".")
    if len(parts) >= 2:
        return parts[-2].replace("-", " ").title()
    return host


def _discovery_rejection_reason(reason: str) -> str:
    if reason in {"DOMAIN_VALIDATION_FAILED", "SOURCE_QUALITY_REJECTED", "DUPLICATE"}:
        return reason
    if reason in {"UNSUPPORTED_URL", "DOMAIN_BLOCKED"}:
        return "DOMAIN_VALIDATION_FAILED"
    if "not permitted" in reason or "private" in reason.lower() or "local" in reason.lower():
        return "DOMAIN_VALIDATION_FAILED"
    return reason


def _profile_display_name(profile: CompanyResearchProfile | EtfResearchProfile) -> str:
    return profile.company_name if isinstance(profile, CompanyResearchProfile) else profile.fund_name


def _infer_underlying_index(name: str) -> str | None:
    upper = name.upper()
    if "S&P 500" in upper or "SP 500" in upper:
        return "S&P 500"
    if "NASDAQ 100" in upper or "NASDAQ-100" in upper:
        return "NASDAQ 100"
    return None


def _dedupe_identity_terms(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip()
        key = normalized.lower()
        if normalized and key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


async def _safe_search_get(
    client: httpx.AsyncClient,
    endpoint: str,
    *,
    params: dict[str, str | int],
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    try:
        response = await client.get(endpoint, params=params, headers=headers)
    except httpx.TimeoutException as exc:
        raise SearchProviderError("SEARCH_PROVIDER_TIMEOUT") from exc
    except httpx.HTTPError as exc:
        raise SearchProviderError("SEARCH_PROVIDER_UNAVAILABLE:http_error") from exc
    if response.status_code in {401, 403}:
        raise SearchProviderError("SEARCH_PROVIDER_FORBIDDEN")
    if response.status_code == 429:
        raise SearchProviderError("SEARCH_PROVIDER_RATE_LIMITED")
    if response.status_code >= 500:
        raise SearchProviderError("SEARCH_PROVIDER_UNAVAILABLE")
    if response.status_code >= 400:
        raise SearchProviderError(f"SEARCH_PROVIDER_UNAVAILABLE:http_status_{response.status_code}")
    return response


def _safe_json(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise SearchProviderError("SEARCH_PROVIDER_UNAVAILABLE:invalid_response") from exc
    if not isinstance(payload, dict):
        raise SearchProviderError("SEARCH_PROVIDER_UNAVAILABLE:invalid_response")
    return payload
