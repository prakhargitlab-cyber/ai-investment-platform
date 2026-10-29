"""Integration: bounded official-document fetch -> existing ingestion contract.

Connects BoundedOfficialDocumentFetcher (app.etf_official_document_fetch) to
the unmodified registered-document ingestion contract
(app.etf_document_discovery.register_official_document /
ingest_etf_facts_from_document). This module never relaxes, bypasses, or
duplicates either the identity gate
(app.etf_document_parser.verified_scheme_section) or the metric parser
(parse_metric) -- it only decides whether an automatically fetched document
is even allowed to reach that unmodified contract, and reports precisely
why when it is not. Everything from registration onward re-derives and
checks identity, content hash, freshness and scheme matching independently
of anything this module asserts.

Disabled by default: `settings.research_etf_official_document_fetch_enabled`
must be explicitly True. Even when enabled, the approved-source registry
passed in is scoped to at most MAX_BOUNDED_INSTRUMENTS ETFs for this
bounded rollout -- constructing this pipeline with a larger registry is a
configuration error, not a silently-expanded rollout.
"""
from dataclasses import dataclass
from datetime import datetime, timezone

from app.etf_document_discovery import (
    OfficialDocumentRegistry,
    ingest_etf_facts_from_document,
    register_official_document,
)
from app.etf_official_document_fetch import BoundedOfficialDocumentFetcher
from app.etf_official_document_sources import ApprovedSourceRegistry
from app.models import EtfResearchProfile
from app.settings import Settings

MAX_BOUNDED_INSTRUMENTS = 2


@dataclass(frozen=True)
class AcquisitionPipelineResult:
    status: str  # "DISABLED" | "NOT_APPROVED" | "FETCH_FAILED" | "INGESTED"
    reason: str
    url: str | None = None
    facts: dict | None = None


class EtfOfficialDocumentAcquisitionPipeline:
    def __init__(self, approved_sources: ApprovedSourceRegistry, settings: Settings, *,
                 fetcher: BoundedOfficialDocumentFetcher | None = None,
                 document_registry: OfficialDocumentRegistry | None = None, clock=None):
        if approved_sources.instrument_count() > MAX_BOUNDED_INSTRUMENTS:
            raise ValueError("BOUNDED_ROLLOUT_INSTRUMENT_LIMIT_EXCEEDED")
        self._approved_sources = approved_sources
        self._settings = settings
        self._fetcher = fetcher or BoundedOfficialDocumentFetcher(approved_sources, settings, clock=clock)
        self._document_registry = document_registry if document_registry is not None else OfficialDocumentRegistry()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def acquire(self, persistence, profile: EtfResearchProfile, url: str) -> AcquisitionPipelineResult:
        if not self._settings.research_etf_official_document_fetch_enabled:
            return AcquisitionPipelineResult("DISABLED", "research_etf_official_document_fetch_enabled is False",
                                              url=url)

        source = self._approved_sources.source_for_url(profile.instrument_id, url)
        if source is None:
            return AcquisitionPipelineResult(
                "NOT_APPROVED", "URL is not in the approved document allowlist for this instrument", url=url)

        fetch_result = await self._fetcher.fetch(profile.instrument_id, url)
        if not fetch_result.ok:
            return AcquisitionPipelineResult("FETCH_FAILED", f"{fetch_result.outcome}: {fetch_result.reason}",
                                              url=url)

        # From here on, register_official_document / verified_scheme_section
        # (both unmodified) independently re-derive and check content hash,
        # scheme ISIN, scheme name/symbol, freshness and identity readiness.
        # This pipeline supplies only provenance it directly observed from
        # the fetch -- it never itself asserts a scheme_isin, name or symbol
        # match; a mismatch is caught downstream, not here.
        try:
            register_official_document(
                profile.instrument_id, source.document_url,
                registry=self._document_registry,
                canonical_url=source.document_url,
                source_name=source.issuer_name,
                normalized_text_sha256=fetch_result.normalized_text_sha256,
                scheme_isin=source.isin,
                approval_reference=source.approval_reference,
                retrieved_at=fetch_result.retrieved_at,
                source_type="COMPANY_WEBSITE",
                source_classification="OFFICIAL_COMPANY",
                document_type="PDF_REFERENCE",
                authority="OFFICIAL_FUND",
                reliability_level="LEVEL_B",
            )
        except ValueError as exc:
            return AcquisitionPipelineResult("FETCH_FAILED", f"REGISTRATION_REJECTED:{exc}", url=url)

        try:
            results = ingest_etf_facts_from_document(
                persistence, profile, fetch_result.normalized_text,
                document_url=source.document_url, registry=self._document_registry, clock=self._clock,
            )
        except ValueError as exc:
            return AcquisitionPipelineResult("FETCH_FAILED", f"INGESTION_REJECTED:{exc}", url=url)

        return AcquisitionPipelineResult(
            "INGESTED", "Document fetched and submitted to the existing ingestion contract",
            url=url, facts=results,
        )
