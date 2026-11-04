"""Explicit, bounded ETF completion acceptance against configured PostgreSQL.

Read-only session enforced before any application readiness evaluation.
No HTTP provider client, no scheduler, no ingestion, no radar publication.
Requires the running research-engine's normal AIP_RESEARCH_DATABASE_* config.
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from app.etf_completion_acceptance import MAX_AUDIT_ETFS, evaluate_etf_completion
from app.postgres_persistence import PostgresResearchPersistence
from app.settings import Settings


def run_postgres_acceptance(settings: Settings, *, now=None) -> dict:
    if (settings.research_database_backend != "postgres"
            or settings.research_database_schema != "research"):
        raise ValueError("ETF_COMPLETION_REQUIRES_RESEARCH_POSTGRES")
    store = PostgresResearchPersistence(settings)
    try:
        # Set the session default before *any* user/data query below. Readonly
        # remains in force after the adapter commits individual SELECTs.
        store._connection.execute("SET default_transaction_read_only = on")
        store._connection.commit()
        mode = store._connection.execute("SHOW default_transaction_read_only").fetchone()
        if (mode is None or str(next(iter(mode.values()))).lower() != "on"):
            raise ValueError("ETF_COMPLETION_READ_ONLY_NOT_ENFORCED")
        rows = store._connection.execute(
            "SELECT instrument_id, isin, primary_symbol "
            "FROM portfolio.instrument_master "
            "WHERE asset_type='ETF' AND status='ACTIVE' "
            "AND primary_exchange='NSE' "
            "ORDER BY instrument_id LIMIT %s",
            (MAX_AUDIT_ETFS + 1,),
        ).fetchall()
        canonical = [
            {
                "globalInstrumentId": str(row["instrument_id"]),
                "isin": row["isin"], "primarySymbol": row["primary_symbol"],
                "assetType": "ETF", "status": "ACTIVE", "primaryExchange": "NSE",
            } for row in rows
        ]
        report = evaluate_etf_completion(canonical, store, now=now)
        report["database_access"] = "POSTGRES_SESSION_READ_ONLY"
        report["database_source_authenticated"] = False
        return report
    finally:
        store._connection.connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="One-pass, read-only persisted ETF completion acceptance (no acquisition)"
    )
    parser.add_argument("--output", type=Path, help="Optional JSON output file (local report only)")
    args = parser.parse_args(argv)
    report = run_postgres_acceptance(Settings(), now=datetime.now(timezone.utc))
    result = json.dumps(report, indent=2)
    if args.output is None:
        print(result)
    else:
        args.output.write_text(result + "\n", encoding="utf-8")
        print(f"ETF_COMPLETION_REPORT_WRITTEN_TO={args.output}")


if __name__ == "__main__":
    main()
