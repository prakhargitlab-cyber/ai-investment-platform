"""Operator-only read-only inspection of a locally obtained official SBI PDF.

This CLI does NOT download, approve or authenticate the claimed source URL.
Run only with original bytes obtained from an independently reviewed issuer
source. Output is a list of REVIEW-ONLY candidates, never a trusted crosswalk.

Example (from ai/research-engine):
  python -m app.sbi_ter_identity_document_cli --pdf C:/tmp/sbi-disclosure.pdf \
      --source-url https://www.sbimf.com/path/reviewed-disclosure.pdf
"""
import argparse
import json
from pathlib import Path

from app.sbi_ter_identity_document import MAX_IDENTITY_PDF_BYTES, inspect_sbi_identity_pdf


def main(argv=None):
    parser = argparse.ArgumentParser(description="Inspect original SBI PDF for explicitly labelled identity pairs (review-only)")
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--source-url", required=True)
    args = parser.parse_args(argv)
    if not args.pdf.is_file() or args.pdf.stat().st_size > MAX_IDENTITY_PDF_BYTES:
        raise ValueError("SBI_IDENTITY_LOCAL_PDF_MISSING_OR_OVERSIZED")
    inspection = inspect_sbi_identity_pdf(args.pdf.read_bytes(), source_url=args.source_url)
    print(json.dumps({
        "status": inspection.status,
        "identity_authenticated": inspection.identity_authenticated,
        "eligible_for_fact_persistence": inspection.eligible_for_fact_persistence,
        "source_url_claimed_not_verified": inspection.document_url,
        "raw_pdf_sha256": inspection.raw_pdf_sha256,
        "pages_scanned": inspection.pages_scanned,
        "candidate_count": len(inspection.candidates),
        "candidates": [{
            "scheme_code": item.scheme_code,
            "isin": item.isin,
            "page_number": item.page_number,
            "status": item.status,
            "eligible_for_fact_persistence": item.eligible_for_fact_persistence,
        } for item in inspection.candidates],
    }, indent=2))


if __name__ == "__main__":
    main()
