"""Financial structure: verified PDF tables and conservative text diagnostics."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace

from app.financial_headers import AUDIT, DATE, GROUP, NOISE, resolve_financial_header
from app.financial_structure_types import (
    FinancialParseResult, FinancialStatementCandidate, HeaderEvidence, HeaderResolution,
    HeaderTokenEvidence, StatementDiagnostic,
)
from app.pdf_structure import PdfTextStructure, preserve_pdf_structure

HEADING = re.compile(
    r'\b(?:extract\s+of\s+)?(?:statement\s+of\s+)?'
    r'(?:(?:audited|unaudited|standalone|consolidated)\s+){0,4}financial\s+results\b'
    r'|\b(?:(?:standalone|consolidated)\s+)?(?:statement\s+of\s+(?:profit\s+and\s+loss|income)|income\s+statement|balance\s+sheet|cash\s+flow\s+statement|statement\s+of\s+cash\s+flows?)\b', re.I)
PARTICULARS = re.compile(r'\bparticulars\b', re.I)
BASIS = re.compile(r'\b(?:standalone|consolidated)\b', re.I)
SECTION_END = re.compile(r'\b(?:notes\s*:|notes\s+to\b|segment\s+(?:information|results|revenue))', re.I)


def _header_line_remainder(text):
    for pattern in (GROUP, DATE, AUDIT, NOISE, PARTICULARS):
        text = pattern.sub('', text)
    return re.sub(r'[\s|():,.*\-]+', '', text)


def _hash(payload) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _fingerprint(kind, basis_pattern, header, text, page_count):
    dates = [t for t in header.evidence.tokens if t.kind == 'DATE']
    equality, seen = [], {}
    for token in dates:
        # Invalid raw tokens never enter the fingerprint.
        if token.value is None:
            equality.append('UNRESOLVED')
        else:
            equality.append(seen.setdefault(token.value, len(seen)))
    formats = [('NUMERIC' if re.search(r'[./-]', t.original) else 'NAMED')
               if t.value else 'DAMAGED' for t in dates]
    return _hash({
        'type': kind, 'basis_placement': basis_pattern,
        'tokens': [(t.kind, t.value if t.kind in {'GROUP', 'AUDIT'} else None)
                   for t in header.evidence.tokens],
        'date_equality': equality, 'date_formats': formats,
        'particulars': 'BEFORE_GROUPS' if PARTICULARS.search(text) and GROUP.search(text)
            and PARTICULARS.search(text).start() < GROUP.search(text).start() else 'OTHER',
        'strategies': header.evidence.strategies, 'page_count': page_count,
        'damage': bool(re.search(r'[\u2500-\u257f\ufffd]', text)),
        'rejections': [d.reason for d in header.diagnostics if d.severity == 'ERROR'],
    })


def parse_financial_structure(text: str = '', *, structure: PdfTextStructure | None = None) -> FinancialParseResult:
    structure = structure or preserve_pdf_structure(text, legacy=True)
    text = structure.extracted_text
    accepted, rejected, diagnostics = [], [], []
    if not text.strip():
        diagnostic = StatementDiagnostic('INPUT', 'NO_NORMALIZED_TEXT', structure.locate(0, 0), severity='ERROR')
        return FinancialParseResult((), (), (diagnostic,), _hash(['EMPTY']), 'INPUT_UNAVAILABLE')
    layout_accepted, layout_rejected, layout_diagnostics, handled_pages = _layout_statements(structure)
    accepted.extend(layout_accepted)
    rejected.extend(layout_rejected)
    diagnostics.extend(layout_diagnostics)
    headings = list(HEADING.finditer(text))
    for index, heading in enumerate(headings):
        if structure.locate(heading.start(), heading.end()).page in handled_pages:
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        section = SECTION_END.search(text, heading.end(), end)
        if section:
            end = section.start()
        source = structure.locate(heading.start(), end)
        local = []

        def diag(stage, reason, severity='INFO'):
            local.append(StatementDiagnostic(stage, reason, source, severity=severity))

        diag('STATEMENT', 'STATEMENT_FOUND')
        kind = 'BALANCE_SHEET' if 'balance' in heading.group().lower() else (
            'CASH_FLOW' if 'cash' in heading.group().lower() else 'INCOME_STATEMENT')
        particulars = PARTICULARS.search(text, heading.end(), min(end, heading.end() + 1500))
        basis_values = {m.group().upper() for m in BASIS.finditer(heading.group())}
        basis = next(iter(basis_values)) if len(basis_values) == 1 else None
        # A separate immediately preceding basis line is explicit scope evidence.
        prefix = text[max(0, heading.start() - 40):heading.start()]
        preceding = re.search(r'(?:^|\n)\s*(standalone|consolidated)\s*$', prefix, re.I)
        if preceding:
            basis_values.add(preceding.group(1).upper())
            basis = next(iter(basis_values)) if len(basis_values) == 1 else None
        diag('BASIS', 'REPORTING_BASIS_FOUND' if basis else 'REPORTING_BASIS_AMBIGUOUS',
             'INFO' if basis else 'ERROR')
        # Extracts and narrative mentions are visible rejected candidates.
        preceding_sentence = text[max(0, heading.start() - 100):heading.start()].split('\n')[-1]
        unscoped_prefix = re.sub(r'^.*\[PDF_PAGE \d+\]\s*$', '', preceding_sentence).strip()
        if 'extract' in heading.group().lower() or unscoped_prefix:
            diag('STATEMENT', 'UNSUPPORTED_STATEMENT_LAYOUT', 'ERROR')
        header_region = body_region = None
        header = HeaderResolution((), HeaderEvidence(()), ())
        if not particulars:
            diag('HEADER', 'NO_PARTICULARS', 'ERROR')
        else:
            if '[PDF_PAGE' in text[heading.end():particulars.start()]:
                diag('STATEMENT', 'STATEMENT_BOUNDARY_AMBIGUOUS', 'ERROR')
            # Header begins at the contiguous period-group suffix before
            # Particulars, excluding the statement's narrative reporting date.
            before = text[heading.end():particulars.start()]
            suffix = re.search(r'((?:(?:quarter|three months|nine months|half year|year)\s+ended\s*)+(?:(?:S\.?No\.?|Sl\.\s*No\.|Sr\.\s*No\.)\s*)?)$', before, re.I)
            start = heading.end() + suffix.start() if suffix else particulars.end()
            if structure.representation != 'LEGACY_FLATTENED':
                cursor = particulars.start()
                for line in reversed(before.splitlines(keepends=True)):
                    cursor -= len(line)
                    if _header_line_remainder(line) or DATE.search(line):
                        break
                    if GROUP.search(line):
                        start = cursor
            # Structured input uses header grammar, not metric recognition.
            # Legacy flattened text retains a conservative lexical boundary.
            limit = min(end, start + 1600)
            header_end = None
            if structure.representation != 'LEGACY_FLATTENED':
                cursor = particulars.end()
                saw_dates = False
                for line in text[cursor:limit].splitlines(keepends=True):
                    if saw_dates and _header_line_remainder(line):
                        header_end = cursor
                        break
                    saw_dates = saw_dates or bool(DATE.search(line))
                    cursor += len(line)
            else:
                boundary = re.search(r'\b(?:income|revenue|expenses?|net\s+profit|profit\s+(?:before|after)|assets|liabilities|cash\s+flows?\s+from)\b', text[particulars.end():limit], re.I)
                header_end = particulars.end() + boundary.start() if boundary else None
            if header_end is None:
                header_end = limit
                diag('HEADER', 'STATEMENT_BOUNDARY_AMBIGUOUS', 'ERROR')
            header_region = structure.locate(start, header_end)
            body_region = structure.locate(header_end, end)
            header = resolve_financial_header(text[start:header_end], header_region)
            header = replace(header,
                evidence=replace(header.evidence, tokens=tuple(replace(token,
                    region=structure.locate(token.region.start, token.region.end)) for token in header.evidence.tokens)),
                columns=tuple(replace(column,
                    source_region=structure.locate(column.source_region.start, column.source_region.end),
                    group_evidence=tuple(structure.locate(r.start, r.end) for r in column.group_evidence))
                    for column in header.columns))
            local.extend(header.diagnostics)
        unit_tokens = tuple(HeaderTokenEvidence('UNIT', m.group(), None,
                            structure.locate(heading.start() + m.start(), heading.start() + m.end()))
                            for m in re.finditer(r'\b(?:INR|Rs\.?|rupees|crores?|lakhs?|millions?)\b',
                            text[heading.start():header_region.start if header_region else heading.end()], re.I))
        valid = bool(header.columns) and not any(d.severity == 'ERROR' for d in local)
        fingerprint = _fingerprint(kind, 'TITLE' if BASIS.search(heading.group()) else
                                   'PRECEDING_LINE' if preceding else 'UNRESOLVED', header,
                                   text[heading.start():header_region.end if header_region else heading.end()],
                                   text[heading.start():end].count('[PDF_PAGE'))
        candidate = FinancialStatementCandidate(kind, basis, source, header_region, body_region,
                    unit_tokens, header, 'RESOLVED' if valid else 'REJECTED', fingerprint)
        (accepted if valid else rejected).append(candidate)
        diagnostics.extend(local)
    if not headings:
        diagnostics.append(StatementDiagnostic('STATEMENT', 'NO_FINANCIAL_STATEMENT_FOUND',
                           structure.locate(0, len(text)), severity='ERROR'))
    state = 'SUCCESS_PARTIAL' if accepted and rejected else 'SUCCESS_COMPLETE' if accepted else 'REJECTED' if rejected else 'NO_STATEMENT'
    fingerprint = _hash([c.structural_fingerprint for c in sorted(accepted + rejected, key=lambda c: c.source_region.start)])
    return FinancialParseResult(tuple(accepted), tuple(rejected), tuple(diagnostics), fingerprint, state)


def _physical_column(table, box):
    """A word must fit wholly inside exactly one ruled physical column."""
    owners = [i for i, (left, right) in enumerate(zip(table.column_edges, table.column_edges[1:]))
              if left - 1 <= box[0] and box[2] <= right + 1]
    return owners[0] if len(owners) == 1 else None


def _layout_unit(structure, page, heading, last_header_row, basis, period_end):
    """A damaged scale needs a unique exact INR scale in same-basis result notes.

    Corroboration cannot supply a missing currency or a missing unit label, nor
    cross reporting bases. The damaged label must independently identify the
    same scale within two character edits. Every supporting span is retained.
    """
    from app.structured_research import _edit_distance
    from app.financial_headers import parse_date
    scales = ('lakh', 'crore', 'million', 'billion', 'thousand')
    labels = []
    for line in page.lines:
        if heading.ordinal < line.ordinal < last_header_row:
            for match in re.finditer(r'\b(?:Rs\.?|INR|rupees?)\s+in\s+([A-Za-z.]+)', line.original_text, re.I):
                raw = match.group(1).lower().replace('.', '')
                exact = next((s for s in scales if raw in {s, s + 's'}), None)
                labels.append((line, match, raw, exact))
    if not labels:
        return None, ()
    evidence, resolved = [], set()
    for line, match, raw, exact in labels:
        region = structure.locate(line.region.start + match.start(), line.region.start + match.end())
        support = []
        if exact is None:
            possibilities = {s for s in scales if min(_edit_distance(raw, s), _edit_distance(raw, s + 's')) <= 2}
            for notes_page in structure.pages:
                body = structure.extracted_text[notes_page.region.start:notes_page.region.end]
                note = re.search(r'\bnotes\s+to\s+(?:the\s+)?statement\s+of\s+(?:audited\s+|unaudited\s+)?'
                                 + basis + r'\s+financial\s+results\b', body, re.I)
                title = body[note.end():note.end() + 180] if note else ''
                title = re.sub(r'(?<=[A-Za-z]),(?=\s*\d{4})', '', title)
                if not note or period_end not in {parse_date(m.group()) for m in DATE.finditer(title)}:
                    continue
                for corroboration in re.finditer(r'\b(?:Rs\.?|INR|rupees?)\s+[0-9][0-9,.]*\s+'
                                                  r'(lakhs?|crores?|millions?|billions?|thousands?)\b', body[note.end():], re.I):
                    scale = corroboration.group(1).lower().rstrip('s')
                    start = notes_page.region.start + note.end() + corroboration.start()
                    support.append(HeaderTokenEvidence('UNIT_CORROBORATION', corroboration.group(), 'INR ' + scale,
                                                       structure.locate(start, start + len(corroboration.group()))))
            corroborated = {s.value.removeprefix('INR ') for s in support}
            if len(corroborated) == 1 and corroborated <= possibilities:
                exact = next(iter(corroborated))
        value = 'INR ' + exact if exact else None
        evidence.append(HeaderTokenEvidence('UNIT', match.group(), value, region))
        evidence.extend(support)
        resolved.add(value)
    return (next(iter(resolved)) if len(resolved) == 1 else None), tuple(evidence)


def _layout_statements(structure):
    from app.financial_headers import parse_date
    from app.financial_structure_types import FinancialColumn

    accepted, rejected, diagnostics, handled = [], [], [], set()
    for page in structure.pages:
        titles = [(line, HEADING.search(line.original_text)) for line in page.lines]
        titles = [(line, match) for line, match in titles if match and not line.original_text[:match.start()].strip()]
        if not page.tables or not titles:
            continue
        handled.add(page.ordinal)
        diagnostics.append(StatementDiagnostic('LAYOUT', 'LAYOUT_STATEMENT_PAGE', page.region))
        for title, heading in titles:
            bases = {b.group().upper() for b in BASIS.finditer(heading.group())}
            basis = next(iter(bases)) if len(bases) == 1 else None
            source = structure.locate(title.region.start, page.region.end)
            if not basis or len(titles) != 1:
                diagnostics.append(StatementDiagnostic('BASIS', 'REPORTING_BASIS_AMBIGUOUS', source, severity='ERROR'))
                continue
            for table in page.tables:
                for group in table.cells:
                    label = re.sub(r'\s+on\s*$', '', group.text, flags=re.I)
                    if not title.ordinal < group.row <= title.ordinal + 6 or not GROUP.fullmatch(label):
                        continue
                    group_columns = set(range(group.column, group.column + group.column_span))
                    date_lines = [line for line in page.lines if group.row < line.ordinal <= group.row + 4
                                  and any(re.fullmatch(r'\d{1,2}[-./<]+[0-9A-Za-z?]{1,2}[-./]+[0-9A-Za-z?]{4}', w.text)
                                          for w in line.words)]
                    reason, columns, tokens, audit_tokens = None, [], [], []
                    if group.quality != 'RESOLVED' or len(date_lines) != 1:
                        reason = 'HEADER_GROUP_SPAN_AMBIGUOUS'
                    else:
                        date_line = date_lines[0]
                        dates = {}
                        all_date_columns = []
                        for word in date_line.words:
                            owner = _physical_column(table, word.region.geometry)
                            if re.search(r'\d', word.text) and owner is not None:
                                all_date_columns.append(owner)
                            if owner in group_columns:
                                dates.setdefault(owner, []).append(word)
                        if set(dates) != group_columns or any(len(v) != 1 for v in dates.values()):
                            reason = 'HEADER_DATE_SLOT_MISMATCH'
                        else:
                            statuses = {}
                            audit_lines = [line for line in page.lines
                                           if date_line.ordinal < line.ordinal <= date_line.ordinal + 2
                                           and any(AUDIT.fullmatch(w.text.replace('-', '')) for w in line.words)]
                            for line in audit_lines:
                                for word in line.words:
                                    owner = _physical_column(table, word.region.geometry)
                                    if owner in group_columns:
                                        statuses.setdefault(owner, []).append(word)
                            date_values, audit_values = [], []
                            for owner in sorted(group_columns):
                                word = dates[owner][0]
                                # Repeated separators change no date digits.
                                value = parse_date(re.sub(r'(?<=\d)--(?=\d)', '-', word.text))
                                status = statuses.get(owner, [])
                                audit = status[0].text.upper().replace('-', '') if len(status) == 1 else None
                                tokens.append(HeaderTokenEvidence('DATE', word.text, value, replace(word.region, column=owner)))
                                if len(status) == 1:
                                    audit_tokens.append(HeaderTokenEvidence('AUDIT', status[0].text, audit,
                                                                           replace(status[0].region, column=owner)))
                                if value is None or audit not in {'AUDITED', 'UNAUDITED'}:
                                    reason = 'PERIOD_DATE_INVALID' if value is None else 'AUDIT_STATUS_UNRESOLVED'
                                date_values.append(value)
                                audit_values.append(audit)
                            if reason is None:
                                raw_dates = [re.sub(r'(?<=\d)--(?=\d)', '-', dates[owner][0].text)
                                             for owner in sorted(group_columns)]
                                header = resolve_financial_header(label + '\n' + ' '.join(raw_dates)
                                                                  + '\n' + ' '.join(audit_values))
                                if not header.columns or any(a <= b for a, b in zip(date_values, date_values[1:])):
                                    reason = 'HEADER_EVIDENCE_CONFLICT'
                                else:
                                    first = min(all_date_columns)
                                    columns = [replace(c, source_region=tokens[i].region, group_evidence=(group.region,),
                                                       source_column=owner - first + 1, resolution_state='LAYOUT_RESOLVED')
                                               for i, (c, owner) in enumerate(zip(header.columns, sorted(group_columns)))]
                    tokens = [HeaderTokenEvidence('GROUP', group.text, columns[0].period_type if columns else None,
                                                   group.region), *tokens, *audit_tokens]
                    header = HeaderResolution(tuple(columns), HeaderEvidence(tuple(tokens), ('RULED_PDF_COLUMNS',)), ())
                    current_period = max(c.period_end for c in columns) if columns else ''
                    unit, unit_evidence = _layout_unit(structure, page, title, date_lines[0].ordinal if date_lines else group.row,
                                                      basis, current_period)
                    candidate = FinancialStatementCandidate('INCOME_STATEMENT', basis, source, group.region,
                        source, unit_evidence, header, 'REJECTED' if reason else 'RESOLVED',
                        _hash(['RULED_PDF_COLUMNS', basis, [(c.source_column, c.period_type) for c in columns]]), table)
                    (rejected if reason else accepted).append(candidate)
                    diagnostics.append(StatementDiagnostic('HEADER', reason or 'COLUMN_MODEL_RESOLVED', group.region,
                                                           severity='ERROR' if reason else 'INFO'))
                    if not unit:
                        diagnostics.append(StatementDiagnostic('UNIT', 'UNIT_UNRESOLVED', source, severity='WARNING'))
    return accepted, rejected, diagnostics, handled


def inspect_financial_document_structure(document, *, compare_legacy: bool = True) -> FinancialParseResult:
    """Explicit shadow entry point; does not affect legacy production output."""
    result = parse_financial_structure(document.normalized_text or '', structure=document.pdf_structure)
    if not compare_legacy:
        return result
    from app.structured_research import _FinancialParserDiagnostics, _nse_statement_candidates, _reporting_basis
    evidence_at = document.published_at or document.retrieved_at
    legacy_diagnostics = _FinancialParserDiagnostics(str(document.document_id), document.source_name,
                                                   evidence_at, len(document.normalized_text or ''))
    legacy = _nse_statement_candidates(document.normalized_text or document.raw_text or '',
                                      evidence_at, require_quarterly=False, diagnostics=legacy_diagnostics,
                                      diagnostic_pass='STRUCTURAL_SHADOW')
    # Discovery may propose the same legacy table at both its title and
    # Particulars. Compare semantic models, retaining basis in the identity.
    old = {(_reporting_basis(s.region), tuple((c.index, c.period_end, c.period_type) for c in s.columns))
           for s in legacy}
    new = {(s.reporting_basis, tuple((c.index, c.period_end, c.period_type) for c in s.header.columns))
           for s in result.accepted_statements}
    diagnostic = StatementDiagnostic('SHADOW', 'LEGACY_COLUMN_MODEL_AGREES' if old == new else
                 'LEGACY_COLUMN_MODEL_DISAGREEMENT', result.accepted_statements[0].source_region if result.accepted_statements
                 else preserve_pdf_structure('').locate(0, 0), (('legacy_candidates', str(len(old))),
                 ('resolved_candidates', str(len(new)))))
    detailed = tuple(StatementDiagnostic('LEGACY_CANDIDATE', reason, diagnostic.source_region,
                     (('count', str(count)),), 'WARNING')
                     for reason, count in sorted(legacy_diagnostics.candidate_rejection_counts.items()))
    return replace(result, diagnostics=(*result.diagnostics, diagnostic, *detailed))
