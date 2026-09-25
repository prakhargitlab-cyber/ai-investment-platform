"""Transient PDF text structure. Character offsets are not PDF geometry."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceRegion:
    start: int
    end: int
    page: int | None = None
    line: int | None = None
    coordinate_space: str = "EXTRACTED_TEXT"
    geometry: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class PdfTextLine:
    ordinal: int
    original_text: str
    normalized_text: str
    region: SourceRegion


@dataclass(frozen=True)
class PdfTextPage:
    ordinal: int
    region: SourceRegion
    lines: tuple[PdfTextLine, ...]


@dataclass(frozen=True)
class PdfTextStructure:
    extracted_text: str
    pages: tuple[PdfTextPage, ...]
    representation: str = "EXTRACTED_LINES"

    def locate(self, start: int, end: int) -> SourceRegion:
        for page in self.pages:
            for line in page.lines:
                if line.region.start <= start < line.region.end:
                    return SourceRegion(start, end, page.ordinal, line.ordinal,
                                        self.representation)
        return SourceRegion(start, end, coordinate_space=self.representation)


def preserve_pdf_structure(text: str, *, legacy: bool = False) -> PdfTextStructure:
    # Import locally: normalization imports the document models.
    from app.normalization import normalize_text

    markers = list(re.finditer(r"\[PDF_PAGE (\d+)\]", text))
    spans = [(int(m.group(1)), m.end(), markers[i + 1].start() if i + 1 < len(markers) else len(text))
             for i, m in enumerate(markers)]
    if not spans or text[:markers[0].start()].strip():
        spans.insert(0, (0, 0, markers[0].start() if markers else len(text)))
    pages = []
    space = "LEGACY_FLATTENED" if legacy else "EXTRACTED_LINES"
    for page, start, end in spans:
        lines = []
        offset = start
        for ordinal, raw in enumerate(text[start:end].splitlines(keepends=True)):
            original = raw.rstrip("\r\n")
            lines.append(PdfTextLine(ordinal, original, normalize_text(original),
                                    SourceRegion(offset, offset + len(original), page, ordinal, space)))
            offset += len(raw)
        pages.append(PdfTextPage(page, SourceRegion(start, end, page, coordinate_space=space), tuple(lines)))
    return PdfTextStructure(text, tuple(pages), space)
