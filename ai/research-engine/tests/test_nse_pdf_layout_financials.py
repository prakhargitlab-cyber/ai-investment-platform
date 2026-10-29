"""Local original-PDF regression and small ruled PDFs; no network or database.

The binary fixture is the original supplied at the repository root, not a
transcribed/normalized replacement. Its hash prevents accidental substitution.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from app.fact_precedence import FinancialFact, FactSourceTier
from app.financial_metric_extraction import extract_semantic_facts
from app.financial_projection import project_semantic_financial_facts
from app.models import ResearchDocument
from app.normalization import normalize_financial_amount, normalize_text
from app.research_fetching import HttpResearchFetcher, NetworkFetchResult
from app.settings import Settings


PDF = Path(__file__).resolve().parents[3] / '20microns-may2026-original.pdf'
PDF_HASH = 'bdfbbeafc1faa9d6e512d78ccdfc605b50cb61e14fb29ab021f7223e6efcd392'
SOURCE = 'https://nsearchives.nseindia.com/corporate/20MICRONS_22052026161535_BseNseBoardOutcomeMarch2026Sign.pdf'
EXPECTED = {
    ('revenue_from_operations', '2026-03-31'): Decimal('95383.26'),
    ('revenue_from_operations', '2025-03-31'): Decimal('91278.52'),
    ('profit_after_tax', '2026-03-31'): Decimal('6667.00'),
    ('profit_after_tax', '2025-03-31'): Decimal('6248.47'),
}


def _fetch(content):
    def deny(request):
        raise AssertionError('A local PDF regression must not request a provider')
    client = httpx.AsyncClient(transport=httpx.MockTransport(deny))
    fetcher = HttpResearchFetcher(Settings(), client=client)
    try:
        response = NetworkFetchResult(SOURCE, 200, 'application/pdf', content, {}, False, None, 0, 0, 0)
        return fetcher.process_network_response(response)
    finally:
        fetcher._pdf_extraction_executor.shutdown(wait=True)
        asyncio.run(client.aclose())


def _document(fetch):
    return ResearchDocument(
        canonical_url=SOURCE, original_url=SOURCE, source_type='EXCHANGE_ANNOUNCEMENT',
        source_classification='EXCHANGE', source_name='NSE', publisher='NSE', content_type='application/pdf',
        document_type='PDF_REFERENCE', normalized_text=normalize_text(fetch.text),
        content_hash=hashlib.sha256(fetch.text.encode()).hexdigest(), status='PROCESSED', reliability_level='LEVEL_A',
        source_mode='REAL', instrument_id=UUID(int=701), company_id=UUID(int=702),
        published_at=datetime(2026, 5, 22, tzinfo=timezone.utc), discovery_provider='NSE_OFFICIAL_API',
        entity_resolution_confidence=.99, pdf_structure=fetch.pdf_structure)


def _extract(document):
    return extract_semantic_facts(document.normalized_text, structure=document.pdf_structure,
                                  evidence_at=document.published_at)


@pytest.fixture(scope='module')
def original():
    content = PDF.read_bytes()
    assert hashlib.sha256(content).hexdigest() == PDF_HASH
    return _document(_fetch(content))


@pytest.fixture(scope='module')
def extracted(original):
    return _extract(original)


def _consolidated(extracted):
    return next(e for e in extracted.statements if e.layout_candidate is not None
                and e.layout_candidate.reporting_basis == 'CONSOLIDATED'
                and e.statement.columns[0].period_type == 'ANNUAL')


def test_original_word_geometry_and_column_ownership(original, extracted):
    structure = original.pdf_structure
    assert structure.representation == 'PDF_LAYOUT'
    assert structure.content_hash == PDF_HASH and structure.source_reference == SOURCE
    evidence = _consolidated(extracted)
    assert [(c.source_column, c.period_end, c.period_type, c.audit_status) for c in evidence.statement.columns] == [
        (4, '2026-03-31', 'ANNUAL', 'AUDITED'), (5, '2025-03-31', 'ANNUAL', 'AUDITED')]
    assert evidence.source_region.page == 17
    for row in evidence.rows:
        if row.canonical_metric not in {'revenue_from_operations', 'profit_after_tax'} or row.qualifiers:
            continue
        for cell, column in zip(row.cells, evidence.statement.columns):
            region = cell.source_region
            assert region.page == 17 and region.line == row.source_region.line
            assert region.column == column.source_region.column
            assert structure.extracted_text[region.start:region.end] == cell.original_token
            table = evidence.layout_candidate.table
            left, right = table.column_edges[region.column:region.column + 2]
            assert left <= region.geometry[0] < region.geometry[2] <= right
            word = next(w for line in structure.pages[16].lines for w in line.words if w.region == region)
            assert word.confidence is None and word.quality == 'EMBEDDED_TEXT'


def test_original_expected_facts_units_and_existing_projection(original, extracted):
    actual = {(f.metric, f.period_end): f.value for f in extracted.accepted_facts if (f.metric, f.period_end) in EXPECTED}
    assert actual == EXPECTED
    assert all(f.reporting_basis == 'CONSOLIDATED' and f.period_type == 'ANNUAL'
               and f.unit == 'INR lakh' for f in extracted.accepted_facts if (f.metric, f.period_end) in EXPECTED)
    projection = project_semantic_financial_facts(original, extracted)
    names = {'revenue': 'revenue_from_operations', 'pat': 'profit_after_tax'}
    facts = [f for f in projection.facts if f.key.metric in names]
    assert {(names[f.key.metric], f.key.period_end): f.value.value for f in facts} == EXPECTED
    assert not projection.scopes  # partial column recovery never authorizes deletion
    for fact in facts:
        assert isinstance(fact, FinancialFact) and fact.source_tier == FactSourceTier.OFFICIAL_NSE
        assert fact.key.reporting_basis == 'CONSOLIDATED' and fact.key.period_type == 'ANNUAL'
        assert fact.value.source_url == SOURCE and fact.source_identity == str(original.document_id)
        assert 'page=17;' in fact.value.calculation_basis and 'audit=AUDITED' in fact.value.calculation_basis
        assert 'column=' in fact.value.calculation_basis and 'bbox=' in fact.value.calculation_basis
        assert PDF_HASH in fact.value.calculation_basis
        amount, currency = normalize_financial_amount(fact.value.value, fact.value.unit)
        assert currency == 'INR' and amount == EXPECTED[names[fact.key.metric], fact.key.period_end] * Decimal(100000)
    assert not any(f.key.metric in {'ebit', 'roce'} or f.key.period_end.startswith('2024') for f in projection.facts)
    assert 'pdf_structure' not in original.model_dump()  # persistence/serialization contract stays unchanged


def test_original_unit_corroboration_and_standalone_isolation(original, extracted):
    evidence = _consolidated(extracted)
    assert [(u.kind, u.original, u.value, u.region.page) for u in evidence.layout_candidate.unit_evidence] == [
        ('UNIT', 'Rs. In ukhs', 'INR lakh', 17),
        ('UNIT_CORROBORATION', 'Rs. 441.08 Lakhs', 'INR lakh', 20)]
    standalone = next(e for e in extracted.statements if e.layout_candidate and e.layout_candidate.reporting_basis == 'STANDALONE')
    assert standalone.source_region.page == 7 and standalone.unit is None
    assert not any(f.reporting_basis == 'STANDALONE' for f in extracted.accepted_facts)
    assert not any(f.key.reporting_basis == 'STANDALONE' for f in project_semantic_financial_facts(original, extracted).facts)
    assert any('6,023,06' == c.original_token and c.value is None for r in standalone.rows for c in r.cells)
    legacy = extract_semantic_facts(original.normalized_text, evidence_at=original.published_at)
    assert not any((f.metric, f.period_end) in EXPECTED for f in legacy.accepted_facts)


@pytest.mark.parametrize('damage', ['missing', 'wrong_basis', 'wrong_period', 'conflicting'])
def test_damaged_unit_requires_independent_matching_notes(original, damage):
    structure = original.pdf_structure
    page = structure.pages[19]
    body = structure.extracted_text[page.region.start:page.region.end]
    if damage == 'missing':
        changed = body.replace('Lakhs', '?????')
    elif damage == 'wrong_basis':
        changed = body.replace('Consolidated', 'Standalone  ')
    elif damage == 'wrong_period':
        changed = body.replace('March, 2026', 'March, 2027')
    else:
        changed = body.replace('unmodified opinion', 'Rs. 1 Crore       ')
    assert changed != body and len(changed) == len(body)
    text = structure.extracted_text[:page.region.start] + changed + structure.extracted_text[page.region.end:]
    document = original.model_copy(update={'pdf_structure': replace(structure, extracted_text=text),
                                            'normalized_text': normalize_text(text)})
    assert not any(f.reporting_basis == 'CONSOLIDATED' for f in _extract(document).accepted_facts)
    assert not project_semantic_financial_facts(document).facts


def _pdf_pages(specs):
    """Four value columns: two quarterly + two annual, deliberately not 3+2."""
    writer = PdfWriter()
    for spec in specs:
        page = writer.add_blank_page(width=500, height=800)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica'), NameObject('/Encoding'): NameObject('/WinAnsiEncoding')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
        commands = []
        def text(x, y, value):
            escaped = value.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
            commands.append(f'BT /F1 7 Tf 1 0 0 1 {x} {y} Tm ({escaped}) Tj ET')
        if spec.get('ruled', True):
            for x in [10, 30, 200, 260, 320, 380, 440]:
                top = 690 if x in {260, 380} else 710
                commands.append(f'{x} 600 m {x} {top} l S')
        text(35, 740, f"Statement of Audited {spec.get('basis', 'Consolidated')} Financial Results")
        text(325, 722, 'Rs. in ' + spec.get('unit', 'lakh'))
        text(40, 700, 'Particulars')
        text(215, 700, 'Quarter ended')
        text(335, 700, spec.get('annual_group', 'Year ended'))
        values = [
            (207, 680, '31-03-2026'), (267, 680, '31-03-2025'),
            (327, 680, '31-03-2026'), (387, 680, spec.get('annual_date', '31-03-2025')),
            *[(x, 665, spec.get('annual_audit', 'Audited') if x >= 335 else 'Audited') for x in [215, 275, 335, 395]],
            (210, 645, '11.00'), (270, 645, '12.00'),
            (spec.get('annual_x', 330), 645, spec.get('annual_revenue', '101.00')), (390, 645, '91.00'),
            (210, 625, '1.00'), (270, 625, '2.00'), (330, 625, '10.00'), (390, 625, '9.00'),
        ]
        text(15, 645, 'I')
        text(40, 645, 'Revenue from operations')
        text(15, 625, 'II')
        text(40, 625, 'Profit after tax')
        for x, y, value in reversed(values):  # stream order must not determine ownership
            text(x, y, value)
        if spec.get('duplicate_revenue'):
            text(40, 610, 'Revenue from operations')
            for x in [210, 270, 330, 390]:
                text(x, 610, '999.00')
        stream = DecodedStreamObject()
        stream.set_data('\n'.join(commands).encode('ascii'))
        page[NameObject('/Contents')] = stream
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_generic_annual_quarterly_groups_and_basis_units():
    document = _document(_fetch(_pdf_pages([
        {'basis': 'Standalone', 'unit': 'crore', 'annual_revenue': '202.00'},
        {'basis': 'Consolidated', 'unit': 'lakh'},
    ])))
    result = _extract(document)
    for basis, annual, unit in [('STANDALONE', Decimal('202'), 'INR crore'), ('CONSOLIDATED', Decimal('101'), 'INR lakh')]:
        facts = [f for f in result.accepted_facts if f.reporting_basis == basis and f.metric == 'revenue_from_operations']
        assert {(f.period_type, f.period_end): f.value for f in facts} == {
            ('QUARTERLY', '2026-03-31'): Decimal('11'), ('QUARTERLY', '2025-03-31'): Decimal('12'),
            ('ANNUAL', '2026-03-31'): annual, ('ANNUAL', '2025-03-31'): Decimal('91')}
        assert {f.unit for f in facts} == {unit}
    projected = project_semantic_financial_facts(document, result)
    assert len([f for f in projected.facts if f.key.metric == 'revenue']) == 8
    assert not projected.scopes


@pytest.mark.parametrize('spec', [
    {'annual_group': 'Year ended Quarter ended'}, {'annual_date': '31-03-202S'},
    {'annual_audit': 'Aud1ted'},
])
def test_ambiguous_or_corrupted_headers_cannot_fall_back_to_annual_guesses(spec):
    document = _document(_fetch(_pdf_pages([spec])))
    result = _extract(document)
    assert any(f.period_type == 'QUARTERLY' for f in result.accepted_facts)
    assert not any(f.period_type == 'ANNUAL' for f in result.accepted_facts)
    assert not any(f.key.period_type == 'ANNUAL' for f in project_semantic_financial_facts(document, result).facts)


@pytest.mark.parametrize('spec', [{'annual_revenue': '1O1.00'}, {'annual_x': 371}])
def test_damaged_or_boundary_crossing_cell_is_rejected_without_shifting(spec):
    document = _document(_fetch(_pdf_pages([spec])))
    result = _extract(document)
    assert not any(f.metric == 'revenue_from_operations' and f.period_type == 'ANNUAL' for f in result.accepted_facts)
    assert any(f.metric == 'profit_after_tax' and f.period_type == 'ANNUAL' for f in result.accepted_facts)
    assert not any(f.key.metric == 'revenue' and f.key.period_type == 'ANNUAL'
                   for f in project_semantic_financial_facts(document, result).facts)


@pytest.mark.parametrize('damage', ['value', 'column', 'basis', 'unit'])
def test_projection_rechecks_structured_provenance(original, extracted, damage):
    evidence = _consolidated(extracted)
    rows = list(evidence.rows)
    row = rows[0]
    if damage == 'value':
        rows[0] = replace(row, cells=(replace(row.cells[0], value=Decimal('999')), *row.cells[1:]))
    elif damage == 'column':
        rows[0] = replace(row, cells=(replace(row.cells[0], source_region=row.cells[1].source_region), *row.cells[1:]))
    elif damage == 'basis':
        rows[0] = replace(row, reporting_scope='STANDALONE')
    forged = replace(evidence, rows=tuple(rows), unit='INR crore' if damage == 'unit' else evidence.unit)
    projection = project_semantic_financial_facts(original, replace(extracted, statements=(forged,)))
    assert not projection.facts and 'LAYOUT_EVIDENCE_UNPROVEN' in projection.diagnostics


def test_source_authority_still_required(original, extracted):
    document = original.model_copy(update={'discovery_provider': None})
    assert not project_semantic_financial_facts(document, extracted).facts


def test_unresolved_unit_is_not_borrowed_from_another_basis():
    document = _document(_fetch(_pdf_pages([
        {'basis': 'Standalone', 'unit': 'lakh'}, {'basis': 'Consolidated', 'unit': 'ukhs'},
    ])))
    result = _extract(document)
    assert any(f.reporting_basis == 'STANDALONE' for f in result.accepted_facts)
    assert not any(f.reporting_basis == 'CONSOLIDATED' for f in result.accepted_facts)
    assert {f.key.reporting_basis for f in project_semantic_financial_facts(document, result).facts} == {'STANDALONE'}


def test_ambiguous_reporting_basis_cannot_use_flattened_fallback():
    document = _document(_fetch(_pdf_pages([{'basis': 'Standalone Consolidated'}])))
    assert not _extract(document).accepted_facts
    assert not project_semantic_financial_facts(document).facts


def test_duplicate_rows_are_not_first_match_wins():
    document = _document(_fetch(_pdf_pages([{'duplicate_revenue': True}])))
    result = _extract(document)
    assert not any(f.metric == 'revenue_from_operations' for f in result.accepted_facts)
    assert any(f.metric == 'profit_after_tax' for f in result.accepted_facts)
    assert not any(f.key.metric == 'revenue' for f in project_semantic_financial_facts(document, result).facts)


def test_structure_from_another_document_cannot_supply_provenance(original, extracted):
    document = original.model_copy(update={'normalized_text': 'Different document content'})
    projection = project_semantic_financial_facts(document, extracted)
    assert not projection.facts and 'SOURCE_REGION_MISMATCH' in projection.diagnostics


def test_legacy_text_projection_still_works():
    from app.pdf_structure import preserve_pdf_structure
    text = ('Statement of Standalone financial results (Rs. in Crore) Particulars Year ended '
            '31.03.2026 31.03.2025 Revenue from operations 101.01 90.02 Profit after tax 11.01 9.02')
    document = _document(type('LocalText', (), {'text': text, 'pdf_structure': preserve_pdf_structure(text)})())
    facts = project_semantic_financial_facts(document).facts
    assert {(f.key.metric, f.key.period_end): f.value.value for f in facts} == {
        ('revenue', '2026-03-31'): Decimal('101.01'), ('revenue', '2025-03-31'): Decimal('90.02'),
        ('pat', '2026-03-31'): Decimal('11.01'), ('pat', '2025-03-31'): Decimal('9.02')}
