"""Focused backend tests for generalized manual evidence: CURRENT_NEWS.

CURRENT_NEWS has no safe deterministic auto-extraction of headline/date/
impact from arbitrary prose, so (unlike SHAREHOLDING) the draft always
starts with proposed_facts=[] and requires_manual_fields=True; the user
supplies the canonical ResearchEvent fields explicitly via `corrections` at
accept() time. Everything downstream (persistence, dedup, readiness
recalculation) reuses the exact same event pipeline the automated news
acquisition path writes through (repository.events.add +
persistence.upsert_event), so a manually accepted event is read back by
readiness exactly like any other event -- no parallel model.
"""
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from app.manual_evidence import (
    AcceptanceError,
    EvidenceType,
    ManualEvidenceAcceptor,
    ManualEvidenceIngestor,
    MANUAL_FIELD_SCHEMA,
    SUPPORTED_EVIDENCE_TYPES,
)
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository
from app.settings import Settings

RELIANCE_ID = UUID("44444444-4444-4444-4444-444444444444")
AIXTRON_ID = UUID("11111111-1111-1111-1111-111111111111")  # a different demo-profile instrument, used for isolation checks


@pytest.fixture
def repository():
    return ResearchRepository(
        settings=Settings(
            research_mcp_gateway_base_url="http://localhost",
            research_mcp_gateway_timeout_seconds=30,
            research_mcp_service_identity="test",
            research_mcp_first_enabled=False,
            research_readiness_ensure_timeout_seconds=30,
            research_opportunity_scheduler_enabled=False,
            country="IN",
            exchange="XNSE",
        ),
        persistence=SqliteResearchPersistence(),
    )


@pytest.fixture
def ingestor(repository):
    from app import manual_evidence
    manual_evidence._drafts.clear()
    return ManualEvidenceIngestor(repository=repository)


@pytest.fixture
def acceptor(repository):
    from app import manual_evidence
    from app.research_readiness_runtime import (
        ResearchReadinessRuntime,
        RepositoryResearchReadinessAdapter,
    )
    manual_evidence._drafts.clear()
    adapter = RepositoryResearchReadinessAdapter(repository)
    runtime = ResearchReadinessRuntime(repository, adapter, None, ensure_timeout_seconds=30)
    return ManualEvidenceAcceptor(repository=repository, runtime=runtime)


def _valid_fields(**overrides):
    fields = {
        "title": "Government tightens export tariffs affecting the sector",
        "summary": "A new regulatory announcement affects the company's export operations.",
        "eventDate": "2024-02-10T00:00:00+00:00",
        "sourceUrl": "https://example-news.test/article/12345",
        "eventType": "REGULATORY_EVENT",
        "impact": "NEGATIVE",
        "timeHorizon": "SHORT_TERM",
    }
    fields.update(overrides)
    return fields


class TestCurrentNewsCapability:
    def test_current_news_is_a_supported_evidence_type(self):
        assert EvidenceType.CURRENT_NEWS in SUPPORTED_EVIDENCE_TYPES

    def test_schema_lists_the_canonical_fields(self):
        schema = MANUAL_FIELD_SCHEMA[EvidenceType.CURRENT_NEWS]
        for required in ("title", "summary", "eventDate", "sourceUrl", "eventType", "impact", "timeHorizon"):
            assert required in schema


class TestCurrentNewsDraft:
    def test_upload_never_auto_accepts_undated_text_as_a_fact(self, ingestor):
        """1. Extraction creates a DRAFT only -- never a normalized fact."""
        draft = ingestor.ingest(
            evidence_type="CURRENT_NEWS",
            file_bytes=b"Some old news article with no structured fields at all.",
            filename="article.txt",
            instrument_id=RELIANCE_ID,
        )
        assert draft.proposed_facts == []
        assert draft.requires_manual_fields is True
        assert draft.manual_field_schema == MANUAL_FIELD_SCHEMA[EvidenceType.CURRENT_NEWS]


class TestCurrentNewsAccept:
    @pytest.mark.asyncio
    async def test_accept_requires_corrections_payload(self, ingestor, acceptor):
        """4. A CURRENT_NEWS draft with no supplied fields cannot be accepted."""
        draft = ingestor.ingest(
            evidence_type="CURRENT_NEWS", file_bytes=b"irrelevant",
            filename="article.txt", instrument_id=RELIANCE_ID,
        )
        with pytest.raises(AcceptanceError, match="DRAFT_INVALID"):
            await acceptor.accept(draft_id=draft.draft_id)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("missing_field", ["title", "summary", "sourceUrl", "eventDate", "eventType", "impact", "timeHorizon"])
    async def test_accept_rejects_each_missing_required_field(self, ingestor, acceptor, missing_field):
        """4. Missing required date/provenance/field => cannot be accepted."""
        draft = ingestor.ingest(
            evidence_type="CURRENT_NEWS", file_bytes=b"irrelevant",
            filename="article.txt", instrument_id=RELIANCE_ID,
        )
        fields = _valid_fields()
        fields.pop(missing_field)
        with pytest.raises(AcceptanceError, match="DRAFT_INVALID"):
            await acceptor.accept(draft_id=draft.draft_id, corrections=fields)

    @pytest.mark.asyncio
    async def test_accept_rejects_unparseable_event_date(self, ingestor, acceptor):
        draft = ingestor.ingest(
            evidence_type="CURRENT_NEWS", file_bytes=b"irrelevant",
            filename="article.txt", instrument_id=RELIANCE_ID,
        )
        with pytest.raises(AcceptanceError, match="INVALID_EVENT_DATE"):
            await acceptor.accept(draft_id=draft.draft_id, corrections=_valid_fields(eventDate="not-a-date"))

    @pytest.mark.asyncio
    async def test_accept_rejects_future_event_date(self, ingestor, acceptor):
        draft = ingestor.ingest(
            evidence_type="CURRENT_NEWS", file_bytes=b"irrelevant",
            filename="article.txt", instrument_id=RELIANCE_ID,
        )
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        with pytest.raises(AcceptanceError, match="EVENT_DATE_IN_FUTURE"):
            await acceptor.accept(draft_id=draft.draft_id, corrections=_valid_fields(eventDate=future))

    @pytest.mark.asyncio
    async def test_accept_rejects_unknown_event_type(self, ingestor, acceptor):
        draft = ingestor.ingest(
            evidence_type="CURRENT_NEWS", file_bytes=b"irrelevant",
            filename="article.txt", instrument_id=RELIANCE_ID,
        )
        with pytest.raises(AcceptanceError, match="INVALID_EVENT_TYPE"):
            await acceptor.accept(draft_id=draft.draft_id, corrections=_valid_fields(eventType="NOT_A_REAL_TYPE"))

    @pytest.mark.asyncio
    async def test_accept_persists_a_real_research_event(self, ingestor, acceptor, repository):
        """3. A valid dated/provenanced draft is accepted and persisted.
        6. It is consumed by readiness through the normal events_for() path."""
        draft = ingestor.ingest(
            evidence_type="CURRENT_NEWS", file_bytes=b"irrelevant",
            filename="article.txt", instrument_id=RELIANCE_ID,
        )
        result = await acceptor.accept(draft_id=draft.draft_id, corrections=_valid_fields())
        assert result["persisted"] is True
        assert result["evidenceType"] == "CURRENT_NEWS"

        events = repository.events_for(RELIANCE_ID)
        matching = [e for e in events if e.title == _valid_fields()["title"]]
        assert len(matching) == 1
        event = matching[0]
        assert str(event.source_mode) == "REAL"
        assert str(event.reliability) == "LEVEL_D"
        assert event.event_date == datetime.fromisoformat("2024-02-10T00:00:00+00:00")
        assert event.raw_evidence_reference == f"manual-evidence:{draft.content_hash}"

        # Durable, not just in-memory: a fresh repository reading the same
        # persistence must see it too.
        durable_events = repository.persistence.load_events({RELIANCE_ID})
        assert any(e.event_id == event.event_id for e in durable_events)

    @pytest.mark.asyncio
    async def test_duplicate_accept_is_idempotent_not_duplicated(self, ingestor, acceptor, repository):
        """5/idempotency: uploading + accepting the same article twice must
        not create two events (same dedup key the automated pipeline uses)."""
        draft1 = ingestor.ingest(
            evidence_type="CURRENT_NEWS", file_bytes=b"same bytes",
            filename="article.txt", instrument_id=RELIANCE_ID,
        )
        await acceptor.accept(draft_id=draft1.draft_id, corrections=_valid_fields())

        draft2 = ingestor.ingest(
            evidence_type="CURRENT_NEWS", file_bytes=b"same bytes",
            filename="article.txt", instrument_id=RELIANCE_ID,
        )
        # Re-accepting the identical fields/content must not raise and must
        # not create a second event.
        await acceptor.accept(draft_id=draft2.draft_id, corrections=_valid_fields())

        events = repository.events_for(RELIANCE_ID)
        matching = [e for e in events if e.title == _valid_fields()["title"]]
        assert len(matching) == 1

    @pytest.mark.asyncio
    async def test_stale_old_event_date_does_not_force_freshness(self, ingestor, acceptor, repository):
        """5. An old article's real event_date must not be reinterpreted as
        fresh just because it was uploaded today."""
        draft = ingestor.ingest(
            evidence_type="CURRENT_NEWS", file_bytes=b"irrelevant",
            filename="old_article.txt", instrument_id=RELIANCE_ID,
        )
        old_date = "2019-01-01T00:00:00+00:00"
        await acceptor.accept(draft_id=draft.draft_id, corrections=_valid_fields(eventDate=old_date))

        events = repository.events_for(RELIANCE_ID)
        matching = [e for e in events if e.title == _valid_fields()["title"]]
        assert len(matching) == 1
        # as_of for CURRENT_NEWS evidence is event_date/published_at, never
        # detected_at/retrieved_at -- the upload moment never substitutes for
        # the real date (see app.research_readiness_runtime._evidence_from_event).
        assert matching[0].event_date == datetime.fromisoformat(old_date)
        assert matching[0].event_date < datetime.now(timezone.utc) - timedelta(days=365)

    @pytest.mark.asyncio
    async def test_extraction_failure_never_persists_an_empty_event(self, ingestor, acceptor, repository):
        """10. Extraction/validation failure (here: no fields supplied at
        all) must never persist an empty/fabricated USER_UPLOAD event.

        RELIANCE_ID carries pre-seeded demo events (this repository is
        built from the same demo fixture production uses), so the
        no-new-event assertion is a before/after count, not an emptiness
        check -- the point is that the failed accept adds nothing, not
        that this instrument starts with zero events.
        """
        before_cache = list(repository.events_for(RELIANCE_ID))
        before_durable = list(repository.persistence.load_events({RELIANCE_ID}))
        draft = ingestor.ingest(
            evidence_type="CURRENT_NEWS", file_bytes=b"irrelevant",
            filename="article.txt", instrument_id=RELIANCE_ID,
        )
        with pytest.raises(AcceptanceError):
            await acceptor.accept(draft_id=draft.draft_id)
        assert len(repository.events_for(RELIANCE_ID)) == len(before_cache)
        assert len(repository.persistence.load_events({RELIANCE_ID})) == len(before_durable)

    @pytest.mark.asyncio
    async def test_fresh_dated_news_drives_the_real_requirement_to_ready_fresh(self, ingestor, acceptor, repository):
        """G (READY_FRESH TRANSITION TEST), positive case: a genuinely
        current, complete, correctly provenanced news upload must cause the
        REAL ResearchReadinessRuntime (never a mock, never a hard-coded
        status) to derive CURRENT_NEWS == READY_FRESH for that instrument.
        CURRENT_NEWS freshness policy is ROLLING_EVENT_WINDOW, maximum_age=
        1 day, date_basis=EVENT_OR_PUBLICATION (see
        FreshnessPolicyRegistry.default()["CURRENT_NEWS"]), so eventDate
        must be ~now, not merely "uploaded now".
        """
        draft = ingestor.ingest(
            evidence_type="CURRENT_NEWS", file_bytes=b"irrelevant",
            filename="article.txt", instrument_id=RELIANCE_ID,
        )
        result = await acceptor.accept(
            draft_id=draft.draft_id,
            corrections=_valid_fields(
                eventDate=datetime.now(timezone.utc).isoformat(),
                sourceUrl="https://example-news.test/article/ready-fresh-proof",
            ),
        )
        requirements = result["readiness"]["requirements"]
        current_news = next(r for r in requirements if r["requirementId"] == "CURRENT_NEWS")
        assert current_news["status"] == "READY_FRESH"
        assert current_news["coveredInputIds"] == ["RELEVANT_CURRENT_EVENT_EVIDENCE"]
        assert current_news["missingInputIds"] == []

    @pytest.mark.asyncio
    async def test_stale_dated_news_never_reaches_ready_fresh_via_real_runtime(self, ingestor, acceptor, repository):
        """G, negative case: an upload dated outside the CURRENT_NEWS
        scoring window (30 days) must never be derived as READY_FRESH by
        the real runtime, however complete/valid/provenanced it otherwise
        is. Upload time is irrelevant -- only the real eventDate counts."""
        draft = ingestor.ingest(
            evidence_type="CURRENT_NEWS", file_bytes=b"irrelevant",
            filename="old_article.txt", instrument_id=RELIANCE_ID,
        )
        old_date = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        result = await acceptor.accept(
            draft_id=draft.draft_id,
            corrections=_valid_fields(eventDate=old_date, sourceUrl="https://example-news.test/article/stale-proof"),
        )
        requirements = result["readiness"]["requirements"]
        current_news = next(r for r in requirements if r["requirementId"] == "CURRENT_NEWS")
        assert current_news["status"] != "READY_FRESH"

    @pytest.mark.asyncio
    async def test_manual_news_for_one_instrument_does_not_contaminate_another(self, ingestor, acceptor, repository):
        """G, wrong-instrument isolation: accepting fresh CURRENT_NEWS
        evidence for RELIANCE_ID must never affect another instrument's
        CURRENT_NEWS requirement."""
        draft = ingestor.ingest(
            evidence_type="CURRENT_NEWS", file_bytes=b"irrelevant",
            filename="article.txt", instrument_id=RELIANCE_ID,
        )
        await acceptor.accept(
            draft_id=draft.draft_id,
            corrections=_valid_fields(sourceUrl="https://example-news.test/article/isolation-proof"),
        )
        readiness_other = await acceptor.runtime.read(AIXTRON_ID, jurisdiction="IN")
        other_current_news = readiness_other.for_requirement("CURRENT_NEWS")
        assert other_current_news.status != "READY_FRESH"
        assert all(
            "isolation-proof" not in (e.source_url or "")
            for e in repository.events_for(AIXTRON_ID)
        )
