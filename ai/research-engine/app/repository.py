from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from app.deduplication import DocumentDeduplicator
from app.entity_resolution import EntityResolver
from app.events import company_updated, document_event, research_event_extracted
from app.extraction import RuleBasedEventExtractor
from app.models import (
    CompanyResearchProfile,
    DocumentStatus,
    DocumentType,
    PlatformEvent,
    ReliabilityLevel,
    ResearchDocument,
    ResearchEvent,
    ResearchEventType,
    ResearchSummary,
    SourceType,
)
from app.normalization import canonicalize_url, content_hash, extract_text, normalize_text
from app.scoring import CatalystScorer


class ResearchRepository:
    def __init__(self) -> None:
        self.profiles = _demo_profiles()
        self.documents: dict[UUID, ResearchDocument] = {}
        self.events: dict[UUID, ResearchEvent] = {}
        self.platform_events: list[PlatformEvent] = []
        self.last_refresh: dict[UUID, datetime] = {}
        self._deduplicator = DocumentDeduplicator()
        self._resolver = EntityResolver(self.profiles)
        self._extractor = RuleBasedEventExtractor()
        self._scorer = CatalystScorer()
        self._seed_demo_data()

    def list_profiles(self) -> list[CompanyResearchProfile]:
        return self.profiles

    def profile(self, instrument_id: UUID) -> CompanyResearchProfile:
        return next(profile for profile in self.profiles if profile.instrument_id == instrument_id)

    def documents_for(self, instrument_id: UUID) -> list[ResearchDocument]:
        return sorted(
            [doc for doc in self.documents.values() if doc.instrument_id == instrument_id],
            key=lambda doc: doc.published_at or doc.retrieved_at,
            reverse=True,
        )

    def events_for(
        self,
        instrument_id: UUID,
        event_type: ResearchEventType | None = None,
        impact: str | None = None,
        reliability: ReliabilityLevel | None = None,
    ) -> list[ResearchEvent]:
        values = [event for event in self.events.values() if event.instrument_id == instrument_id]
        if event_type:
            values = [event for event in values if event.event_type == event_type]
        if impact:
            values = [event for event in values if event.impact == impact]
        if reliability:
            values = [event for event in values if event.reliability == reliability]
        return sorted(values, key=lambda event: event.event_date or event.detected_at, reverse=True)

    def summary(self, instrument_id: UUID) -> ResearchSummary:
        profile = self.profile(instrument_id)
        events = self.events_for(instrument_id)
        documents = self.documents_for(instrument_id)
        score = self._scorer.score(instrument_id, events)
        source_mix: dict[str, int] = {}
        for document in documents:
            key = str(document.source_type)
            source_mix[key] = source_mix.get(key, 0) + 1
        return ResearchSummary(
            profile=profile,
            catalyst_score=score,
            recent_events=events[:10],
            documents=documents[:10],
            last_refresh_at=self.last_refresh.get(instrument_id),
            data_freshness="DEMO",
            demo=True,
            source_mix=source_mix,
        )

    def ingest_fixture(
        self,
        *,
        original_url: str,
        source_type: SourceType,
        source_name: str,
        publisher: str,
        content_type: str,
        body: str,
        reliability: ReliabilityLevel,
        published_at: datetime | None = None,
    ) -> ResearchDocument:
        canonical = canonicalize_url(original_url)
        title, extracted = extract_text(body, content_type)
        normalized = normalize_text(extracted or "")
        resolution = self._resolver.resolve(title, normalized, canonical)
        document = ResearchDocument(
            canonical_url=canonical,
            original_url=original_url,
            title=title,
            source_type=source_type,
            source_name=source_name,
            publisher=publisher,
            published_at=published_at,
            content_type=content_type,
            document_type=DocumentType.PDF_REFERENCE if canonical.lower().endswith(".pdf") else DocumentType.HTML,
            raw_text=body if len(body) < 20_000 else None,
            normalized_text=normalized,
            content_hash=content_hash(normalized or canonical),
            instrument_id=resolution.instrument_id,
            company_id=resolution.company_id,
            status=DocumentStatus.PARSED,
            reliability_level=reliability,
            entity_resolution_confidence=resolution.confidence,
        )
        duplicate = self._deduplicator.add(document)
        if duplicate:
            document.status = DocumentStatus.DUPLICATE
            self.documents[document.document_id] = document
            return document
        self.documents[document.document_id] = document
        self.platform_events.append(document_event("research.document.processed", document))
        for event in self._extractor.extract(document):
            self.events[event.event_id] = event
            self.platform_events.append(research_event_extracted(event))
        if document.instrument_id:
            self.last_refresh[document.instrument_id] = datetime.now(timezone.utc)
            self.platform_events.append(company_updated(document.instrument_id))
        document.status = DocumentStatus.PROCESSED
        return document

    def refresh(self, instrument_id: UUID) -> ResearchSummary:
        self.last_refresh[instrument_id] = datetime.now(timezone.utc)
        return self.summary(instrument_id)

    def _seed_demo_data(self) -> None:
        fixtures = [
            (
                "https://ir.aixtron.example/releases/order-capacity?utm_source=test",
                "AIXTRON SE announced a new order worth €350 million from a leading power electronics customer. The XETR AIXA order supports silicon-carbide equipment demand and increases backlog by +42%.",
                SourceType.INVESTOR_RELATIONS,
                ReliabilityLevel.LEVEL_B,
            ),
            (
                "https://exchange.example/xams/besi-capacity",
                "BE Semiconductor Industries BESI XAMS announced capacity expansion of 73.15 MW equivalent production capability and a new facility in the Netherlands. CAPEX is €120 million and the project is under construction.",
                SourceType.EXCHANGE_ANNOUNCEMENT,
                ReliabilityLevel.LEVEL_A,
            ),
            (
                "https://nse.example/reliance-filing",
                "RELIANCE XNSE INE002A01018 disclosed an investment of ₹2,000 crore in new energy manufacturing capacity in India. Management maintained revenue guidance.",
                SourceType.REGULATORY_FILING,
                ReliabilityLevel.LEVEL_A,
            ),
            (
                "https://news.example/nvda-delay",
                "NVIDIA Corporation NVDA XNAS reported that a factory ramp was delayed by one quarter while demand remains strong.",
                SourceType.NEWS,
                ReliabilityLevel.LEVEL_C,
            ),
        ]
        for url, body, source_type, reliability in fixtures:
            self.ingest_fixture(
                original_url=url,
                source_type=source_type,
                source_name="DEMO fixture source",
                publisher="DEMO",
                content_type="text/html",
                body=f"<html><head><title>DEMO research fixture</title></head><body>{body}</body></html>",
                reliability=reliability,
                published_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
            )


def _demo_profiles() -> list[CompanyResearchProfile]:
    return [
        CompanyResearchProfile(
            instrument_id=UUID("11111111-1111-1111-1111-111111111111"),
            company_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"),
            company_name="AIXTRON SE",
            aliases=["AIXTRON", "AIXA"],
            isin="DE000A0WMPJ6",
            ticker="AIXA",
            exchange="XETR",
            mic="XETR",
            country="DE",
            currency="EUR",
            known_domains=["aixtron.example"],
        ),
        CompanyResearchProfile(
            instrument_id=UUID("22222222-2222-2222-2222-222222222222"),
            company_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2"),
            company_name="BE Semiconductor Industries",
            aliases=["BESI", "BE Semiconductor"],
            isin="NL0012866412",
            ticker="BESI",
            exchange="XAMS",
            mic="XAMS",
            country="NL",
            currency="EUR",
            known_domains=["besi.example"],
        ),
        CompanyResearchProfile(
            instrument_id=UUID("33333333-3333-3333-3333-333333333333"),
            company_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa3"),
            company_name="NVIDIA Corporation",
            aliases=["NVIDIA", "NVDA"],
            isin="US67066G1040",
            ticker="NVDA",
            exchange="XNAS",
            mic="XNAS",
            country="US",
            currency="USD",
            known_domains=["nvidia.example"],
        ),
        CompanyResearchProfile(
            instrument_id=UUID("44444444-4444-4444-4444-444444444444"),
            company_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa4"),
            company_name="Reliance Industries Limited",
            aliases=["RELIANCE", "Reliance Industries"],
            isin="INE002A01018",
            ticker="RELIANCE",
            exchange="XNSE",
            mic="XNSE",
            country="IN",
            currency="INR",
            known_domains=["ril.example"],
        ),
    ]
