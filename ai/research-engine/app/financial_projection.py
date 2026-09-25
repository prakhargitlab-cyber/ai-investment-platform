"""Strict DI-20E projection into the existing durable financial vocabulary.

revenue = operations revenue only; pat = unqualified total after-tax profit;
eps = proven diluted EPS, otherwise proven basic EPS. No metric fallback to
the legacy PDF fact producer. Unrepresentable concepts remain shadow-only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from itertools import combinations
from urllib.parse import urlparse

from app.fact_precedence import FinancialFact, FinancialFactKey, FactSourceTier
from app.financial_metric_extraction import ExtractionResult, extract_semantic_facts
from app.models import ProvenancedValue, SourceMode


@dataclass(frozen=True)
class FinancialReconciliationScope:
    """A finite source-owned metric/basis/date scope, never a whole document."""
    metric: str
    reporting_basis: str | None
    period_ends: frozenset[str]
    period_types: frozenset[str]
    completeness: str = "PARTIAL_FOR_SOURCE_SCOPE"

    def contains(self, key: FinancialFactKey) -> bool:
        return (self.completeness == "COMPLETE_FOR_SOURCE_SCOPE"
                and key.metric == self.metric and key.reporting_basis == self.reporting_basis
                and key.period_end in self.period_ends and key.period_type in self.period_types)


@dataclass(frozen=True)
class FinancialProjectionResult:
    facts: tuple[FinancialFact, ...]
    scopes: tuple[FinancialReconciliationScope, ...]
    diagnostics: tuple[str, ...]
    state: str


BALANCE_METRICS = frozenset({"total_assets", "total_equity", "total_liabilities", "current_assets",
    "current_liabilities", "cash_and_cash_equivalents", "total_debt"})
CASH_METRICS = frozenset({"cash_flow_from_operating_activities", "cash_flow_from_investing_activities",
    "cash_flow_from_financing_activities", "net_change_in_cash"})
MAPPING = {"revenue_from_operations": "revenue", "profit_after_tax": "pat",
           "eps_diluted": "eps", "eps_basic": "eps", "ebitda": "ebitda",
           **{metric: metric for metric in BALANCE_METRICS | CASH_METRICS}}


def nse_authority_rejection(document) -> str | None:
    def official(url):
        parsed = urlparse(url)
        host = (parsed.hostname or '').lower()
        return parsed.scheme == 'https' and (host == 'nseindia.com' or host.endswith('.nseindia.com'))
    if (document.source_mode != 'REAL' or document.source_type != 'EXCHANGE_ANNOUNCEMENT'
            or document.source_classification != 'EXCHANGE' or document.discovery_provider != 'NSE_OFFICIAL_API'
            or document.reliability_level != 'LEVEL_A' or document.entity_resolution_confidence < .95
            or not official(document.canonical_url) or not official(document.original_url)
            or not document.instrument_id or not document.company_id or document.status == 'FAILED'):
        return 'UNQUALIFIED_NSE_SOURCE'
    return None


def _column_contract(evidence):
    """Validate legacy compatibility columns independently of assumed widths.

    Explicit ordered groups plus strictly
    descending comparative periods must yield ONE partition. No 3+2 rule.
    Pure annual/as-at tables need no mixed-group partition inference.
    """
    from app.structured_research import (_PARTICULARS_RE, _GROUP_RE, _deduplicate_header_groups,
        _normalize_nse_header_ocr, _nse_header_dates, _STATEMENT_HEADING,
        _BALANCE_SHEET_HEADING, _CASH_FLOW_HEADING, _NSE_DAY_MONTH, _nse_date)
    statement = evidence.statement
    text = statement.region
    heading_re = {'INCOME_STATEMENT': _STATEMENT_HEADING, 'CASH_FLOW': _CASH_FLOW_HEADING,
                  'BALANCE_SHEET': _BALANCE_SHEET_HEADING}[evidence.statement_type]
    heading = heading_re.search(text)
    part = _PARTICULARS_RE.search(text)
    if not heading or heading.start() > 40 or not part or part.start() < heading.end():
        return None, False, 'STATEMENT_SCOPE_UNPROVEN'
    title = text[:heading.end()]
    bases = set(re.findall(r'\b(?:standalone|consolidated)\b', title, re.I))
    bases = {b.upper() for b in bases}
    if len(bases) > 1:
        return None, False, 'REPORTING_BASIS_AMBIGUOUS'
    basis = next(iter(bases), None)
    # Unknown is retained as None; never inferred from later notes or another table.
    if any(row.reporting_scope != basis for row in evidence.rows):
        return basis, False, 'REPORTING_BASIS_SCOPE_CONFLICT'
    header = text[part.end():part.end() + 700]
    cut = re.search(r'\b(?:revenue|income|profit|expenses?|assets|liabilities|net\s+cash)\b', header, re.I)
    if cut:
        header = header[:cut.start()]
    prefix = text[heading.end():part.start()]
    suffix = re.search(r'((?:(?:quarter|nine months?|half year|year)\s+ended\s*)+(?:(?:S\.?No\.?|Sl\.?\s*No\.?|Sr\.?\s*No\.?)\s*)?)$', prefix, re.I)
    if suffix:
        header = suffix.group() + ' ' + header
    header = _normalize_nse_header_ocr(header)
    columns = statement.columns
    if not columns or any(c.index != i for i, c in enumerate(columns)):
        return basis, False, 'COLUMN_ORDINAL_INVALID'
    direct = _nse_header_dates(header)
    if any(d is None for d in direct):
        return basis, False, 'HEADER_DATE_SLOT_MISMATCH'
    if len(direct) != len(columns):
        # Deferred-year rows retain every day/month slot and every year slot.
        # A partial direct regex match is not a missing-column repair.
        fragments = list(_NSE_DAY_MONTH.finditer(header))
        years = re.findall(r'\b([2Z][0-9Z]{3})\b', header)
        if len(fragments) != len(columns) or len(years) != len(columns):
            return basis, False, 'HEADER_DATE_SLOT_MISMATCH'
        paired = [_nse_date(m.group(1), m.group(2), y.replace('Z', '2')) for m, y in zip(fragments, years)]
        if any(d is None for d in paired) or [d.isoformat() for d in paired] != [c.period_end for c in columns]:
            return basis, False, 'HEADER_DATE_SLOT_MISMATCH'
    try:
        dates = [date.fromisoformat(c.period_end) for c in columns]
    except ValueError:
        return basis, False, 'PERIOD_DATE_INVALID'
    if evidence.statement_type == 'BALANCE_SHEET':
        return basis, bool(re.search(r'\bas\s+at\b', header, re.I)) and len(set(dates)) == len(dates), 'AS_AT_REQUIRED'
    groups = _deduplicate_header_groups([m.group(1).lower() for m in _GROUP_RE.finditer(header)])
    kinds = ['QUARTERLY' if g == 'quarter' else 'NINE_MONTH' if g.startswith('nine') else
             'HALF_YEAR' if g.startswith('half') else 'ANNUAL' for g in groups]
    if not kinds:
        return basis, False, 'PERIOD_TYPE_AMBIGUOUS'
    proposals = []
    for cuts in combinations(range(1, len(dates)), len(kinds) - 1):
        spans = list(zip((0, *cuts), (*cuts, len(dates))))
        valid = True
        for kind, (start, end) in zip(kinds, spans):
            values = dates[start:end]
            if not values or any(a <= b for a, b in zip(values, values[1:])):
                valid = False
            if kind != 'QUARTERLY' and len({(d.month, d.day) for d in values}) != 1:
                valid = False
        if valid:
            proposals.append(tuple(kind for kind, (start, end) in zip(kinds, spans) for _ in range(start, end)))
    expected = tuple(c.period_type for c in columns)
    return basis, len(set(proposals)) == 1 and proposals[0] == expected, 'HEADER_GROUP_SPAN_AMBIGUOUS'


def project_semantic_financial_facts(document, extraction: ExtractionResult | None = None) -> FinancialProjectionResult:
    rejection = nse_authority_rejection(document)
    if rejection:
        return FinancialProjectionResult((), (), (rejection,), 'FAILED')
    text = document.normalized_text or ''
    extraction = extraction or extract_semantic_facts(text, evidence_at=document.published_at or document.retrieved_at,
                                                     structure=document.pdf_structure)
    diagnostics, candidates, complete_rows, blocked = [], {}, [], set()
    from app.structured_research import _financial_number, _structural_row_text
    seen_regions = set()
    # NSE financial results state the unit once at the top of the document
    # (e.g., "Amounts in Rs. Crore") and it applies to every statement.
    # Propagate the unit from any statement that carries it to statements
    # that don't, rather than dropping facts due to a missing per-statement
    # unit on a subsidiary statement (balance sheet, cash flow).
    shared_unit = next((evidence.unit for evidence in extraction.statements if evidence.unit), None)
    for evidence in extraction.statements:
        statement = evidence.statement
        region = evidence.source_region
        if region is None or text[region.start:region.end] != statement.region:
            diagnostics.append('SOURCE_REGION_MISMATCH')
            continue
        identity = (evidence.statement_type, region.start, region.end)
        if identity in seen_regions:
            continue
        seen_regions.add(identity)
        basis, valid, reason = _column_contract(evidence)
        if not valid:
            diagnostics.append(reason)
            blocked.update((MAPPING[r.canonical_metric], r.reporting_scope) for r in evidence.rows if r.canonical_metric in MAPPING)
            continue
        rows = list(evidence.rows)
        # Prefer the accounting concept, never the number of surviving cells.
        diluted = any(r.canonical_metric == 'eps_diluted' and r.resolution_state == 'ACCEPTED'
                      and 'basic' not in r.original_label.lower() for r in rows)
        for row in rows:
            metric = MAPPING.get(row.canonical_metric)
            if metric is None:
                diagnostics.append('UNSUPPORTED_DURABLE_CONCEPT:' + row.canonical_metric)
                continue
            scope_key = (metric, basis)
            if row.canonical_metric == 'eps_basic' and diluted:
                continue
            if (row.resolution_state != 'ACCEPTED' or row.qualifiers
                    or re.search(r'owners|parent|attribut|non.?controlling|continuing|discontinued|comprehensive|before\s+exceptional|segment', row.original_label, re.I)
                    or (metric == 'eps' and re.search('basic' if row.canonical_metric == 'eps_diluted' else 'diluted', row.original_label, re.I))):
                diagnostics.append('UNREPRESENTABLE_METRIC_SCOPE:' + row.canonical_metric)
                blocked.add(scope_key)
                continue
            if len(row.cells) != len(statement.columns) or any(c.column_ordinal != i for i, c in enumerate(row.cells)):
                diagnostics.append('VALUE_COLUMN_MISMATCH')
                blocked.add(scope_key)
                continue
            label = row.original_label.strip().rstrip('(').strip()
            row_text = _structural_row_text(statement.region)
            # First-match extraction is insufficient when a statement repeats
            # an accounting row. Do not choose one subtotal/version silently.
            if label and len(list(re.finditer(re.escape(label), row_text, re.I))) > 1:
                diagnostics.append('DUPLICATE_METRIC_ROW:' + row.canonical_metric)
                blocked.add(scope_key)
                continue
            if any(c.original_token.count('(') != c.original_token.count(')')
                   or (c.value is None and c.original_token.strip() not in {'-', '--', '—', '–'})
                   for c in row.cells):
                diagnostics.append('UNPROVEN_CELL_SLOT:' + row.canonical_metric)
                blocked.add(scope_key)
                continue
            label_match = re.search(re.escape(label), row_text, re.I) if label else None
            if label_match and re.search(r'(?:segment|owners?\s+of\s+(?:the\s+)?parent|attributable\s+to)\s*[:\-]?\s*$',
                                         row_text[max(0, label_match.start() - 70):label_match.start()], re.I):
                diagnostics.append('UNREPRESENTABLE_METRIC_SCOPE:' + row.canonical_metric)
                blocked.add(scope_key)
                continue
            if metric == 'pat' and re.search(r'\brevenue|\bincome|\bexpenses?|\beps|\bearnings?', label, re.I):
                diagnostics.append('ROW_LABEL_BOUNDARY_AMBIGUOUS:pat')
                blocked.add(scope_key)
                continue
            token_patterns = [re.escape(c.original_token).replace(r'\.', r'\s*\.\s*') for c in row.cells]
            run = re.search(r'(?<!\S)' + r'\s+'.join(token_patterns) + r'(?!\S)',
                            row_text[label_match.start():label_match.end() + 360]) if label_match else None
            if run is None:
                diagnostics.append('ROW_CELL_PROVENANCE_UNPROVEN:' + row.canonical_metric)
                blocked.add(scope_key)
                continue
            after = row_text[label_match.start() + run.end():].split()[:2]
            if len(after) == 2 and all(_financial_number(token) is not None for token in after):
                diagnostics.append('UNSAFE_NUMERIC_RUN:' + row.canonical_metric)
                blocked.add(scope_key)
                continue
            if metric == 'eps' and not re.search(r'earn(?:ing|lng)s?\s+per|\beps\b', statement.region, re.I):
                diagnostics.append('EPS_SECTION_UNPROVEN')
                blocked.add(scope_key)
                continue
            eps_currency_proven = bool(re.search(r'\b(?:rs\.?|rupees?|inr)\b|₹', statement.region, re.I))
            if metric == 'eps' and not eps_currency_proven:
                # Preserve the explicitly identified per-share measure without
                # manufacturing a currency from a damaged currency glyph.
                diagnostics.append('EPS_CURRENCY_UNRESOLVED')
                blocked.add(scope_key)
            full = True
            emitted = []
            for cell, column in zip(row.cells, statement.columns):
                period_type = 'AS_AT' if evidence.statement_type == 'BALANCE_SHEET' else column.period_type
                if period_type not in {'QUARTERLY', 'ANNUAL', 'AS_AT'}:
                    full = False
                    continue
                value = cell.value
                if (cell.is_missing or value is None or not value.is_finite()
                        or _financial_number(cell.original_token) != value or 'SYNTHESIZED_TOKEN' in cell.normalization):
                    full = False
                    continue
                key = FinancialFactKey(document.instrument_id, metric, column.period_end, period_type, basis)
                unit = ('INR per share' if eps_currency_proven else 'per share') if metric == 'eps' else (evidence.unit or shared_unit)
                if not unit:
                    diagnostics.append('UNIT_UNRESOLVED')
                    full = False
                    continue
                fact = FinancialFact(key, ProvenancedValue(value=value, unit=unit, period=column.period_end,
                    source_url=document.canonical_url, source_name=document.source_name, source_type=str(document.source_type),
                    published_at=document.published_at, retrieved_at=document.retrieved_at, confidence=.82,
                    calculation_basis=f'DI20E:{row.canonical_metric};region={region.start}:{region.end}'),
                    FactSourceTier.OFFICIAL_NSE, 'NSE', str(document.document_id), SourceMode.REAL)
                candidates.setdefault(key, []).append(fact)
                emitted.append(key)
            structural = extraction.structural_result
            # Legacy compatibility permits upserts only. Destructive cleanup
            # additionally requires a matching unambiguous DI-20C statement.
            proven_scope = bool(structural and not structural.rejected_statements and any(
                s.reporting_basis == basis and s.statement_type == evidence.statement_type
                and tuple((c.period_end, c.period_type) for c in s.header.columns) ==
                    tuple((c.period_end, c.period_type) for c in statement.columns)
                for s in structural.accepted_statements))
            if full and emitted and proven_scope:
                complete_rows.append(FinancialReconciliationScope(metric, basis,
                    frozenset(c.period_end for c in statement.columns),
                    frozenset({'AS_AT'} if evidence.statement_type == 'BALANCE_SHEET' else {'QUARTERLY', 'ANNUAL'}),
                    'COMPLETE_FOR_SOURCE_SCOPE'))
            else:
                blocked.add(scope_key)
    facts = []
    for key, alternatives in candidates.items():
        if len({(f.value.value, f.value.unit) for f in alternatives}) != 1:
            diagnostics.append('DURABLE_FACT_CONFLICT:' + key.metric)
            blocked.add((key.metric, key.reporting_basis))
            continue
        facts.append(alternatives[0])
    scopes = tuple(dict.fromkeys(s for s in complete_rows if (s.metric, s.reporting_basis) not in blocked))
    facts.sort(key=lambda f: (f.key.metric, f.key.reporting_basis or '', f.key.period_type, f.key.period_end), reverse=True)
    state = 'FAILED' if not facts else 'PARTIAL_FOR_SOURCE_SCOPE'
    # Completeness is explicitly per scope; never claim the entire PDF complete.
    return FinancialProjectionResult(tuple(facts), scopes, tuple(sorted(set(diagnostics))), state)
