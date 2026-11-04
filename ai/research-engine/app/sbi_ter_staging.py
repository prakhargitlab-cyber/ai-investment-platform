"""End-to-end SBI TER download + exact-identity coverage audit (staging only).

Live workbook is downloaded on each explicit call; nothing is published to
investment evidence. Independently reviewed scheme-code crosswalks may be
supplied for AUDIT ONLY. The caller can inspect unresolved codes, but
this service intentionally has no capability to persist TER values.

A reviewed record's metadata is not proof of the original issuer document.
Do not connect audit-only matches to BoundedSbiTerAdapter.identity_resolver
until official source authenticity and scheme-code/ISIN linkage are verified.
"""
from collections import Counter
from dataclasses import dataclass
from uuid import UUID

from app.persistence import SqliteResearchPersistence
from app.sbi_ter_identity_audit import SbiIdentityAuditRow, audit_staged_sbi_schemes
from app.sbi_ter_runtime_download import SbiTerRuntimeAcquisition, SbiTerRuntimeDownloader


@dataclass(frozen=True)
class SbiTerStagingReport:
    source_sha256: str
    source_url: str
    staged_count: int
    unresolved_count: int
    review_matched_count: int
    rows: tuple[SbiIdentityAuditRow, ...]
    # Hardcoded false by design: a reviewed record is not source authentication.
    authorized_for_fact_persistence: bool = False


async def stage_and_audit_current_sbi_ter(
    downloader: SbiTerRuntimeDownloader,
    canonical_etfs: list[dict],
    *,
    reviewed_records=(),
) -> SbiTerStagingReport:
    """Download current workbook with a fresh, short-lived in-memory store.

    Never accepts a production persistence handle. The caller-supplied
    downloader MUST use its default staging-only adapter; any injected
    identity resolver that returns a fact causes a hard rejection here.
    """
    store = SqliteResearchPersistence(":memory:")
    acquired: SbiTerRuntimeAcquisition = await downloader.acquire(store)
    if not acquired.results or any(
        row.instrument_id is not None or row.fact is not None
        or str(row.outcome) != "IDENTITY_UNRESOLVED_STAGED_ONLY"
        for row in acquired.results
    ):
        raise ValueError("SBI_TER_STAGING_MUST_NOT_PERSIST_FACTS")
    rows = audit_staged_sbi_schemes(acquired.results, reviewed_records, canonical_etfs)
    counts = Counter(row.status for row in rows)
    return SbiTerStagingReport(
        source_sha256=acquired.sha256,
        source_url=acquired.source_url,
        staged_count=len(rows),
        unresolved_count=counts["IDENTITY_UNRESOLVED_STAGED_ONLY"],
        review_matched_count=counts["REVIEWED_CANONICAL_MATCH"],
        rows=rows,
    )
