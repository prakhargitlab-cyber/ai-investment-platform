"""Explicit allowlist of approved official ETF document sources.

This is a human-reviewed, exact-match contract: canonical instrument ->
verified AMC issuer domain -> exact official document URL -> declared
document type. No URL here is invented or inferred from a domain, and
nothing downstream may treat an arbitrary PDF reachable on an approved
domain as in-scope -- only the exact URLs registered below are ever
fetched, and only for the instrument they are registered against.

Being listed here means a URL was manually reviewed as belonging to the
named AMC and instrument. It is NOT a claim that the document contains a
labelled ISIN, AUM or TER -- the downstream identity gate
(app.etf_document_parser.verified_scheme_section) and metric parser
(parse_metric) independently decide that from the document's own content,
and this module must never bypass or relax those checks.
"""
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit
from uuid import UUID


class ApprovedDocumentType(StrEnum):
    PRODUCT_NOTE = "PRODUCT_NOTE"
    FACTSHEET = "FACTSHEET"


class AccessTier(StrEnum):
    """Whether a source can ever be fetched unattended.

    Only PUBLIC_UNAUTHENTICATED sources are eligible for automated fetch.
    REQUIRES_AUTHENTICATION is also the default for any source whose access
    requirements have not been explicitly reviewed and set -- an unreviewed
    or unknown source is never fetchable by omission, only by an explicit,
    reviewed PUBLIC_UNAUTHENTICATED declaration. This module never performs
    or automates authentication of any kind; a REQUIRES_AUTHENTICATION
    source is recorded here only so it is visible in the registry (and in
    the known-not-yet-automatable column of a source coverage review), not
    so it can be fetched.
    """

    PUBLIC_UNAUTHENTICATED = "PUBLIC_UNAUTHENTICATED"
    REQUIRES_AUTHENTICATION = "REQUIRES_AUTHENTICATION"


@dataclass(frozen=True)
class ApprovedDocumentSource:
    """One manually reviewed source. Every field is reviewer-supplied."""

    instrument_id: UUID
    isin: str
    ticker: str
    issuer_name: str
    document_url: str
    document_host: str
    document_type: ApprovedDocumentType
    approval_reference: str
    # Safe-by-default: an entry that does not explicitly set this stays
    # REQUIRES_AUTHENTICATION, i.e. not fetchable by BoundedOfficialDocumentFetcher.
    access_tier: AccessTier = AccessTier.REQUIRES_AUTHENTICATION

    def __post_init__(self):
        parsed = urlsplit(self.document_url)
        if (parsed.scheme != "https" or (parsed.hostname or "").lower() != self.document_host.lower()
                or not self.document_url.lower().endswith(".pdf")):
            raise ValueError("APPROVED_SOURCE_URL_HOST_MISMATCH")
        if not self.approval_reference.strip():
            raise ValueError("APPROVED_SOURCE_APPROVAL_REFERENCE_REQUIRED")


class ApprovedSourceRegistry:
    """Caller-owned allowlist. Only sources explicitly added are fetchable."""

    def __init__(self, sources=()):
        self._by_instrument: dict[UUID, list[ApprovedDocumentSource]] = {}
        for source in sources:
            self.add(source)

    def add(self, source: ApprovedDocumentSource) -> None:
        self._by_instrument.setdefault(source.instrument_id, []).append(source)

    def sources_for(self, instrument_id: UUID) -> list[ApprovedDocumentSource]:
        return list(self._by_instrument.get(instrument_id, []))

    def is_approved(self, instrument_id: UUID, url: str) -> bool:
        return any(source.document_url == url for source in self.sources_for(instrument_id))

    def source_for_url(self, instrument_id: UUID, url: str) -> ApprovedDocumentSource | None:
        return next((s for s in self.sources_for(instrument_id) if s.document_url == url), None)

    def all_sources(self) -> list[ApprovedDocumentSource]:
        return [source for sources in self._by_instrument.values() for source in sources]

    def instrument_count(self) -> int:
        return len(self._by_instrument)


def default_bounded_registry() -> ApprovedSourceRegistry:
    """Seeded with exactly the two sources reviewed for this bounded rollout.

    NIFTYBEES and JUNIORBEES "Product Note" PDFs, retrieved from Nippon
    India Mutual Fund's own domain and hash-verified on 2026-10-28 (see
    .tmp/etf-validation/sources.txt and the prior genuine-document
    validation report). The instrument_id/isin values are the real,
    independently re-verified canonical identities from the archived NSE
    master-data backup (.tmp/etf-validation/nse-identity-manifest.json),
    not placeholders.

    Known, already-proven limitation of both documents: neither contains
    a labelled ISIN/AUM/TER line that the identity gate or metric parser
    can accept (see the genuine-document validation report). Registering
    them here only makes the fetch layer allowed to retrieve the exact
    bytes; it does not and cannot make the documents pass the unmodified
    downstream identity/metric checks.

    Both sources are explicitly marked access_tier=REQUIRES_AUTHENTICATION:
    this exact ProductNotes path requires OTP-based authentication to reach
    the document (confirmed by direct review, 2026-10-28). They are listed
    here for visibility only -- BoundedOfficialDocumentFetcher refuses to
    fetch either one, before any HTTP request, for exactly this reason.
    """
    return ApprovedSourceRegistry([
        ApprovedDocumentSource(
            instrument_id=UUID("805e0ebc-ea95-4859-b739-bdfbf5db327e"),
            isin="INF204KB14I2",
            ticker="NIFTYBEES",
            issuer_name="Nippon India Mutual Fund",
            document_url="https://mf.nipponindiaim.com/FundsAndPerformance/ProductNotes/"
                          "NipponIndia-ETF-Nifty-50-BeES-Jun-2026.pdf",
            document_host="mf.nipponindiaim.com",
            document_type=ApprovedDocumentType.PRODUCT_NOTE,
            approval_reference="etf-radar-bounded-fetch-2026-10-28:NIFTYBEES",
            access_tier=AccessTier.REQUIRES_AUTHENTICATION,
        ),
        ApprovedDocumentSource(
            instrument_id=UUID("526e78c2-a5af-46e0-af0e-af470ca966ca"),
            isin="INF732E01045",
            ticker="JUNIORBEES",
            issuer_name="Nippon India Mutual Fund",
            document_url="https://mf.nipponindiaim.com/FundsAndPerformance/ProductNotes/"
                          "NipponIndia-ETF-Nifty-Next-50-Junior-BeES-Mar-2026.pdf",
            document_host="mf.nipponindiaim.com",
            document_type=ApprovedDocumentType.PRODUCT_NOTE,
            approval_reference="etf-radar-bounded-fetch-2026-10-28:JUNIORBEES",
            access_tier=AccessTier.REQUIRES_AUTHENTICATION,
        ),
    ])
