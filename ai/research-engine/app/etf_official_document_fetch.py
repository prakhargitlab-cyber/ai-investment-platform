"""Bounded, allowlist-only fetch adapter for official ETF documents.

Fetches only exact URLs present in an ApprovedSourceRegistry
(app.etf_official_document_sources), over HTTPS, and verifies the final
(post-redirect) host matches the approved document's own host before
accepting any content -- this is the one SSRF-adjacent check this module
adds on top of app.url_security.validate_public_http_url, which only
blocks private/metadata-service IPs and would otherwise happily follow a
redirect to a different, still-public, unapproved host.

Everything else -- request timeout, connect timeout, retry/backoff,
response-size limits (header and streaming), PDF magic-byte validation,
PDF corruption/parse-error detection, and content-type validation -- is
reused unmodified from app.research_fetching.HttpResearchFetcher. None of
it is reimplemented here.

No network call in this module is ever made against a URL that is not in
the registry for the given instrument. The HTTP client is always
injectable (HttpResearchFetcher's own constructor argument), so every test
of this module runs fully offline against an httpx.MockTransport. This
module never performs a real network call on its own initiative; it is
exercised only when explicitly invoked with a live or mock client.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from app.etf_official_document_sources import AccessTier, ApprovedDocumentSource, ApprovedSourceRegistry
from app.research_fetching import (
    DocumentSizeLimitExceeded,
    FetchError,
    HttpResearchFetcher,
    HttpStatusFetchError,
    RestrictedFetchError,
    TransportFetchError,
)
from app.settings import Settings
from app.url_security import UnsafeUrlError

# Only a PDF is ever an acceptable response for an official ETF document in
# this bounded rollout. A 200 OK text/html page (login wall, captcha, error
# page served with a "successful" status) is rejected here, before any PDF
# extraction is attempted, even though HttpResearchFetcher's own generic
# content-type allowlist would otherwise accept text/html for extraction.
_ACCEPTED_CONTENT_TYPES = {"application/pdf"}


class BoundedFetchOutcome(StrEnum):
    FETCHED = "FETCHED"
    URL_NOT_APPROVED = "URL_NOT_APPROVED"
    AUTHENTICATION_REQUIRED_NOT_SUITABLE = "AUTHENTICATION_REQUIRED_NOT_SUITABLE"
    SSRF_BLOCKED_BY_URL_VALIDATION = "SSRF_BLOCKED_BY_URL_VALIDATION"
    CROSS_HOST_REDIRECT_REJECTED = "CROSS_HOST_REDIRECT_REJECTED"
    HTTP_STATUS_ERROR = "HTTP_STATUS_ERROR"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    SIZE_LIMIT_EXCEEDED = "SIZE_LIMIT_EXCEEDED"
    UNEXPECTED_CONTENT_TYPE = "UNEXPECTED_CONTENT_TYPE"
    PDF_EXTRACTION_FAILED = "PDF_EXTRACTION_FAILED"


@dataclass(frozen=True)
class BoundedFetchResult:
    outcome: BoundedFetchOutcome
    reason: str
    source: ApprovedDocumentSource | None = None
    final_url: str | None = None
    status_code: int | None = None
    content_type: str | None = None
    raw_bytes_sha256: str | None = None
    normalized_text: str | None = None
    normalized_text_sha256: str | None = None
    retrieved_at: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is BoundedFetchOutcome.FETCHED


class BoundedOfficialDocumentFetcher:
    """Fetches one ETF's approved official document, or explains why not.

    One instance wraps one HttpResearchFetcher (and therefore one HTTP
    client -- real or mocked). Callers never pass an arbitrary URL that
    bypasses the registry: `fetch` looks the url up in the registry itself
    and refuses anything not already present there.
    """

    def __init__(self, registry: ApprovedSourceRegistry, settings: Settings, *,
                 client: httpx.AsyncClient | None = None, clock=None):
        self._registry = registry
        self._settings = settings
        self._fetcher = HttpResearchFetcher(settings, client=client)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def fetch(self, instrument_id: UUID, url: str) -> BoundedFetchResult:
        source = self._registry.source_for_url(instrument_id, url)
        if source is None:
            return BoundedFetchResult(
                BoundedFetchOutcome.URL_NOT_APPROVED,
                "URL is not in the approved document allowlist for this instrument",
            )

        # No HTTP request is made for a source that is not explicitly
        # reviewed as PUBLIC_UNAUTHENTICATED -- this includes a source left
        # at its default (unreviewed) access_tier. This module never
        # automates, bypasses, or reuses authentication of any kind.
        if source.access_tier is not AccessTier.PUBLIC_UNAUTHENTICATED:
            return BoundedFetchResult(
                BoundedFetchOutcome.AUTHENTICATION_REQUIRED_NOT_SUITABLE,
                f"Source access_tier is {source.access_tier.value}, not PUBLIC_UNAUTHENTICATED "
                "-- unattended fetch is not permitted for this source",
                source=source,
            )

        retrieved_at = self._clock()
        try:
            network = await self._fetcher.fetch_network(
                source.document_url,
                headers={"User-Agent": self._settings.research_official_document_user_agent},
                max_bytes=self._settings.research_official_document_max_bytes,
            )
        except UnsafeUrlError as exc:
            return BoundedFetchResult(BoundedFetchOutcome.SSRF_BLOCKED_BY_URL_VALIDATION, str(exc), source=source)
        except DocumentSizeLimitExceeded as exc:
            return BoundedFetchResult(BoundedFetchOutcome.SIZE_LIMIT_EXCEEDED, str(exc), source=source)
        except RestrictedFetchError as exc:
            return BoundedFetchResult(BoundedFetchOutcome.HTTP_STATUS_ERROR, f"Restricted response: {exc}",
                                       source=source)
        except HttpStatusFetchError as exc:
            return BoundedFetchResult(BoundedFetchOutcome.HTTP_STATUS_ERROR, str(exc), source=source)
        except TransportFetchError as exc:
            return BoundedFetchResult(BoundedFetchOutcome.TRANSPORT_ERROR, str(exc), source=source)
        except FetchError as exc:
            # fetch_network's own non-redirect-specific failures (e.g.
            # "Maximum redirects exceeded", "Redirect without Location
            # header") surface as plain FetchError.
            return BoundedFetchResult(BoundedFetchOutcome.TRANSPORT_ERROR, str(exc), source=source)

        final_host = (urlsplit(network.final_url).hostname or "").lower()
        if final_host != source.document_host.lower():
            return BoundedFetchResult(
                BoundedFetchOutcome.CROSS_HOST_REDIRECT_REJECTED,
                f"Final URL host {final_host!r} does not match the approved host {source.document_host!r}",
                source=source, final_url=network.final_url, status_code=network.status_code,
            )

        if network.status_code >= 400:
            # A persistent 5xx (after HttpResearchFetcher exhausts its own
            # retries) is returned by fetch_network rather than raised, so
            # it must be checked here, not only via exception handling.
            return BoundedFetchResult(
                BoundedFetchOutcome.HTTP_STATUS_ERROR, f"Persistent HTTP {network.status_code} after retries",
                source=source, final_url=network.final_url, status_code=network.status_code,
            )

        content_type = (network.content_type or "").split(";")[0].strip().lower()
        if content_type not in _ACCEPTED_CONTENT_TYPES:
            return BoundedFetchResult(
                BoundedFetchOutcome.UNEXPECTED_CONTENT_TYPE,
                f"Expected application/pdf, received {content_type or '(none)'!r}",
                source=source, final_url=network.final_url, status_code=network.status_code,
                content_type=content_type,
            )

        raw_sha256 = hashlib.sha256(network.content).hexdigest()

        try:
            fetch_result = self._fetcher.process_network_response(
                network, max_bytes=self._settings.research_official_document_max_bytes,
            )
        except FetchError as exc:
            # Covers, unmodified: PDF_SIGNATURE_INVALID (magic-byte check),
            # PDF_PARSE_ERROR (pypdf.errors.PdfReadError -- corrupt PDF),
            # PDF_TEXT_EXTRACTION_FAILED, and any other extraction failure
            # HttpResearchFetcher already detects.
            return BoundedFetchResult(
                BoundedFetchOutcome.PDF_EXTRACTION_FAILED, str(exc),
                source=source, final_url=network.final_url, status_code=network.status_code,
                content_type=content_type, raw_bytes_sha256=raw_sha256,
            )

        normalized_text = fetch_result.text or ""
        normalized_sha256 = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
        return BoundedFetchResult(
            BoundedFetchOutcome.FETCHED, "Fetched and extracted within the approved allowlist",
            source=source, final_url=network.final_url, status_code=network.status_code,
            content_type=content_type, raw_bytes_sha256=raw_sha256,
            normalized_text=normalized_text, normalized_text_sha256=normalized_sha256,
            retrieved_at=retrieved_at,
        )
