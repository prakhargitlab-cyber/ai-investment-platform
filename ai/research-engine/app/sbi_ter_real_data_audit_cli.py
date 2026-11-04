"""No-network, read-only SBI identity candidate audit using genuine local exports.

From ai/research-engine:
 python -m app.sbi_ter_real_data_audit_cli --reconciliation-json .tmp/etf-validation/sbi-review.json --canonical-json .tmp/etf-validation/canonical-etfs.json --research-sqlite .tmp/research.db
"""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path

from app.sbi_ter_canonical_preflight import preflight_sbi_candidates
from app.sbi_ter_real_data_audit import (
    ReadOnlySqliteEtfEvidence, read_portfolio_etf_snapshot, read_reconciliation_report,
)


def run(reconciliation_json: Path, canonical_json: Path, research_sqlite: Path, *, now=None) -> dict:
    candidate_report = read_reconciliation_report(reconciliation_json)
    canonical_rows = read_portfolio_etf_snapshot(canonical_json)
    now = now or datetime.now(timezone.utc)
    with ReadOnlySqliteEtfEvidence(research_sqlite) as store:
        audit = preflight_sbi_candidates(candidate_report, canonical_rows, store, now=now)
    return {
        "status": audit.status,
        "approved_for_persistence": False,
        "identity_verified_for_scoring": False,
        "ter_fact_written": False,
        "read_only_sqlite": True,
        "canonical_snapshot_source_authenticated": False,
        "nse_pdf_sha256": audit.nse_pdf_sha256,
        "ter_workbook_sha256": audit.ter_workbook_sha256,
        "candidate_count": len(audit.rows),
        "outcomes": {status: sum(row.status == status for row in audit.rows)
                     for status in sorted({row.status for row in audit.rows})},
        "rows": [
            {
                **asdict(row),
                "canonical_instrument_id": (
                    str(row.canonical_instrument_id) if row.canonical_instrument_id else None
                ),
            } for row in audit.rows
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only local canonical ETF + NSE listing preflight")
    parser.add_argument("--reconciliation-json", type=Path, required=True)
    parser.add_argument("--canonical-json", type=Path, required=True)
    parser.add_argument("--research-sqlite", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(run(args.reconciliation_json, args.canonical_json, args.research_sqlite), indent=2))


if __name__ == "__main__":
    main()
