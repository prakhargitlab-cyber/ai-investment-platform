"""Print a review-only comparison between a local NSE CSV and Nippon PDF.

Use a separately obtained official NSE listing CSV, retaining its original
bytes and provenance. This CLI does NOT download the exchange CSV or claim
its authenticity.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from app.etf_nippon_factsheet_review import HEADINGS, MAX_PDF_BYTES, extract_review_only
from app.etf_nippon_nse_comparison import MAX_EXCHANGE_CSV_BYTES, review_report


def _bounded_bytes(path: Path, max_bytes: int) -> bytes:
    if not path.is_file() or not 0 < path.stat().st_size <= max_bytes:
        raise ValueError(f"SOURCE_MISSING_OR_OVERSIZED:{path.name}")
    return path.read_bytes()


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline, non-persisting NSE/AMC identity comparison")
    parser.add_argument("--factsheet", required=True, type=Path)
    parser.add_argument("--nse-csv", required=True, type=Path)
    args = parser.parse_args()
    try:
        pdf = _bounded_bytes(args.factsheet, MAX_PDF_BYTES)
        nse_csv = _bounded_bytes(args.nse_csv, MAX_EXCHANGE_CSV_BYTES)
        metrics = tuple(extract_review_only(pdf, ticker) for ticker in HEADINGS)
        report = review_report(nse_csv, metrics)
        report["factsheet_sha256"] = metrics[0].raw_pdf_sha256
        report["factsheet_sections"] = [
            {"ticker": m.ticker, "heading": m.scheme_heading,
             "pdf_page": m.pdf_page, "report_date": m.report_date.isoformat()}
            for m in metrics
        ]
    except ValueError as exc:
        print(json.dumps({"status": "IDENTITY_REVIEW_FAILED", "reason": str(exc)}, indent=2))
        return 2
    print(json.dumps(report, indent=2, default=str))
    return 0 if all(
        s["status"] == "MATCH_EXACT_NAME_REVIEW_REQUIRED" for s in report["schemes"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
