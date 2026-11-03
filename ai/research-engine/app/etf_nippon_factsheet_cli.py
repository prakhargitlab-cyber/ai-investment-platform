"""Offline Nippon factsheet inspection; never fetches or persists investment facts.

Run from ai/research-engine:
    python -m app.etf_nippon_factsheet_cli "C:\\path\\to\\Nippon-FS-October-2026.pdf"
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from app.etf_nippon_factsheet_review import extract_review_only, HEADINGS, MAX_PDF_BYTES


def main() -> int:
    parser = argparse.ArgumentParser(description="Review two scheme pages without authorizing ingestion")
    parser.add_argument("pdf", type=Path, help="Previously downloaded official Nippon PDF")
    args = parser.parse_args()
    if not args.pdf.is_file() or args.pdf.stat().st_size > MAX_PDF_BYTES:
        parser.error("PDF missing or exceeds the bounded size limit")
    pdf_bytes = args.pdf.read_bytes()
    result = {}
    for ticker in HEADINGS:
        try:
            result[ticker] = asdict(extract_review_only(pdf_bytes, ticker))
        except ValueError as exc:
            result[ticker] = {"status": "REVIEW_FAILED", "reason": str(exc), "identity_verified": False}
    print(json.dumps(result, indent=2, default=str))
    return 0 if all(item.get("status") == "DOCUMENT_REVIEW_ONLY" for item in result.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
