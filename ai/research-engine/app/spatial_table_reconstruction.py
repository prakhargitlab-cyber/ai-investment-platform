"""Reusable OCR spatial reconstruction.

Root cause this module fixes: the existing table interpreter
(``app.evidence_interpretation._parse_shareholding_table``) read tesseract's
plain TEXT output and assigned each row's Nth number to the header's Nth
period column BY POSITION ONLY -- "the 3rd number on this line belongs to
the 3rd header column". That is exactly the "numerical proximity" guessing
this closure's task prohibits: if OCR drops, merges, or reorders a single
token anywhere in a row (a real, common OCR failure mode, independent of
Screener's specific layout), every value after it silently shifts onto the
wrong column with no warning.

This module instead uses tesseract's WORD-LEVEL BOUNDING BOXES (``tesseract
... tsv``, already produced by the same single subprocess call this
pipeline already runs -- see app.image_evidence_extraction) to associate
each value with its header column by actual horizontal position (x-center
distance to each column's x-center), the same way a person visually reads
a table. A value whose x-position doesn't genuinely line up under any
header column is left UNASSIGNED rather than guessed; a value equidistant
between two columns is flagged ambiguous rather than silently picked.

Evidence-type-agnostic: nothing here knows about shareholding, percentages,
or any specific category label. Any table-shaped evidence type (a
multi-period balance sheet/cash-flow/quarterly-financials screenshot, not
just shareholding) can reuse ``group_into_lines`` / ``find_header_period_columns``
/ ``nearest_column_index`` the same way.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OcrWord:
    """One word-level tesseract TSV row (level 5)."""

    text: str
    left: int
    top: int
    width: int
    height: int
    conf: float
    line_key: tuple[int, int, int]  # (block_num, par_num, line_num) -- tesseract's own visual-line grouping

    @property
    def x_center(self) -> float:
        return self.left + self.width / 2.0

    @property
    def y_center(self) -> float:
        return self.top + self.height / 2.0


def group_into_lines(words: list[OcrWord]) -> list[list[OcrWord]]:
    """Reconstruct reading order: group words into visual lines using
    tesseract's own (block_num, par_num, line_num) grouping (requirement:
    preserve reading order / line positions), each line's words sorted
    left-to-right by x-position, lines sorted top-to-bottom by vertical
    position -- independent of whatever order the words happen to appear
    in the input list.
    """
    lines: dict[tuple[int, int, int], list[OcrWord]] = {}
    for word in words:
        if not word.text.strip():
            continue
        lines.setdefault(word.line_key, []).append(word)
    ordered: list[list[OcrWord]] = []
    for line_words in lines.values():
        line_words.sort(key=lambda w: w.left)
        ordered.append(line_words)
    ordered.sort(key=lambda line_words: min(w.top for w in line_words))
    return ordered


def line_text(line_words: list[OcrWord]) -> str:
    """The plain-text reconstruction of one line, for label matching --
    callers that only need text (not spatial assignment) can treat this
    exactly like a line of the existing text-based parser."""
    return " ".join(w.text for w in line_words)


def find_header_period_columns(line_words: list[OcrWord]) -> list[tuple[object, float]]:
    """Scan one line's words for recognizable reporting-period tokens,
    reusing the exact same deterministic parser every other period-aware
    interpreter uses (app.reporting_period.parse_reporting_period) --
    never a bespoke date guess. Tries a two-word window first ("Jun" +
    "2026" is the common OCR word split for a month/year header cell),
    then a single word. Returns (ReportingPeriod, x_center) tuples in
    left-to-right order; x_center is the column's horizontal position for
    later value association.
    """
    from app.reporting_period import parse_reporting_period

    columns: list[tuple[object, float]] = []
    index = 0
    count = len(line_words)
    while index < count:
        consumed = 1
        period = None
        center = line_words[index].x_center
        if index + 1 < count:
            two_word = f"{line_words[index].text} {line_words[index + 1].text}"
            period = parse_reporting_period(two_word)
            if period is not None:
                center = (line_words[index].x_center + line_words[index + 1].x_center) / 2.0
                consumed = 2
        if period is None:
            period = parse_reporting_period(line_words[index].text)
        if period is not None:
            columns.append((period, center))
        index += consumed
    return columns


def nearest_column_index(
    x_center: float, column_centers: list[float], tolerance_ratio: float = 0.6
) -> tuple[int | None, bool]:
    """Assign a value token to the nearest header column by x-center
    distance -- genuine spatial alignment, never "the Nth number in
    reading order".

    Returns (index, ambiguous):
      - index is None when the nearest column is still farther away than
        a sane tolerance (derived from the smallest real gap between
        adjacent columns) -- a value clearly not under any header is left
        unassigned rather than forced onto whatever column happens to be
        least-far-away.
      - ambiguous=True when two columns are nearly equidistant (within
        20% of the tolerance of each other) -- the caller must flag this
        for manual review rather than silently picking the nearer one.
    """
    if not column_centers:
        return None, False
    distances = [abs(x_center - center) for center in column_centers]
    order = sorted(range(len(distances)), key=lambda i: distances[i])
    best = order[0]
    if len(column_centers) > 1:
        sorted_centers = sorted(column_centers)
        gaps = [sorted_centers[i + 1] - sorted_centers[i] for i in range(len(sorted_centers) - 1)]
        min_gap = min(gaps) if gaps else 0.0
    else:
        min_gap = 0.0
    tolerance = max(min_gap * tolerance_ratio, 45.0)
    if distances[best] > tolerance:
        return None, False
    ambiguous = len(order) > 1 and (distances[order[1]] - distances[best]) < (tolerance * 0.2)
    return best, ambiguous


def looks_like_number_token(text: str) -> bool:
    """True for a word that is purely a numeric/percentage token (no
    letters) -- used to pick out VALUE words on a row independent of how
    many words the row's LABEL happens to be split into by OCR, rather
    than counting word positions after the label."""
    import re

    stripped = text.strip().rstrip("%").replace(",", "")
    return bool(stripped) and bool(re.fullmatch(r"-?\d+(?:\.\d+)?", stripped))
