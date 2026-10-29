"""Transient PDF evidence. Text offsets and PDF user-space points stay distinct."""
from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class SourceRegion:
    start: int
    end: int
    page: int | None = None
    line: int | None = None
    coordinate_space: str = "EXTRACTED_TEXT"
    geometry: tuple[float, float, float, float] | None = None
    column: int | None = None
    table: int | None = None


@dataclass(frozen=True)
class PdfTextWord:
    text: str
    region: SourceRegion
    # Native text has no measured recognition confidence.
    confidence: float | None = None
    quality: str = "EMBEDDED_TEXT"


@dataclass(frozen=True)
class PdfTableCell:
    row: int
    column: int
    column_span: int
    text: str
    words: tuple[PdfTextWord, ...]
    region: SourceRegion
    quality: str = "RESOLVED"


@dataclass(frozen=True)
class PdfTextTable:
    ordinal: int
    column_edges: tuple[float, ...]
    cells: tuple[PdfTableCell, ...]
    region: SourceRegion


@dataclass(frozen=True)
class PdfTextLine:
    ordinal: int
    original_text: str
    normalized_text: str
    region: SourceRegion
    words: tuple[PdfTextWord, ...] = ()


@dataclass(frozen=True)
class PdfTextPage:
    ordinal: int
    region: SourceRegion
    lines: tuple[PdfTextLine, ...]
    tables: tuple[PdfTextTable, ...] = ()


@dataclass(frozen=True)
class PdfTextStructure:
    extracted_text: str
    pages: tuple[PdfTextPage, ...]
    representation: str = "EXTRACTED_LINES"
    source_reference: str | None = None
    content_hash: str | None = None

    def locate(self, start: int, end: int) -> SourceRegion:
        for page in self.pages:
            for line in page.lines:
                if line.region.start <= start < line.region.end:
                    words = [w for w in line.words if w.region.start < end and w.region.end > start]
                    if words:
                        return replace(line.region, start=start, end=end,
                                       geometry=_bounds(w.region.geometry for w in words),
                                       column=words[0].region.column if len({w.region.column for w in words}) == 1 else None,
                                       table=words[0].region.table if len({w.region.table for w in words}) == 1 else None)
                    return SourceRegion(start, end, page.ordinal, line.ordinal,
                                        self.representation)
        return SourceRegion(start, end, coordinate_space=self.representation)


def preserve_pdf_structure(text: str, *, legacy: bool = False,
                           page_layouts: tuple[PdfTextPage | None, ...] = (),
                           source_reference: str | None = None,
                           content_hash: str | None = None) -> PdfTextStructure:
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
        if 0 < page <= len(page_layouts) and page_layouts[page - 1] is not None:
            layout = page_layouts[page - 1]
            shift = start + 1  # the newline immediately after [PDF_PAGE n]

            def region(r):
                return replace(r, start=r.start + shift, end=r.end + shift, page=page)

            words = {w.region.start: replace(w, region=region(w.region)) for line in layout.lines for w in line.words}
            lines = tuple(replace(line, region=region(line.region),
                                  words=tuple(words[w.region.start] for w in line.words)) for line in layout.lines)
            tables = tuple(replace(table, region=region(table.region), cells=tuple(
                replace(cell, region=region(cell.region), words=tuple(words[w.region.start] for w in cell.words))
                for cell in table.cells)) for table in layout.tables)
            pages.append(PdfTextPage(page, region(layout.region), lines, tables))
            continue
        lines = []
        offset = start
        for ordinal, raw in enumerate(text[start:end].splitlines(keepends=True)):
            original = raw.rstrip("\r\n")
            lines.append(PdfTextLine(ordinal, original, normalize_text(original),
                                    SourceRegion(offset, offset + len(original), page, ordinal, space)))
            offset += len(raw)
        pages.append(PdfTextPage(page, SourceRegion(start, end, page, coordinate_space=space), tuple(lines)))
    return PdfTextStructure(text, tuple(pages), "PDF_LAYOUT" if any(p.tables for p in pages) else space,
                            source_reference, content_hash)


def _bounds(boxes):
    boxes = [b for b in boxes if b is not None]
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)) if boxes else None


def extract_pdf_page_layout(page) -> tuple[str, PdfTextPage | None]:
    """Use pypdf's text transforms/font metrics; never estimate glyph widths.

    Only ruled, horizontal tables opt in. Unsupported transforms/fonts, missing
    text, or incompatible pypdf layout APIs retain the existing native-text path.
    The small internal-API adapter is isolated here (layout mode exists in pypdf 5+).
    """
    if not hasattr(page, "get_contents"):
        return page.extract_text() or "", None
    rules = []
    pending_rules = []
    point = None

    def visit(op, args, cm, tm):
        nonlocal point
        def transform(x, y):
            return (float(x) * cm[0] + float(y) * cm[2] + cm[4],
                    float(x) * cm[1] + float(y) * cm[3] + cm[5])
        if op == b"m":
            point = transform(*args)
        elif op == b"l" and point:
            end = transform(*args)
            if abs(end[0] - point[0]) <= 1 and abs(end[1] - point[1]) >= 12:
                pending_rules.append(((point[0] + end[0]) / 2, min(point[1], end[1]), max(point[1], end[1])))
            point = end
        elif op in (b"n", b"S", b"s", b"f", b"f*"):
            if op in (b"S", b"s"):
                rules.extend(pending_rules)
            pending_rules.clear()
            point = None

    native = page.extract_text(visitor_operand_before=visit) or ""
    if len(rules) < 4 or page.get("/Rotate", 0) % 360:
        return native, None
    try:
        from pypdf.generic import ContentStream
        from pypdf._text_extraction._layout_mode._fixed_width_page import recurse_to_target_op
        from pypdf._text_extraction._layout_mode._text_state_manager import TextStateManager

        ops = ContentStream(page["/Contents"].get_object(), page.pdf, "bytes").operations
        _, runs = recurse_to_target_op(iter([*ops, ([], b"Q")]), TextStateManager(),
                                      b"Q", page._layout_mode_fonts())
        fragments = []
        for run in runs:
            a, b, c, d, _, _ = run.transform
            if run.rotated or not run.font.interpretable or abs(b) > 1e-6 or abs(c) > 1e-6 or a <= 0 or d <= 0:
                return native, None
            for match in re.finditer(r"\S+", run.text):
                x0 = run.tx + a * run.word_tx(run.text[:match.start()])
                x1 = run.tx + a * run.word_tx(run.text[:match.end()])
                box = (x0, run.ty, x1, run.ty + run.font_height)
                if not all(math.isfinite(v) for v in box) or x1 <= x0 or run.font_height <= 0:
                    return native, None
                fragments.append((match.group(), box, match.end() < len(run.text) and run.text[match.end()].isspace()))
        # A Form XObject, font decoding loss, etc. must not silently remove text.
        if Counter(re.sub(r"\s", "", native)) != Counter("".join(f[0] for f in fragments)):
            return native, None
        return _ruled_page(fragments, rules) or (native, None)
    except (AttributeError, ImportError, KeyError, TypeError, ValueError, IndexError):
        logging.getLogger(__name__).debug("pdf_layout_unavailable", exc_info=True)
        return native, None


def _ruled_page(fragments, rules):
    from app.normalization import normalize_text

    # Scanned ruling segments drift slightly; cluster only nearby x coordinates.
    clusters = []
    for rule in sorted(rules):
        if not clusters or rule[0] - clusters[-1][0][0] > 3:
            clusters.append([])
        clusters[-1].append(rule)
    clusters = [c for c in clusters if sum(r[2] - r[1] for r in c) >= 25]
    edges = tuple(sum(r[0] for r in c) / len(c) for c in clusters)
    if len(edges) < 4:
        return None
    rows = []
    for fragment in sorted(fragments, key=lambda f: (-f[1][1], f[1][0])):
        if not rows or abs(fragment[1][1] - rows[-1][0][1][1]) > min(fragment[1][3] - fragment[1][1], 7) * .3:
            rows.append([])
        rows[-1].append(fragment)
    lines, cells, text_parts, offset = [], [], [], 0
    for row, fragments in enumerate(rows):
        merged = []
        for token, box, space in sorted(fragments, key=lambda f: f[1][0]):
            if merged and not merged[-1][2] and -.2 <= box[0] - merged[-1][1][2] <= (box[3] - box[1]) * .25:
                old, old_box, _ = merged.pop()
                merged.append((old + token, _bounds((old_box, box)), space))
            else:
                merged.append((token, box, space))
        words = []
        for token, box, _ in merged:
            words.append(PdfTextWord(token, SourceRegion(offset, offset + len(token), 0, row, "PDF_POINTS", box),
                                     quality="DAMAGED" if "\ufffd" in token else "EMBEDDED_TEXT"))
            offset += len(token) + 1
        original = " ".join(w.text for w in words)
        line_region = SourceRegion(offset - len(original) - 1, offset - 1, 0, row, "PDF_POINTS",
                                   _bounds(w.region.geometry for w in words))
        text_parts.append(original)
        # Rulings at the text's vertical center establish merged header cells.
        center = sum((w.region.geometry[1] + w.region.geometry[3]) / 2 for w in words) / len(words)
        active = [i for i, c in enumerate(clusters) if any(lo - .5 <= center <= hi + .5 for _, lo, hi in c)]
        ownership = {}
        for left, right in zip(active, active[1:]):
            owned = [w for w in words if edges[left] - 1 <= w.region.geometry[0]
                     and w.region.geometry[2] <= edges[right] + 1]
            if not owned:
                continue
            quality = "AMBIGUOUS" if any(a.region.geometry[2] > b.region.geometry[0] + .3
                                        for a, b in zip(owned, owned[1:])) else "RESOLVED"
            owned = tuple(replace(w, region=replace(w.region, column=left, table=0)) for w in owned)
            ownership.update({w.region.start: w for w in owned})
            region = SourceRegion(owned[0].region.start, owned[-1].region.end, 0, row, "PDF_POINTS",
                                  (edges[left], line_region.geometry[1], edges[right], line_region.geometry[3]), left, 0)
            cells.append(PdfTableCell(row, left, right - left, " ".join(w.text for w in owned), owned, region, quality))
        words = tuple(ownership.get(w.region.start, w) for w in words)
        lines.append(PdfTextLine(row, original, normalize_text(original), line_region, words))
    text = "\n".join(text_parts)
    region = SourceRegion(0, len(text), 0, coordinate_space="PDF_POINTS",
                          geometry=_bounds(line.region.geometry for line in lines), table=0)
    return text, PdfTextPage(0, region, tuple(lines), (PdfTextTable(0, edges, tuple(cells), region),))
