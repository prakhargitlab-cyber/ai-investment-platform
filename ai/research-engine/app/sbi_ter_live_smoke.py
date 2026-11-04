"""Explicit, read-only smoke test of the current official SBI TER workbook.

Run from ai/research-engine:
    python -m app.sbi_ter_live_smoke
    python -m app.sbi_ter_live_smoke --include-staged-schemes

No scheduler, DB file or canonical identity mapping is used. Every scheme
stays staged-only. The download is bounded by SbiTerRuntimeDownloader.
A live network request occurs ONLY when this CLI is explicitly invoked.
The optional inventory is source-derived, NOT an approved code-to-ISIN map.
"""
import argparse
import asyncio
from collections import Counter
from datetime import timezone
import json

import httpx

from app.persistence import SqliteResearchPersistence
from app.sbi_ter_runtime_download import SbiTerRuntimeDownloader
from app.sbi_ter_workbook_adapter import AdapterOutcome
from app.settings import Settings


def make_staging_smoke_report(acquired, *, include_staged_schemes=False):
    """Pure reporting: reject any resolved identity/fact before showing output.

    Printing source codes is useful for finding authoritative crosswalk
    evidence. Names are displayed for human review only, never joined
    to NSE symbols or interpreted as verified identity.
    """
    rows = tuple(acquired.results)
    if not rows or any(
        row.fact is not None
        or row.instrument_id is not None
        or row.outcome != AdapterOutcome.IDENTITY_UNRESOLVED_STAGED_ONLY
        for row in rows
    ):
        raise ValueError("SBI_TER_SMOKE_UNEXPECTED_RESOLVED_IDENTITY")
    codes = [row.scheme_code for row in rows]
    if any(not isinstance(code, str) or not code.strip() for code in codes) or len(codes) != len(set(codes)):
        raise ValueError("SBI_TER_SMOKE_DUPLICATE_OR_INVALID_SCHEME_CODE")
    counts = Counter(str(row.outcome) for row in rows)
    report = {
        "source_url": acquired.source_url,
        "retrieved_at_utc": acquired.retrieved_at.astimezone(timezone.utc).isoformat(),
        "download_bytes": acquired.bytes_downloaded,
        "source_sha256": acquired.sha256,
        "staged_scheme_count": len(rows),
        "outcomes": dict(sorted(counts.items())),
        "database": "IN_MEMORY_ONLY",
        "verified_identity_mapping": False,
        "production_evidence_written": False,
    }
    if include_staged_schemes:
        report["identity_inventory_status"] = "OFFICIAL_TER_CODES_ONLY_NO_ISIN_MAPPING"
        report["staged_schemes"] = [
            {"nsdl_scheme_code": row.scheme_code, "scheme_name_for_review_only": row.scheme_name,
             "isin": None, "canonical_instrument_id": None,
             "identity_verified": False}
            for row in sorted(rows, key=lambda item: item.scheme_code)
        ]
    return report


async def _run(*, include_staged_schemes=False):
    settings = Settings()
    timeout = httpx.Timeout(
        timeout=settings.research_request_timeout_seconds,
        connect=settings.research_connect_timeout_seconds,
    )
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        downloader = SbiTerRuntimeDownloader(settings, client=client)
        # Default adapter has no approved mappings. The DB is ephemeral.
        store = SqliteResearchPersistence(":memory:")
        acquired = await downloader.acquire(store)
        report = make_staging_smoke_report(
            acquired, include_staged_schemes=include_staged_schemes
        )
        print(json.dumps(report, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Explicit official SBI TER download (staging only; no ISIN crosswalk)"
    )
    parser.add_argument(
        "--include-staged-schemes", action="store_true",
        help="Print exact official NSDL scheme codes and scheme names; never infer ISINs"
    )
    args = parser.parse_args(argv)
    asyncio.run(_run(include_staged_schemes=args.include_staged_schemes))


if __name__ == "__main__":
    main()
