"""Regression tests for the INVALIDATED_FOR_SOURCE_SCOPE invalidation
mechanism connecting AMBIGUOUS_DECIMAL_SCALE (app/financial_projection.py)
to the existing document-scoped deletion primitive
ResearchPersistence.reconcile_financial_facts_for_source (app/persistence.py).

Design, restated for the tests below:
  * COMPLETE_FOR_SOURCE_SCOPE keeps its pre-existing meaning and
    pre-existing corroboration requirement (an incoming fact must prove the
    scope) completely unchanged -- see test_di20e_financial_projection.py
    for its own, untouched regression coverage.
  * INVALIDATED_FOR_SOURCE_SCOPE is a new, distinct completeness value.
    It is produced only when a row is rejected for being internally
    self-contradictory (AMBIGUOUS_DECIMAL_SCALE), never for ordinary
    incomplete extraction (missing cells, unresolved layout, a duplicate
    conflict). It authorizes deletion through a separate code path in
    reconcile_financial_facts_for_source that does NOT require a
    corroborating incoming fact (there isn't one -- the row emitted
    nothing), but is still restricted to exactly the same
    (instrument_id, source_identity=document_id, metric, reporting_basis,
    period_end/period_type) tuple as every other deletion this primitive
    performs.
"""
from decimal import Decimal
from uuid import UUID

from app.fact_precedence import FactSourceTier, FinancialFact, FinancialFactKey
from app.financial_projection import FinancialReconciliationScope, project_semantic_financial_facts
from app.models import ProvenancedValue, SourceMode
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository
from test_di20e_financial_projection import projected, values
from test_official_nse_financial_parsing import JUNE, _document, _document_fact

_PAT_ROW = 'Net Profit for the period after Tax 1,927.21 1,684.31 1,745.69 6,427.00'
_CORRUPTED_PAT_ROW = 'Net Profit for the period after Tax 27347 7303 373.08 50183'


def _july_document_with_corrupted_pat():
    text = JUNE.replace(_PAT_ROW, _CORRUPTED_PAT_ROW)
    return _document(text).model_copy(update={"document_id": UUID("cc5618c4-fe85-4752-969f-c93a8faec2e4")})


def test_ambiguous_row_emits_an_invalidated_scope_not_a_complete_one():
    document, result = projected(JUNE.replace(_PAT_ROW, _CORRUPTED_PAT_ROW))
    pat_scopes = [s for s in result.scopes if s.metric == 'pat']
    assert pat_scopes, "expected an INVALIDATED_FOR_SOURCE_SCOPE entry for the blocked pat row"
    assert all(s.completeness == 'INVALIDATED_FOR_SOURCE_SCOPE' for s in pat_scopes)
    assert not any(s.completeness == 'COMPLETE_FOR_SOURCE_SCOPE' for s in pat_scopes)
    # No scope at all claims 'pat' is COMPLETE_FOR_SOURCE_SCOPE -- the
    # pre-existing contract for that value is left completely alone.
    assert values(result, 'pat') == []
    # revenue/eps rows in the same statement are entirely unaffected by the
    # pat-only defect: their facts are still projected normally.
    assert len(values(result, 'revenue')) == 4
    assert len(values(result, 'eps')) == 4


def test_ordinary_incomplete_extraction_never_produces_an_invalidated_scope():
    # VALUE_COLUMN_MISMATCH-style incompleteness (here: a row with fewer
    # cells than the statement's columns) must never be treated as
    # "confirmed unreliable" -- only AMBIGUOUS_DECIMAL_SCALE may invalidate.
    text = JUNE.replace(_PAT_ROW, 'Net Profit for the period after Tax 1,927.21 1,684.31')
    _, result = projected(text)
    assert not any(s.completeness == 'INVALIDATED_FOR_SOURCE_SCOPE' for s in result.scopes)


def _seed_this_documents_own_unaffected_facts(persistence, document):
    # The real, correctly-extracted revenue/eps facts the SAME document
    # also produces, pre-seeded with their real values so reconciliation's
    # written count below reflects ONLY the pat invalidation, not ordinary
    # first-time upserts of unrelated metrics.
    for period_end, period_type, value in [
        ("2026-06-30", "QUARTERLY", "8261.11"), ("2026-03-31", "QUARTERLY", "7335.75"),
        ("2025-06-30", "QUARTERLY", "6915.38"), ("2026-03-31", "ANNUAL", "27284.15"),
    ]:
        persistence.upsert_financial_fact(_document_fact(document, "revenue", period_end, period_type, None, value))
    for period_end, period_type, value in [
        ("2026-06-30", "QUARTERLY", "1.47"), ("2026-03-31", "QUARTERLY", "1.29"),
        ("2025-06-30", "QUARTERLY", "1.34"), ("2026-03-31", "ANNUAL", "5.36"),
    ]:
        persistence.upsert_financial_fact(_document_fact(document, "eps", period_end, period_type, None, value))


def test_invalidation_scope_deletes_only_this_documents_pat_keys_for_the_affected_periods():
    document = _july_document_with_corrupted_pat()
    repository = ResearchRepository(persistence=SqliteResearchPersistence())
    persistence = repository._persistence
    # Seed the four stale pat facts this same document previously wrote --
    # exactly the CORDELIA pattern (27347, 7303, 373.08, 50183 once /100'd,
    # or as originally mis-parsed; the seeded *values* don't matter here,
    # only that they are owned by this document/metric/periods).
    periods = [("2026-06-30", "QUARTERLY"), ("2026-03-31", "QUARTERLY"),
               ("2025-06-30", "QUARTERLY"), ("2026-03-31", "ANNUAL")]
    for period_end, period_type in periods:
        persistence.upsert_financial_fact(
            _document_fact(document, "pat", period_end, period_type, None, "999"))
    # This same document's other, unaffected metrics -- pre-seeded with
    # their real values so this run has nothing else to upsert.
    _seed_this_documents_own_unaffected_facts(persistence, document)

    facts, scopes, diagnostics = _project(document)
    assert any(d.startswith('AMBIGUOUS_DECIMAL_SCALE:profit_after_tax') for d in diagnostics)
    persistence.reconcile_financial_facts_for_source(
        document.instrument_id, str(document.document_id), facts, complete_scopes=scopes)

    remaining = persistence.load_financial_facts({document.instrument_id})
    remaining_metrics = {f.key.metric for f in remaining}
    assert 'pat' not in remaining_metrics          # all 4 stale pat facts removed
    assert len([f for f in remaining if f.key.metric == 'revenue']) == 4  # untouched in count
    assert len([f for f in remaining if f.key.metric == 'eps']) == 4      # untouched in count
    # Exactly the pat keys owned by this document for these periods are
    # gone; nothing with a different metric or a different document is
    # affected (same assertion restated at the row level, not just count).
    assert all(f.source_identity == str(document.document_id) for f in remaining)


def test_invalidation_does_not_touch_facts_owned_by_a_different_document():
    document = _july_document_with_corrupted_pat()
    other_document_id = "11111111-1111-1111-1111-111111111111"
    repository = ResearchRepository(persistence=SqliteResearchPersistence())
    persistence = repository._persistence
    persistence.upsert_financial_fact(
        _document_fact(document, "pat", "2026-06-30", "QUARTERLY", None, "999"))
    # Same instrument, same metric, same period -- but a DIFFERENT document.
    other_fact = FinancialFact(
        FinancialFactKey(document.instrument_id, "pat", "2026-06-30", "QUARTERLY", None),
        ProvenancedValue(value=Decimal("12.34"), unit="INR crore", source_url="https://other.example",
                          source_name="Other filing", retrieved_at=document.retrieved_at),
        FactSourceTier.OFFICIAL_NSE, "NSE", other_document_id, SourceMode.REAL,
    )
    persistence._write_financial_fact(other_fact)

    facts, scopes, _ = _project(document)
    persistence.reconcile_financial_facts_for_source(
        document.instrument_id, str(document.document_id), facts, complete_scopes=scopes)

    remaining = persistence.load_financial_facts({document.instrument_id})
    other_owned = [f for f in remaining if f.source_identity == other_document_id]
    assert len(other_owned) == 1 and other_owned[0].value.value == Decimal("12.34")


def test_invalidation_does_not_touch_a_different_instrument():
    document = _july_document_with_corrupted_pat()
    other_instrument_id = UUID("22222222-2222-2222-2222-222222222222")
    other_document = document.model_copy(update={"instrument_id": other_instrument_id,
                                                   "document_id": UUID("33333333-3333-3333-3333-333333333333")})
    repository = ResearchRepository(persistence=SqliteResearchPersistence())
    persistence = repository._persistence
    persistence.upsert_financial_fact(
        _document_fact(document, "pat", "2026-06-30", "QUARTERLY", None, "999"))
    persistence.upsert_financial_fact(
        _document_fact(other_document, "pat", "2026-06-30", "QUARTERLY", None, "555"))

    facts, scopes, _ = _project(document)
    persistence.reconcile_financial_facts_for_source(
        document.instrument_id, str(document.document_id), facts, complete_scopes=scopes)

    other_remaining = persistence.load_financial_facts({other_instrument_id})
    assert len(other_remaining) == 1 and other_remaining[0].value.value == Decimal("555")


def test_october_total_equity_survives_july_pat_invalidation():
    # The exact two-document CORDELIA scenario: an October filing owning
    # total_equity, a July filing whose pat row is ambiguous. Invalidating
    # July's pat must never touch October's total_equity.
    july_document = _july_document_with_corrupted_pat()
    october_document = _document(JUNE).model_copy(
        update={"document_id": UUID("44444444-4444-4444-4444-444444444444"),
                "instrument_id": july_document.instrument_id})
    repository = ResearchRepository(persistence=SqliteResearchPersistence())
    persistence = repository._persistence
    persistence.upsert_financial_fact(
        _document_fact(july_document, "pat", "2026-06-30", "QUARTERLY", None, "999"))
    persistence.upsert_financial_fact(
        _document_fact(october_document, "total_equity", "2026-03-31", "AS_AT", "STANDALONE", "7315.07"))
    persistence.upsert_financial_fact(
        _document_fact(october_document, "total_equity", "2025-03-31", "AS_AT", "STANDALONE", "1321.05"))

    facts, scopes, _ = _project(july_document)
    persistence.reconcile_financial_facts_for_source(
        july_document.instrument_id, str(july_document.document_id), facts, complete_scopes=scopes)

    remaining = persistence.load_financial_facts({july_document.instrument_id})
    equity_values = sorted(f.value.value for f in remaining if f.key.metric == "total_equity")
    assert equity_values == [Decimal("1321.05"), Decimal("7315.07")]
    assert not any(f.key.metric == "pat" for f in remaining)


def test_invalidation_is_idempotent():
    document = _july_document_with_corrupted_pat()
    repository = ResearchRepository(persistence=SqliteResearchPersistence())
    persistence = repository._persistence
    persistence.upsert_financial_fact(
        _document_fact(document, "pat", "2026-06-30", "QUARTERLY", None, "999"))
    _seed_this_documents_own_unaffected_facts(persistence, document)

    facts, scopes, _ = _project(document)
    persistence.reconcile_financial_facts_for_source(
        document.instrument_id, str(document.document_id), facts, complete_scopes=scopes)
    remaining_after_first = persistence.load_financial_facts({document.instrument_id})
    assert not any(f.key.metric == "pat" for f in remaining_after_first)

    second = persistence.reconcile_financial_facts_for_source(
        document.instrument_id, str(document.document_id), facts, complete_scopes=scopes)
    assert second == 0  # already gone and everything else already matches -- a true no-op
    remaining_after_second = persistence.load_financial_facts({document.instrument_id})
    assert not any(f.key.metric == "pat" for f in remaining_after_second)
    assert sorted((str(f.key), f.value.value) for f in remaining_after_second) == \
           sorted((str(f.key), f.value.value) for f in remaining_after_first)


def test_invalidation_without_any_incoming_facts_still_reconciles():
    # A document whose ENTIRE output happens to be withheld must still be
    # able to invalidate stale facts -- reconcile_financial_facts_for_source
    # must not bail out early just because `facts` is empty when an
    # INVALIDATED_FOR_SOURCE_SCOPE is present.
    document = _july_document_with_corrupted_pat()
    scope = FinancialReconciliationScope(
        "pat", None, frozenset({"2026-06-30"}), frozenset({"QUARTERLY"}), "INVALIDATED_FOR_SOURCE_SCOPE")
    repository = ResearchRepository(persistence=SqliteResearchPersistence())
    persistence = repository._persistence
    persistence.upsert_financial_fact(
        _document_fact(document, "pat", "2026-06-30", "QUARTERLY", None, "999"))

    written = persistence.reconcile_financial_facts_for_source(
        document.instrument_id, str(document.document_id), [], complete_scopes=(scope,))
    assert written == 1
    remaining = persistence.load_financial_facts({document.instrument_id})
    assert not any(f.key.metric == "pat" for f in remaining)


def _project(document):
    projection = project_semantic_financial_facts(document)
    return list(projection.facts), projection.scopes, list(projection.diagnostics)
