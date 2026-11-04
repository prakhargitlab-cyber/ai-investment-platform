"""Explicit on-demand SBI/NSE cross-source candidate report (never an approval).

Example from ai/research-engine:
  python -m app.sbi_ter_nse_circular_review_cli --nse-pdf C:/review/CML77115.pdf

Per invocation: reads a *local* NSE circular PDF, downloads ONE fresh official
SBI TER workbook, stages to in-memory persistence and prints review candidates.
No scheduler, resolver, production database, or expense-ratio fact writing.
"""
import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path

import httpx

from app.persistence import SqliteResearchPersistence
from app.sbi_ter_nse_circular_review import MAX_NSE_CIRCULAR_BYTES, reconcile_sbi_ter_nse_circular
from app.sbi_ter_runtime_download import SbiTerRuntimeDownloader
from app.settings import Settings


async def _run(nse_pdf: Path):
    if not nse_pdf.is_file() or not 0 < nse_pdf.stat().st_size <= MAX_NSE_CIRCULAR_BYTES:
        raise ValueError("NSE_CIRCULAR_LOCAL_PDF_MISSING_OR_OVERSIZED")
    nse_bytes = nse_pdf.read_bytes()
    settings = Settings()
    timeout = httpx.Timeout(
        timeout=settings.research_request_timeout_seconds,
        connect=settings.research_connect_timeout_seconds,
    )
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        store = SqliteResearchPersistence(":memory:")
        downloader = SbiTerRuntimeDownloader(settings, client=client)
        acquired = await downloader.acquire(store)
        report = reconcile_sbi_ter_nse_circular(
            acquired.results,
            ter_workbook_sha256=acquired.sha256,
            nse_pdf_bytes=nse_bytes,
            claimed_nse_url="https://nsearchives.nseindia.com/content/circulars/CML77115.pdf",
            effective_on=acquired.retrieved_at.date(),
        )
        print(json.dumps({
            "status": report.status,
            "approved_for_persistence": report.approved_for_persistence,
            "nse_circular": report.circular_reference,
            "nse_pdf_sha256": report.nse_pdf_sha256,
            "nse_pdf_source_claim_not_network_authenticated": True,
            "ter_workbook_sha256": report.ter_workbook_sha256,
            "ter_download_retrieved_at": acquired.retrieved_at.astimezone(timezone.utc).isoformat(),
            "nse_effective_date": report.nse_effective_date.isoformat(),
            "staged_count": len(acquired.results),
            "candidate_count": len(report.candidate_rows),
            "unmatched_scheme_count": len(report.unmatched_scheme_codes),
            "candidates": [
                {**asdict(row), "nse_effective_date": row.nse_effective_date.isoformat()}
                for row in report.candidate_rows
            ],
            "unmatched_scheme_codes": report.unmatched_scheme_codes,
            "database": "IN_MEMORY_ONLY",
            "production_evidence_written": False,
        }, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Review-only: compare official SBI TER codes to uploaded NSE circular rows"
    )
    parser.add_argument("--nse-pdf", required=True, type=Path)
    args = parser.parse_args(argv)
    asyncio.run(_run(args.nse_pdf))


if __name__ == "__main__":
    main()
