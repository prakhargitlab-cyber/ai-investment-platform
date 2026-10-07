"""Real-PostgreSQL validation of the research Flyway migrations (V1..V17) and
the PostgreSQL-specific SQL added for resumable cycles (checkpoint, lease,
fencing, active slot), bounded document reads and VARCHAR-bounded persistence.

Opt-in, isolated: set AIP_TEST_POSTGRES_ADMIN_DSN to a DISPOSABLE server (e.g.
postgresql://postgres@/postgres?host=/tmp/pg). Each run creates and drops its
own database; it never touches an existing database. H2 cannot execute V13's
PL/pgSQL, so PostgreSQL-specific SQL is validated here against the real dialect.
"""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

psycopg = pytest.importorskip("psycopg")
ADMIN_DSN = os.environ.get("AIP_TEST_POSTGRES_ADMIN_DSN")
pytestmark = pytest.mark.skipif(not ADMIN_DSN, reason="set AIP_TEST_POSTGRES_ADMIN_DSN to a disposable PostgreSQL")
MIGRATIONS = Path(__file__).resolve().parents[3] / "services/research-service/src/main/resources/db/migration"


def _migrations():
    return sorted(MIGRATIONS.glob("V*__*.sql"), key=lambda p: int(p.name[1:].split("__")[0]))


def _apply(conn, files):
    for path in files:
        conn.execute(path.read_text(encoding="utf-8"))
        conn.commit()


@pytest.fixture
def database():
    name = f"aip_research_test_{uuid.uuid4().hex[:10]}"
    admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    admin.execute(f'CREATE DATABASE "{name}"')
    parsed = urlparse(ADMIN_DSN)
    host = parse_qs(parsed.query).get("host", [parsed.hostname or "localhost"])[0]
    info = dict(host=host, port=parsed.port or 5432, user=parsed.username or "postgres",
                password=parsed.password or "", dbname=name)
    try:
        yield info
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def _connect(info):
    conn = psycopg.connect(**info)
    conn.execute("CREATE SCHEMA IF NOT EXISTS research")
    conn.execute("SET search_path TO research")
    conn.commit()
    return conn


def _settings(info):
    from app.settings import Settings
    return Settings(research_database_backend="postgres", research_database_host=info["host"],
                    research_database_port=info["port"], research_database_user=info["user"],
                    research_database_password=info["password"] or None, research_database_name=info["dbname"],
                    research_database_schema="research")


def _store(info):
    from app.postgres_persistence import PostgresResearchPersistence
    return PostgresResearchPersistence(_settings(info))


def test_v1_to_v16_apply_and_engine_schema_check_passes(database):
    conn = _connect(database)
    files = _migrations()
    assert files[-1].name.startswith("V17__")
    _apply(conn, files)
    tables = {r[0] for r in conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='research'").fetchall()}
    assert {"global_opportunity_cycle_run", "global_opportunity_cycle_progress", "global_opportunity_cycle_active"} <= tables
    _store(database)  # the engine's startup schema assertion (incl. V17) passes


def test_v10_upgrade_to_v16_preserves_existing_rows(database):
    conn = _connect(database)
    files = _migrations()
    upto_v10 = [f for f in files if int(f.name[1:].split("__")[0]) <= 10]
    rest = [f for f in files if int(f.name[1:].split("__")[0]) > 10]
    _apply(conn, upto_v10)
    conn.execute("""INSERT INTO global_market_price_observations
        (instrument_id, observed_at, price, provider, source_url, retrieved_at)
        VALUES ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', now(), 100, 'EXISTING', 'https://example.test', now())""")
    conn.commit()
    _apply(conn, rest)
    assert len(rest) == 7  # V11..V17 (the Java upgrade test's stale "3" predates V14..V17)
    assert conn.execute("SELECT COUNT(*) FROM global_market_price_observations").fetchone()[0] == 1


def test_checkpoint_lease_fencing_and_active_slot_on_postgres(database):
    from app.cycle_checkpoint import CandidateState, CycleOwnershipLost
    _apply(_connect(database), _migrations())
    pod_a, pod_b = _store(database), _store(database)  # two replicas, two connections
    run, created = pod_a.create_cycle_run("c1", {"top_n": 4})
    again, created_b = pod_b.create_cycle_run("c2", {"top_n": 4})
    assert created and not created_b and again["cycle_id"] == "c1"
    assert pod_a._connection.execute("SELECT COUNT(*) AS n FROM global_opportunity_cycle_run").fetchone()["n"] == 1
    pod_a._connection.commit()
    assert pod_a.claim_cycle_run("c1", "A", lease_seconds=60)
    assert not pod_b.claim_cycle_run("c1", "B", lease_seconds=60)            # live lease is exclusive
    assert pod_a.renew_cycle_lease("c1", "A", lease_seconds=60)
    pod_a.record_cycle_progress("c1", "A", "DEEP", "x", CandidateState.IN_PROGRESS, start_attempt=True)
    pod_a.record_cycle_progress("c1", "A", "DEEP", "x", CandidateState.COMPLETED, disposition="ANALYZED", payload={"a": 1})
    pod_a.record_cycle_progress("c1", "A", "DEEP", "x", CandidateState.COMPLETED, disposition="ANALYZED", payload={"a": 1})
    row = pod_b.cycle_progress("c1")[("DEEP", "x")]
    assert row["state"] == "COMPLETED" and row["attempts"] == 1 and row["payload"] == {"a": 1}  # idempotent
    with pytest.raises(CycleOwnershipLost):
        pod_b.record_cycle_progress("c1", "B", "DEEP", "y", CandidateState.IN_PROGRESS)          # fenced
    with pytest.raises(CycleOwnershipLost):
        pod_b.update_cycle_run("c1", "B", selection={"deep_ids": []})
    pod_a.update_cycle_run("c1", "A", selection={"deep_ids": ["x"]}, as_of="2026-09-22T00:00:00+00:00")
    assert pod_b.cycle_run("c1")["selection"] == {"deep_ids": ["x"]}
    # expired lease -> takeover with resume_count, and the old owner is fenced out
    past = datetime.now(timezone.utc) - timedelta(minutes=10)
    pod_a._connection.execute("UPDATE global_opportunity_cycle_run SET lease_expires_at=%s WHERE cycle_id='c1'",
                              ((past).isoformat(timespec="microseconds"),))
    pod_a._connection.commit()
    assert pod_b.claim_cycle_run("c1", "B", lease_seconds=60)
    assert pod_b.cycle_run("c1")["resume_count"] == 1
    with pytest.raises(CycleOwnershipLost):
        pod_a.record_cycle_progress("c1", "A", "DEEP", "z", CandidateState.IN_PROGRESS)
    # fenced, exactly-once publication in the publish transaction
    with pytest.raises(CycleOwnershipLost):
        pod_a.publish_opportunity_cycle([], [], [], {"cycle_id": "c1", "generated_at": "2026-09-22T00:00:00+00:00",
                                                    "market": "NSE"}, fence=("c1", "A"))
    assert pod_b.cycle_published_selection("c1") is None                    # rolled back
    pod_b.publish_opportunity_cycle([], [], [], {"cycle_id": "c1", "generated_at": "2026-09-22T00:00:00+00:00",
                                                "market": "NSE"}, fence=("c1", "B"))
    assert pod_b.cycle_run("c1")["status"] == "PUBLISHED" and pod_a.cycle_published_selection("c1")["cycle_id"] == "c1"
    with pytest.raises(CycleOwnershipLost):  # second publication is refused (status no longer RUNNING)
        pod_b.publish_opportunity_cycle([], [], [], {"cycle_id": "c1", "generated_at": "2026-09-22T00:00:01+00:00",
                                                    "market": "NSE"}, fence=("c1", "B"))
    pod_b.update_cycle_run("c1", "B", status="COMPLETED")
    assert pod_a.active_cycle_run() is None
    assert pod_a.create_cycle_run("c3", {})[1] is True                      # slot freed
    pod_a.fail_unowned_cycle_run("c3", "TEST")
    assert pod_a.active_cycle_run() is None


def test_document_sql_and_postgres_varchar_bound(database):
    from app.models import DocumentStatus, ReliabilityLevel, SourceClassification, SourceMode, SourceType
    from app.repository import ResearchRepository
    from app.settings import Settings
    _apply(_connect(database), _migrations())
    store = _store(database)
    repo = ResearchRepository(settings=Settings(research_live_enabled=False, research_demo_enabled=False), persistence=store)
    doc = repo._prepare_ingested_document(
        original_url="https://nsearchives.nseindia.com/corporate/PG_Outcome_of_Board_Meeting.pdf",
        source_type=SourceType.EXCHANGE_ANNOUNCEMENT, source_name="NSE corporate announcements", publisher="NSE",
        content_type="application/pdf", body="[PDF_PAGE 1]\nOutcome of board meeting; unaudited results approved.\n",
        reliability=ReliabilityLevel.LEVEL_A, published_at=None, source_mode=SourceMode.REAL,
        source_classification=SourceClassification.EXCHANGE, discovered_at=None, discovery_provider="NSE_OFFICIAL_API",
        expected_profile=None, document_status=DocumentStatus.PARSED, allow_empty_content=False, trusted_profile_identity=None)
    doc.instrument_id = uuid.UUID(int=7)
    doc.title = "Outcome of Board Meeting " + "x" * 700                     # real VARCHAR(500) column
    stored = repo._apply_prepared_ingested_document(doc, document_status=DocumentStatus.PARSED)
    assert stored.status == DocumentStatus.PROCESSED
    loaded = store.load_documents_by_ids([doc.document_id])
    assert len(loaded) == 1 and len(loaded[0].title) <= 500
    identities = store.load_document_identities()
    assert identities[0][0] == doc.document_id and identities[0][1] == uuid.UUID(int=7)
    assert repo.document_count_for(uuid.UUID(int=7)) == 1


def test_acquisition_observation_cooldown_read_on_postgres(database):
    _apply(_connect(database), _migrations())
    store = _store(database)
    now = datetime.now(timezone.utc)
    store.upsert_acquisition_observation(uuid.UUID(int=9), "QUARTERLY_FINANCIALS", "NSE", "FAILED", now, None,
                                         failure_reason="PDF_EXTRACTION_TIMEOUT")
    rows = store.load_acquisition_observations(uuid.UUID(int=9))
    assert rows[0]["outcome"] == "FAILED" and rows[0]["failure_reason"] == "PDF_EXTRACTION_TIMEOUT"
    from app.repository import _aware_observation_time
    assert _aware_observation_time(rows[0]) is not None


def test_event_refresh_aggregate_on_postgres(database):
    _apply(_connect(database), _migrations())
    store = _store(database)
    assert store.load_event_refresh_times() == []
    assert store.load_events({uuid.UUID(int=1)}) == []


# --- V17: research_documents.{canonical_url,original_url,source_url} widened
# from VARCHAR(1000) to TEXT (StringDataRightTruncation fix). URLs are never
# truncated by design: truncation would change resource identity and break
# provenance / deduplication, so the fix is schema-only, not a bound applied
# in code. ---------------------------------------------------------------

def _url_of_length(label: str, length: int) -> str:
    base = f"https://news.example.test/{label}?q="
    assert length > len(base)
    return base + ("a" * (length - len(base)))


def test_v1_to_v17_url_columns_become_text(database):
    conn = _connect(database)
    files = _migrations()
    assert files[-1].name.startswith("V17__")
    _apply(conn, files)
    doc_rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema='research' AND table_name='research_documents' "
        "AND column_name IN ('canonical_url','original_url','source_url')"
    ).fetchall()
    doc_types = {r[0]: r[1] for r in doc_rows}
    assert doc_types == {"canonical_url": "text", "original_url": "text", "source_url": "text"}
    event_rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema='research' AND table_name='research_events' AND column_name = 'source_url'"
    ).fetchall()
    assert {r[0]: r[1] for r in event_rows} == {"source_url": "text"}
    event_source_rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema='research' AND table_name='research_event_sources' "
        "AND column_name IN ('source_url','canonical_url')"
    ).fetchall()
    assert {r[0]: r[1] for r in event_source_rows} == {"source_url": "text", "canonical_url": "text"}
    # The uniqueness/dedup index and NOT NULL semantics survive the widening,
    # on research_documents, research_events, and research_event_sources.
    doc_indexes = {r[0] for r in conn.execute(
        "SELECT indexname FROM pg_indexes WHERE schemaname='research' AND tablename='research_documents'").fetchall()}
    assert "ux_research_documents_canonical_url" in doc_indexes
    event_indexes = {r[0] for r in conn.execute(
        "SELECT indexname FROM pg_indexes WHERE schemaname='research' AND tablename='research_events'").fetchall()}
    assert {"ux_research_events_fingerprint", "idx_research_events_instrument_id",
            "idx_research_events_company_id", "idx_research_events_type"} <= event_indexes
    fk = conn.execute(
        "SELECT conname FROM pg_constraint WHERE conrelid = 'research.research_events'::regclass AND contype = 'f'"
    ).fetchall()
    assert {r[0] for r in fk} == {"fk_research_events_document"}
    event_source_fk = conn.execute(
        "SELECT conname FROM pg_constraint WHERE conrelid = 'research.research_event_sources'::regclass AND contype = 'f'"
    ).fetchall()
    assert {r[0] for r in event_source_fk} == {"fk_research_event_sources_event", "fk_research_event_sources_document"}
    event_source_unique = conn.execute(
        "SELECT conname FROM pg_constraint WHERE conrelid = 'research.research_event_sources'::regclass AND contype = 'u'"
    ).fetchall()
    assert {r[0] for r in event_source_unique} == {"ux_research_event_sources_event_document_excerpt"}
    not_null = conn.execute(
        "SELECT column_name, is_nullable FROM information_schema.columns WHERE table_schema='research' "
        "AND ((table_name='research_events' AND column_name='source_url') "
        "OR (table_name='research_event_sources' AND column_name IN ('source_url','canonical_url')))"
    ).fetchall()
    assert {r[0]: r[1] for r in not_null} == {"source_url": "NO", "canonical_url": "NO"}  # survives the TEXT widening
    _store(database)  # the engine's startup schema assertion (incl. V17) passes


def test_v16_upgrade_to_v17_preserves_existing_document_url_value(database):
    conn = _connect(database)
    files = _migrations()
    upto_v16 = [f for f in files if int(f.name[1:].split("__")[0]) <= 16]
    rest = [f for f in files if int(f.name[1:].split("__")[0]) > 16]
    _apply(conn, upto_v16)
    # Longest value the pre-migration VARCHAR(1000) columns could legally hold.
    existing_url = _url_of_length("pre-existing-doc", 1000)
    existing_event_url = _url_of_length("pre-existing-event", 1000)
    doc_id = str(uuid.uuid4())
    doc_content_hash = hashlib.sha256(existing_url.encode("utf-8")).hexdigest()
    conn.execute(
        """INSERT INTO research_documents (
            document_id, source_type, source_classification, source_name, source_url,
            canonical_url, original_url, document_type, retrieved_at, content_type,
            content_hash, source_mode, freshness, reliability_level, status,
            entity_resolution_confidence
        ) VALUES (%s,'NEWS','REPUTABLE_NEWS','Existing Wire',%s,%s,%s,'HTML',now(),'text/html',
                  %s,'REAL','REAL','LEVEL_B','PROCESSED',0.9)""",
        (doc_id, existing_url, existing_url, existing_url, doc_content_hash),
    )
    event_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO research_events (
            event_id, instrument_id, company_id, event_fingerprint, event_type, detected_at,
            title, summary, impact, time_horizon, confidence, status, source_document_id,
            source_url, source_type, source_classification, reliability, source_mode,
            raw_evidence_reference
        ) VALUES (%s,%s,%s,%s,'OTHER',now(),'Existing event','Existing summary','UNCERTAIN','UNKNOWN',
                  0.8,'VALIDATED',%s,%s,'NEWS','REPUTABLE_NEWS','LEVEL_B','REAL','Existing evidence')""",
        (event_id, str(uuid.uuid4()), str(uuid.uuid4()), f"pre-existing-fingerprint-{event_id}", doc_id, existing_event_url),
    )
    conn.commit()
    _apply(conn, rest)
    doc_row = conn.execute(
        "SELECT canonical_url, original_url, source_url FROM research_documents WHERE document_id = %s", (doc_id,)
    ).fetchone()
    assert doc_row[0] == existing_url
    assert doc_row[1] == existing_url
    assert doc_row[2] == existing_url
    assert len(doc_row[0]) == 1000
    event_row = conn.execute(
        "SELECT source_url FROM research_events WHERE event_id = %s", (event_id,)
    ).fetchone()
    assert event_row[0] == existing_event_url
    assert len(event_row[0]) == 1000


@pytest.mark.parametrize("label,length", [
    ("exactly-1000", 1000),
    ("just-over-1000", 1001),
    ("well-over-2048", 2600),
])
def test_document_url_round_trips_exactly_at_boundary_lengths(database, label, length):
    # Confirmed defect: PostgreSQL previously raised
    # psycopg.errors.StringDataRightTruncation (SQLSTATE 22001) once the
    # canonicalized URL exceeded the old VARCHAR(1000) columns. Proves the
    # complete URL -- not a truncated prefix -- round-trips through Postgres
    # for values at, just past, and well past that old boundary.
    from app.models import DocumentType, ReliabilityLevel, ResearchDocument, SourceClassification, SourceMode, SourceType
    _apply(_connect(database), _migrations())
    store = _store(database)
    url = _url_of_length(label, length)
    assert len(url) == length
    doc = ResearchDocument(
        canonical_url=url, original_url=url, title="Headline",
        source_type=SourceType.NEWS, source_classification=SourceClassification.REPUTABLE_NEWS,
        source_name="Wire Service", content_type="text/html", document_type=DocumentType.HTML,
        content_hash=hashlib.sha256(url.encode("utf-8")).hexdigest(),
        reliability_level=ReliabilityLevel.LEVEL_B, source_mode=SourceMode.REAL, freshness="REAL",
    )
    inserted = store.upsert_document(doc)
    assert inserted is True
    loaded = store.load_documents_by_ids([doc.document_id])
    assert len(loaded) == 1
    assert loaded[0].canonical_url == url
    assert loaded[0].original_url == url
    assert len(loaded[0].canonical_url) == length


@pytest.mark.parametrize("label,length", [
    ("event-exactly-1000", 1000),
    ("event-just-over-1000", 1001),
    ("event-well-over-2048", 2600),
])
def test_event_source_url_round_trips_exactly_at_boundary_lengths(database, label, length):
    # Same confirmed defect, on research_events.source_url (also VARCHAR(1000)
    # pre-V17): proves the complete URL round-trips through Postgres for
    # values at, just past, and well past the old boundary.
    from app.models import (
        DocumentType, EventImpact, ReliabilityLevel, ResearchDocument, ResearchEvent,
        SourceClassification, SourceMode, SourceType, TimeHorizon,
    )
    _apply(_connect(database), _migrations())
    store = _store(database)
    url = _url_of_length(label, length)
    assert len(url) == length
    doc = ResearchDocument(
        canonical_url=url, original_url=url, title="Headline",
        source_type=SourceType.NEWS, source_classification=SourceClassification.REPUTABLE_NEWS,
        source_name="Wire Service", content_type="text/html", document_type=DocumentType.HTML,
        content_hash=hashlib.sha256(url.encode("utf-8")).hexdigest(),
        reliability_level=ReliabilityLevel.LEVEL_B, source_mode=SourceMode.REAL, freshness="REAL",
    )
    assert store.upsert_document(doc) is True
    event = ResearchEvent(
        instrument_id=uuid.uuid4(), company_id=uuid.uuid4(), event_type="OTHER",
        title="Headline event", summary="Summary.",
        source_document_id=doc.document_id, source_url=url, source_type=SourceType.NEWS,
        source_classification=SourceClassification.REPUTABLE_NEWS, reliability=ReliabilityLevel.LEVEL_B,
        source_mode=SourceMode.REAL, confidence=0.8, impact=EventImpact.UNCERTAIN,
        time_horizon=TimeHorizon.UNKNOWN, raw_evidence_reference="Headline event",
    )
    inserted = store.upsert_event(event)                              # must not raise StringDataRightTruncation
    assert inserted is True
    loaded = store.load_events({event.instrument_id})
    assert len(loaded) == 1
    assert loaded[0].source_url == url
    assert len(loaded[0].source_url) == length


def test_canonical_url_dedup_still_works_for_long_urls(database):
    # Uniqueness/dedup semantics (ux_research_documents_canonical_url) must
    # keep working after the VARCHAR(1000) -> TEXT widening: the unique index
    # still rejects a second row with the same long canonical_url, and two
    # distinct long URLs both persist as distinct rows.
    #
    # Checked at the SQL/index level directly (INSERT ... and assert on the
    # resulting row count / UniqueViolation), independent of
    # ResearchPersistence.upsert_document()'s own duplicate-detection branch,
    # which has its own dedicated regression test:
    # test_upsert_document_duplicate_branch_reuses_existing_identity_on_postgres.
    _apply(_connect(database), _migrations())
    conn = _connect(database)
    url_a = _url_of_length("dup-a", 1600)
    url_b = _url_of_length("dup-b", 1600)

    def insert(url: str, content_hash: str):
        conn.execute(
            """INSERT INTO research_documents (
                document_id, source_type, source_classification, source_name, source_url,
                canonical_url, original_url, document_type, retrieved_at, content_type,
                content_hash, source_mode, freshness, reliability_level, status,
                entity_resolution_confidence
            ) VALUES (%s,'NEWS','REPUTABLE_NEWS','Wire Service',%s,%s,%s,'HTML',now(),'text/html',
                      %s,'REAL','REAL','LEVEL_B','PROCESSED',0.9)""",
            (str(uuid.uuid4()), url, url, url, content_hash),
        )

    insert(url_a, hashlib.sha256(b"first").hexdigest())
    conn.commit()

    with pytest.raises(psycopg.errors.UniqueViolation):
        insert(url_a, hashlib.sha256(b"second-different-hash").hexdigest())  # same long URL -> rejected
    conn.rollback()

    insert(url_b, hashlib.sha256(b"third").hexdigest())                     # different long URL -> allowed
    conn.commit()

    rows = conn.execute("SELECT canonical_url FROM research_documents ORDER BY canonical_url").fetchall()
    assert [r[0] for r in rows] == sorted([url_a, url_b])


@pytest.mark.asyncio
async def test_current_news_persists_long_discovered_url_without_document_persist_failure(database):
    # CURRENT_NEWS regression for the confirmed DEV defect: a provider that
    # discovers a URL over 1000 characters must not blow up DOCUMENT_PERSIST
    # with StringDataRightTruncation, and the run must not degrade to
    # SEARCH_PARTIAL because of it.
    from types import SimpleNamespace
    from app.news_acquisition import acquire_news
    from app.source_discovery import CandidateSearchResult

    _apply(_connect(database), _migrations())
    store = _store(database)

    class Provider:
        provider_name = "web"

        def __init__(self, results):
            self.results = results
            self.last_query_degraded = False

        async def discover(self, company, category, window):
            return list(self.results)

    class Repository:
        def __init__(self, persistence):
            self.settings = SimpleNamespace(market_data_population_request_interval_seconds=0)
            self._persistence = persistence
            self.documents = {}
            self.fetches = []
            self._fetcher = SimpleNamespace(fetch=self.fetch)

        def documents_for(self, *a, **k):
            return list(self.documents.values())

        def remember_persisted_document(self, d):
            self.documents[d.document_id] = d

        def structured_market_snapshots_for(self, keys):
            return {k: [] for k in keys}

        async def append_news_record(self, value):
            return self._persistence.append_news_record(value)

        async def _run_blocking_persistence(self, fn, *args):
            return fn(*args)

        async def fetch(self, url):
            self.fetches.append(url)
            return SimpleNamespace(
                text="<html><title>Board approves buyback</title><body>"
                     "The board approved a capacity expansion plan.</body></html>",
                content_type="text/html", final_url=url,
            )

    long_url = "https://aggregator.example.test/redirect?target=" + ("b" * 1200)
    long_candidate = CandidateSearchResult(
        "Board approves buyback", long_url, "snippet is not evidence",
        datetime.now(timezone.utc), "web", "id", "buyback", "CURRENT_NEWS",
    )

    from test_research_readiness_runtime import _profile
    company = _profile(uuid.uuid4()).model_copy(update={"company_id": uuid.uuid4(), "company_name": "Long URL Ltd", "ticker": "LURL"})

    async def no_sleep(seconds):
        pass

    repo = Repository(store)
    provider = Provider(results=[long_candidate])
    run, _ = await acquire_news(repo, company, providers=[provider], now=datetime.now(timezone.utc),
                                sleep=no_sleep)

    assert run.outcome != "SEARCH_PARTIAL"
    assert all("DOCUMENT_PERSIST_FAILED" not in (p.failure_code or "") for p in run.providers)
    persisted = store.load_documents_by_ids([d.document_id for d in repo.documents.values()])
    assert len(persisted) == 1
    assert persisted[0].canonical_url == long_url
    assert len(persisted[0].canonical_url) > 1000


def test_upsert_document_duplicate_branch_reuses_existing_identity_on_postgres(database):
    # Confirmed defect: PostgresResearchPersistence.upsert_document()'s
    # duplicate-lookup branch did `UUID(existing["document_id"])`, which
    # raised AttributeError against real Postgres because psycopg3 adapts
    # the `uuid` column type to a native uuid.UUID -- UUID(...) only accepts
    # a string/hex form. Proves: (a) a document persists, (b) persisting the
    # same canonical source again is recognized as a duplicate rather than
    # raising, (c)/(d) the duplicate branch is actually exercised and raises
    # nothing, (e) the original durable document_id is reused, not replaced,
    # and (f) no second row is created (canonical_url uniqueness untouched).
    from app.models import DocumentType, ReliabilityLevel, ResearchDocument, SourceClassification, SourceMode, SourceType
    _apply(_connect(database), _migrations())
    store = _store(database)

    def make(url: str, content_hash_value: str) -> "ResearchDocument":
        return ResearchDocument(
            canonical_url=url, original_url=url, title="Headline",
            source_type=SourceType.NEWS, source_classification=SourceClassification.REPUTABLE_NEWS,
            source_name="Wire Service", content_type="text/html", document_type=DocumentType.HTML,
            content_hash=content_hash_value,
            reliability_level=ReliabilityLevel.LEVEL_B, source_mode=SourceMode.REAL, freshness="REAL",
        )

    url = "https://news.example.test/duplicate-branch-regression"
    first = make(url, hashlib.sha256(b"first-body").hexdigest())
    assert store.upsert_document(first) is True                       # (a) persists

    duplicate = make(url, hashlib.sha256(b"a-different-body-same-url").hexdigest())
    inserted = store.upsert_document(duplicate)                       # (b)+(c)+(d): no exception raised here
    assert inserted is False                                          # recognized as a duplicate, not a fresh insert
    assert duplicate.duplicate_of_document_id == first.document_id    # (e) original identity reused, exactly
    assert isinstance(duplicate.duplicate_of_document_id, uuid.UUID)

    rows = store.load_documents_by_ids([first.document_id, duplicate.document_id])
    assert len(rows) == 1 and rows[0].document_id == first.document_id  # (f) no duplicate row created

    conn = _connect(database)
    count = conn.execute("SELECT COUNT(*) FROM research_documents WHERE canonical_url = %s", (url,)).fetchone()[0]
    assert count == 1


def test_event_independence_key_hash_persists_without_truncation_on_postgres(database):
    # research_events.independence_key is CHAR(64), same as
    # research_documents.source_independence_key. Confirms the deterministic
    # SHA-256 hex identity used for Yahoo-MCP-sourced ResearchEvent.independence_key
    # persists cleanly against real Postgres, without StringDataRightTruncation
    # -- the same class of failure V17 fixes for the URL columns, now proven
    # for this fixed-width identity column too.
    #
    # research_events.source_url is a separate VARCHAR(1000) column on a
    # different table, out of scope for this fix (only independence_key was
    # confirmed as a defect here), so it is kept at a normal length; only the
    # *conceptual* URL fed into content_hash() to build independence_key is
    # long, proving the identity computation itself never needs the long
    # value to be stored verbatim anywhere for the hash to be safe to persist.
    from app.models import (
        DocumentType, EventImpact, ReliabilityLevel, ResearchDocument, ResearchEvent,
        SourceClassification, SourceMode, SourceType, TimeHorizon,
    )
    from app.normalization import content_hash
    _apply(_connect(database), _migrations())
    store = _store(database)

    event_source_url = "https://news.example.test/postgres-event-identity/short"
    long_conceptual_url = "https://news.example.test/postgres-event-identity/" + ("k" * 2100)
    assert len(long_conceptual_url) > 2000
    doc = ResearchDocument(
        canonical_url=event_source_url, original_url=event_source_url, title="Headline",
        source_type=SourceType.NEWS, source_classification=SourceClassification.REPUTABLE_NEWS,
        source_name="Wire Service", content_type="text/html", document_type=DocumentType.HTML,
        content_hash=hashlib.sha256(event_source_url.encode("utf-8")).hexdigest(),
        reliability_level=ReliabilityLevel.LEVEL_B, source_mode=SourceMode.REAL, freshness="REAL",
    )
    assert store.upsert_document(doc) is True

    independence_key = content_hash(f"YAHOO_FINANCE_MCP:{long_conceptual_url.casefold()}")
    assert len(independence_key) == 64
    event = ResearchEvent(
        instrument_id=uuid.uuid4(), company_id=uuid.uuid4(), event_type="OTHER",
        title="Board approves capacity expansion", summary="Summary.",
        source_document_id=doc.document_id, source_url=event_source_url, source_type=SourceType.NEWS,
        source_classification=SourceClassification.REPUTABLE_NEWS, reliability=ReliabilityLevel.LEVEL_B,
        source_mode=SourceMode.REAL, confidence=0.8, impact=EventImpact.UNCERTAIN,
        time_horizon=TimeHorizon.UNKNOWN, raw_evidence_reference="Board approves capacity expansion",
        independence_key=independence_key,
    )
    inserted = store.upsert_event(event)                              # must not raise StringDataRightTruncation
    assert inserted is True

    loaded = store.load_events({event.instrument_id})
    assert len(loaded) == 1
    assert loaded[0].independence_key == independence_key
    assert len(loaded[0].independence_key) == 64


def test_yahoo_mcp_event_with_long_url_persists_through_production_path_on_postgres(database):
    # End-to-end regression through the ACTUAL production persistence path
    # (YahooMcpResultPersister -> ResearchRepository.persist_external_mcp_evidence_async
    # -> ResearchPersistence.upsert_document / upsert_event against real
    # Postgres), not a direct store call. Proves, for a >2000-char article
    # URL: (1) research_events.source_url round-trips the complete string,
    # (2) independence_key stays exactly 64 lowercase hex chars, (3) no
    # StringDataRightTruncation, and (4) source classification remains
    # APPROVED_EXTERNAL_TOOL.
    import asyncio
    from app.models import CompanyResearchProfile
    from app.normalization import content_hash
    from app.repository import ResearchRepository
    from app.research_readiness import ResearchSourceTier
    from app.research_readiness_runtime import _event_source
    from app.settings import Settings
    from app.yahoo_mcp_acquisition import YahooMcpNormalizedResult, YahooMcpResultPersister

    _apply(_connect(database), _migrations())
    store = _store(database)
    repo = ResearchRepository(settings=Settings(research_live_enabled=False, research_demo_enabled=False), persistence=store)

    instrument_id = uuid.uuid4()
    profile = CompanyResearchProfile(
        instrument_id=instrument_id, company_id=uuid.uuid4(), company_name="Long URL Regression Ltd",
        ticker="LURL", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        provider_instrument_ids={"YAHOO_FINANCE": "LURL.NS"},
    )
    long_url = "https://aggregator.example.test/redirect?target=" + ("m" * 2100)
    assert len(long_url) > 2048
    now = datetime.now(timezone.utc)

    normalized = YahooMcpNormalizedResult.model_validate({
        "adapterVersion": "YAHOO_FINANCE_MCP_ADAPTER_V1", "providerId": "YAHOO_FINANCE_MCP",
        "sourceTier": "APPROVED_EXTERNAL_TOOL", "sourceTool": "get_quote", "region": "INDIA",
        "requirementId": "CURRENT_NEWS", "globalInstrumentId": str(instrument_id), "symbol": "LURL.NS",
        "exchange": "NSE", "currency": "INR", "retrievedAt": now.isoformat(), "observedAt": now.isoformat(),
        "sourceUrl": "https://finance.yahoo.com/quote/LURL.NS", "confidence": 0.8, "freshness": "FRESH",
        "structuredFacts": [], "financialFacts": [], "marketObservations": [], "companyProfile": None,
        "news": [{
            "headline": "Board approves capacity expansion", "url": long_url, "publishedAt": now.isoformat(),
            "publisher": "Example Wire", "issuerSymbol": "LURL.NS",
            "summary": "The board approved a capacity expansion plan.",
        }],
        "events": [], "shareholding": None,
    })

    asyncio.run(YahooMcpResultPersister(repo).persist(normalized, profile))

    loaded_docs = store.load_documents_by_ids(list(repo.documents.keys()))
    assert len(loaded_docs) == 1
    assert loaded_docs[0].canonical_url == long_url                       # (1) document URL round-trips too

    loaded_events = store.load_events({instrument_id})
    assert len(loaded_events) == 1
    event = loaded_events[0]
    assert event.source_url == long_url                                  # (1) event source_url round-trips completely
    assert len(event.source_url) == len(long_url)

    key = event.independence_key
    assert key is not None and len(key) == 64                            # (2) exactly 64 chars
    assert key == key.lower() and all(c in "0123456789abcdef" for c in key)
    assert key == content_hash(f"YAHOO_FINANCE_MCP:{long_url.casefold()}")

    source, tier = _event_source(event, "CURRENT_NEWS")                  # (4) classification unaffected
    assert source == "APPROVED_EXTERNAL_TOOL"
    assert tier == ResearchSourceTier.APPROVED_EXTERNAL_TOOL
    # (3) no StringDataRightTruncation was raised anywhere above -- the test
    # would have failed with that exception rather than completing.



# ---------------------------------------------------------------------------
# Runtime defect closure: global_manual_evidence migration (Defect B) and
# PostgreSQL transaction-poisoning recovery (Defect C).
#
# A real browser upload against a real deployed PostgreSQL-backed
# research-engine failed with:
#   psycopg.errors.UndefinedTable: relation "global_manual_evidence" does
#   not exist
# via create_evidence_draft -> manual_evidence_ingestor.ingest ->
# _find_duplicate -> load_manual_evidence_drafts -> SELECT * FROM
# global_manual_evidence. No Flyway migration in
# services/research-service/.../db/migration ever created this table --
# app/persistence.py's SQLite dev/test backend self-creates it inline
# (CREATE TABLE IF NOT EXISTS), which masked the gap until a real
# PostgreSQL-backed request hit it. V22__global_manual_evidence.sql fixes
# this with the smallest correct migration, following the exact column
# shapes already used by the SQLite schema and this repository's existing
# PostgreSQL type conventions (see V2/V20 for precedent).
#
# A second, independent failure followed in the same real runtime:
#   psycopg.errors.InFailedSqlTransaction: current transaction is aborted,
#   commands ignored until end of transaction block
# surfacing in UNRELATED background code (opportunity_worker ->
# _maybe_take_over -> active_cycle_run), because
# load_manual_evidence_drafts() issued its SELECT directly on the shared
# _PostgresConnectionAdapter without a `with self._connection:` block, so
# nothing rolled back the aborted transaction after UndefinedTable --
# poisoning the one shared psycopg connection (OpportunityPersistenceMixin
# and the manual-evidence persistence methods are mixed into the same
# PostgresResearchPersistence instance, i.e. the same connection) for every
# later caller. The fix moves the rollback-on-failure into
# _PostgresConnectionAdapter.execute() itself, so it protects every call
# site -- wrapped in `with self._connection:` or not -- without suppressing
# the original exception or reconnecting.
# ---------------------------------------------------------------------------


def test_v22_global_manual_evidence_migration_creates_expected_schema(database):
    conn = _connect(database)
    _apply(conn, _migrations())

    tables = {r[0] for r in conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='research'").fetchall()}
    assert "global_manual_evidence" in tables

    columns = {
        r[0]: r[1] for r in conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema='research' AND table_name='global_manual_evidence'"
        ).fetchall()
    }
    assert columns == {
        "draft_id": "uuid",
        "evidence_type": "character varying",
        "content_hash": "character",
        "original_filename": "character varying",
        "content_type": "character varying",
        "instrument_id": "uuid",
        "reporting_period": "timestamp with time zone",
        "extraction_method": "character varying",
        "extraction_results": "text",
        "validation_results": "text",
        "corrections": "text",
        "accepted_at": "timestamp with time zone",
    }

    indexes = {r[0] for r in conn.execute(
        "SELECT indexname FROM pg_indexes WHERE schemaname='research' AND tablename='global_manual_evidence'"
    ).fetchall()}
    assert {"idx_global_manual_evidence_instrument", "idx_global_manual_evidence_content_hash"} <= indexes

    # The engine's own startup schema assertion (now including
    # global_manual_evidence) must pass against the fully migrated schema.
    _store(database)


def test_global_manual_evidence_unique_constraint_rejects_duplicate_content_hash_and_type(database):
    conn = _connect(database)
    _apply(conn, _migrations())
    row = dict(
        draft_id=str(uuid.uuid4()), evidence_type="SHAREHOLDING", content_hash="a" * 64,
        original_filename="snip.png", content_type="image/png", instrument_id=None,
        reporting_period=None, extraction_method="PNG_TEXT", extraction_results="{}",
        validation_results="{}", corrections="{}", accepted_at=datetime.now(timezone.utc),
    )
    conn.execute(
        """INSERT INTO global_manual_evidence (
            draft_id, evidence_type, content_hash, original_filename, content_type,
            instrument_id, reporting_period, extraction_method, extraction_results,
            validation_results, corrections, accepted_at
        ) VALUES (%(draft_id)s, %(evidence_type)s, %(content_hash)s, %(original_filename)s, %(content_type)s,
                  %(instrument_id)s, %(reporting_period)s, %(extraction_method)s, %(extraction_results)s,
                  %(validation_results)s, %(corrections)s, %(accepted_at)s)""",
        row,
    )
    conn.commit()
    duplicate = dict(row, draft_id=str(uuid.uuid4()))
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            """INSERT INTO global_manual_evidence (
                draft_id, evidence_type, content_hash, original_filename, content_type,
                instrument_id, reporting_period, extraction_method, extraction_results,
                validation_results, corrections, accepted_at
            ) VALUES (%(draft_id)s, %(evidence_type)s, %(content_hash)s, %(original_filename)s, %(content_type)s,
                      %(instrument_id)s, %(reporting_period)s, %(extraction_method)s, %(extraction_results)s,
                      %(validation_results)s, %(corrections)s, %(accepted_at)s)""",
            duplicate,
        )
    conn.rollback()


def test_manual_evidence_draft_persistence_and_duplicate_lookup_on_real_postgres(database):
    """Defect B closure, end-to-end through the real persistence contract
    (not an in-memory fake): the exact call path that previously hit
    UndefinedTable -- upsert_manual_evidence_draft / load_manual_evidence_drafts
    -- now works against a real PostgreSQL database migrated by the real
    Flyway SQL files, including V22."""
    _apply(_connect(database), _migrations())
    store = _store(database)

    instrument_id = uuid.uuid4()
    accepted_at = datetime.now(timezone.utc)
    digest = hashlib.sha256(b"fake-shareholding-png-bytes").hexdigest()

    inserted = store.upsert_manual_evidence_draft(
        draft_id=uuid.uuid4(), evidence_type="SHAREHOLDING", content_hash=digest,
        original_filename="gokul-agro-shareholding.png", content_type="image/png",
        instrument_id=instrument_id, reporting_period=datetime(2026, 6, 1, tzinfo=timezone.utc),
        extraction_method="PNG_TEXT", extraction_results='{"promoter": "74.24"}',
        validation_results="{}", corrections="{}", accepted_at=accepted_at,
    )
    assert inserted is True

    drafts = store.load_manual_evidence_drafts(evidence_type="SHAREHOLDING")
    assert len(drafts) == 1
    assert drafts[0]["content_hash"] == digest
    assert str(drafts[0]["instrument_id"]) == str(instrument_id)

    # Dedup by (content_hash, evidence_type): re-uploading the identical file
    # for the same evidence type must not create a second row.
    inserted_again = store.upsert_manual_evidence_draft(
        draft_id=uuid.uuid4(), evidence_type="SHAREHOLDING", content_hash=digest,
        original_filename="gokul-agro-shareholding.png", content_type="image/png",
        instrument_id=instrument_id, reporting_period=datetime(2026, 6, 1, tzinfo=timezone.utc),
        extraction_method="PNG_TEXT", extraction_results='{"promoter": "74.24"}',
        validation_results="{}", corrections="{}", accepted_at=datetime.now(timezone.utc),
    )
    assert inserted_again is False
    assert len(store.load_manual_evidence_drafts(evidence_type="SHAREHOLDING")) == 1

    # A different evidence type with the same bytes is a genuinely distinct
    # record (the UNIQUE constraint is on (content_hash, evidence_type), not
    # content_hash alone).
    inserted_other_type = store.upsert_manual_evidence_draft(
        draft_id=uuid.uuid4(), evidence_type="CURRENT_NEWS", content_hash=digest,
        original_filename="gokul-agro-shareholding.png", content_type="image/png",
        instrument_id=instrument_id, reporting_period=None,
        extraction_method="PNG_TEXT", extraction_results="{}",
        validation_results="{}", corrections="{}", accepted_at=datetime.now(timezone.utc),
    )
    assert inserted_other_type is True
    assert len(store.load_manual_evidence_drafts()) == 2


def test_failed_sql_operation_does_not_poison_the_shared_connection_for_the_next_operation(database):
    """Defect C closure: a failed persistence operation must not leave the
    shared PostgreSQL connection in InFailedSqlTransaction for whatever
    unrelated operation runs next (the real failure surfaced in
    opportunity_worker's background cycle takeover, which shares this same
    connection via OpportunityPersistenceMixin).

    This drives a real SQL failure (querying a table that genuinely does not
    exist) directly against the adapter -- the same kind of failure
    UndefinedTable was -- and proves a subsequent, unrelated, legitimate
    query on the SAME store/connection succeeds afterwards rather than
    raising InFailedSqlTransaction."""
    _apply(_connect(database), _migrations())
    store = _store(database)

    with pytest.raises(psycopg.errors.UndefinedTable):
        store._connection.execute("SELECT * FROM table_that_does_not_exist_at_all")

    # Before the fix, nothing rolled back here, so the next statement on this
    # same connection would raise InFailedSqlTransaction instead of running.
    drafts = store.load_manual_evidence_drafts()
    assert drafts == []

    # And a completely unrelated operation (the real-world symptom: the
    # opportunity worker's background takeover check) also succeeds on the
    # same connection afterwards.
    assert store.active_cycle_run() is None


def test_manual_evidence_sql_failure_does_not_poison_connection_for_opportunity_worker_path(database):
    """Reproduces the real observed failure shape as closely as possible
    without the application's HTTP layer: the manual-evidence persistence
    path fails first (UndefinedTable, simulated by querying the real table
    with a typo'd/missing column so it fails the same way a genuinely
    missing relation would), then the opportunity-cycle background path --
    which shares this exact connection via OpportunityPersistenceMixin --
    must still succeed."""
    _apply(_connect(database), _migrations())
    store = _store(database)

    with pytest.raises(psycopg.errors.UndefinedColumn):
        store._connection.execute("SELECT nonexistent_column FROM global_manual_evidence")

    run, created = store.create_cycle_run("defect-c-regression", {"top_n": 4})
    assert created is True
    assert run["cycle_id"] == "defect-c-regression"
