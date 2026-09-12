from __future__ import annotations

import re
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
ENGINE_APP = REPOSITORY_ROOT / "ai" / "research-engine" / "app"
RESET_SQL = REPOSITORY_ROOT / "docs" / "architecture" / "04-safe-research-reset.sql"
V9_MIGRATION = (
    REPOSITORY_ROOT
    / "services"
    / "research-service"
    / "src"
    / "main"
    / "resources"
    / "db"
    / "migration"
    / "V9__remove_legacy_research_refresh_jobs.sql"
)


def _executable_sql(path: Path) -> str:
    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("--")
    )


def test_removed_legacy_orchestrators_have_no_production_import_or_table_caller() -> None:
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in ENGINE_APP.glob("*.py")
    )

    assert not (ENGINE_APP / "refresh_jobs.py").exists()
    assert not (ENGINE_APP / "market_intelligence_research.py").exists()
    assert "ResearchRefreshJobManager" not in sources
    assert "MarketIntelligenceResearchPrefetch" not in sources
    assert "research_refresh_jobs" not in sources


def test_safe_reset_deletes_only_the_reviewed_rebuildable_allowlist() -> None:
    sql = _executable_sql(RESET_SQL)
    deleted_tables = re.findall(
        r"DELETE\s+FROM\s+([a-z_]+\.[a-z0-9_]+)", sql, flags=re.IGNORECASE
    )

    assert set(deleted_tables) == {
        "research.global_stock_rule_engine_results",
        "research.research_event_sources",
        "research.global_shareholding_snapshot_values",
        "research.global_shareholding_snapshots",
        "research.research_events",
        "research.global_financial_facts",
        "research.global_structured_market_snapshots",
        "research.research_documents",
        "research.research_refresh_jobs",
        "research.research_refresh_runs",
    }
    assert len(deleted_tables) == 10
    assert not re.search(r"\bTRUNCATE\b", sql, flags=re.IGNORECASE)
    assert not re.search(r"\bDROP\s+SCHEMA\b", sql, flags=re.IGNORECASE)
    assert "DELETE FROM research.global_market_price_observations" not in sql
    assert "DELETE FROM research.flyway_schema_history_research" not in sql
    assert "DELETE FROM research.irfc_financial_facts_backup_20260902" not in sql
    assert "DELETE FROM portfolio." not in sql


def test_v9_drops_only_the_dead_refresh_job_table() -> None:
    sql = _executable_sql(V9_MIGRATION)
    drops = re.findall(
        r"DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?([a-z_]+\.[a-z0-9_]+)",
        sql,
        flags=re.IGNORECASE,
    )

    assert drops == ["research.research_refresh_jobs"]
    assert "research_refresh_runs" not in sql
