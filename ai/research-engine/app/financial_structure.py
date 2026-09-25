"""Opt-in structural shadow parser. No metric binding, I/O, or persistence."""
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
    headings = list(HEADING.finditer(text))
    for index, heading in enumerate(headings):
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
