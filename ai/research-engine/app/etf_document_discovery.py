"""Internal, bounded ingestion of explicitly authorized local document text.

No fetcher, upload route, startup singleton, or scheduler is installed here.
Trusted operator code must inject a registry after reviewing source provenance,
the exact normalized text and (for multi-scheme documents) its section bounds.
Registry possession is an internal capability, not HTTP authentication. Never
construct approvals from untrusted request parameters.
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
import re
from uuid import UUID

from app.etf_document_parser import MAX_DOCUMENT_CHARACTERS
from app.etf_evidence import EtfMetric, evidence_id
from app.models import EtfResearchProfile, ResearchDocument

MAX_OFFICIAL_DOCUMENTS = 5


@dataclass(frozen=True)
class VerifiedDocumentSource:
    instrument_id: UUID
    document_url: str
    canonical_url: str
    source_type: str
    source_classification: str
    source_name: str
    document_type: str
    authority: str
    reliability_level: str
    # Digest of exact UTF-8 normalized text, not the original PDF bytes.
    normalized_text_sha256: str
    scheme_isin: str
    approval_reference: str
    retrieved_at: datetime
    published_at: datetime | None = None
    # Only valid with an explicit "AUM and TER as of YYYY-MM-DD" in the section.
    as_of_date: date | None = None
    # Reviewed character offsets, with both boundaries on complete lines.
    section_span: tuple[int, int] | None = None


class OfficialDocumentRegistry:
    """Caller-owned authorization snapshot. Rebuild/review on every operation."""

    def __init__(self):
        self._sources: dict[tuple[UUID, str], VerifiedDocumentSource] = {}

    def register(self, source: VerifiedDocumentSource) -> None:
        if (not re.fullmatch(r"[0-9a-f]{64}", source.normalized_text_sha256)
                or not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", source.scheme_isin)
                or not source.approval_reference.strip()):
            raise ValueError("EXPLICIT_DOCUMENT_APPROVAL_REQUIRED")
        if source.section_span is not None and (
                not isinstance(source.section_span, tuple) or len(source.section_span) != 2
                or any(type(boundary) is not int for boundary in source.section_span)):
            raise ValueError("INVALID_SCHEME_SECTION")
        if source.retrieved_at.tzinfo is None or source.retrieved_at.utcoffset() is None:
            raise ValueError("VERIFIED_DOCUMENT_RETRIEVAL_TIME_REQUIRED")
        key = source.instrument_id, source.document_url
        if key in self._sources:
            raise ValueError("Document URL already registered for instrument")
        if len(self._sources) >= MAX_OFFICIAL_DOCUMENTS:
            raise ValueError("OFFICIAL_DOCUMENT_LIMIT_EXCEEDED")
        self._sources[key] = source

    def sources_for(self, instrument_id: UUID) -> list[VerifiedDocumentSource]:
        return [source for (key, _), source in self._sources.items() if key == instrument_id]

    def has_sources(self, instrument_id: UUID) -> bool:
        return bool(self.sources_for(instrument_id))

    def source_for(self, instrument_id: UUID, document_url: str):
        return self._sources.get((instrument_id, document_url))


def register_official_document(instrument_id, document_url, *, registry: OfficialDocumentRegistry,
                               canonical_url, source_name, normalized_text_sha256, scheme_isin,
                               approval_reference, retrieved_at, source_type="COMPANY_WEBSITE",
                               source_classification="OFFICIAL_COMPANY", document_type="HTML",
                               authority="OFFICIAL_FUND", reliability_level="LEVEL_A",
                               published_at=None, as_of_date=None, section_span=None):
    source = VerifiedDocumentSource(instrument_id=instrument_id, document_url=document_url,
        canonical_url=canonical_url, source_type=source_type, source_classification=source_classification,
        source_name=source_name, document_type=document_type, authority=authority,
        reliability_level=reliability_level, normalized_text_sha256=normalized_text_sha256,
        scheme_isin=scheme_isin, approval_reference=approval_reference,
        retrieved_at=retrieved_at, published_at=published_at, as_of_date=as_of_date, section_span=section_span)
    registry.register(source)
    return source


def ingest_etf_facts_from_document(persistence, profile: EtfResearchProfile, document_text: str, *,
                                  document_url: str, registry: OfficialDocumentRegistry, clock=None) -> dict:
    """One approved local text, two metric attempts. Metadata comes from approval.

    Persisted listing evidence must independently match the section's ISIN.
    This function cannot fetch URLs and cannot authorize its own input.
    """
    from app.etf_acquisition import EtfAcquisitionService
    source = registry.source_for(profile.instrument_id, document_url)
    if source is None:
        raise ValueError("OFFICIAL_DOCUMENT_NOT_AUTHORIZED")
    if len(document_text) > MAX_DOCUMENT_CHARACTERS:
        raise ValueError("OFFICIAL_DOCUMENT_SIZE_LIMIT_EXCEEDED")
    clock = clock or (lambda: datetime.now(timezone.utc))
    document = ResearchDocument(instrument_id=profile.instrument_id, canonical_url=source.canonical_url,
        original_url=source.document_url, source_type=source.source_type,
        source_classification=source.source_classification, source_name=source.source_name,
        published_at=source.published_at, retrieved_at=source.retrieved_at, content_type="text/plain",
        document_type=source.document_type, normalized_text=document_text,
        content_hash=source.normalized_text_sha256, status="PROCESSED",
        reliability_level=source.reliability_level, source_mode="REAL")
    service = EtfAcquisitionService(persistence, clock=clock, document_registry=registry)
    results = {}
    for name, metric in (("aum", EtfMetric.AUM), ("expense_ratio", EtfMetric.EXPENSE_RATIO)):
        attempt = service.acquire_official_document(profile, document, metric)
        # Report this attempt's evidence, never a different higher-precedence row.
        fact = next((f for f in persistence.etf_facts(profile.instrument_id, metric)
                     if evidence_id(f) in attempt.evidence_ids), None)
        results[name] = {"outcome": attempt.outcome, "reason": attempt.reason,
            "value": fact.value if fact else None, "unit": fact.unit if fact else None,
            "as_of_date": fact.as_of_date if fact else None, "evidence_ids": attempt.evidence_ids}
    return results


def ingest_all_registered_documents(persistence, profile, document_contents: dict[str, str], *,
                                    registry: OfficialDocumentRegistry, clock=None) -> dict:
    """At most five approved texts; missing content or rejected facts are visible."""
    sources = registry.sources_for(profile.instrument_id)
    if not sources:
        return {"status": "NO_REGISTERED_SOURCES", "sources_processed": 0, "results": []}
    if (len(document_contents) > MAX_OFFICIAL_DOCUMENTS
            or set(document_contents) - {source.document_url for source in sources}):
        raise ValueError("UNREGISTERED_OR_EXCESS_DOCUMENTS")
    if any(len(text) > MAX_DOCUMENT_CHARACTERS for text in document_contents.values()):
        raise ValueError("OFFICIAL_DOCUMENT_SIZE_LIMIT_EXCEEDED")
    results, missing = [], []
    for source in sources:
        if source.document_url not in document_contents:
            missing.append(source.document_url)
            continue
        result = ingest_etf_facts_from_document(persistence, profile, document_contents[source.document_url],
            document_url=source.document_url, registry=registry, clock=clock)
        results.append({"source": {"url": source.document_url, "name": source.source_name,
                                   "authority": source.authority}, "result": result})
    failed = any(metric["outcome"] == "TECHNICAL_FAILURE"
                 for row in results for metric in row["result"].values())
    return {"status": "PARTIAL_FAILURE" if failed or missing else "COMPLETED",
            "sources_processed": len(results), "missing_documents": missing, "results": results}
