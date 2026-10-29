"""Regression: FinancialGapState.accepts() must accept Yahoo's deliberate
"UNKNOWN" reporting_basis for GROWTH_FACTS (and the other FINANCIAL_GAP_
REQUIREMENTS), instead of blanket-rejecting it.

Root cause (confirmed via a real bounded Equity Radar run, Ellenbarrie
Industrial Gases): every GROWTH_FACTS fact the Yahoo Finance MCP tool
returns for revenue/PAT/EPS/etc. is built by
app/structured_market.py's _normalize_statement_facts (reused verbatim by
ai/yahoo-finance-mcp/yahoo_mcp_server/server.py's _statement_facts, per
that package's own "Reuses the proven research-engine Yahoo client and
normalizers" docstring) with reportingBasis hardcoded to "UNKNOWN" --
"Yahoo does not expose standalone/consolidated basis here, so UNKNOWN is
a deliberate canonical isolation value rather than an implied equivalence"
(structured_market.py's own comment). FinancialGapState.accepts() only
allowed {"STANDALONE", "CONSOLIDATED"}, so every one of these facts was
rejected with INVALID_BASIS_PERIOD_TYPE_OR_NON_NUMERIC_VALUE (observed:
76/76 raw GROWTH_FACTS facts rejected with exactly this code for one real
instrument) -- not because the data was bad, but because the gate never
recognized the isolation category the rest of the pipeline (e.g.
app/research_readiness_runtime.py's own
`fact.key.reporting_basis or "UNKNOWN"` grouping fallback) already treats
as legitimate.

The fix adds "UNKNOWN" to the allowed set. This test also proves the fix
is narrow: a still-genuinely-invalid basis string, and a non-numeric
value, are both still rejected exactly as before.

Provider-free: no real NSE/Yahoo/network calls, no deploy.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from app.fact_precedence import FactSourceTier, FinancialFact, FinancialFactKey
from app.financial_gap_fill import FinancialGapState
from app.models import ProvenancedValue, SourceMode

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


class _Readiness:
    generated_at = NOW


def _fact(metric, value, period_end, period_type, reporting_basis, *, unit="INR"):
    return FinancialFact(
        FinancialFactKey(UUID("11111111-1111-1111-1111-111111111111"), metric, period_end, period_type, reporting_basis),
        ProvenancedValue(
            value=value, unit=unit,
            as_of_date=datetime.fromisoformat(period_end).replace(tzinfo=timezone.utc),
            source_url="https://finance.yahoo.com/quote/ELLENBARRIE.NS",
            source_name="Yahoo Finance MCP", source_type="EXTERNAL_MCP_PROVIDER",
            published_at=NOW - timedelta(days=2), retrieved_at=NOW - timedelta(hours=1), confidence=0.78,
        ),
        FactSourceTier.YAHOO, "YAHOO_FINANCE_MCP",
        f"YAHOO_FINANCE_MCP:ELLENBARRIE.NS:get_financials:{metric}:{period_end}:{period_type}", SourceMode.REAL,
    )


def _state(missing_input_ids):
    return FinancialGapState(_Readiness(), (), {"GROWTH_FACTS": frozenset(missing_input_ids)})


def test_unknown_reporting_basis_is_accepted_for_growth_facts():
    state = _state({"REVENUE_HISTORY"})
    fact = _fact("revenue", Decimal("100"), "2026-06-30", "QUARTERLY", "UNKNOWN")
    diagnostics: list[str] = []
    assert state.accepts("GROWTH_FACTS", fact, "INR", diagnostics) is True
    assert diagnostics == []


def test_unknown_basis_fact_never_merges_with_an_existing_standalone_series():
    # An UNKNOWN-basis fact must not be treated as interchangeable with a
    # real STANDALONE/CONSOLIDATED series already on file for the same
    # metric/period_type -- basis stays the outer grouping key.
    existing = _fact("revenue", Decimal("90"), "2026-03-31", "QUARTERLY", "STANDALONE")
    state = FinancialGapState(_Readiness(), (existing,), {"GROWTH_FACTS": frozenset({"REVENUE_HISTORY"})})
    fact = _fact("revenue", Decimal("100"), "2026-06-30", "QUARTERLY", "UNKNOWN")
    diagnostics: list[str] = []
    assert state.accepts("GROWTH_FACTS", fact, "INR", diagnostics) is False
    assert diagnostics == ["REPORTING_BASIS_INCONSISTENT_WITH_EXISTING_SERIES"]


def test_two_unknown_basis_facts_do_combine_with_each_other():
    existing = _fact("revenue", Decimal("90"), "2026-03-31", "QUARTERLY", "UNKNOWN")
    state = FinancialGapState(_Readiness(), (existing,), {"GROWTH_FACTS": frozenset({"REVENUE_HISTORY"})})
    fact = _fact("revenue", Decimal("100"), "2026-06-30", "QUARTERLY", "UNKNOWN")
    diagnostics: list[str] = []
    assert state.accepts("GROWTH_FACTS", fact, "INR", diagnostics) is True


def test_genuinely_invalid_reporting_basis_is_still_rejected():
    state = _state({"REVENUE_HISTORY"})
    fact = _fact("revenue", Decimal("100"), "2026-06-30", "QUARTERLY", "SEGMENT")
    diagnostics: list[str] = []
    assert state.accepts("GROWTH_FACTS", fact, "INR", diagnostics) is False
    assert diagnostics == ["INVALID_BASIS_PERIOD_TYPE_OR_NON_NUMERIC_VALUE"]


def test_non_numeric_value_is_still_rejected_even_with_unknown_basis():
    state = _state({"REVENUE_HISTORY"})
    fact = _fact("revenue", "not-a-number", "2026-06-30", "QUARTERLY", "UNKNOWN")
    diagnostics: list[str] = []
    assert state.accepts("GROWTH_FACTS", fact, "INR", diagnostics) is False
    assert diagnostics == ["INVALID_BASIS_PERIOD_TYPE_OR_NON_NUMERIC_VALUE"]


def test_real_basis_fact_keeps_its_own_basis_key_when_only_unknown_facts_exist():
    # Asymmetric case the fix does not need to block, and must not mix:
    # the `bases` set in accepts() is built only from {"STANDALONE",
    # "CONSOLIDATED"} series members, so when the only existing facts for
    # this metric/period_type are UNKNOWN, `bases` is empty and this check
    # does not fire -- a genuinely STANDALONE fact is accepted on its own
    # terms, not merged into or coerced by the UNKNOWN series. This is safe
    # because every downstream consumer that builds a financial series
    # keys explicitly by reporting_basis (app/financial_authority.py's
    # `group = (key.period_type, key.reporting_basis)` /
    # `group = (fact.key.instrument_id, fact.key.reporting_basis, unit, ...)`,
    # and app/research_readiness_runtime.py's
    # `reporting_basis = fact.key.reporting_basis or "UNKNOWN"` then
    # `quarterly_metrics.setdefault(reporting_basis, {})...`) -- so this
    # fact's own key.reporting_basis staying "STANDALONE" (never silently
    # rewritten to "UNKNOWN" or vice versa) is what keeps it out of the
    # UNKNOWN group in every one of those downstream groupings.
    existing = _fact("revenue", Decimal("90"), "2026-03-31", "QUARTERLY", "UNKNOWN")
    state = FinancialGapState(_Readiness(), (existing,), {"GROWTH_FACTS": frozenset({"REVENUE_HISTORY"})})
    fact = _fact("revenue", Decimal("100"), "2026-06-30", "QUARTERLY", "STANDALONE")
    diagnostics: list[str] = []
    assert state.accepts("GROWTH_FACTS", fact, "INR", diagnostics) is True
    assert fact.key.reporting_basis == "STANDALONE"
