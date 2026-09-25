from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.fact_precedence import FactSourceTier
from app.financial_metric_extraction import extract_semantic_facts
from app.financial_projection import project_semantic_financial_facts, FinancialReconciliationScope
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository
from test_official_nse_financial_parsing import _document, _document_fact


def annual(rows='Revenue from operations 101.01 90.02 Profit after tax 11.01 9.02', basis='Standalone'):
    return (f'Statement of {basis} financial results (Rs. in Crore) Particulars '
            'Year ended 31.03.2026 31.03.2025 ' + rows)


def projected(text):
    document = _document(text)
    return document, project_semantic_financial_facts(document)


def values(result, metric):
    return [f.value.value for f in result.facts if f.key.metric == metric]


def store():
    return ResearchRepository(persistence=SqliteResearchPersistence())


def test_zero_and_failed_parse_cannot_delete_even_with_complete_scope():
    repo = store()
    doc = _document('No financial statement')
    old = _document_fact(doc, 'revenue', '2026-03-31', 'ANNUAL', 'STANDALONE', '100')
    repo._persistence.upsert_financial_fact(old)
    scope = FinancialReconciliationScope('revenue', 'STANDALONE', frozenset({'2026-03-31'}),
                                        frozenset({'ANNUAL'}), 'COMPLETE_FOR_SOURCE_SCOPE')
    assert repo._persistence.reconcile_financial_facts_for_source(doc.instrument_id, str(doc.document_id), [], complete_scopes=(scope,)) == 0
    assert repo._reconcile_persisted_official_financial_document(doc) == (False, 0)
    assert repo.financial_facts_for(doc.instrument_id) == [old]


def test_partial_row_upserts_without_deleting_missing_cell_or_other_family():
    repo = store()
    doc = _document(annual('Revenue from operations 101.01 -'))
    old = _document_fact(doc, 'revenue', '2025-03-31', 'ANNUAL', 'STANDALONE', '90.02')
    cash = _document_fact(doc, 'cash_flow_from_operating_activities', '2026-03-31', 'ANNUAL', 'STANDALONE', '42.02')
    for fact in (old, cash):
        repo._persistence.upsert_financial_fact(fact)
    result = project_semantic_financial_facts(doc)
    assert values(result, 'revenue') == [Decimal('101.01')]
    assert not result.scopes
    assert repo._reconcile_persisted_official_financial_document(doc)[0]
    facts = repo.financial_facts_for(doc.instrument_id)
    assert old in facts and cash in facts and len(facts) == 3


def test_complete_scope_deletes_only_owned_obsolete_keys_in_exact_dates_basis_family():
    repo = store()
    doc, result = projected(annual())
    assert result.scopes
    obsolete = _document_fact(doc, 'revenue', '2026-03-31', 'QUARTERLY', 'STANDALONE', '777')
    outside = [
        _document_fact(doc, 'revenue', '2020-03-31', 'QUARTERLY', 'STANDALONE', '777'),
        _document_fact(doc, 'revenue', '2026-03-31', 'QUARTERLY', 'CONSOLIDATED', '777'),
        _document_fact(doc, 'cash_flow_from_operating_activities', '2026-03-31', 'ANNUAL', 'STANDALONE', '777'),
        replace(_document_fact(doc, 'revenue', '2025-03-31', 'QUARTERLY', 'STANDALONE', '777'), source_identity='another-document'),
    ]
    for fact in [obsolete, *outside]:
        repo._persistence.upsert_financial_fact(fact)
    assert repo._reconcile_persisted_official_financial_document(doc)[0]
    facts = repo.financial_facts_for(doc.instrument_id)
    assert obsolete not in facts
    assert all(f in facts for f in outside)


def test_nonempty_without_explicit_scope_is_non_destructive():
    repo = store()
    doc, result = projected(annual())
    old = _document_fact(doc, 'revenue', '2026-03-31', 'QUARTERLY', 'STANDALONE', '777')
    repo._persistence.upsert_financial_fact(old)
    repo._persistence.reconcile_financial_facts_for_source(doc.instrument_id, str(doc.document_id), list(result.facts))
    assert old in repo.financial_facts_for(doc.instrument_id)


@pytest.mark.parametrize('change', [
    {'discovery_provider': None}, {'source_mode': 'DEMO'}, {'source_classification': 'OFFICIAL_COMPANY'},
    {'source_type': 'COMPANY_WEBSITE'}, {'entity_resolution_confidence': .5}, {'company_id': None},
    {'canonical_url': 'https://nseindia.com.evil.test/file.pdf'}, {'status': 'FAILED'},
])
def test_unqualified_source_cannot_receive_nse_authority(change):
    doc = _document(annual()).model_copy(update=change)
    assert not project_semantic_financial_facts(doc).facts
    assert not ResearchRepository._official_financial_fact_candidates(doc, parsed_periods=('untrusted bypass',))


def test_weaker_source_cannot_overwrite_or_delete_stronger_facts():
    repo = store()
    doc, result = projected(annual())
    stronger = replace(result.facts[0], source_tier=FactSourceTier.OFFICIAL_REGULATORY,
                       source_provider='REGULATOR', value=result.facts[0].value.model_copy(update={'value': Decimal('999')}))
    obsolete_stronger = replace(_document_fact(doc, 'revenue', '2026-03-31', 'QUARTERLY', 'STANDALONE', '444'),
                               source_tier=FactSourceTier.OFFICIAL_REGULATORY, source_provider='REGULATOR')
    for fact in (stronger, obsolete_stronger):
        repo._persistence.upsert_financial_fact(fact)
    repo._reconcile_persisted_official_financial_document(doc)
    persisted = next(f for f in repo.financial_facts_for(doc.instrument_id) if f.key == stronger.key)
    assert persisted.value.value == Decimal('999')
    assert persisted.source_tier == FactSourceTier.OFFICIAL_REGULATORY
    assert obsolete_stronger in repo.financial_facts_for(doc.instrument_id)


def test_revenue_is_operations_only_never_total_income():
    _, result = projected(annual('Revenue from operations 101.01 90.02 Total income 110.01 99.02'))
    assert set(values(result, 'revenue')) == {Decimal('101.01'), Decimal('90.02')}
    _, only_income = projected(annual('Total income 110.01 99.02'))
    assert not only_income.facts


@pytest.mark.parametrize('label', [
    'Profit before tax', 'Profit for the period from continuing operations',
    'Profit after tax attributable to owners of the parent', 'Profit from discontinued operations after tax',
    'Total comprehensive income', 'Profit before exceptional items after tax',
])
def test_alternative_profit_scopes_do_not_become_generic_pat(label):
    _, result = projected(annual(f'Revenue from operations 101.01 90.02 {label} 11.01 9.02'))
    assert not values(result, 'pat')


def test_eps_projection_prefers_diluted_concept_not_cell_count():
    _, result = projected(annual('Earnings per share Basic EPS 2.10 2.20 Diluted EPS 1.90 -'))
    assert values(result, 'eps') == [Decimal('1.90')]
    _, basic = projected(annual('Earnings per share Basic EPS 2.10 2.20'))
    assert set(values(basic, 'eps')) == {Decimal('2.10'), Decimal('2.20')}
    _, orphan = projected(annual('1.90 1.80 Earnings per share Basic EPS'))
    assert not values(orphan, 'eps')


def test_accounting_parentheses_survive_projection_and_storage():
    repo = store()
    doc = _document('Standalone Statement of Cash Flows (Rs. in Crore) Particulars Year ended '
                    '31.03.2026 31.03.2025 Net cash from operating activities (458.51) 741.70')
    assert repo._persist_official_financial_facts(doc)[0]
    assert Decimal('-458.51') in [f.value.value for f in repo.financial_facts_for(doc.instrument_id)]


def test_conflicting_statement_versions_are_not_first_wins():
    _, result = projected(annual() + ' ' + annual().replace('101.01', '999.99'))
    assert Decimal('101.01') not in values(result, 'revenue')
    assert Decimal('999.99') not in values(result, 'revenue')
    assert 'DURABLE_FACT_CONFLICT:revenue' in result.diagnostics


def test_duplicate_row_is_not_a_first_match_subtotal():
    _, result = projected(annual('Revenue from operations 101.01 90.02 Revenue from operations 999.99 900.09'))
    assert not values(result, 'revenue')


def test_bare_semantic_facts_without_row_evidence_cannot_be_persisted():
    doc = _document(annual())
    extraction = extract_semantic_facts(doc.normalized_text, evidence_at=doc.published_at)
    assert extraction.accepted_facts
    assert not project_semantic_financial_facts(doc, replace(extraction, statements=())).facts


def test_fabricated_cells_with_no_source_tokens_fail_projection():
    doc = _document(annual())
    extraction = extract_semantic_facts(doc.normalized_text, evidence_at=doc.published_at)
    statement = extraction.statements[0]
    row = next(r for r in statement.rows if r.canonical_metric == 'revenue_from_operations')
    row = replace(row, cells=tuple(replace(c, original_token='666.66', value=Decimal('666.66')) for c in row.cells))
    extraction = replace(extraction, statements=(replace(statement, rows=(row,)),))
    result = project_semantic_financial_facts(doc, extraction)
    assert not result.facts
    assert 'ROW_CELL_PROVENANCE_UNPROVEN:revenue_from_operations' in result.diagnostics


def test_no_currency_is_invented_for_eps():
    _, result = projected(annual('Earnings per share Diluted EPS 1.90 1.80').replace('(Rs. in Crore)', ''))
    assert result.facts and {f.value.unit for f in result.facts} == {'per share'}
    assert not result.scopes
    assert 'EPS_CURRENCY_UNRESOLVED' in result.diagnostics


def test_failure_during_deletion_rolls_back_upserts_and_deletions():
    repo = store()
    doc = _document(annual())
    prior = [_document_fact(doc, 'revenue', end, 'QUARTERLY', 'STANDALONE', '777')
             for end in ('2026-03-31', '2025-03-31')]
    for fact in prior:
        repo._persistence.upsert_financial_fact(fact)
    connection = repo._persistence._connection
    class FailDelete:
        deletes = 0
        def execute(self, sql, params=()):
            if sql.startswith('DELETE FROM global_financial_facts'):
                self.deletes += 1
                if self.deletes == 2:
                    raise RuntimeError('delete failure')
            return connection.execute(sql, params)
        def __enter__(self):
            connection.__enter__()
            return self
        def __exit__(self, *args):
            return connection.__exit__(*args)
    repo._persistence._connection = FailDelete()
    with pytest.raises(RuntimeError, match='delete failure'):
        repo._reconcile_persisted_official_financial_document(doc)
    repo._persistence._connection = connection
    assert {f.key: f.value.value for f in repo.financial_facts_for(doc.instrument_id)} == {f.key: f.value.value for f in prior}


def test_ncc_march_durable_identity_values_and_partial_investing_preservation():
    repo = store()
    text = (Path(__file__).parent / 'fixtures/nse/ncc/ncc_2026_03_31_normalized.txt').read_text(encoding='utf-8')
    doc, result = projected(text)
    assert len(extract_semantic_facts(text, evidence_at=doc.published_at).accepted_facts) == 84
    assert len(result.facts) == 34
    assert not result.scopes  # Legacy flattened structure is not deletion authority.
    expected = {
        ('STANDALONE', 'QUARTERLY'): ('5315.71', '202.88', '3.23'),
        ('STANDALONE', 'ANNUAL'): ('17463.49', '576.76', '9.19'),
        ('CONSOLIDATED', 'QUARTERLY'): ('6232.71', '216.77', '3.28'),
        ('CONSOLIDATED', 'ANNUAL'): ('20823.00', '723.96', '10.76'),
    }
    repo._persist_official_financial_facts(doc)
    facts = {f.key: f for f in repo.financial_facts_for(doc.instrument_id)}
    for (basis, kind), numbers in expected.items():
        for metric, number in zip(('revenue', 'pat', 'eps'), numbers):
            key = _document_fact(doc, metric, '2026-03-31', kind, basis, number).key
            assert facts[key].value.value == Decimal(number)


def test_ncc_june_zero_output_preserves_historical_source_facts():
    repo = store()
    doc = _document((Path(__file__).parent / 'fixtures/nse/ncc/ncc_2026_06_30_normalized.txt').read_text(encoding='utf-8'))
    old = _document_fact(doc, 'eps', '2026-06-30', 'QUARTERLY', 'STANDALONE', '5315.71')
    repo._persistence.upsert_financial_fact(old)
    assert repo._reconcile_persisted_official_financial_document(doc) == (False, 0)
    assert repo.financial_facts_for(doc.instrument_id) == [old]


def test_irfc_eps_values_are_proven_diluted_cells():
    for name, expected in [('irfc_2026_03_31_normalized.txt', {'1.29', '1.38', '4.98', '5.36'}),
                           ('irfc_2026_06_30_normalized.txt', {'1.47', '1.29', '5.36'})]:
        doc = _document((Path(__file__).parent / 'fixtures/nse/irfc' / name).read_text(encoding='utf-8'))
        result = project_semantic_financial_facts(doc)
        assert {Decimal(v) for v in expected} <= set(values(result, 'eps'))
        assert all('eps_diluted' in f.value.calculation_basis for f in result.facts if f.key.metric == 'eps')


def test_idempotence_and_complete_transaction_rollback(monkeypatch):
    repo = store()
    doc = _document(annual())
    old = _document_fact(doc, 'revenue', '2026-03-31', 'QUARTERLY', 'STANDALONE', '777')
    repo._persistence.upsert_financial_fact(old)
    original = repo._persistence._write_financial_fact
    calls = 0
    def fail_second(fact):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError('injected failure')
        original(fact)
    monkeypatch.setattr(repo._persistence, '_write_financial_fact', fail_second)
    with pytest.raises(RuntimeError, match='injected failure'):
        repo._reconcile_persisted_official_financial_document(doc)
    assert repo.financial_facts_for(doc.instrument_id) == [old]
    monkeypatch.setattr(repo._persistence, '_write_financial_fact', original)
    assert repo._reconcile_persisted_official_financial_document(doc)[1] > 0
    before = repo.financial_facts_for(doc.instrument_id)
    assert repo._reconcile_persisted_official_financial_document(doc) == (True, 0)
    assert repo.financial_facts_for(doc.instrument_id) == before
