"""Explicit, read-only smoke test of the current official SBI TER workbook.

Run from ai/research-engine:
    python -m app.sbi_ter_live_smoke

No scheduler, DB file or canonical identity mapping is used. Every scheme
stays staged-only. The download is bounded by SbiTerRuntimeDownloader.
A live network request occurs ONLY when this CLI is explicitly invoked.
"""
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json

import httpx

from app.persistence import SqliteResearchPersistence
from app.sbi_ter_runtime_download import SbiTerRuntimeDownloader
from app.settings import Settings


async def _run():
    settings = Settings()
    timeout = httpx.Timeout(
        timeout=settings.research_request_timeout_seconds,
        connect=settings.research_connect_timeout_seconds,
    )
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        downloader = SbiTerRuntimeDownloader(settings, client=client)
        # The default downloader uses BoundedSbiTerAdapter with NO approved
        # mappings; it must never create real financial observations.
        store = SqliteResearchPersistence(":memory:")
        result = await downloader.acquire(store)
        counts = Counter(str(row.outcome) for row in result.results)
        if any(row.fact is not None or row.instrument_id is not None for row in result.results):
            raise ValueError("SBI_TER_SMOKE_UNEXPECTED_RESOLVED_IDENTITY")
        print(json.dumps({
            "source_url": result.source_url,
            "retrieved_at_utc": result.retrieved_at.astimezone(timezone.utc).isoformat(),
            "download_bytes": result.bytes_downloaded,
            "source_sha256": result.sha256,
            "staged_scheme_count": len(result.results),
            "outcomes": dict(sorted(counts.items())),
            "database": "IN_MEMORY_ONLY",
            "verified_identity_mapping": False,
            "production_evidence_written": False,
        }, indent=2))


def main():
    asyncio.run(_run())


if __name__ == "__main__":
    main()
