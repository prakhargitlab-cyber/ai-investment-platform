"""Slice 5: real failure regression matrix (generic fixes; named companies are
fixtures only -- no company-specific production logic).

Evidence: .tmp/radar-deep-readiness-diagnosis.txt and
.tmp/radar-full-issue-audit.txt (live cycle logs, 2026-09-21/22).
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from app.failure_taxonomy import TECHNICAL_RETRYABLE, classify_reason
from app.models import CompanyResearchProfile, DocumentStatus, ReliabilityLevel, SourceClassification, SourceMode, SourceType
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository
from app.research_fetching import FetchError
from app.settings import Settings
from app.source_discovery import OfficialFilingDiscovery

MIGRATIONS = Path(__file__).resolve().parents[3] / "services/research-service/src/main/resources/db/migration"


def _postgres_varchar_limits(table: str) -> dict[str, int]:
    """Column length limits of `table` as declared by the Flyway migrations."""
    limits: dict[str, int] = {}
    for path in sorted(MIGRATIONS.glob("V*.sql"), key=lambda p: int(p.name[1:].split("__")[0])):
        sql = path.read_text(encoding="utf-8")
        block = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", sql, re.S)
        if block:
            for name, size in re.findall(r"^\s*(\w+)\s+VARCHAR\((\d+)\)", block.group(1), re.M):
                limits[name] = int(size)
    return limits


class _VarcharEnforcingConnection:
    """Wraps the SQLite connection and enforces PostgreSQL VARCHAR(n) limits on
    the values actually INSERTed into research_documents -- the same boundary
    at which PostgreSQL raises StringDataRightTruncation."""

    def __init__(self, connection, limits):
        self._inner, self._limits = connection, limits

    def execute(self, sql, params=()):
        match = re.search(r"INSERT INTO research_documents\s*\((.*?)\)\s*VALUES", sql, re.S)
        if match and params:
            columns = [c.strip() for c in match.group(1).split(",")]
            for column, value in zip(columns, params):
                limit = self._limits.get(column)
                if isinstance(value, str) and limit is not None and len(value) > limit:
                    raise RuntimeError(f"value too long for type character varying({limit}) column={column}")
        return self._inner.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __enter__(self):
        return self._inner.__enter__()

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)


class PostgresColumnContract(SqliteResearchPersistence):
    """SQLite store with the production PostgreSQL research_documents VARCHAR
    limits (SQLite TEXT has none, which hid this class of defect)."""

    LIMITS = _postgres_varchar_limits("research_documents")

    def __init__(self):
        super().__init__()
        self._connection = _VarcharEnforcingConnection(self._connection, self.LIMITS)


def _profile():
    return CompanyResearchProfile(
        instrument_id=UUID("0a250fa4-07ee-48a2-91d8-0c75fb9b3799"), company_id=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1"),
        company_name="Belrise Industries Ltd.", isin="INE894V01022", ticker="BELRISE", exchange="NSE", mic="XNSE",
        country="IN", currency="INR", provider_instrument_ids={"NSE": "BELRISE"})


# --- BELRISE: DOCUMENT_PERSIST_FAILED on a long official NSE title -----------
LONG_NSE_TITLE = ("Outcome of Board Meeting " + "Belrise Industries Limited has informed the Exchange regarding the "
                  "outcome of the Board Meeting held on August 14, 2026 which inter alia considered and approved the "
                  "Unaudited Standalone and Consolidated Financial Results for the quarter ended June 30, 2026 along "
                  "with the Limited Review Report issued by the Statutory Auditors, appointment of Secretarial Auditor "
                  "for a term of five consecutive years, re-appointment of Independent Director, approval of related "
                  "party transactions and the schedule of analyst and institutional investor meetings. ")


def test_contract_harness_reads_production_limits():
    from app.persistence import RESEARCH_DOCUMENT_COLUMN_LIMITS
    assert PostgresColumnContract.LIMITS["title"] == 500
    for column, limit in RESEARCH_DOCUMENT_COLUMN_LIMITS.items():
        assert PostgresColumnContract.LIMITS[column] == limit, column
    assert len(LONG_NSE_TITLE) > 500


def test_belrise_long_official_title_persists_within_postgres_limits():
    repo = ResearchRepository(settings=Settings(research_live_enabled=False, research_demo_enabled=False),
                              persistence=PostgresColumnContract())
    profile = _profile()
    document = repo._prepare_ingested_document(
        original_url="https://nsearchives.nseindia.com/corporate/BELRISE_14082026185306_BELRISE_Outcome_of_Board_Meeting_14082026.pdf",
        source_type=SourceType.EXCHANGE_ANNOUNCEMENT, source_name="NSE corporate announcements", publisher="NSE",
        content_type="application/pdf",
        body="[PDF_PAGE 1]\nOutcome of Board Meeting. Unaudited financial results for the quarter ended June 30, 2026.\n",
        reliability=ReliabilityLevel.LEVEL_A, published_at=datetime(2026, 8, 14, tzinfo=timezone.utc),
        source_mode=SourceMode.REAL, source_classification=SourceClassification.EXCHANGE, discovered_at=None,
        discovery_provider="NSE_OFFICIAL_API", expected_profile=None, document_status=DocumentStatus.PARSED,
        allow_empty_content=False, trusted_profile_identity=None)
    document.instrument_id, document.company_id = profile.instrument_id, profile.company_id
    document.title = LONG_NSE_TITLE  # exactly what the trusted-NSE path does with source.official_title
    stored = repo._apply_prepared_ingested_document(document, document_status=DocumentStatus.PARSED)
    assert stored.status == DocumentStatus.PROCESSED
    persisted = repo._persistence.load_documents_by_ids([stored.document_id])
    assert persisted and len(persisted[0].title) <= 500
    assert LONG_NSE_TITLE.startswith(persisted[0].title.rstrip("\u2026").rstrip())


# --- relative NSE URL / no-attachment placeholder ----------------------------
@pytest.mark.asyncio
async def test_relative_nse_archive_path_is_resolved_and_placeholder_is_not_unsafe(caplog):
    profile = _profile()
    rows = [
        {"an_dt": "14-Aug-2026 18:53:06", "desc": "Financial Results", "category": "Financial Results",
         "attchmntText": "Financial results", "attchmntFile": "/corporate/BELRISE_14082026185306_Results.pdf"},
        {"an_dt": "13-Aug-2026 18:53:06", "desc": "Financial Results", "category": "Financial Results",
         "attchmntText": "Financial results", "attchmntFile": "-"},
        {"an_dt": "12-Aug-2026 18:53:06", "desc": "Financial Results", "category": "Financial Results",
         "attchmntText": "Financial results", "attchmntFile": "/relative.pdf"},
        {"an_dt": "11-Aug-2026 18:53:06", "desc": "Financial Results", "category": "Financial Results",
         "attchmntText": "Financial results", "attchmntFile": "https://nsearchives.nseindia.com/corporate/valid.pdf"},
    ]
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=rows, request=request)))
    with caplog.at_level(logging.INFO, logger="app.source_discovery"):
        result = await OfficialFilingDiscovery(client).discover(profile, {"FINANCIAL_RESULTS"}, set())
    urls = [item.source.url for item in result]
    assert "https://nsearchives.nseindia.com/corporate/BELRISE_14082026185306_Results.pdf" in urls
    assert "https://nsearchives.nseindia.com/corporate/valid.pdf" in urls
    assert not any("relative.pdf" in url for url in urls)  # non-archive relative paths stay rejected
    rejected = [r.message for r in caplog.records if "official_candidate_rejected" in r.message]
    assert any("reason=NO_ATTACHMENT" in m for m in rejected)
    assert sum("reason=UNSAFE_URL" in m for m in rejected) == 1


# --- zero-document / zero-query plan: authority cooldown ----------------------
def _authority_repo():
    from test_di15_financial_authority_upgrade import _repo, _facts, _seed
    repo, profile = _repo()
    _seed(repo, _facts(repo))  # fresh secondary facts -> NSE authority upgrade required
    return repo, profile


def test_technical_authority_failure_is_retryable_after_bounded_backoff():
    repo, profile = _authority_repo()
    now = datetime.now(timezone.utc)
    repo._financial_authority_attempts[profile.instrument_id] = now - timedelta(minutes=30)
    asyncio.run(repo.record_acquisition_observation(profile.instrument_id, "QUARTERLY_FINANCIALS", "NSE", "FAILED",
                                                    now - timedelta(minutes=29), failure_reason="PDF_EXTRACTION_TIMEOUT"))
    # A transient failure must not lock the official source out for 3 days.
    assert repo.financial_authority_upgrade_due(profile, now)
    # ... but an immediate re-attempt is still bounded.
    assert not repo.financial_authority_upgrade_due(profile, now - timedelta(minutes=25))


def test_successful_authority_check_cooldown_survives_restart():
    repo, profile = _authority_repo()
    now = datetime.now(timezone.utc)
    asyncio.run(repo.record_acquisition_observation(profile.instrument_id, "QUARTERLY_FINANCIALS", "NSE", "SUCCESS_EMPTY",
                                                    now - timedelta(hours=1)))
    restarted = ResearchRepository(settings=repo.settings, persistence=repo._persistence)
    restarted.profiles = [profile]
    # Fresh process: no in-memory attempt, but the durable check is recent.
    assert not restarted.financial_authority_upgrade_due(profile, now)
    assert restarted.financial_authority_upgrade_due(profile, now + timedelta(days=4))


@pytest.mark.asyncio
async def test_zero_attempt_plan_reports_prior_technical_failure_not_terminal_reason():
    from app.deep_investigation import investigate
    from test_research_readiness_runtime import StateDataSource, RuntimeRepository, UpdatingExecutor, INSTRUMENT_ID
    from app.research_readiness_runtime import ResearchReadinessRuntime

    observations = [{"requirement_id": "QUARTERLY_FINANCIALS", "provider": "NSE", "outcome": "FAILED",
                     "observed_at": datetime.now(timezone.utc).isoformat(), "failure_reason": "PDF_EXTRACTION_TIMEOUT"}]

    class Repo(RuntimeRepository):
        def acquisition_observations_for(self, instrument_id):
            return observations

    source = StateDataSource({"QUARTERLY_FINANCIALS"})
    executor = UpdatingExecutor(source, update_primary=False)
    runtime = ResearchReadinessRuntime(Repo(), source, executor)
    runtime.executor = executor

    async def noop_ensure(*args, **kwargs):  # cooldown: the plan attempts nothing
        from app.research_readiness_runtime import TargetedEnsureResult
        return TargetedEnsureResult(await runtime.read(INSTRUMENT_ID, jurisdiction="INDIA"), (), ())
    runtime.ensure = noop_ensure
    result, _, matrix = await investigate(runtime, INSTRUMENT_ID, jurisdiction="INDIA")
    reason = result.failures.get("QUARTERLY_FINANCIALS")
    assert reason and "PDF_EXTRACTION_TIMEOUT" in reason
    assert classify_reason(reason) == TECHNICAL_RETRYABLE


# --- SKY GOLD / DOCUMENT_BUDGET_EXHAUSTED + PDF_EXTRACTION_TIMEOUT -----------
def test_derivative_newspaper_publication_does_not_precede_primary_results():
    from app.repository import _fair_official_filing_order
    from app.source_discovery import DiscoveryResult
    from test_di11c_official_document_budget import _official_source

    profile = _profile()

    def filing(suffix, published, title):
        from dataclasses import replace as dc_replace
        source = dc_replace(_official_source(profile, suffix, "FINANCIAL_RESULTS"),
                            official_published_at=published, official_title=title)
        return DiscoveryResult("FINANCIAL_RESULTS", source)

    clipping = filing("SKYGOLD_10082026122719_Newspaperpublication", datetime(2026, 8, 10, 12, tzinfo=timezone.utc),
                      "Newspaper Publication Copy of newspaper publication of financial results")
    june = filing("SKYGOLD_08082026_Financial_Results_June2026", datetime(2026, 8, 8, tzinfo=timezone.utc),
                  "Financial Results Unaudited financial results for the quarter ended June 30, 2026")
    march = filing("SKYGOLD_15052026_Financial_Results_March2026", datetime(2026, 5, 15, tzinfo=timezone.utc),
                   "Financial Results Audited financial results for the quarter ended March 31, 2026")
    ordered = [f.source.url.rsplit("/", 1)[1] for f in _fair_official_filing_order([clipping, march, june])]
    assert ordered == ["SKYGOLD_08082026_Financial_Results_June2026.pdf",
                       "SKYGOLD_15052026_Financial_Results_March2026.pdf",
                       "SKYGOLD_10082026122719_Newspaperpublication.pdf"]
