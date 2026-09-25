from __future__ import annotations

from typing import NamedTuple
from uuid import UUID

from app.models import ResearchDocument


class DocumentIdentity(NamedTuple):
    """Lightweight, durable identity of a persisted document.

    Memory hardening: the deduplicator must not pin full ResearchDocument
    objects (raw_text / normalized_text / pdf_structure) for the life of the
    process just to detect a repeat ingestion. It only needs the compact
    identity below to recognize a duplicate and resolve the earlier
    document's id -- the full object, if needed again, is reloaded from
    durable persistence (the source of truth) via ResearchRepository's
    bounded document cache.
    """
    document_id: UUID
    canonical_url: str
    content_hash: str


class DocumentDeduplicator:
    def __init__(self) -> None:
        self._by_url: dict[str, DocumentIdentity] = {}
        self._by_hash: dict[str, DocumentIdentity] = {}

    def find_duplicate(self, document: ResearchDocument) -> DocumentIdentity | None:
        return self._by_url.get(document.canonical_url) or self._by_hash.get(document.content_hash)

    def add(self, document: ResearchDocument) -> DocumentIdentity | None:
        duplicate = self.find_duplicate(document)
        if duplicate:
            return duplicate
        identity = DocumentIdentity(document.document_id, document.canonical_url, document.content_hash)
        self._by_url[document.canonical_url] = identity
        self._by_hash[document.content_hash] = identity
        return None

    def add_identity(self, document_id: UUID, canonical_url: str, content_hash: str) -> None:
        """Seed known identity (e.g. from durable storage at startup) without
        requiring the full document object -- keeps startup rehydration cheap
        and bounded, independent of historical document body size."""
        identity = DocumentIdentity(document_id, canonical_url, content_hash)
        if canonical_url:
            self._by_url.setdefault(canonical_url, identity)
        if content_hash:
            self._by_hash.setdefault(content_hash, identity)

    def discard(self, document_id: UUID) -> None:
        """Forget an identity whose document was never durably stored (its
        persistence failed or was cancelled), so a retry of the same URL/hash
        is processed instead of being silently marked a duplicate of nothing."""
        for index in (self._by_url, self._by_hash):
            for key in [key for key, identity in index.items() if identity.document_id == document_id]:
                del index[key]

    def forget(self, canonical_url: str, content_hash: str, document_id: UUID) -> None:
        """O(1) variant of discard() when the keys are known."""
        if self._by_url.get(canonical_url, (None,))[0] == document_id:
            del self._by_url[canonical_url]
        if self._by_hash.get(content_hash, (None,))[0] == document_id:
            del self._by_hash[content_hash]

    def __len__(self) -> int:
        return len(self._by_url)
