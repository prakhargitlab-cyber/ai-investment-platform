"""Offline regression tests for scripts/reconcile_persisted_financial_facts.py.

Covers: acquisition-freedom (static import check), instrument scoping,
dry-run producing no writes, apply-mode writing exactly through the
existing reconcile_financial_facts_for_source primitive, and
idempotency/no-op on a second apply.
"""
import ast
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from app.fact_precedence import FactSourceTier, FinancialFact, FinancialFactKey
from app.models import ProvenancedValue, SourceMode
from app.persistence import SqliteResearchPersistence
from test_official_nse_financial_parsing import _document, _document_fact

from scripts.reconcile_persisted_financial_facts import eligible_official_documents, run

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "reconcile_persisted_financial_facts.py"

# Mirrors the total_equity-vs-total_equity_and_liabilities fixture already
# used in tests/test_di20e_financial_projection.py for the parser fix itself.
_BALANCE_SHEET_TEXT = (
    'Statement of Standalone financial results (Rs. in Crore) Particulars '
    'Year ended 31.03.2026 31.03.2025 Revenue from operations 101.01 90.02 '
    'Profit after tax 11.01 9.02 Balance Sheet Particulars As at 31 March 2026 31 March 2025 '
    'Total Assets 500.00 450.00 Total Equity 120.00 110.00 '
    'Total Equity and Liabilities 500.00 450.00 Total Liabilities 380.00 340.00'
)

OTHER_INSTRUMENT_ID = UUID("11111111-1111-1111-1111-111111111111")


def test_module_imports_no_acquisition_machinery():
    """Static guarantee: nothing in this module can reach NSE/Yahoo.

    No import of a fetcher, discovery, or search-discovery module, and no
    reference to the live-refresh/readiness-ensure entry points.
    """
    tree = ast.parse(SCRIPT_PATH.read_text())
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported_names.add(alias.name)
    forbidden_substrings = ("fetch", "discovery", "httpx", "aiohttp", "search_discovery")
    offending = [name for name in imported_names if any(bad in name.lower() for bad in forbidden_substrings)]
    assert offending == [], f"acquisition-related import found: {offending}"

    # Scan identifiers actually used in code (not the module docstring, which
    # names these for documentation) for the live-acquisition entry points.
    identifiers = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
    assert "_refresh_live" not in identifiers
    assert "readiness_runtime" not in identifiers
    assert "ensure_research_readiness" not in identifiers


def _persisted_document(persistence, instrument_id, document_id, text):
    document = _document(text).model_copy(update={"instrument_id": instrument_id, "document_id": document_id})
    persistence.upsert_document(document)
    return document


def test_eligible_official_documents_is_scoped_to_the_requested_instrument():
    persistence = SqliteResearchPersistence()
    target = UUID("002e10ae-c9e7-4ac8-9b05-7ebb310140f0")
    target_doc_id = UUID("22222222-2222-2222-2222-222222222222")
    other_doc_id = UUID("33333333-3333-3333-3333-333333333333")
    _persisted_document(persistence, target, target_doc_id, _BALANCE_SHEET_TEXT)
    _persisted_document(persistence, OTHER_INSTRUMENT_ID, other_doc_id, _BALANCE_SHEET_TEXT)

    docs = eligible_official_documents(persistence, target)
    assert [d.document_id for d in docs] == [target_doc_id]


def test_dry_run_reports_total_equity_without_writing_anything():
    persistence = SqliteResearchPersistence()
    instrument_id = UUID("002e10ae-c9e7-4ac8-9b05-7ebb310140f0")
    document_id = UUID("44444444-4444-4444-4444-444444444444")
    _persisted_document(persistence, instrument_id, document_id, _BALANCE_SHEET_TEXT)

    rc = run(persistence, instrument_id, apply=False)
    assert rc == 0
    # Dry run must leave the database untouched.
    assert persistence.load_financial_facts({instrument_id}) == []


def test_apply_writes_via_reconcile_financial_facts_for_source_and_is_idempotent():
    persistence = SqliteResearchPersistence()
    instrument_id = UUID("002e10ae-c9e7-4ac8-9b05-7ebb310140f0")
    document_id = UUID("55555555-5555-5555-5555-555555555555")
    document = _persisted_document(persistence, instrument_id, document_id, _BALANCE_SHEET_TEXT)

    # Seed a fact owned by a DIFFERENT document to prove reconciliation never
    # touches facts it does not own.
    unrelated = FinancialFact(
        FinancialFactKey(instrument_id, "revenue", "2023-03-31", "ANNUAL", "STANDALONE"),
        ProvenancedValue(value=Decimal("77"), unit="INR crore", source_url="https://other.example",
                          source_name="Other", retrieved_at=document.retrieved_at),
        FactSourceTier.OFFICIAL_NSE, "NSE", "other-official-document", SourceMode.REAL,
    )
    persistence.upsert_financial_fact(unrelated)

    rc = run(persistence, instrument_id, apply=True)
    assert rc == 0

    facts = {fact.key: fact for fact in persistence.load_financial_facts({instrument_id})}
    total_equity_keys = [key for key in facts if key.metric == "total_equity"]
    assert len(total_equity_keys) == 2, "both balance-sheet periods for total_equity should now be persisted"
    latest_key = next(key for key in total_equity_keys if key.period_end == "2026-03-31")
    assert facts[latest_key].value.value == Decimal("120.00")
    assert facts[latest_key].source_identity == str(document_id)
    # The unrelated, differently-sourced fact must survive untouched.
    assert facts[unrelated.key] == unrelated

    before_second_run = dict(facts)
    rc2 = run(persistence, instrument_id, apply=True)
    assert rc2 == 0
    after = {fact.key: fact for fact in persistence.load_financial_facts({instrument_id})}
    assert after == before_second_run, "a second apply run must be a no-op (idempotent)"


def test_apply_respects_document_id_filter():
    persistence = SqliteResearchPersistence()
    instrument_id = UUID("002e10ae-c9e7-4ac8-9b05-7ebb310140f0")
    keep_id = UUID("66666666-6666-6666-6666-666666666666")
    skip_id = UUID("77777777-7777-7777-7777-777777777777")
    _persisted_document(persistence, instrument_id, keep_id, _BALANCE_SHEET_TEXT)
    _persisted_document(persistence, instrument_id, skip_id, _BALANCE_SHEET_TEXT)

    run(persistence, instrument_id, apply=True, document_id=keep_id)

    facts = persistence.load_financial_facts({instrument_id})
    assert facts and all(fact.source_identity == str(keep_id) for fact in facts)
