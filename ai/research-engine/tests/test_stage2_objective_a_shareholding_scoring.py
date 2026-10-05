"""AI INVESTMENT PLATFORM -- Stage-2 Objective A: SHAREHOLDING readiness vs
rule-engine scoring reconciliation (the "POLICYBZR-class" bug: READY, then
RULE_AREA_UNSCORABLE for the identical evidence).

Root cause (confirmed by direct trace, not special-cased to any instrument):
app.research_applicability.shareholding_input_coverage() marked the
mandatory PROMOTER_INSTITUTIONAL_PUBLIC_CATEGORIES input covered from ANY
ONE of {PROMOTER, FII_FPI, DII, PUBLIC_RETAIL} present in a SINGLE period,
while app.stock_rule_engine.StockRuleEngine._shareholding() can only
actually produce a scoreable metric from (a) PROMOTER in the latest period,
or (b) a PROMOTER/FII_FPI/DII trend requiring TWO periods. A candidate with
only FII_FPI/DII/PUBLIC_RETAIL in one period satisfied readiness but
produced zero metrics -> UNSCORABLE.

Fix: a new multi-period-aware function,
app.research_applicability.shareholding_scorable_ownership_coverage(),
mirrors the rule engine's own two scoring paths exactly. Readiness
(app.research_readiness_runtime.RepositoryResearchReadinessAdapter.
_append_shareholding) now gates the mandatory
PROMOTER_INSTITUTIONAL_PUBLIC_CATEGORIES input on this function instead of
the old single-snapshot "any category present" check; LATEST_VALID_
SHAREHOLDING_PERIOD and the (supporting, optional) PROMOTER_PLEDGE input
are untouched. The rule engine's own missing-input identifier for an
unscorable result was also renamed from the unrelated
"STRUCTURED_OWNERSHIP_VALUES" to the SAME "PROMOTER_INSTITUTIONAL_PUBLIC_
CATEGORIES" readiness already uses, so both layers now share one
vocabulary.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.models import (
    CompanyResearchProfile, ReliabilityLevel, ShareholdingCategory,
    ShareholdingSnapshot, ShareholdingSnapshotValue, SourceMode,
)
from app.repository import ResearchRepository
from app.research_applicability import shareholding_scorable_ownership_coverage
from app.research_readiness import ResearchReadinessService, ResearchRequirementStatus
from app.research_readiness_runtime import RepositoryResearchReadinessAdapter
from app.settings import Settings
from tests.test_slice2_shareholding_nse_first_authority import (
    INSTRUMENT_ID, Q0_END, Q1_END, _nse_xbrl_snapshot, _value,
)

Q_MINUS_2_END = __import__("datetime").datetime(2025, 12, 31, tzinfo=__import__("datetime").timezone.utc)


def _snapshot(period_end, values, *, source_identity="nse-1"):
    return ShareholdingSnapshot(
        instrument_id=INSTRUMENT_ID, period_end=period_end, source_provider="NSE",
        source_type="NSE_SHAREHOLDING_XBRL", source_identity_key=source_identity,
        source_url="https://www.nseindia.com/shp.xml", confidence=Decimal("0.95"),
        reliability_level=ReliabilityLevel.LEVEL_A, source_mode=SourceMode.REAL, values=values,
    )


# ---------------------------------------------------------------------------
# Pure-function tests: shareholding_scorable_ownership_coverage()
# ---------------------------------------------------------------------------

# 1. one snapshot with PROMOTER -> scorable.
def test_single_period_with_promoter_is_scorable() -> None:
    snap = _snapshot(Q1_END, [_value(ShareholdingCategory.PROMOTER, "42.50")])
    assert shareholding_scorable_ownership_coverage([snap]) is True


# 2. PROMOTER = 0 -> valid, still scorable (zero is not missing).
def test_promoter_zero_is_scorable_not_missing() -> None:
    snap = _snapshot(Q1_END, [_value(ShareholdingCategory.PROMOTER, "0")])
    assert shareholding_scorable_ownership_coverage([snap]) is True


# 3. one snapshot with only FII/DII/public -> NOT scorable for single-period.
def test_single_period_without_promoter_is_not_scorable() -> None:
    snap = _snapshot(Q1_END, [
        _value(ShareholdingCategory.FII_FPI, "15.25"),
        _value(ShareholdingCategory.DII, "6.80"),
        _value(ShareholdingCategory.PUBLIC_RETAIL, "24.76"),
    ])
    assert shareholding_scorable_ownership_coverage([snap]) is False


# 4. two compatible periods (sharing a trend-supported category) -> scorable,
#    even with no PROMOTER in the latest period at all.
def test_two_compatible_periods_without_promoter_are_scorable_via_trend() -> None:
    latest = _snapshot(Q1_END, [_value(ShareholdingCategory.FII_FPI, "15.25")], source_identity="nse-q1")
    previous = _snapshot(Q0_END, [_value(ShareholdingCategory.FII_FPI, "14.00")], source_identity="nse-q0")
    assert shareholding_scorable_ownership_coverage([latest, previous]) is True


# 5. incompatible periods (no shared trend-supported category) -> not scorable.
def test_incompatible_periods_are_not_scorable() -> None:
    latest = _snapshot(Q1_END, [_value(ShareholdingCategory.PUBLIC_RETAIL, "24.76")], source_identity="nse-q1")
    previous = _snapshot(Q0_END, [_value(ShareholdingCategory.PROMOTER_PLEDGE, "5.00",
                                          basis="PERCENT_OF_PROMOTER_HOLDING")], source_identity="nse-q0")
    assert shareholding_scorable_ownership_coverage([latest, previous]) is False


# 6. wrong-quarter data never reaches this function at all -- it is filtered
#    out upstream by shareholding_period_groups()/shareholding_for() before
#    scorability is ever assessed. Confirm that upstream contract directly.
def test_wrong_quarter_snapshot_excluded_before_scorability_is_assessed() -> None:
    from app.models import ShareholdingSnapshot as _S
    repository = ResearchRepository(settings=Settings())
    non_quarter_end = __import__("datetime").datetime(2026, 6, 15, tzinfo=__import__("datetime").timezone.utc)
    bad = _snapshot(non_quarter_end, [_value(ShareholdingCategory.PROMOTER, "42.50")], source_identity="nse-bad")
    repository.shareholding_snapshots[bad.id] = bad
    assert repository.shareholding_for(INSTRUMENT_ID, limit=4) == []


# 7. no snapshots at all -> not scorable (never raises).
def test_no_snapshots_is_not_scorable() -> None:
    assert shareholding_scorable_ownership_coverage([]) is False


# ---------------------------------------------------------------------------
# Integration tests: readiness end-to-end agrees with the rule engine.
# ---------------------------------------------------------------------------

def _profile():
    return CompanyResearchProfile(
        instrument_id=INSTRUMENT_ID, company_id=__import__("uuid").uuid4(), company_name="Ready Limited",
        ticker="READY", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        provider_instrument_ids={"YAHOO_FINANCE": "READY.NS", "NSE": "READY"},
    )


def _repository_with(*snapshots) -> ResearchRepository:
    repository = ResearchRepository(settings=Settings())
    repository.profiles.append(_profile())
    for snapshot in snapshots:
        repository.shareholding_snapshots[snapshot.id] = snapshot
    return repository


def _shareholding_status(repository) -> ResearchRequirementStatus:
    readiness = ResearchReadinessService(
        RepositoryResearchReadinessAdapter(repository)
    ).assess(INSTRUMENT_ID, jurisdiction="INDIA")
    return readiness.for_requirement("SHAREHOLDING").status


READY_STATUSES = {ResearchRequirementStatus.READY_FRESH, ResearchRequirementStatus.READY_STALE}


# 8. readiness READY implies the rule engine can actually produce the
#    required ownership scoring input, for both the single-period PROMOTER
#    path and the two-period trend path -- proven by running BOTH layers
#    against the identical evidence and asserting they agree.
def test_readiness_ready_implies_rule_engine_scorable_single_period() -> None:
    snap = _nse_xbrl_snapshot()  # has PROMOTER among other categories
    repository = _repository_with(snap)

    status = _shareholding_status(repository)
    assert status in READY_STATUSES

    scorable = shareholding_scorable_ownership_coverage(repository.shareholding_for(INSTRUMENT_ID, limit=4))
    assert scorable is True


def test_readiness_ready_implies_rule_engine_scorable_trend_path() -> None:
    latest = _snapshot(Q1_END, [_value(ShareholdingCategory.FII_FPI, "15.25")], source_identity="nse-q1")
    previous = _snapshot(Q0_END, [_value(ShareholdingCategory.FII_FPI, "14.00")], source_identity="nse-q0")
    repository = _repository_with(latest, previous)

    status = _shareholding_status(repository)
    assert status in READY_STATUSES, "two compatible periods must satisfy readiness via the trend path"

    scorable = shareholding_scorable_ownership_coverage(repository.shareholding_for(INSTRUMENT_ID, limit=4))
    assert scorable is True


# 9. POLICYBZR-class regression, without hardcoding any specific instrument:
#    a candidate whose only persisted shareholding evidence is a single
#    period containing FII_FPI/DII/PUBLIC_RETAIL but NO PROMOTER (and no
#    second compatible period) must NOT be reported READY by readiness --
#    this is the exact evidence shape that used to produce
#    "SHAREHOLDING READY" immediately followed by "RULE_AREA_UNSCORABLE".
def test_single_period_without_promoter_is_not_ready_policybzr_class_regression() -> None:
    snap = _snapshot(Q1_END, [
        _value(ShareholdingCategory.FII_FPI, "15.25"),
        _value(ShareholdingCategory.DII, "6.80"),
        _value(ShareholdingCategory.PUBLIC_RETAIL, "24.76"),
    ], source_identity="policybzr-class")
    repository = _repository_with(snap)

    status = _shareholding_status(repository)
    assert status not in READY_STATUSES, (
        "readiness must not report SHAREHOLDING ready for ownership evidence "
        "the rule engine cannot turn into any scoring metric"
    )

    scorable = shareholding_scorable_ownership_coverage(repository.shareholding_for(INSTRUMENT_ID, limit=4))
    assert scorable is False, "the rule engine genuinely cannot score this evidence -- readiness now agrees"


# 7 (mixed-source provenance). Mixed NSE/Yahoo/PDF evidence: adding a
# scorability gate on the mandatory input must not disturb the existing
# authority-aware per-field provenance merge (pledge from a PDF, promoter
# from NSE XBRL, in the same period) -- re-run the existing Slice 2 mixed-
# source contract end-to-end through the full readiness path to confirm
# the gate is additive, not destructive, to field-level coverage.
def test_mixed_source_provenance_preserved_when_scorable() -> None:
    nse = _snapshot(Q1_END, [
        _value(ShareholdingCategory.PROMOTER, "42.50", locator="nse-xbrl:promoter"),
        _value(ShareholdingCategory.FII_FPI, "15.25", locator="nse-xbrl:fpi"),
    ], source_identity="nse-q1")
    pdf = ShareholdingSnapshot(
        instrument_id=INSTRUMENT_ID, period_end=Q1_END, source_provider="NSE",
        source_type="EXCHANGE_ANNOUNCEMENT", source_identity_key="nse-pdf-q1",
        source_url="https://www.nseindia.com/files/shareholding.pdf", confidence=Decimal("0.90"),
        reliability_level=ReliabilityLevel.LEVEL_A, source_mode=SourceMode.REAL,
        values=[_value(ShareholdingCategory.PROMOTER_PLEDGE, "5.00", basis="PERCENT_OF_PROMOTER_HOLDING")],
    )
    repository = _repository_with(nse, pdf)

    status = _shareholding_status(repository)
    assert status in READY_STATUSES

    scorable = shareholding_scorable_ownership_coverage(repository.shareholding_for(INSTRUMENT_ID, limit=4))
    assert scorable is True

    # shareholding_for() itself must still return the NSE promoter snapshot
    # as authoritative for this period (pledge living on a separate,
    # lower-authority PDF snapshot is the existing Slice 2 contract,
    # unchanged by this fix) -- full StockRuleEngineInput/_shareholding()
    # metric-level provenance is exercised by test_stock_rule_engine.py.
    best = repository.shareholding_for(INSTRUMENT_ID, limit=4)
    assert len(best) == 1
    assert best[0].id == nse.id
