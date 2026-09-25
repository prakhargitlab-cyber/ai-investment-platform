"""Bounded retention of full ResearchDocument bodies.

Memory hardening (OOM, Slice 1). ResearchRepository is a process-lifetime
singleton. It previously kept every ingested ResearchDocument -- including
raw_text, normalized_text and the per-line PdfTextStructure sidecar -- in a
plain dict for the life of the process, so retained memory grew with the
total number of documents processed across a 2580-instrument cycle.

What the repository actually needs after durable persistence:

* per-instrument enumeration (documents_for) -- satisfied by a compact,
  insertion-ordered index of DocumentRef (ids, url, hash, mode, sort key);
* counts / seen-URL sets for refresh accounting and discovery -- answered
  from the same compact index, never by loading bodies;
* full bodies for re-reads during *active* work on an instrument (financial
  reconciliation, readiness, summaries) -- served from a small cache bounded
  by BOTH a document count and an approximate byte budget, and otherwise
  reloaded from durable storage (PostgreSQL research_documents is the source
  of truth; canonical_url and content_hash are UNIQUE there).

Documents that are NOT durably stored (DEMO fixtures, persistence disabled,
or a REAL document whose persistence has not completed yet) are pinned: they
are never evicted, because eviction would lose the only copy. In production
every REAL document is marked durable as soon as its persistence commits, so
retained bodies are O(active work + explicitly bounded cache), not
O(total documents processed).
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import MutableMapping
from datetime import datetime
from typing import Any, NamedTuple
from uuid import UUID

DEFAULT_MAX_DOCUMENTS = 64
DEFAULT_MAX_BYTES = 64 * 1024 * 1024

# Rough CPython per-object overheads used only to *estimate* retained size;
# the estimate is conservative (over-counts) so the byte bound holds.
_STR_OVERHEAD = 49
_LINE_OVERHEAD = 400  # PdfTextLine + SourceRegion dataclass objects per line


class DocumentRef(NamedTuple):
    """Compact identity of a known document; never carries a body."""
    document_id: UUID
    instrument_id: UUID | None
    source_mode: Any
    canonical_url: str
    content_hash: str
    sort_key: datetime | None


def estimate_document_bytes(document: Any) -> int:
    """Conservative estimate of the memory a cached document keeps alive."""
    size = 2_048
    for field in ("raw_text", "normalized_text", "title"):
        value = getattr(document, field, None)
        if value:
            size += len(value) + _STR_OVERHEAD
    structure = getattr(document, "pdf_structure", None)
    if structure is not None:
        size += len(getattr(structure, "extracted_text", "") or "") + _STR_OVERHEAD
        for page in getattr(structure, "pages", ()) or ():
            for line in getattr(page, "lines", ()) or ():
                size += _LINE_OVERHEAD
                for attr in ("original", "normalized", "text", "original_text", "normalized_text"):
                    value = getattr(line, attr, None)
                    if isinstance(value, str):
                        size += len(value) + _STR_OVERHEAD
    return size


def _ref(document: Any) -> DocumentRef:
    return DocumentRef(
        document.document_id,
        document.instrument_id,
        document.source_mode,
        document.canonical_url,
        document.content_hash,
        document.published_at or document.retrieved_at,
    )


class BoundedDocumentCache(MutableMapping):
    """dict[UUID, ResearchDocument]-compatible store with bounded bodies.

    * ``self[id] = doc`` / ``put(doc)`` registers the compact ref and caches
      the body PINNED (not evictable) -- the safe default for any writer that
      has not proven durability (tests, DEMO, in-flight ingestion).
    * ``mark_durable(id)`` / ``put(doc, durable=True)`` makes a body
      evictable once durable storage holds it.
    * Evictable bodies are bounded by ``max_documents`` and ``max_bytes``.
    * The per-instrument ref index survives eviction and preserves insertion
      order, so enumeration is deterministic and complete.
    """

    def __init__(self, max_documents: int = DEFAULT_MAX_DOCUMENTS, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.max_documents = max(1, int(max_documents))
        self.max_bytes = max(1, int(max_bytes))
        self._data: OrderedDict[UUID, Any] = OrderedDict()
        self._sizes: dict[UUID, int] = {}
        self._pinned: set[UUID] = set()
        self._evictable_bytes = 0
        self._refs: dict[UUID, DocumentRef] = {}
        self._index: dict[UUID, dict[UUID, None]] = {}
        self.evictions = 0
        self.reloads = 0

    # -- MutableMapping surface (bodies currently resident) -----------------
    def __contains__(self, document_id: object) -> bool:
        return document_id in self._data

    def __len__(self) -> int:
        return len(self._data)

    def __iter__(self):
        return iter(list(self._data))

    def __getitem__(self, document_id: UUID):
        # Non-reordering peek: values()/items() iterate through here.
        return self._data[document_id]

    def __setitem__(self, document_id: UUID, document) -> None:
        self.put(document, durable=False, document_id=document_id)

    def __delitem__(self, document_id: UUID) -> None:
        self._data.pop(document_id)
        self._drop_accounting(document_id)
        ref = self._refs.pop(document_id, None)
        if ref is not None and ref.instrument_id is not None:
            bucket = self._index.get(ref.instrument_id)
            if bucket is not None:
                bucket.pop(document_id, None)
                if not bucket:
                    del self._index[ref.instrument_id]

    def get(self, document_id: UUID, default=None):
        if document_id in self._data:
            self._data.move_to_end(document_id)
            return self._data[document_id]
        return default

    # -- writes --------------------------------------------------------------
    def put(self, document, *, durable: bool = False, document_id: UUID | None = None) -> None:
        document_id = document_id or document.document_id
        if document_id in self._data:
            self._drop_accounting(document_id)
        self._data[document_id] = document
        self._data.move_to_end(document_id)
        self._sizes[document_id] = estimate_document_bytes(document)
        if durable:
            self._evictable_bytes += self._sizes[document_id]
        else:
            self._pinned.add(document_id)
        self.register_ref(_ref(document))
        self._evict()

    def mark_durable(self, document_id: UUID) -> None:
        if document_id in self._pinned:
            self._pinned.discard(document_id)
            self._evictable_bytes += self._sizes.get(document_id, 0)
            self._evict()

    def register_ref(self, ref: DocumentRef) -> None:
        self._refs[ref.document_id] = ref
        if ref.instrument_id is not None:
            self._index.setdefault(ref.instrument_id, {})[ref.document_id] = None

    # -- compact reads -------------------------------------------------------
    def refs_for_instrument(self, instrument_id: UUID, source_mode=None) -> list[DocumentRef]:
        refs = [self._refs[i] for i in self._index.get(instrument_id, ()) if i in self._refs]
        if source_mode is not None:
            refs = [ref for ref in refs if ref.source_mode == source_mode]
        return refs

    def ids_for_instrument(self, instrument_id: UUID) -> list[UUID]:
        return list(self._index.get(instrument_id, ()))

    def is_pinned(self, document_id: UUID) -> bool:
        return document_id in self._pinned

    def stats(self) -> dict[str, int]:
        return {
            "resident_documents": len(self._data),
            "pinned_documents": len(self._pinned),
            "evictable_documents": len(self._data) - len(self._pinned),
            "evictable_bytes": self._evictable_bytes,
            "known_refs": len(self._refs),
            "indexed_instruments": len(self._index),
            "evictions": self.evictions,
            "reloads": self.reloads,
        }

    # -- internals -----------------------------------------------------------
    def _drop_accounting(self, document_id: UUID) -> None:
        size = self._sizes.pop(document_id, 0)
        if document_id in self._pinned:
            self._pinned.discard(document_id)
        else:
            self._evictable_bytes -= size

    def _evict(self) -> None:
        evictable = len(self._data) - len(self._pinned)
        if evictable <= self.max_documents and self._evictable_bytes <= self.max_bytes:
            return
        for document_id in list(self._data):
            if evictable <= self.max_documents and self._evictable_bytes <= self.max_bytes:
                break
            if document_id in self._pinned:
                continue
            # Evict only the body; the compact ref stays so the document
            # remains enumerable and reloadable from durable storage.
            self._data.pop(document_id)
            self._evictable_bytes -= self._sizes.pop(document_id, 0)
            evictable -= 1
            self.evictions += 1
