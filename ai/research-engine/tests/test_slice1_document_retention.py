"""Slice 1 (OOM): retained application-owned document state is
O(active work + explicitly bounded cache), not O(total documents processed).

Deterministic and offline: in-memory SQLite persistence, the production
prepare -> apply -> persist ingestion path, weakref-based reachability
counting of the large per-document objects (normalized text holder and the
PdfTextStructure sidecar). RSS is deliberately not used.
"""
from __future__ import annotations

import asyncio
import gc
import sys
import weakref
from uuid import UUID

import pytest

from app.deduplication import DocumentDeduplicator
from app.document_cache import BoundedDocumentCache, DocumentRef
from app.models import DocumentStatus, ReliabilityLevel, SourceClassification, SourceMode, SourceType
from app.persistence import DisabledResearchPersistence, SqliteResearchPersistence
from app.repository import ResearchRepository
from app.research_fetching import FetchError
from app.settings import Settings

MAX_DOCS = 8
MAX_BYTES = 4 * 1024 * 1024


def _settings(**overrides) -> Settings:
    values = dict(research_live_enabled=False, research_demo_enabled=False,
                  research_document_cache_max_documents=MAX_DOCS,
                  research_document_cache_max_bytes=MAX_BYTES)
    values.update(overrides)
    return Settings(**values)


def _repo(persistence=None, **overrides) -> ResearchRepository:
    repo = ResearchRepository(settings=_settings(**overrides),
                              persistence=persistence if persistence is not None else SqliteResearchPersistence())
    # Construction seeds a handful of DEMO fixtures; they are never durable,
    # so they are pinned. Tests measure retention on top of this baseline.
    repo._baseline = repo.documents.stats()
    return repo


def _pinned_delta(repo) -> int:
    return repo.documents.stats()["pinned_documents"] - repo._baseline["pinned_documents"]


def _refs_delta(repo) -> int:
    return repo.documents.stats()["known_refs"] - repo._baseline["known_refs"]


def _durable_resident(repo) -> int:
    return repo.documents.stats()["evictable_documents"]


def _pdf_body(n: int, lines: int = 400) -> str:
    rows = "\n".join(f"Line {i} of synthetic filing {n}: revenue segment detail value {n * 1000 + i}" for i in range(lines))
    return f"[PDF_PAGE 1]\nSynthetic quarterly filing number {n}\n{rows}\n[PDF_PAGE 2]\nNotes to accounts {n}\n"


def _instrument(n: int) -> UUID:
    return UUID(int=10_000 + n)


async def _ingest(repo: ResearchRepository, n: int, *, instrument: UUID | None = None, url: str | None = None,
                  body: str | None = None):
    document = repo._prepare_ingested_document(
        original_url=url or f"https://filings.example.test/{n}.pdf",
        source_type=SourceType.EXCHANGE_ANNOUNCEMENT, source_name="Synthetic", publisher="Synthetic",
        content_type="application/pdf", body=body or _pdf_body(n), reliability=ReliabilityLevel.LEVEL_A,
        published_at=None, source_mode=SourceMode.REAL, source_classification=SourceClassification.OTHER,
        discovered_at=None, discovery_provider=None, expected_profile=None,
        document_status=DocumentStatus.PARSED, allow_empty_content=False, trusted_profile_identity=None)
    document.instrument_id = instrument or _instrument(n % 50)
    return await repo._apply_prepared_ingested_document_async(document, document_status=DocumentStatus.PARSED)


class _Tracker:
    """Weakly tracks the large per-document objects the process could pin."""

    def __init__(self) -> None:
        self.structures: list[weakref.ref] = []
        self.documents: list[weakref.ref] = []

    def track(self, document) -> None:
        self.documents.append(weakref.ref(document))
        if document.pdf_structure is not None:
            self.structures.append(weakref.ref(document.pdf_structure))

    def alive(self) -> tuple[int, int]:
        gc.collect()
        return (sum(ref() is not None for ref in self.documents),
                sum(ref() is not None for ref in self.structures))


# 1 + stress ------------------------------------------------------------------
@pytest.mark.parametrize("total", [300, 2000])
def test_retained_bodies_do_not_grow_with_documents_processed(total):
    repo = _repo()
    tracker = _Tracker()

    async def run():
        for n in range(total):
            document = await _ingest(repo, n)
            assert document.status == DocumentStatus.PROCESSED
            tracker.track(document)
            del document
            stats = repo.documents.stats()
            assert _pinned_delta(repo) == 0  # every REAL doc became durable
            assert stats["evictable_documents"] <= MAX_DOCS
            assert stats["evictable_bytes"] <= MAX_BYTES

    asyncio.run(run())
    stats = repo.documents.stats()
    alive_docs, alive_structures = tracker.alive()
    # O(bounded cache), independent of total.
    assert _durable_resident(repo) <= MAX_DOCS
    assert alive_docs <= MAX_DOCS
    assert alive_structures <= MAX_DOCS
    assert stats["evictions"] >= total - MAX_DOCS
    # Compact refs remain complete and enumerable (O(total) but body-free).
    assert _refs_delta(repo) == total
    assert sum(repo.document_count_for(_instrument(i)) for i in range(50)) == total
    # Never-read platform event log is bounded.
    assert len(repo.platform_events) <= 1024


def test_compact_state_holds_no_document_bodies():
    repo = _repo()
    asyncio.run(_ingest(repo, 1))
    for n in range(2, 60):
        asyncio.run(_ingest(repo, n))
    refs = list(repo.documents._refs.values())
    assert refs and all(isinstance(ref, DocumentRef) for ref in refs)
    for ref in refs:
        for value in ref:
            assert not hasattr(value, "normalized_text") and not hasattr(value, "pages")
    # Per-ref footprint is small and independent of document body size.
    per_ref = sum(sys.getsizeof(ref) + sys.getsizeof(ref.canonical_url) + sys.getsizeof(ref.content_hash)
                  for ref in refs) / len(refs)
    assert per_ref < 1024


# 2 ---------------------------------------------------------------------------
def test_deduplicator_retains_only_compact_identities():
    dedupe = DocumentDeduplicator()
    tracker = _Tracker()
    repo = _repo()
    for n in range(200):
        document = repo._prepare_ingested_document(
            original_url=f"https://filings.example.test/d{n}.pdf", source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
            source_name="S", publisher="S", content_type="application/pdf", body=_pdf_body(n, 50),
            reliability=ReliabilityLevel.LEVEL_A, published_at=None, source_mode=SourceMode.REAL,
            source_classification=SourceClassification.OTHER, discovered_at=None, discovery_provider=None,
            expected_profile=None, document_status=DocumentStatus.PARSED, allow_empty_content=False,
            trusted_profile_identity=None)
        tracker.track(document)
        assert dedupe.add(document) is None
        del document
    assert tracker.alive() == (0, 0)  # the deduplicator pins no document objects
    assert len(dedupe) == 200


# 3 ---------------------------------------------------------------------------
def test_large_objects_unreachable_after_processing_beyond_cache_bound():
    repo = _repo()
    tracker = _Tracker()

    async def run():
        for n in range(MAX_DOCS * 5):
            document = await _ingest(repo, n, instrument=_instrument(1))
            tracker.track(document)

    asyncio.run(run())
    alive_docs, alive_structures = tracker.alive()
    assert alive_structures <= MAX_DOCS and alive_docs <= MAX_DOCS
    repo.documents.max_documents = 1
    repo.documents._evict()
    assert tracker.alive()[1] <= 1


def test_byte_budget_bounds_large_documents_even_below_count_bound():
    repo = _repo(research_document_cache_max_documents=1000, research_document_cache_max_bytes=2 * 1024 * 1024)
    tracker = _Tracker()

    async def run():
        for n in range(30):
            tracker.track(await _ingest(repo, n, body=_pdf_body(n, lines=3000)))

    asyncio.run(run())
    assert repo.documents.stats()["evictable_bytes"] <= 2 * 1024 * 1024
    assert tracker.alive()[1] < 30


# 4 ---------------------------------------------------------------------------
class _FailingPersistence(SqliteResearchPersistence):
    def upsert_document(self, document):
        raise RuntimeError("synthetic persistence failure")


def test_persistence_failure_path_releases_document():
    repo = _repo(_FailingPersistence())
    tracker = _Tracker()

    async def run():
        for n in range(20):
            with pytest.raises(FetchError, match="DOCUMENT_PERSIST_FAILED"):
                document = repo._prepare_ingested_document(
                    original_url=f"https://filings.example.test/f{n}.pdf", source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
                    source_name="S", publisher="S", content_type="application/pdf", body=_pdf_body(n),
                    reliability=ReliabilityLevel.LEVEL_A, published_at=None, source_mode=SourceMode.REAL,
                    source_classification=SourceClassification.OTHER, discovered_at=None, discovery_provider=None,
                    expected_profile=None, document_status=DocumentStatus.PARSED, allow_empty_content=False,
                    trusted_profile_identity=None)
                document.instrument_id = _instrument(1)
                tracker.track(document)
                await repo._apply_prepared_ingested_document_async(document, document_status=DocumentStatus.PARSED)

    asyncio.run(run())
    assert _durable_resident(repo) == 0 and _pinned_delta(repo) == 0
    assert _refs_delta(repo) == 0
    assert repo.document_count_for(_instrument(1)) == 0
    assert tracker.alive() == (0, 0)


def test_timeout_during_processing_releases_document():
    repo = _repo()
    tracker = _Tracker()

    async def slow_persist(*_args, **_kwargs):
        await asyncio.sleep(10)

    repo._run_blocking_persistence = slow_persist  # type: ignore[method-assign]

    async def run():
        for n in range(10):
            document = repo._prepare_ingested_document(
                original_url=f"https://filings.example.test/t{n}.pdf", source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
                source_name="S", publisher="S", content_type="application/pdf", body=_pdf_body(n),
                reliability=ReliabilityLevel.LEVEL_A, published_at=None, source_mode=SourceMode.REAL,
                source_classification=SourceClassification.OTHER, discovered_at=None, discovery_provider=None,
                expected_profile=None, document_status=DocumentStatus.PARSED, allow_empty_content=False,
                trusted_profile_identity=None)
            document.instrument_id = _instrument(1)
            tracker.track(document)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    repo._apply_prepared_ingested_document_async(document, document_status=DocumentStatus.PARSED), 0.01)
            del document

    asyncio.run(run())
    assert _durable_resident(repo) == 0 and _pinned_delta(repo) == 0
    assert repo.document_count_for(_instrument(1)) == 0
    assert tracker.alive() == (0, 0)


# 5 ---------------------------------------------------------------------------
def test_durable_documents_reusable_after_eviction_and_restart():
    store = SqliteResearchPersistence()
    repo = _repo(store)

    async def run():
        for n in range(MAX_DOCS * 3):
            await _ingest(repo, n, instrument=_instrument(7))

    asyncio.run(run())
    assert _durable_resident(repo) <= MAX_DOCS
    before_reloads = repo.documents.reloads
    documents = repo.documents_for(_instrument(7), source_mode=SourceMode.REAL)
    assert len(documents) == MAX_DOCS * 3
    assert all(document.normalized_text for document in documents)
    assert repo.documents.reloads > before_reloads
    assert _durable_resident(repo) <= MAX_DOCS  # re-reads stay bounded too

    restarted = _repo(store)
    assert _durable_resident(restarted) == 0  # restart loads compact refs only
    assert restarted.document_count_for(_instrument(7), source_mode=SourceMode.REAL) == MAX_DOCS * 3
    reloaded = restarted.documents_for(_instrument(7), source_mode=SourceMode.REAL)
    assert [d.document_id for d in reloaded] == [d.document_id for d in documents]
    assert _durable_resident(restarted) <= MAX_DOCS


def test_documents_for_preserves_insertion_order_for_ties():
    repo = _repo()
    ids = []

    async def run():
        for n in range(5):
            ids.append((await _ingest(repo, n, instrument=_instrument(3))).document_id)

    asyncio.run(run())
    first = [d.document_id for d in repo.documents_for(_instrument(3))]
    assert sorted(first) == sorted(ids)
    for _ in range(5):
        assert [d.document_id for d in repo.documents_for(_instrument(3))] == first


def test_non_durable_documents_are_never_evicted():
    repo = _repo(DisabledResearchPersistence())

    async def run():
        for n in range(MAX_DOCS * 3):
            await _ingest(repo, n, instrument=_instrument(9))

    asyncio.run(run())
    # Persistence disabled: the in-memory copy is the only copy -- pinned.
    assert _pinned_delta(repo) == MAX_DOCS * 3
    assert len(repo.documents_for(_instrument(9))) == MAX_DOCS * 3


# 6 ---------------------------------------------------------------------------
def test_duplicate_detection_survives_eviction_and_restart():
    store = SqliteResearchPersistence()
    repo = _repo(store)

    async def run(target):
        first = await _ingest(target, 0, instrument=_instrument(2), url="https://filings.example.test/original.pdf")
        return first

    first = asyncio.run(run(repo))
    asyncio.run(_ingest_many(repo, 1, MAX_DOCS * 4))
    assert first.document_id not in repo.documents  # body evicted

    same_url = asyncio.run(_ingest(repo, 999, instrument=_instrument(2), url="https://filings.example.test/original.pdf"))
    assert same_url.status == DocumentStatus.DUPLICATE and same_url.duplicate_of_document_id == first.document_id
    same_hash = asyncio.run(_ingest(repo, 998, instrument=_instrument(2), url="https://mirror.example.test/copy.pdf",
                                    body=_pdf_body(0)))
    assert same_hash.status == DocumentStatus.DUPLICATE and same_hash.duplicate_of_document_id == first.document_id

    restarted = _repo(store)
    again = asyncio.run(_ingest(restarted, 997, instrument=_instrument(2), url="https://filings.example.test/original.pdf"))
    assert again.status == DocumentStatus.DUPLICATE and again.duplicate_of_document_id == first.document_id
    assert restarted.document_count_for(_instrument(2)) == 1


async def _ingest_many(repo, start, stop):
    for n in range(start, stop):
        await _ingest(repo, n, instrument=_instrument(4))


def test_cache_unit_bounds_and_pinning():
    class Doc:
        def __init__(self, n, size):
            self.document_id = UUID(int=n); self.instrument_id = UUID(int=1); self.source_mode = SourceMode.REAL
            self.canonical_url = f"u{n}"; self.content_hash = f"h{n}"; self.published_at = None
            self.retrieved_at = None; self.normalized_text = "x" * size; self.raw_text = None
            self.pdf_structure = None; self.title = None
    cache = BoundedDocumentCache(max_documents=3, max_bytes=10_000_000)
    for n in range(10):
        cache.put(Doc(n, 10), durable=True)
    assert len(cache) == 3 and cache.stats()["known_refs"] == 10
    cache[UUID(int=100)] = Doc(100, 10)  # subscript write = pinned
    for n in range(10, 20):
        cache.put(Doc(n, 10), durable=True)
    assert UUID(int=100) in cache and cache.is_pinned(UUID(int=100))
    cache.mark_durable(UUID(int=100))
    assert UUID(int=100) not in cache  # oldest evictable once durable
    del cache[UUID(int=19)]
    assert cache.stats()["known_refs"] == 20 and UUID(int=19) not in cache.ids_for_instrument(UUID(int=1))


# Failure-path identity regressions (found by Slice 1; reproduced before fix) --
class _FlakyPersistence(SqliteResearchPersistence):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    def upsert_document(self, document):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("transient persistence failure")
        return super().upsert_document(document)


def test_persistence_failure_then_success_is_not_marked_duplicate():
    repo = _repo(_FlakyPersistence(failures=1))
    url = "https://filings.example.test/retry.pdf"
    with pytest.raises(FetchError, match="DOCUMENT_PERSIST_FAILED"):
        asyncio.run(_ingest(repo, 1, instrument=_instrument(5), url=url))
    retried = asyncio.run(_ingest(repo, 1, instrument=_instrument(5), url=url))
    assert retried.status == DocumentStatus.PROCESSED
    assert retried.duplicate_of_document_id is None
    assert [d.document_id for d in repo.documents_for(_instrument(5))] == [retried.document_id]
    assert [d.document_id for d in repo._persistence.load_documents_by_ids([retried.document_id])] == [retried.document_id]


def test_cancelled_persistence_then_retry_is_not_marked_duplicate():
    repo = _repo()
    url = "https://filings.example.test/cancel.pdf"
    original = repo._run_blocking_persistence

    async def slow_persist(*_args, **_kwargs):
        await asyncio.sleep(10)

    repo._run_blocking_persistence = slow_persist  # type: ignore[method-assign]
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(asyncio.wait_for(_ingest(repo, 1, instrument=_instrument(6), url=url), 0.01))
    repo._run_blocking_persistence = original  # type: ignore[method-assign]
    retried = asyncio.run(_ingest(repo, 1, instrument=_instrument(6), url=url))
    assert retried.status == DocumentStatus.PROCESSED
    assert repo.document_count_for(_instrument(6)) == 1


def test_document_already_durable_but_unknown_in_memory_is_adopted_not_orphaned():
    store = SqliteResearchPersistence()
    first_repo = _repo(store)
    first = asyncio.run(_ingest(first_repo, 1, instrument=_instrument(8), url="https://filings.example.test/adopt.pdf"))
    # A second process that has not rehydrated this identity (e.g. the row was
    # committed by a persistence thread after its caller was cancelled).
    second_repo = _repo(store)
    second_repo._deduplicator = DocumentDeduplicator()
    second_repo.documents = BoundedDocumentCache(MAX_DOCS, MAX_BYTES)
    again = asyncio.run(_ingest(second_repo, 1, instrument=_instrument(8), url="https://filings.example.test/adopt.pdf"))
    assert again.status == DocumentStatus.DUPLICATE
    assert again.duplicate_of_document_id == first.document_id
    ids = [d.document_id for d in second_repo.documents_for(_instrument(8))]
    assert ids == [first.document_id]  # durable row indexed, no orphan id
    assert all(d.normalized_text for d in second_repo.documents_for(_instrument(8)))


# Research events: O(resident instruments), durable reload, restart loads none ---
_EVENT_BODY = ("<html><body><p>Example Industries Limited announced a new order worth Rs {n} crore from a leading "
               "customer and a purchase order under a framework agreement.</p></body></html>")


async def _ingest_event_doc(repo, n, instrument):
    document = repo._prepare_ingested_document(
        original_url=f"https://example.test/news/{n}.html", source_type=SourceType.NEWS, source_name="News",
        publisher="News", content_type="text/html", body=_EVENT_BODY.format(n=100 + n),
        reliability=ReliabilityLevel.LEVEL_B, published_at=None, source_mode=SourceMode.REAL,
        source_classification=SourceClassification.OTHER, discovered_at=None, discovery_provider=None,
        expected_profile=None, document_status=DocumentStatus.PARSED, allow_empty_content=False,
        trusted_profile_identity=None)
    document.instrument_id, document.company_id = instrument, UUID(int=90_000 + instrument.int)
    return await repo._apply_prepared_ingested_document_async(document, document_status=DocumentStatus.PARSED)


def test_research_events_are_bounded_per_instrument_and_reload_from_storage():
    store = SqliteResearchPersistence()
    repo = _repo(store, research_event_cache_max_instruments=5)
    baseline_events = len(repo.events)
    applied_before = repo.events.applied  # DEMO fixtures seeded at construction

    async def run():
        for n in range(200):
            await _ingest_event_doc(repo, n, _instrument(n % 40))

    asyncio.run(run())
    stats = repo.events.stats()
    applied = stats["applied"] - applied_before
    assert applied >= 200
    assert stats["resident_instruments"] <= 5 + stats["pinned_instruments"]
    assert len(repo.events) - baseline_events < applied                   # not O(total events)
    total = sum(len(repo.events_for(_instrument(i))) for i in range(40))  # reload on demand: nothing lost
    assert total == applied
    # Dedup survives eviction: re-ingesting the same content adds no event.
    before = repo.events.applied
    asyncio.run(_ingest_event_doc(repo, 0, _instrument(0)))
    assert repo.events.applied == before
    # Restart: nothing is loaded up front; per-instrument events reload lazily.
    restarted = _repo(store, research_event_cache_max_instruments=5)
    assert len(restarted.events) == baseline_events  # only the pinned DEMO fixtures are resident
    assert len(restarted.events_for(_instrument(3))) == len(repo.events_for(_instrument(3)))
    assert _instrument(3) in restarted.last_refresh
