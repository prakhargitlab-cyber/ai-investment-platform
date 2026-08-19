from __future__ import annotations

from app.models import ResearchDocument


class DocumentDeduplicator:
    def __init__(self) -> None:
        self._by_url: dict[str, ResearchDocument] = {}
        self._by_hash: dict[str, ResearchDocument] = {}

    def find_duplicate(self, document: ResearchDocument) -> ResearchDocument | None:
        return self._by_url.get(document.canonical_url) or self._by_hash.get(document.content_hash)

    def add(self, document: ResearchDocument) -> ResearchDocument | None:
        duplicate = self.find_duplicate(document)
        if duplicate:
            return duplicate
        self._by_url[document.canonical_url] = document
        self._by_hash[document.content_hash] = document
        return None
