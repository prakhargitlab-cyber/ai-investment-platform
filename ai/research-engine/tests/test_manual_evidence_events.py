"""Focused backend tests for generalized manual evidence:
ORDER_BOOK_CAPEX_GUIDANCE and GOVERNANCE_HISTORY.

Both requirements already consume ResearchEvent-style evidence (see
app.research_readiness_runtime._append_events(), which routes ANY
ResearchEvent whose event_type is in _ORDER_EVENTS / _GOVERNANCE_EVENTS into
that requirement's evidence -- automated and manual alike). This file proves
the manual upload path reuses that EXACT architecture: the same
repository.events.add() + persistence.upsert_event() write path CURRENT_NEWS
already uses, the same dedup key, the same freshness policies computed from
the real eventDate (never the upload/accept time), and the same real
ResearchReadinessRuntime -- no parallel manual-event model.

ORDER_BOOK_CAPEX_GUIDANCE has THREE mandatory inputs:
  MATERIAL_CATALYST_EVIDENCE      -- covered by ANY event in _ORDER_EVENTS
  ORDER_BOOK_OR_MAJOR_CONTRACT    -- covered only by the "order" bucket
                                     (NEW_ORDER, ORDER_BACKLOG_CHANGE,
                                     MAJOR_CONTRACT, GOVERNMENT_CONTRACT,
                                     ORDER_CANCELLED)
  CAPACITY_OR_CAPEX_OR_COMMISSIONING -- covered only by the "capex" bucket
                                     (CAPEX, FACTORY_EXPANSION,
                                     CAPACITY_EXPANSION, NEW_FACILITY,
                                     PROJECT_DELAY)
A single event's event_type can only ever be in one bucket (they are
disjoint), so READY_FRESH for this requirement genuinely requires TWO
separate fresh manual events, one per bucket -- this is not a test
artifact, it is how the real runtime already scores automated evidence too.

GOVERNANCE_HISTORY has ONE mandatory input (GOVERNANCE_EVIDENCE), covered by
any single event in _GOVERNANCE_EVENTS (MANAGEMENT_CHANGE, REGULATORY_EVENT,
CREDIT_RATING, BORROWING_CHANGE).
"""
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from app.manual_evidence import (
    AcceptanceError,
    ALLOWED_EVENT_TYPES_BY_EVIDENCE_TYPE,
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


def _order_fields(**overrides):
    fields = {
        "title": "Company wins new order from a major client",
        "summary": "A new order was booked, adding to the order book.",
        "eventDate": "2024-02-10T00:00:00+00:00",
        "sourceUrl": "https://example-filing.test/order/12345",
        "eventType": "NEW_ORDER",
        "impact": "POSITIVE",
        "timeHorizon": "MEDIUM_TERM",
    }
    fields.update(overrides)
    return fields


def _capex_fields(**overrides):
    fields = {
        "title": "Company announces new capacity expansion",
        "summary": "A capex program was announced to expand manufacturing capacity.",
        "eventDate": "2024-02-11T00:00:00+00:00",
        "sourceUrl": "https://example-filing.test/capex/12345",
        "eventType": "CAPEX",
        "impact": "POSITIVE",
        "timeHorizon": "LONG_TERM",
    }
    fields.update(overrides)
    return fields


def _governance_fields(**overrides):
    fields = {
        "title": "CFO resigns, new CFO appointed",
        "summary": "The company announced a change in the finance leadership team.",
        "eventDate": "2024-02-10T00:00:00+00:00",
        "sourceUrl": "https://example-filing.test/governance/12345",
        "eventType": "MANAGEMENT_CHANGE",
        "impact": "UNCERTAIN",
        "timeHorizon": "SHORT_TERM",
    }
    fields.update(overrides)
    return fields


class TestEventEvidenceCapability:
    def test_order_book_and_governance_are_supported_evidence_types(self):
        assert EvidenceType.ORDER_BOOK_CAPEX_GUIDANCE in SUPPORTED_EVIDENCE_TYPES
        assert EvidenceType.GOVERNANCE_HISTORY in SUPPORTED_EVIDENCE_TYPES

    def test_schema_lists_the_canonical_fields(self):
        for evidence_type in (EvidenceType.ORDER_BOOK_CAPEX_GUIDANCE, EvidenceType.GOVERNANCE_HISTORY):
            schema = MANUAL_FIELD_SCHEMA[evidence_type]
            for required in ("title", "summary", "eventDate", "sourceUrl", "eventType", "impact", "timeHorizon"):
                assert required in schema

    def test_order_book_event_types_are_restricted_to_the_order_capex_buckets(self):
        allowed = ALLOWED_EVENT_TYPES_BY_EVIDENCE_TYPE[EvidenceType.ORDER_BOOK_CAPEX_GUIDANCE]
        allowed_values = {t.value for t in allowed}
        assert "NEW_ORDER" in allowed_values
        assert "CAPEX" in allowed_values
        # Not a governance or unrelated event type.
        assert "MANAGEMENT_CHANGE" not in allowed_values
        assert "PRODUCT_LAUNCH" not in allowed_values

    def test_governance_event_types_are_restricted_to_canonical_governance_types(self):
        allowed = ALLOWED_EVENT_TYPES_BY_EVIDENCE_TYPE[EvidenceType.GOVERNANCE_HISTORY]
        allowed_values = {t.value for t in allowed}
        assert allowed_values == {"MANAGEMENT_CHANGE", "REGULATORY_EVENT", "CREDIT_RATING", "BORROWING_CHANGE"}


class TestEventEvidenceDraft:
    @pytest.mark.parametrize("evidence_type", ["ORDER_BOOK_CAPEX_GUIDANCE", "GOVERNANCE_HISTORY"])
    def test_upload_never_auto_accepts_undated_text_as_a_fact(self, ingestor, evidence_type):
        """A. Extraction creates a DRAFT only -- never a normalized fact."""
        draft = ingestor.ingest(
            evidence_type=evidence_type,
            file_bytes=b"Some disclosure text with no structured fields at all.",
            filename="disclosure.txt",
            instrument_id=RELIANCE_ID,
        )
        assert draft.proposed_facts == []
        assert draft.requires_manual_fields is True
        assert draft.manual_field_schema == MANUAL_FIELD_SCHEMA[draft.evidence_type]


class TestEventEvidenceValidation:
    @pytest.mark.asyncio
    async def test_accept_requires_corrections_payload(self, ingestor, acceptor):
        """D. A draft with no supplied fields cannot be accepted."""
        draft = ingestor.ingest(
            evidence_type="ORDER_BOOK_CAPEX_GUIDANCE", file_bytes=b"irrelevant",
            filename="disclosure.txt", instrument_id=RELIANCE_ID,
        )
        with pytest.raises(AcceptanceError, match="DRAFT_INVALID"):
            await acceptor.accept(draft_id=draft.draft_id)

    @pytest.mark.asyncio
    async def test_order_book_rejects_a_governance_event_type(self, ingestor, acceptor):
        """D. invalid event type for this requirement => rejected."""
        draft = ingestor.ingest(
            evidence_type="ORDER_BOOK_CAPEX_GUIDANCE", file_bytes=b"irrelevant",
            filename="disclosure.txt", instrument_id=RELIANCE_ID,
        )
        with pytest.raises(AcceptanceError, match="INVALID_EVENT_TYPE"):
            await acceptor.accept(draft_id=draft.draft_id, corrections=_order_fields(eventType="MANAGEMENT_CHANGE"))

    @pytest.mark.asyncio
    async def test_governance_rejects_an_order_book_event_type(self, ingestor, acceptor):
        """D. invalid event type for this requirement => rejected."""
        draft = ingestor.ingest(
            evidence_type="GOVERNANCE_HISTORY", file_bytes=b"irrelevant",
            filename="disclosure.txt", instrument_id=RELIANCE_ID,
        )
        with pytest.raises(AcceptanceError, match="INVALID_EVENT_TYPE"):
            await acceptor.accept(draft_id=draft.draft_id, corrections=_governance_fields(eventType="NEW_ORDER"))

    @pytest.mark.asyncio
    async def test_governance_rejects_an_unrelated_event_type(self, ingestor, acceptor):
        """D. a real ResearchEventType that is simply not a canonical
        governance type (e.g. PRODUCT_LAUNCH) must still be rejected."""
        draft = ingestor.ingest(
            evidence_type="GOVERNANCE_HISTORY", file_bytes=b"irrelevant",
            filename="disclosure.txt", instrument_id=RELIANCE_ID,
        )
        with pytest.raises(AcceptanceError, match="INVALID_EVENT_TYPE"):
            await acceptor.accept(draft_id=draft.draft_id, corrections=_governance_fields(eventType="PRODUCT_LAUNCH"))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("missing_field", ["title", "summary", "sourceUrl", "eventDate", "eventType", "impact", "timeHorizon"])
    async def test_order_book_rejects_each_missing_required_field(self, ingestor, acceptor, missing_field):
        draft = ingestor.ingest(
            evidence_type="ORDER_BOOK_CAPEX_GUIDANCE", file_bytes=b"irrelevant",
            filename="disclosure.txt", instrument_id=RELIANCE_ID,
        )
        fields = _order_fields()
        fields.pop(missing_field)
        with pytest.raises(AcceptanceError, match="DRAFT_INVALID"):
            await acceptor.accept(draft_id=draft.draft_id, corrections=fields)

    @pytest.mark.asyncio
    async def test_order_book_rejects_unparseable_event_date(self, ingestor, acceptor):
        draft = ingestor.ingest(
            evidence_type="ORDER_BOOK_CAPEX_GUIDANCE", file_bytes=b"irrelevant",
            filename="disclosure.txt", instrument_id=RELIANCE_ID,
        )
        with pytest.raises(AcceptanceError, match="INVALID_EVENT_DATE"):
            await acceptor.accept(draft_id=draft.draft_id, corrections=_order_fields(eventDate="not-a-date"))

    @pytest.mark.asyncio
    async def test_order_book_rejects_future_event_date(self, ingestor, acceptor):
        draft = ingestor.ingest(
            evidence_type="ORDER_BOOK_CAPEX_GUIDANCE", file_bytes=b"irrelevant",
            filename="disclosure.txt", instrument_id=RELIANCE_ID,
        )
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        with pytest.raises(AcceptanceError, match="EVENT_DATE_IN_FUTURE"):
            await acceptor.accept(draft_id=draft.draft_id, corrections=_order_fields(eventDate=future))


class TestOrderBookCapexGuidanceAccept:
    @pytest.mark.asyncio
    async def test_accept_persists_a_real_research_event(self, ingestor, acceptor, repository):
        """Persist via the canonical events_for()/research_events path --
        no parallel model."""
        draft = ingestor.ingest(
            evidence_type="ORDER_BOOK_CAPEX_GUIDANCE", file_bytes=b"irrelevant",
            filename="disclosure.txt", instrument_id=RELIANCE_ID,
        )
        result = await acceptor.accept(draft_id=draft.draft_id, corrections=_order_fields())
        assert result["persisted"] is True
        assert result["evidenceType"] == "ORDER_BOOK_CAPEX_GUIDANCE"

        events = repository.events_for(RELIANCE_ID)
        matching = [e for e in events if e.title == _order_fields()["title"]]
        assert len(matching) == 1
        event = matching[0]
        assert str(event.source_mode) == "REAL"
        assert str(event.reliability) == "LEVEL_D"
        assert event.event_date == datetime.fromisoformat("2024-02-10T00:00:00+00:00")
        assert event.raw_evidence_reference == f"manual-evidence:{draft.content_hash}"

        durable_events = repository.persistence.load_events({RELIANCE_ID})
        assert any(e.event_id == event.event_id for e in durable_events)

    @pytest.mark.asyncio
    async def test_duplicate_accept_is_idempotent_not_duplicated(self, ingestor, acceptor, repository):
        """G. Re-uploading the identical content must not create a second event."""
        draft1 = ingestor.ingest(
            evidence_type="ORDER_BOOK_CAPEX_GUIDANCE", file_bytes=b"same order bytes",
            filename="disclosure.txt", instrument_id=RELIANCE_ID,
        )
        await acceptor.accept(draft_id=draft1.draft_id, corrections=_order_fields())

        draft2 = ingestor.ingest(
            evidence_type="ORDER_BOOK_CAPEX_GUIDANCE", file_bytes=b"same order bytes",
            filename="disclosure.txt", instrument_id=RELIANCE_ID,
        )
        await acceptor.accept(draft_id=draft2.draft_id, corrections=_order_fields())

        events = repository.events_for(RELIANCE_ID)
        matching = [e for e in events if e.title == _order_fields()["title"]]
        assert len(matching) == 1

    @pytest.mark.asyncio
    async def test_extraction_failure_never_persists_an_empty_event(self, ingestor, acceptor, repository):
        """F. Validation failure (no fields supplied) persists nothing
        authoritative."""
        before_cache = list(repository.events_for(RELIANCE_ID))
        before_durable = list(repository.persistence.load_events({RELIANCE_ID}))
        draft = ingestor.ingest(
            evidence_type="ORDER_BOOK_CAPEX_GUIDANCE", file_bytes=b"irrelevant",
            filename="disclosure.txt", instrument_id=RELIANCE_ID,
        )
        with pytest.raises(AcceptanceError):
            await acceptor.accept(draft_id=draft.draft_id)
        assert len(repository.events_for(RELIANCE_ID)) == len(before_cache)
        assert len(repository.persistence.load_events({RELIANCE_ID})) == len(before_durable)

    @pytest.mark.asyncio
    async def test_one_bucket_alone_leaves_it_incomplete_not_ready_fresh(self, ingestor, acceptor, repository):
        """B. incomplete evidence (only the order-book bucket covered, the
        capex/capacity bucket still missing) -> never READY_FRESH."""
        draft = ingestor.ingest(
            evidence_type="ORDER_BOOK_CAPEX_GUIDANCE", file_bytes=b"irrelevant",
            filename="disclosure.txt", instrument_id=RELIANCE_ID,
        )
        result = await acceptor.accept(
            draft_id=draft.draft_id,
            corrections=_order_fields(
                eventDate=datetime.now(timezone.utc).isoformat(),
                sourceUrl="https://example-filing.test/order/incomplete-proof",
            ),
        )
        requirements = result["readiness"]["requirements"]
        requirement = next(r for r in requirements if r["requirementId"] == "ORDER_BOOK_CAPEX_GUIDANCE")
        assert requirement["status"] != "READY_FRESH"
        assert "CAPACITY_OR_CAPEX_OR_COMMISSIONING" in (requirement.get("missingInputIds") or [])

    @pytest.mark.asyncio
    async def test_both_buckets_fresh_drive_the_real_requirement_to_ready_fresh(self, ingestor, acceptor, repository):
        """A (positive READY_FRESH E2E proof). Two genuinely fresh, complete,
        correctly provenanced manual events -- one per mandatory bucket --
        must cause the REAL ResearchReadinessRuntime (never a mock, never a
        hard-coded status) to derive ORDER_BOOK_CAPEX_GUIDANCE ==
        READY_FRESH. Freshness policy is EVENT_DRIVEN_BOUNDED, maximum_age=
        30 days (FreshnessPolicyRegistry.default()["ORDER_BOOK_CAPEX_GUIDANCE"]),
        so eventDate must be recent, not merely "uploaded now"."""
        now = datetime.now(timezone.utc)

        order_draft = ingestor.ingest(
            evidence_type="ORDER_BOOK_CAPEX_GUIDANCE", file_bytes=b"order evidence bytes",
            filename="order.txt", instrument_id=RELIANCE_ID,
        )
        await acceptor.accept(
            draft_id=order_draft.draft_id,
            corrections=_order_fields(eventDate=now.isoformat(), sourceUrl="https://example-filing.test/order/ready-fresh-proof"),
        )

        capex_draft = ingestor.ingest(
            evidence_type="ORDER_BOOK_CAPEX_GUIDANCE", file_bytes=b"capex evidence bytes",
            filename="capex.txt", instrument_id=RELIANCE_ID,
        )
        result = await acceptor.accept(
            draft_id=capex_draft.draft_id,
            corrections=_capex_fields(eventDate=now.isoformat(), sourceUrl="https://example-filing.test/capex/ready-fresh-proof"),
        )

        requirements = result["readiness"]["requirements"]
        requirement = next(r for r in requirements if r["requirementId"] == "ORDER_BOOK_CAPEX_GUIDANCE")
        assert requirement["status"] == "READY_FRESH"
        covered = set(requirement["coveredInputIds"])
        assert {"MATERIAL_CATALYST_EVIDENCE", "ORDER_BOOK_OR_MAJOR_CONTRACT", "CAPACITY_OR_CAPEX_OR_COMMISSIONING"} <= covered
        # MANAGEMENT_GUIDANCE is a SUPPORTING (not mandatory) input -- see
        # ResearchRequirementRegistry.default()["ORDER_BOOK_CAPEX_GUIDANCE"]
        # -- so it legitimately stays in missingInputIds (neither of the
        # two uploaded events is a *_GUIDANCE event_type) while the
        # requirement is still correctly READY_FRESH: only the three
        # MANDATORY inputs gate that status.
        assert requirement["missingInputIds"] == ["MANAGEMENT_GUIDANCE"]

    @pytest.mark.asyncio
    async def test_stale_old_event_dates_never_reach_ready_fresh_via_real_runtime(self, ingestor, acceptor, repository):
        """C. Both buckets covered, but both events are outside the 30-day
        freshness window -- upload time is irrelevant, only the real
        eventDate counts, so this must never be derived as READY_FRESH."""
        old_date = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()

        order_draft = ingestor.ingest(
            evidence_type="ORDER_BOOK_CAPEX_GUIDANCE", file_bytes=b"stale order evidence",
            filename="order.txt", instrument_id=RELIANCE_ID,
        )
        await acceptor.accept(
            draft_id=order_draft.draft_id,
            corrections=_order_fields(eventDate=old_date, sourceUrl="https://example-filing.test/order/stale-proof"),
        )

        capex_draft = ingestor.ingest(
            evidence_type="ORDER_BOOK_CAPEX_GUIDANCE", file_bytes=b"stale capex evidence",
            filename="capex.txt", instrument_id=RELIANCE_ID,
        )
        result = await acceptor.accept(
            draft_id=capex_draft.draft_id,
            corrections=_capex_fields(eventDate=old_date, sourceUrl="https://example-filing.test/capex/stale-proof"),
        )

        requirements = result["readiness"]["requirements"]
        requirement = next(r for r in requirements if r["requirementId"] == "ORDER_BOOK_CAPEX_GUIDANCE")
        assert requirement["status"] != "READY_FRESH"

    @pytest.mark.asyncio
    async def test_manual_order_book_for_one_instrument_does_not_contaminate_another(self, ingestor, acceptor, repository):
        """E. wrong-instrument isolation."""
        draft = ingestor.ingest(
            evidence_type="ORDER_BOOK_CAPEX_GUIDANCE", file_bytes=b"irrelevant",
            filename="disclosure.txt", instrument_id=RELIANCE_ID,
        )
        await acceptor.accept(
            draft_id=draft.draft_id,
            corrections=_order_fields(sourceUrl="https://example-filing.test/order/isolation-proof"),
        )
        readiness_other = await acceptor.runtime.read(AIXTRON_ID, jurisdiction="IN")
        other_requirement = readiness_other.for_requirement("ORDER_BOOK_CAPEX_GUIDANCE")
        assert other_requirement.status != "READY_FRESH"
        assert all(
            "isolation-proof" not in (e.source_url or "")
            for e in repository.events_for(AIXTRON_ID)
        )

    @pytest.mark.asyncio
    async def test_manual_event_is_tagged_user_upload_lowest_authority(self, ingestor, acceptor, repository):
        """USER_UPLOAD precedence: the manually accepted event must be
        classified at ResearchSourceTier.USER_UPLOAD by the real
        _event_source() used by readiness -- not silently misclassified as
        an automated-equivalent tier (see the _event_source fix in
        research_readiness_runtime.py)."""
        from app.research_readiness_runtime import _event_source

        draft = ingestor.ingest(
            evidence_type="ORDER_BOOK_CAPEX_GUIDANCE", file_bytes=b"irrelevant",
            filename="disclosure.txt", instrument_id=RELIANCE_ID,
        )
        await acceptor.accept(
            draft_id=draft.draft_id,
            corrections=_order_fields(sourceUrl="https://example-filing.test/order/tier-proof"),
        )
        events = repository.events_for(RELIANCE_ID)
        event = next(e for e in events if e.source_url == "https://example-filing.test/order/tier-proof")
        source, tier = _event_source(event, "ORDER_BOOK_CAPEX_GUIDANCE")
        assert source == "USER_UPLOAD"
        assert str(tier) == "USER_UPLOAD"

    @pytest.mark.asyncio
    async def test_manual_event_never_outranks_an_existing_official_event(self, ingestor, acceptor, repository):
        """H. A stronger official event covering the SAME buckets must
        remain selected/visible alongside a conflicting manual one -- the
        manual event is additive (append-only ResearchEvent persistence),
        so it is never silently destroyed, but USER_UPLOAD's lower
        authority rank means it never displaces the official evidence as
        the SELECTED evidence for a given input."""
        from app.models import (
            EventImpact, ReliabilityLevel, ResearchEvent, ResearchEventType,
            SourceClassification, SourceMode, SourceType, TimeHorizon,
        )
        from app.models import DocumentStatus, DocumentType, ResearchDocument
        from uuid import uuid4

        now = datetime.now(timezone.utc)
        profile = repository.profile(RELIANCE_ID)
        document = ResearchDocument(
            document_id=uuid4(),
            canonical_url="https://nseindia.com/official/order-disclosure",
            original_url="https://nseindia.com/official/order-disclosure",
            title="Official exchange disclosure",
            source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
            source_classification=SourceClassification.EXCHANGE,
            source_name="NSE",
            published_at=now,
            retrieved_at=now,
            content_type="text/html",
            document_type=DocumentType.HTML,
            status=DocumentStatus.PROCESSED,
            reliability_level=ReliabilityLevel.LEVEL_A,
            instrument_id=RELIANCE_ID,
            company_id=profile.company_id,
            source_mode=SourceMode.REAL,
            freshness="REAL",
            content_hash="official-order-disclosure-hash",
        )
        repository._persistence.upsert_document(document)
        official_event = ResearchEvent(
            instrument_id=RELIANCE_ID,
            company_id=profile.company_id,
            event_type=ResearchEventType.NEW_ORDER,
            event_date=now,
            title="Official: large new order confirmed by exchange filing",
            summary="Exchange-filed confirmation of a new order.",
            source_document_id=document.document_id,
            source_url="https://nseindia.com/official/order-disclosure",
            source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
            source_classification=SourceClassification.EXCHANGE,
            reliability=ReliabilityLevel.LEVEL_A,
            source_mode=SourceMode.REAL,
            confidence=0.95,
            impact=EventImpact.POSITIVE,
            time_horizon=TimeHorizon.MEDIUM_TERM,
            raw_evidence_reference="official-exchange-filing-reference",
            published_at=now,
            retrieved_at=now,
        )
        created = repository.events.add(official_event, durable=repository._documents_are_durable())
        assert created
        repository._persistence.upsert_event(official_event)

        draft = ingestor.ingest(
            evidence_type="ORDER_BOOK_CAPEX_GUIDANCE", file_bytes=b"irrelevant",
            filename="disclosure.txt", instrument_id=RELIANCE_ID,
        )
        await acceptor.accept(
            draft_id=draft.draft_id,
            corrections=_order_fields(
                eventDate=now.isoformat(),
                sourceUrl="https://example-filing.test/order/conflicting-manual-proof",
                title="Unverified manual claim of a different order",
            ),
        )

        # Both events survive -- the official one was never overwritten.
        events = repository.events_for(RELIANCE_ID)
        assert any(e.source_url == "https://nseindia.com/official/order-disclosure" for e in events)
        assert any(e.source_url == "https://example-filing.test/order/conflicting-manual-proof" for e in events)


class TestGovernanceHistoryAccept:
    @pytest.mark.asyncio
    async def test_accept_persists_a_real_research_event(self, ingestor, acceptor, repository):
        draft = ingestor.ingest(
            evidence_type="GOVERNANCE_HISTORY", file_bytes=b"irrelevant",
            filename="disclosure.txt", instrument_id=RELIANCE_ID,
        )
        result = await acceptor.accept(draft_id=draft.draft_id, corrections=_governance_fields())
        assert result["persisted"] is True
        assert result["evidenceType"] == "GOVERNANCE_HISTORY"

        events = repository.events_for(RELIANCE_ID)
        matching = [e for e in events if e.title == _governance_fields()["title"]]
        assert len(matching) == 1
        event = matching[0]
        assert str(event.source_mode) == "REAL"
        assert str(event.reliability) == "LEVEL_D"
        assert event.raw_evidence_reference == f"manual-evidence:{draft.content_hash}"

        durable_events = repository.persistence.load_events({RELIANCE_ID})
        assert any(e.event_id == event.event_id for e in durable_events)

    @pytest.mark.asyncio
    async def test_duplicate_accept_is_idempotent_not_duplicated(self, ingestor, acceptor, repository):
        """G. duplicate manual upload -> deterministic canonical behavior
        (no second event)."""
        draft1 = ingestor.ingest(
            evidence_type="GOVERNANCE_HISTORY", file_bytes=b"same governance bytes",
            filename="disclosure.txt", instrument_id=RELIANCE_ID,
        )
        await acceptor.accept(draft_id=draft1.draft_id, corrections=_governance_fields())

        draft2 = ingestor.ingest(
            evidence_type="GOVERNANCE_HISTORY", file_bytes=b"same governance bytes",
            filename="disclosure.txt", instrument_id=RELIANCE_ID,
        )
        await acceptor.accept(draft_id=draft2.draft_id, corrections=_governance_fields())

        events = repository.events_for(RELIANCE_ID)
        matching = [e for e in events if e.title == _governance_fields()["title"]]
        assert len(matching) == 1

    @pytest.mark.asyncio
    async def test_fresh_resolved_management_change_drives_the_real_requirement_to_ready_fresh(self, ingestor, acceptor, repository):
        """A (positive READY_FRESH E2E proof). A single, genuinely current,
        complete, correctly provenanced governance event must cause the
        REAL ResearchReadinessRuntime to derive GOVERNANCE_HISTORY ==
        READY_FRESH. GOVERNANCE_HISTORY freshness policy is
        UNRESOLVED_HISTORY, maximum_age=365 days, date_basis=
        EVENT_OR_PUBLICATION (FreshnessPolicyRegistry.default()
        ["GOVERNANCE_HISTORY"]). A POSITIVE impact event is never
        "unresolved" (see _append_events()'s unresolved computation --
        only NEGATIVE/STRONG_NEGATIVE/UNCERTAIN), so freshness here comes
        purely from the recent eventDate, not from retain_while_unresolved.
        """
        draft = ingestor.ingest(
            evidence_type="GOVERNANCE_HISTORY", file_bytes=b"irrelevant",
            filename="disclosure.txt", instrument_id=RELIANCE_ID,
        )
        result = await acceptor.accept(
            draft_id=draft.draft_id,
            corrections=_governance_fields(
                eventDate=datetime.now(timezone.utc).isoformat(),
                sourceUrl="https://example-filing.test/governance/ready-fresh-proof",
                impact="POSITIVE",
                title="New, well-regarded CFO appointed",
            ),
        )
        requirements = result["readiness"]["requirements"]
        requirement = next(r for r in requirements if r["requirementId"] == "GOVERNANCE_HISTORY")
        assert requirement["status"] == "READY_FRESH"
        assert requirement["coveredInputIds"] == ["GOVERNANCE_EVIDENCE"]
        assert requirement["missingInputIds"] == []

    @pytest.mark.asyncio
    async def test_old_unresolved_negative_event_stays_fresh_via_retain_while_unresolved(self, ingestor, acceptor, repository):
        """Canonical freshness exception, proven against the real runtime:
        retain_while_unresolved=True means an old but still-UNRESOLVED
        negative governance event (status != REJECTED, impact in
        {NEGATIVE, STRONG_NEGATIVE, UNCERTAIN}) stays READY_FRESH
        indefinitely -- this is production behavior the manual path must
        reuse exactly, not something manual upload invents."""
        old_date = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        draft = ingestor.ingest(
            evidence_type="GOVERNANCE_HISTORY", file_bytes=b"irrelevant",
            filename="disclosure.txt", instrument_id=RELIANCE_ID,
        )
        result = await acceptor.accept(
            draft_id=draft.draft_id,
            corrections=_governance_fields(
                eventDate=old_date,
                sourceUrl="https://example-filing.test/governance/unresolved-proof",
                impact="NEGATIVE",
                title="Ongoing regulatory action against the company",
            ),
        )
        requirements = result["readiness"]["requirements"]
        requirement = next(r for r in requirements if r["requirementId"] == "GOVERNANCE_HISTORY")
        assert requirement["status"] == "READY_FRESH"

    @pytest.mark.asyncio
    async def test_old_resolved_positive_event_does_not_reach_ready_fresh(self, ingestor, acceptor, repository):
        """C. stale evidence (old, POSITIVE -- therefore resolved/never
        "unresolved" -- event) -> not READY_FRESH. Upload time is
        irrelevant; only the real eventDate and resolution state count."""
        old_date = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        draft = ingestor.ingest(
            evidence_type="GOVERNANCE_HISTORY", file_bytes=b"irrelevant",
            filename="disclosure.txt", instrument_id=RELIANCE_ID,
        )
        result = await acceptor.accept(
            draft_id=draft.draft_id,
            corrections=_governance_fields(
                eventDate=old_date,
                sourceUrl="https://example-filing.test/governance/stale-resolved-proof",
                impact="POSITIVE",
                title="Old, positive management change, long resolved",
            ),
        )
        requirements = result["readiness"]["requirements"]
        requirement = next(r for r in requirements if r["requirementId"] == "GOVERNANCE_HISTORY")
        assert requirement["status"] != "READY_FRESH"

    @pytest.mark.asyncio
    async def test_manual_governance_for_one_instrument_does_not_contaminate_another(self, ingestor, acceptor, repository):
        """E. wrong-instrument isolation."""
        draft = ingestor.ingest(
            evidence_type="GOVERNANCE_HISTORY", file_bytes=b"irrelevant",
            filename="disclosure.txt", instrument_id=RELIANCE_ID,
        )
        await acceptor.accept(
            draft_id=draft.draft_id,
            corrections=_governance_fields(sourceUrl="https://example-filing.test/governance/isolation-proof"),
        )
        readiness_other = await acceptor.runtime.read(AIXTRON_ID, jurisdiction="IN")
        other_requirement = readiness_other.for_requirement("GOVERNANCE_HISTORY")
        assert other_requirement.status != "READY_FRESH"
        assert all(
            "isolation-proof" not in (e.source_url or "")
            for e in repository.events_for(AIXTRON_ID)
        )

    @pytest.mark.asyncio
    async def test_extraction_failure_never_persists_an_empty_event(self, ingestor, acceptor, repository):
        """F. Validation failure persists nothing authoritative."""
        before_cache = list(repository.events_for(RELIANCE_ID))
        before_durable = list(repository.persistence.load_events({RELIANCE_ID}))
        draft = ingestor.ingest(
            evidence_type="GOVERNANCE_HISTORY", file_bytes=b"irrelevant",
            filename="disclosure.txt", instrument_id=RELIANCE_ID,
        )
        with pytest.raises(AcceptanceError):
            await acceptor.accept(draft_id=draft.draft_id)
        assert len(repository.events_for(RELIANCE_ID)) == len(before_cache)
        assert len(repository.persistence.load_events({RELIANCE_ID})) == len(before_durable)

    @pytest.mark.asyncio
    async def test_manual_event_is_tagged_user_upload_lowest_authority(self, ingestor, acceptor, repository):
        from app.research_readiness_runtime import _event_source

        draft = ingestor.ingest(
            evidence_type="GOVERNANCE_HISTORY", file_bytes=b"irrelevant",
            filename="disclosure.txt", instrument_id=RELIANCE_ID,
        )
        await acceptor.accept(
            draft_id=draft.draft_id,
            corrections=_governance_fields(sourceUrl="https://example-filing.test/governance/tier-proof"),
        )
        events = repository.events_for(RELIANCE_ID)
        event = next(e for e in events if e.source_url == "https://example-filing.test/governance/tier-proof")
        source, tier = _event_source(event, "GOVERNANCE_HISTORY")
        assert source == "USER_UPLOAD"
        assert str(tier) == "USER_UPLOAD"
