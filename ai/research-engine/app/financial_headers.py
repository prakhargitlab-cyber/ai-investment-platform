"""Header-only constraint resolver. Never reads financial metric values."""
from __future__ import annotations

import re
from datetime import date

from app.financial_structure_types import (
    FinancialColumn, HeaderEvidence, HeaderResolution, HeaderTokenEvidence, StatementDiagnostic,
)
from app.pdf_structure import SourceRegion

GROUP = re.compile(r"\b(?:(?:quarter|three\s+months?|nine\s+months?|half\s+year|six\s+months?|year)\s+ended|as\s+at|(?:current|previous|corresponding)\s+quarter)\b", re.I)
MONTHS = {name: i for i, name in enumerate(
    ('january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
     'september', 'october', 'november', 'december'), 1)}
MONTH = '(?:' + '|'.join(MONTHS) + ')'
# Date-shaped corruption is retained as a slot, not removed before alignment.
DATE = re.compile(
    rf"(?<!\w)[0-9?]{{1,2}}[./-][0-9?]{{1,2}}[./-][0-9A-Za-z?]{{4}}(?!\w)"
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+{MONTH}\s+[0-9?]{{4}}(?!\w)"
    rf"|\b{MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+[0-9?]{{4}}(?!\w)"
    r"|\?{2,}|\[missing(?: date)?\]", re.I)
AUDIT = re.compile(r"\b(?:unaudited|audited)\b", re.I)
NOISE = re.compile(r"\b(?:s\.?\s*no\.?|sl\.?\s*no\.?|sr\.?\s*no\.?|refer\s+note\s+\d+)\b", re.I)


def period_kind(label: str) -> tuple[str, int | None]:
    value = label.lower()
    if 'quarter' in value or value.startswith('three'):
        return 'QUARTERLY', 3
    if value.startswith('nine'):
        return 'NINE_MONTH', 9
    if value.startswith(('half', 'six')):
        return 'HALF_YEAR', 6
    if value.startswith('as'):
        return 'AS_AT', None
    return 'ANNUAL', 12


def parse_date(value: str) -> str | None:
    parts = re.sub(r'(\d)(?:st|nd|rd|th)', r'\1', value.lower())
    try:
        if match := re.fullmatch(r'(\d{1,2})[./-](\d{1,2})[./-](\d{4})', parts):
            day, month, year = map(int, match.groups())
        else:
            words = parts.replace(',', '').split()
            if len(words) != 3:
                return None
            if words[0] in MONTHS:
                month, day, year = MONTHS[words[0]], int(words[1]), int(words[2])
            else:
                day, month, year = int(words[0]), MONTHS[words[1]], int(words[2])
        return date(year, month, day).isoformat()
    except (ValueError, KeyError):
        return None


def resolve_financial_header(text: str, region: SourceRegion | None = None) -> HeaderResolution:
    """Resolve only unique explicit group ownership; counts never imply widths.

    Supported proofs: one uniform group, interleaved group/date runs, pipe
    partitions, or an exact character-column text grid. Text-grid positions
    are offsets in extracted lines, not manufactured PDF coordinates.
    """
    region = region or SourceRegion(0, len(text))
    diagnostics = []
    tokens = []

    def source(start, end):
        return SourceRegion(region.start + start, region.start + end, region.page,
                            region.line, region.coordinate_space)

    def diag(reason, severity='INFO', **details):
        diagnostics.append(StatementDiagnostic('HEADER', reason, region,
                           tuple(sorted((k, str(v)) for k, v in details.items())), severity))

    groups = list(GROUP.finditer(text))
    dates = list(DATE.finditer(text))
    audits = list(AUDIT.finditer(text))
    for kind, matches in [('GROUP', groups), ('DATE', dates), ('AUDIT', audits),
                          ('ANNOTATION', list(NOISE.finditer(text)))]:
        for match in matches:
            value = parse_date(match.group()) if kind == 'DATE' else (
                period_kind(match.group())[0] if kind == 'GROUP' else match.group().upper())
            tokens.append(HeaderTokenEvidence(kind, match.group(), value, source(match.start(), match.end())))
    # Empty explicit cells are unresolved evidence, never silently removed.
    offset = 0
    for line in text.splitlines(keepends=True):
        if '|' in line and DATE.search(line):
            cell_offset = 0
            for cell in line.split('|'):
                if cell.strip() in {'', '-'}:
                    tokens.append(HeaderTokenEvidence('DATE', cell, None,
                                  source(offset + cell_offset, offset + cell_offset + len(cell))))
                cell_offset += len(cell) + 1
        offset += len(line)
    tokens.sort(key=lambda token: token.region.start)
    diag('HEADER_FOUND')
    if groups:
        diag('GROUPS_FOUND', count=len(groups))
    if dates:
        diag('DATES_FOUND', count=len(dates))

    def finish(columns=(), strategies=()):
        return HeaderResolution(tuple(columns), HeaderEvidence(tuple(tokens), tuple(strategies)), tuple(diagnostics))

    if not dates:
        diag('PERIOD_DATE_AMBIGUOUS', 'ERROR')
        return finish()
    date_tokens = [token for token in tokens if token.kind == 'DATE']
    if any(token.value is None for token in date_tokens):
        diag('PERIOD_DATE_INVALID', 'ERROR', unresolved_slots=','.join(
            str(i) for i, token in enumerate(date_tokens) if token.value is None))
        return finish()
    residue = text
    for pattern in (GROUP, DATE, AUDIT, NOISE):
        residue = pattern.sub('', residue)
    residue = re.sub(r'\b(?:particulars|for\s+the)\b', '', residue, flags=re.I)
    if re.sub(r'[\s|():,.*\-]+', '', residue):
        # A lost year, an unknown date token, or other unexplained header
        # content must not disappear while the remaining dates shift left.
        diag('PERIOD_DATE_AMBIGUOUS', 'ERROR')
        return finish()
    if not groups:
        diag('PERIOD_TYPE_AMBIGUOUS', 'ERROR')
        return finish()

    proposals: list[tuple[str, tuple[int, ...]]] = []
    if len(groups) == 1 and groups[0].end() <= dates[0].start():
        proposals.append(('UNIFORM_GROUP', tuple(0 for _ in dates)))
    # Each group must own at least one date before the next group begins.
    runs = [[i for i, d in enumerate(dates) if g.end() <= d.start()
             < (groups[j + 1].start() if j + 1 < len(groups) else len(text))]
            for j, g in enumerate(groups)]
    if all(runs) and sum(map(len, runs)) == len(dates):
        assignment = tuple(next(j for j, run in enumerate(runs) if i in run) for i in range(len(dates)))
        proposals.append(('INTERLEAVED_GROUPS', assignment))

    lines = text.splitlines(keepends=True)
    offsets, offset = [], 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)

    def in_line(matches, index):
        return [(i, m) for i, m in enumerate(matches)
                if offsets[index] <= m.start() < offsets[index] + len(lines[index])]

    for gi, group_line in enumerate(lines):
        local_groups = in_line(groups, gi)
        if not local_groups:
            continue
        for di in range(gi + 1, len(lines)):
            local_dates = in_line(dates, di)
            if len(local_dates) != len(dates):
                continue
            date_line = lines[di]
            # Explicit partitions provide spans even with proportional spacing.
            if '|' in group_line and '|' in date_line:
                gp, dp = group_line.split('|'), date_line.split('|')
                if len(gp) != len(dp) or any(len(list(GROUP.finditer(part))) != 1 for part in gp):
                    diag('HEADER_EVIDENCE_CONFLICT', 'ERROR')
                    return finish()
                assignment = []
                for part_index, part in enumerate(dp):
                    assignment.extend([local_groups[part_index][0]] * len(list(DATE.finditer(part))))
                if all(DATE.search(part) for part in dp) and len(assignment) == len(dates):
                    proposals.append(('DELIMITED_GROUP_SPANS', tuple(assignment)))
            # Exact starts establish a text grid; ordinary single-space prose
            # cannot qualify. Every group start must coincide with a date slot.
            elif len(local_groups) > 1:
                starts = [m.start() - offsets[gi] for _, m in local_groups]
                date_starts = [m.start() - offsets[di] for _, m in local_dates]
                wide_gaps = all(re.search(r' {2,}|\t', text[a.end():b.start()])
                                for (_, a), (_, b) in zip(local_groups, local_groups[1:]))
                if wide_gaps and starts[0] == date_starts[0] and all(s in date_starts for s in starts):
                    assignment = tuple(local_groups[max(j for j, s in enumerate(starts) if s <= d)][0]
                                       for d in date_starts)
                    proposals.append(('EXPLICIT_TEXT_GRID', assignment))

    semantic = {tuple(period_kind(groups[j].group()) for j in assignment) for _, assignment in proposals}
    covered = {j for _, assignment in proposals for j in assignment}
    # Unconsumed group labels are contradictory/ambiguous evidence, not noise.
    if len(semantic) > 1 or (proposals and covered != set(range(len(groups)))):
        diag('HEADER_EVIDENCE_CONFLICT', 'ERROR')
        return finish(strategies=[name for name, _ in proposals])
    if not proposals:
        diag('HEADER_GROUP_SPAN_AMBIGUOUS', 'ERROR')
        return finish()
    if audits and len(audits) != len(dates):
        diag('COLUMN_COUNT_AMBIGUOUS', 'ERROR', audit_slots=len(audits), date_slots=len(dates))
        return finish()
    assignment = proposals[0][1]
    # Audit labels are attached only for a separate complete status row or
    # one status after each date in an interleaved representation.
    audit_values = [None] * len(dates)
    if audits:
        separate = any(len(in_line(audits, i)) == len(dates) and offsets[i] > dates[-1].end()
                       for i in range(len(lines)))
        interleaved = all(d.end() <= a.start() < (dates[i + 1].start() if i + 1 < len(dates) else len(text))
                          for i, (d, a) in enumerate(zip(dates, audits)))
        if separate or interleaved:
            audit_values = [a.group().upper() for a in audits]
    columns = []
    for i, match in enumerate(dates):
        group = groups[assignment[i]]
        kind, duration = period_kind(group.group())
        columns.append(FinancialColumn(i, parse_date(match.group()), kind, group.group(), duration,
                       audit_values[i], (source(group.start(), group.end()),), source(match.start(), match.end()), 'RESOLVED'))
    diag('COLUMN_MODEL_RESOLVED', count=len(columns))
    return finish(columns, [name for name, _ in proposals])
