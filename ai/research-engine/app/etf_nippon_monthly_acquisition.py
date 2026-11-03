"""Explicitly invoked monthly Nippon review via the existing secured PDF fetcher.

This does not discover or authorize URL families. A candidate MUST already
be approved by exact URL and public access for both canonical instruments.
A disabled settings flag stops any network request. Once approved, the
existing BoundedOfficialDocumentFetcher makes exactly one network fetch,
validates redirects/content type/PDF, and supplies original bytes to the
non-persisting scheme review stage.

No persistent cache is required: the scoped review invocation downloads the
publication only once, and uses the same bytes for both scheme sections.
"""
from __future__ import annotations

from uuid import UUID

from app.etf_nippon_monthly_source import MonthlyFactsheetCandidate
from app.etf_nippon_shared_review import SharedFactsheetReview, review_approved_shared_month
from app.etf_official_document_fetch import BoundedOfficialDocumentFetcher
from app.etf_official_document_sources import ApprovedSourceRegistry
from app.settings import Settings


class NipponMonthlyReviewAcquisition:
    def __init__(self, registry: ApprovedSourceRegistry, settings: Settings, *,
                 fetcher: BoundedOfficialDocumentFetcher | None = None):
        self.registry = registry
        self.settings = settings
        self.fetcher = fetcher or BoundedOfficialDocumentFetcher(registry, settings)

    async def review(self, candidate: MonthlyFactsheetCandidate,
                     instrument_ids_by_ticker: dict[str, UUID]) -> SharedFactsheetReview:
        if not self.settings.research_etf_official_document_fetch_enabled:
            raise ValueError("MONTHLY_REVIEW_FETCH_DISABLED")
        if not instrument_ids_by_ticker:
            raise ValueError("SHARED_FACTSHEET_SCHEME_LIMIT")
        # Coordinator checks ALL source approvals before the callback. A single
        # canonical instrument then owns the one bounded network request.
        first_id = next(iter(instrument_ids_by_ticker.values()))

        async def fetch_once(url: str) -> bytes:
            result = await self.fetcher.fetch(first_id, url)
            if not result.ok:
                raise ValueError(f"MONTHLY_REVIEW_FETCH_FAILED:{result.outcome}:{result.reason}")
            if result.raw_pdf_bytes is None:
                raise ValueError("MONTHLY_REVIEW_ORIGINAL_PDF_BYTES_MISSING")
            return result.raw_pdf_bytes

        return await review_approved_shared_month(
            candidate, self.registry, instrument_ids_by_ticker, fetch_once)
