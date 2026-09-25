"""Synthetic structural cases are not substitutes for exact issuer fixtures."""
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from app.financial_headers import resolve_financial_header
from app.financial_structure import parse_financial_structure, inspect_financial_document_structure
from app.normalization import normalize_text
from app.pdf_structure import preserve_pdf_structure


DATES = '31.03.2026 31.12.2025 31.03.2025 | 31.03.2026 31.03.2025'
HEADER = 'Quarter ended | Year ended\n' + DATES + '\nAudited Unaudited Audited | Audited Audited'


def reasons(result):
    return {d.reason for d in result.diagnostics}


def statement(header=HEADER, basis='Standalone'):
    return f'Statement of {basis} financial results\nParticulars\n{header}\nRevenue from operations 1.01 2.02'


def test_explicit_group_spans_keep_repeated_dates_and_status():
    result = resolve_financial_header(HEADER)
    assert [c.period_type for c in result.columns] == ['QUARTERLY'] * 3 + ['ANNUAL'] * 2
    assert [(c.period_end, c.period_type) for c in result.columns][::3] == [
        ('2026-03-31', 'QUARTERLY'), ('2026-03-31', 'ANNUAL')]
    assert [c.duration_months for c in result.columns] == [3, 3, 3, 12, 12]
    assert [c.audit_status for c in result.columns] == ['AUDITED', 'UNAUDITED', 'AUDITED', 'AUDITED', 'AUDITED']
    assert all(c.source_region and c.group_evidence and c.resolution_state == 'RESOLVED' for c in result.columns)
    with pytest.raises(FrozenInstanceError):
        result.columns[0].period_type = 'ANNUAL'


def test_five_dates_alone_do_not_prove_three_plus_two():
    result = resolve_financial_header(HEADER.replace('|', ' ').replace('\n', ' '))
    assert not result.columns
    assert 'HEADER_GROUP_SPAN_AMBIGUOUS' in reasons(result)


@pytest.mark.parametrize('label,kind,months', [
    ('Three months ended', 'QUARTERLY', 3), ('Nine months ended', 'NINE_MONTH', 9),
    ('Half year ended', 'HALF_YEAR', 6), ('As at', 'AS_AT', None),
    ('Current quarter', 'QUARTERLY', 3), ('Previous quarter', 'QUARTERLY', 3),
    ('Corresponding quarter', 'QUARTERLY', 3), ('For the year ended', 'ANNUAL', 12),
])
def test_duration_concepts_require_explicit_dates(label, kind, months):
    result = resolve_financial_header(f'{label} 31.03.2026 Audited')
    assert [(c.period_type, c.duration_months) for c in result.columns] == [(kind, months)]
    assert not resolve_financial_header(label).columns


def test_audit_does_not_establish_annual_period():
    assert not resolve_financial_header('31.03.2026 Audited').columns
    assert resolve_financial_header('Quarter ended 31.03.2026 Audited').columns[0].period_type == 'QUARTERLY'


@pytest.mark.parametrize('serial', ['S.No', 'Sl. No.', 'Sr. No.'])
def test_serial_and_note_noise_are_not_columns(serial):
    result = resolve_financial_header(f'{serial} Particulars\n{HEADER}\nRefer Note 4')
    assert len(result.columns) == 5
    assert any(t.kind == 'ANNOTATION' for t in result.evidence.tokens)


@pytest.mark.parametrize('bad', ['31.12.20??', '31.02.2026', '[missing date]', '??'])
def test_invalid_middle_date_retains_slot_and_rejects(bad):
    result = resolve_financial_header(HEADER.replace('31.12.2025', bad))
    assert not result.columns
    tokens = [t for t in result.evidence.tokens if t.kind == 'DATE']
    assert len(tokens) == 5
    assert tokens[1].value is None
    assert tokens[2].value == '2025-03-31'
    assert 'PERIOD_DATE_INVALID' in reasons(result)


@pytest.mark.parametrize('bad', ['31 March ????', '31 December', 'unreadable'])
def test_unknown_date_content_cannot_be_silently_discarded(bad):
    assert not resolve_financial_header(HEADER.replace('31.12.2025', bad)).columns


def test_empty_delimited_middle_cell_is_retained():
    result = resolve_financial_header('Year ended\n31.03.2026 | | 31.03.2024')
    assert not result.columns
    assert [t.value for t in result.evidence.tokens if t.kind == 'DATE'] == ['2026-03-31', None, '2024-03-31']


def test_grouping_is_not_always_three_plus_two():
    header = 'Quarter ended | Year ended\n31.03.2026 31.12.2025 | 31.03.2026 31.03.2025 31.03.2024'
    result = resolve_financial_header(header)
    assert [c.period_type for c in result.columns] == ['QUARTERLY'] * 2 + ['ANNUAL'] * 3


def test_group_labels_before_particulars_and_metric_independent_body_boundary():
    text = 'Standalone financial results\nQuarter ended | Year ended\nSr. No. Particulars\n' + DATES + '\nAn unfamiliar primary row 1.01 2.02'
    result = parse_financial_structure(structure=preserve_pdf_structure(text))
    assert len(result.accepted_statements) == 1
    column = result.accepted_statements[0].header.columns[0]
    assert column.source_region.line == 3
    assert column.group_evidence[0].line == 1


def test_narrative_mention_with_a_table_is_not_a_statement_title():
    text = 'This release discusses ' + statement()
    assert not parse_financial_structure(structure=preserve_pdf_structure(text)).accepted_statements
    assert not parse_financial_structure(normalize_text(text)).accepted_statements


def test_conflicting_group_rows_fail_closed():
    result = resolve_financial_header(HEADER + '\nYear ended | Quarter ended')
    assert not result.columns
    assert 'HEADER_EVIDENCE_CONFLICT' in reasons(result)


def test_two_explicit_span_strategies_disagree():
    header = 'Quarter ended | Year ended\nYear ended | Quarter ended\n' + DATES
    result = resolve_financial_header(header)
    assert not result.columns
    assert len(result.evidence.strategies) == 2
    assert 'HEADER_EVIDENCE_CONFLICT' in reasons(result)


def test_header_on_another_page_is_not_assumed_to_belong_to_title():
    text = '[PDF_PAGE 1]\nStatement of Standalone financial results\n[PDF_PAGE 2]\nParticulars\n' + HEADER + '\nRevenue 1.01'
    result = parse_financial_structure(structure=preserve_pdf_structure(text))
    assert not result.accepted_statements
    assert 'STATEMENT_BOUNDARY_AMBIGUOUS' in reasons(result)


def test_interleaved_and_text_grid_are_independent_span_proofs():
    result = resolve_financial_header('Quarter ended 31.03.2026 31.12.2025 Year ended 31.03.2026')
    assert [c.period_type for c in result.columns] == ['QUARTERLY', 'QUARTERLY', 'ANNUAL']
    dates = '31.03.2026 31.12.2025 31.03.2025 31.03.2026 31.03.2025'
    group = 'Quarter ended'.ljust(dates.index('31.03.2026', 1)) + 'Year ended'
    result = resolve_financial_header(group + '\n' + dates)
    assert len(result.columns) == 5
    assert 'EXPLICIT_TEXT_GRID' in result.evidence.strategies


def test_basis_isolation_ignores_notes_and_neighboring_statements():
    text = statement() + '\nNotes: consolidated subsidiary disclosure\n' + statement(basis='Consolidated')
    result = parse_financial_structure(structure=preserve_pdf_structure(text))
    assert [s.reporting_basis for s in result.accepted_statements] == ['STANDALONE', 'CONSOLIDATED']
    assert result.accepted_statements[0].source_region.end <= text.index('Notes:')
    ambiguous = parse_financial_structure(structure=preserve_pdf_structure(statement(basis='Standalone Consolidated')))
    assert not ambiguous.accepted_statements
    assert 'REPORTING_BASIS_AMBIGUOUS' in reasons(ambiguous)


@pytest.mark.parametrize('prefix', ['Extract of ', 'The board approved ', 'Notes regarding '])
def test_extract_and_narrative_do_not_become_primary_statements(prefix):
    result = parse_financial_structure(structure=preserve_pdf_structure(prefix + statement()))
    assert not result.accepted_statements


def test_unknown_basis_is_not_inferred_from_body():
    result = parse_financial_structure(structure=preserve_pdf_structure(statement(basis='') + '\nConsolidated'))
    assert not result.accepted_statements
    assert 'REPORTING_BASIS_AMBIGUOUS' in reasons(result)


def test_fingerprint_ignores_company_values_and_absolute_dates():
    first = parse_financial_structure(structure=preserve_pdf_structure('Alpha Limited\n' + statement()))
    second = parse_financial_structure(structure=preserve_pdf_structure(
        ('Beta Limited\n' + statement()).replace('1.01 2.02', '777.77 888.88').replace('2026', '2024').replace('2025', '2023')))
    assert first.structural_fingerprint == second.structural_fingerprint
    changed = parse_financial_structure(structure=preserve_pdf_structure(statement().replace('Year ended', 'Nine months ended')))
    assert first.structural_fingerprint != changed.structural_fingerprint


def test_legacy_flattened_input_is_explicit_and_conservative():
    result = parse_financial_structure(normalize_text(statement()))
    assert not result.accepted_statements
    assert 'HEADER_GROUP_SPAN_AMBIGUOUS' in reasons(result)
    assert result.rejected_statements[0].source_region.coordinate_space == 'LEGACY_FLATTENED'
    single = parse_financial_structure(normalize_text(statement('Year ended 31.03.2026 31.03.2025')))
    assert len(single.accepted_statements) == 1


def test_page_line_original_text_and_unavailable_geometry():
    text = '[PDF_PAGE 1]\nQuarter ended        Year ended\n31.03.2026\n[PDF_PAGE 2]\nNext page'
    result = preserve_pdf_structure(text)
    assert [p.ordinal for p in result.pages] == [1, 2]
    line = result.pages[0].lines[1]
    assert line.original_text == 'Quarter ended        Year ended'
    assert line.normalized_text == 'Quarter ended Year ended'
    assert line.region.geometry is None
    assert text[line.region.start:line.region.end] == line.original_text
    assert result.locate(text.index('Next page'), len(text)).page == 2
    assert normalize_text(text) == normalize_text(result.extracted_text)


def test_no_input_and_no_statement_have_distinct_diagnostics():
    assert 'NO_NORMALIZED_TEXT' in reasons(parse_financial_structure())
    assert 'NO_FINANCIAL_STATEMENT_FOUND' in reasons(parse_financial_structure('ordinary company narrative'))


def test_exact_legacy_corpus_shadow_does_not_change_metric_results():
    from app.structured_research import parsed_nse_income_statement_periods
    from test_official_nse_financial_parsing import _document

    root = Path(__file__).parent / 'fixtures' / 'nse'
    for file in sorted(root.glob('*/*normalized.txt')):
        document = _document(file.read_text(encoding='utf-8'))
        before = parsed_nse_income_statement_periods([document])
        result = inspect_financial_document_structure(document)
        assert result.parser_version == 'DI20C-1'
        assert any(d.stage == 'SHADOW' for d in result.diagnostics)
        assert parsed_nse_income_statement_periods([document]) == before
        if file.name == 'ncc_2026_06_30_normalized.txt':
            assert before == []
            assert not result.accepted_statements


def test_fetcher_preserves_structure_without_network(monkeypatch):
    import pypdf
    from types import SimpleNamespace
    from app.research_fetching import HttpResearchFetcher, NetworkFetchResult
    from app.settings import Settings

    monkeypatch.setattr(pypdf, 'PdfReader', lambda _: SimpleNamespace(pages=[
        SimpleNamespace(extract_text=lambda: statement()), SimpleNamespace(extract_text=lambda: '')]))
    response = NetworkFetchResult('https://example.test/file.pdf', 200, 'application/pdf',
                                 b'%PDF-fake', {}, False, None, 0, 0, 0)
    result = HttpResearchFetcher(Settings()).process_network_response(response)
    assert result.pdf_structure.extracted_text == result.text
    assert len(result.pdf_structure.pages) == 2
    assert result.pdf_structure.pages[0].lines[1].original_text.startswith('Statement')


def test_document_sidecar_is_excluded_from_serialization():
    from test_official_nse_financial_parsing import _document
    document = _document(normalize_text(statement()))
    document.pdf_structure = preserve_pdf_structure(statement())
    assert inspect_financial_document_structure(document, compare_legacy=False).accepted_statements
    assert 'pdf_structure' not in document.model_dump()
    assert 'pdfStructure' not in document.model_dump(by_alias=True)


def test_preparation_retains_structure_without_persisting():
    from app.repository import ResearchRepository
    from app.models import DocumentStatus, ReliabilityLevel, SourceClassification, SourceMode, SourceType
    repository = ResearchRepository()
    before = dict(repository.documents)
    text = '[PDF_PAGE 1]\n' + statement()
    document = repository._prepare_ingested_document(
        original_url='https://example.test/structural.pdf', source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
        source_name='fixture', publisher='fixture', content_type='application/pdf', body=text,
        reliability=ReliabilityLevel.LEVEL_A, published_at=None, source_mode=SourceMode.DEMO,
        source_classification=SourceClassification.EXCHANGE, discovered_at=None,
        discovery_provider=None, expected_profile=None, document_status=DocumentStatus.PARSED,
        allow_empty_content=False, trusted_profile_identity=None)
    assert document.normalized_text == normalize_text(text)
    assert document.pdf_structure.extracted_text == text
    assert repository.documents == before


def test_shadow_reports_agreement_and_preserves_legacy_rejections():
    from test_official_nse_financial_parsing import _document
    simple = _document(statement('Year ended 31.03.2026 31.03.2025'))
    result = inspect_financial_document_structure(simple)
    assert 'LEGACY_COLUMN_MODEL_AGREES' in reasons(result)
    malformed = _document('Statement of standalone financial results without a table')
    assert 'NO_PARTICULARS' in reasons(inspect_financial_document_structure(malformed))
