"""SLICE 2 -- SHAREHOLDING NSE-FIRST AUTHORITY + FIELD-LEVEL GAP FILL.

Regression tests for:
  - Authority-aware, field-level (not whole-snapshot) shareholding coverage
    merging across NSE structured/XBRL, Yahoo MCP, and official NSE
    PDF/announcement sources (app.research_applicability.shareholding_field_merge
    / shareholding_source_authority_rank).
  - The new app.repository.ResearchRepository.shareholding_period_groups()
    accessor, and shareholding_for() staying bit-for-bit identical to its
    pre-Slice-2 output.
  - NSE-first acquisition ordering for the SHAREHOLDING requirement in
    app.yahoo_mcp_acquisition.McpFirstResearchCapabilityExecutor.execute_primary,
    and reverification of a stale provider failure against persisted
    evidence before it is allowed to remain in the returned failures.

Numbered comments below map 1:1 to the "TESTS REQUIRED" list in the Slice 2
task.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.models import (
    CompanyResearchProfile, ReliabilityLevel, ShareholdingCategory,
    ShareholdingSnapshot, ShareholdingSnapshotValue, SourceMode,
)
from app.repository import ResearchRepository
from app.research_applicability import (
    shareholding_field_merge, shareholding_input_coverage, shareholding_source_authority_rank,
)
from app.research_readiness import (
    ProviderAuthorityRegistry, ResearchRefreshTarget, ResearchRequirementStatus, RuleEngineArea,
)
from app.research_readiness_runtime import CapabilityExecutionResult
from app.settings import Settings
from app.yahoo_mcp_acquisition import (
    ExternalMcpAcquisitionError, McpFirstResearchCapabilityExecutor, YAHOO_FINANCE_MCP,
)


INSTRUMENT_ID = UUID("5c2b5c2b-5c2b-4c2b-8c2b-5c2b5c2b5c2b")
Q1_END = datetime(2026, 6, 30, tzinfo=timezone.utc)
Q0_END = datetime(2026, 3, 31, tzinfo=timezone.utc)


def _value(category, percentage, *, basis=None, locator=None):
    return ShareholdingSnapshotValue(
        category=category, percentage=Decimal(percentage), metric_basis=basis,
        raw_source_label=str(category), source_locator=locator,
    )


def _nse_xbrl_snapshot(period_end=Q1_END, *, values=None, source_identity="nse-xbrl-1"):
    return ShareholdingSnapshot(
        instrument_id=INSTRUMENT_ID, period_end=period_end, source_provider="NSE",
        source_type="NSE_SHAREHOLDING_XBRL", source_identity_key=source_identity,
        source_url="https://www.nseindia.com/shp.xml", confidence=Decimal("0.95"),
        reliability_level=ReliabilityLevel.LEVEL_A, source_mode=SourceMode.REAL,
        values=list(values) if values is not None else [
            _value(ShareholdingCategory.PROMOTER, "42.50", locator="nse-xbrl:promoter"),
            _value(ShareholdingCategory.FII_FPI, "15.25", locator="nse-xbrl:fpi"),
            _value(ShareholdingCategory.DII, "6.80", locator="nse-xbrl:dii"),
            _value(ShareholdingCategory.PUBLIC_RETAIL, "24.76", locator="nse-xbrl:public"),
            _value(ShareholdingCategory.PROMOTER_PLEDGE, "12.00",
                   basis="PERCENT_OF_PROMOTER_HOLDING", locator="nse-xbrl:pledge"),
        ],
    )


def _nse_pdf_snapshot(period_end=Q1_END, *, values, source_identity="nse-pdf-1"):
    return ShareholdingSnapshot(
        instrument_id=INSTRUMENT_ID, period_end=period_end, source_provider="NSE",
        source_type="EXCHANGE_ANNOUNCEMENT", source_identity_key=source_identity,
        source_url="https://www.nseindia.com/files/shareholding.pdf", confidence=Decimal("0.90"),
        reliability_level=ReliabilityLevel.LEVEL_A, source_mode=SourceMode.REAL, values=values,
    )


def _yahoo_snapshot(period_end=Q1_END, *, values, source_identity="yahoo-1"):
    return ShareholdingSnapshot(
        instrument_id=INSTRUMENT_ID, period_end=period_end, source_provider=YAHOO_FINANCE_MCP,
        source_type="EXTERNAL_MCP_PROVIDER", source_identity_key=source_identity,
        source_url="https://finance.yahoo.com/quote/READY.NS", confidence=Decimal("0.70"),
        reliability_level=ReliabilityLevel.LEVEL_B, source_mode=SourceMode.REAL, values=values,
    )


# ---------------------------------------------------------------------------
# 1. Complete NSE shareholding -> Yahoo not needed -> PDF not needed.
# ---------------------------------------------------------------------------

def test_complete_nse_shareholding_needs_no_fallback_source() -> None:
    nse = _nse_xbrl_snapshot()
    contributions = shareholding_field_merge([nse])
    assert len(contributions) == 1
    snapshot, covered = contributions[0]
    assert snapshot is nse
    assert covered == {
        "LATEST_VALID_SHAREHOLDING_PERIOD",
        "PROMOTER_INSTITUTIONAL_PUBLIC_CATEGORIES",
        "PROMOTER_PLEDGE",
    }


# ---------------------------------------------------------------------------
# 2. NSE partial shareholding -> remaining gap calculated correctly.
# ---------------------------------------------------------------------------

def test_nse_partial_shareholding_leaves_pledge_gap() -> None:
    nse_no_pledge = _nse_xbrl_snapshot(values=[
        _value(ShareholdingCategory.PROMOTER, "42.50", locator="nse-xbrl:promoter"),
        _value(ShareholdingCategory.FII_FPI, "15.25", locator="nse-xbrl:fpi"),
    ])
    contributions = shareholding_field_merge([nse_no_pledge])
    covered = contributions[0][1]
    assert covered == {"LATEST_VALID_SHAREHOLDING_PERIOD", "PROMOTER_INSTITUTIONAL_PUBLIC_CATEGORIES"}
    assert "PROMOTER_PLEDGE" not in covered


# ---------------------------------------------------------------------------
# 3. NSE has promoter %, pledge missing -> promoter % preserved, fallback
#    only attempts/claims the unresolved pledge field.
# ---------------------------------------------------------------------------

def test_fallback_only_claims_the_unresolved_field_not_the_resolved_one() -> None:
    nse = _nse_xbrl_snapshot(values=[
        _value(ShareholdingCategory.PROMOTER, "42.50", locator="nse-xbrl:promoter"),
        _value(ShareholdingCategory.FII_FPI, "15.25", locator="nse-xbrl:fpi"),
    ])
    # Yahoo reports BOTH promoter (a different, stale/approximate number) and
    # pledge. Only pledge is a genuine gap -- Yahoo's promoter value must not
    # be read as resolving/overwriting a field NSE already supplied.
    yahoo = _yahoo_snapshot(values=[
        _value(ShareholdingCategory.PROMOTER, "41.00"),
        _value(ShareholdingCategory.PROMOTER_PLEDGE, "3.50", basis="PERCENT_OF_PROMOTER_HOLDING"),
    ])
    contributions = shareholding_field_merge([nse, yahoo])
    by_snapshot = {snapshot.source_provider: (snapshot, covered) for snapshot, covered in contributions}
    assert by_snapshot["NSE"][1] == {"LATEST_VALID_SHAREHOLDING_PERIOD", "PROMOTER_INSTITUTIONAL_PUBLIC_CATEGORIES"}
    assert by_snapshot[YAHOO_FINANCE_MCP][1] == {"PROMOTER_PLEDGE"}


# ---------------------------------------------------------------------------
# 4. promoter pledge = 0 -> treated as valid/present -> no fallback for pledge.
# ---------------------------------------------------------------------------

def test_promoter_pledge_zero_is_present_not_missing() -> None:
    nse_zero_pledge = _nse_xbrl_snapshot(values=[
        _value(ShareholdingCategory.PROMOTER, "60.00", locator="nse-xbrl:promoter"),
        _value(ShareholdingCategory.PROMOTER_PLEDGE, "0", basis="PERCENT_OF_PROMOTER_HOLDING",
               locator="nse-xbrl:pledge"),
    ])
    assert "PROMOTER_PLEDGE" in shareholding_input_coverage(nse_zero_pledge)

    yahoo_nonzero_pledge = _yahoo_snapshot(values=[
        _value(ShareholdingCategory.PROMOTER_PLEDGE, "9.99", basis="PERCENT_OF_PROMOTER_HOLDING"),
    ])
    contributions = shareholding_field_merge([nse_zero_pledge, yahoo_nonzero_pledge])
    # Yahoo contributes nothing: NSE's 0% pledge already claimed the field.
    assert len(contributions) == 1
    assert contributions[0][0].source_provider == "NSE"
    assert "PROMOTER_PLEDGE" in contributions[0][1]


# ---------------------------------------------------------------------------
# 5. Lower-authority source conflicts with NSE -> NSE value preserved.
# ---------------------------------------------------------------------------

def test_lower_authority_conflict_nse_wins_over_yahoo_and_pdf() -> None:
    nse = _nse_xbrl_snapshot(values=[
        _value(ShareholdingCategory.PROMOTER, "50.00", locator="nse-xbrl:promoter"),
    ])
    yahoo = _yahoo_snapshot(values=[_value(ShareholdingCategory.PROMOTER, "48.00")])
    pdf = _nse_pdf_snapshot(values=[_value(ShareholdingCategory.PROMOTER, "47.00")])
    contributions = shareholding_field_merge([pdf, yahoo, nse])
    # Only NSE contributes anything -- both lower-authority sources' PROMOTER
    # values are fully pre-claimed and never surface as contributions.
    assert len(contributions) == 1
    winner, covered = contributions[0]
    assert winner is nse
    assert "PROMOTER_INSTITUTIONAL_PUBLIC_CATEGORIES" in covered
    assert shareholding_source_authority_rank(nse) < shareholding_source_authority_rank(yahoo)
    assert shareholding_source_authority_rank(yahoo) < shareholding_source_authority_rank(pdf)


# ---------------------------------------------------------------------------
# 6. Mixed-source result -> different missing fields satisfied by different
#    sources without losing provenance.
# ---------------------------------------------------------------------------

def test_mixed_source_result_preserves_each_contributors_own_provenance() -> None:
    nse = _nse_xbrl_snapshot(values=[
        _value(ShareholdingCategory.PROMOTER, "42.50", locator="nse-xbrl:promoter"),
        _value(ShareholdingCategory.FII_FPI, "15.25", locator="nse-xbrl:fpi"),
    ])
    pdf = _nse_pdf_snapshot(values=[
        _value(ShareholdingCategory.PROMOTER_PLEDGE, "5.00", basis="PERCENT_OF_PROMOTER_HOLDING"),
    ])
    contributions = shareholding_field_merge([nse, pdf])
    assert len(contributions) == 2
    # No synthetic merged snapshot was manufactured -- both original objects
    # are returned unchanged.
    returned_ids = {snapshot.id for snapshot, _covered in contributions}
    assert returned_ids == {nse.id, pdf.id}
    covered_union: set[str] = set()
    for _snapshot, covered in contributions:
        covered_union |= covered
    assert covered_union == {
        "LATEST_VALID_SHAREHOLDING_PERIOD",
        "PROMOTER_INSTITUTIONAL_PUBLIC_CATEGORIES",
        "PROMOTER_PLEDGE",
    }


# ---------------------------------------------------------------------------
# 7. Period distinction -> evidence from wrong quarter must not satisfy the
#    required quarter.
# ---------------------------------------------------------------------------

def test_wrong_quarter_evidence_does_not_merge_into_required_period() -> None:
    repository = ResearchRepository(settings=Settings())
    current_quarter = _nse_xbrl_snapshot(period_end=Q1_END, values=[
        _value(ShareholdingCategory.PROMOTER, "42.50", locator="nse-xbrl:promoter"),
    ], source_identity="nse-q1")
    older_quarter_with_pledge = _nse_xbrl_snapshot(period_end=Q0_END, values=[
        _value(ShareholdingCategory.PROMOTER, "43.00", locator="nse-xbrl:promoter"),
        _value(ShareholdingCategory.PROMOTER_PLEDGE, "8.00", basis="PERCENT_OF_PROMOTER_HOLDING",
               locator="nse-xbrl:pledge"),
    ], source_identity="nse-q0")
    repository.shareholding_snapshots[current_quarter.id] = current_quarter
    repository.shareholding_snapshots[older_quarter_with_pledge.id] = older_quarter_with_pledge

    groups = repository.shareholding_period_groups(INSTRUMENT_ID, limit=4)
    assert [group[0].period_end for group in groups] == [Q1_END, Q0_END]
    # The required (latest) period's own group must not contain the older
    # quarter's pledge-bearing snapshot.
    required_period_group = groups[0]
    assert all(snapshot.period_end == Q1_END for snapshot in required_period_group)
    covered_union: set[str] = set()
    for _snapshot, covered in shareholding_field_merge(required_period_group):
        covered_union |= covered
    assert "PROMOTER_PLEDGE" not in covered_union

    # shareholding_for() keeps its exact pre-Slice-2 contract: one snapshot
    # per period, newest first.
    assert repository.shareholding_for(INSTRUMENT_ID, limit=4) == [
        groups[0][0], groups[1][0],
    ]


# ---------------------------------------------------------------------------
# Repository plumbing: shareholding_for() stays bit-for-bit identical.
# ---------------------------------------------------------------------------

def test_shareholding_for_output_unchanged_by_grouped_accessor() -> None:
    repository = ResearchRepository(settings=Settings())
    nse = _nse_xbrl_snapshot()
    pdf_same_period = _nse_pdf_snapshot(values=[
        _value(ShareholdingCategory.PROMOTER_PLEDGE, "1.00", basis="PERCENT_OF_PROMOTER_HOLDING"),
    ])
    repository.shareholding_snapshots[nse.id] = nse
    repository.shareholding_snapshots[pdf_same_period.id] = pdf_same_period

    best = repository.shareholding_for(INSTRUMENT_ID, limit=4)
    assert len(best) == 1
    assert best[0].id == nse.id  # the higher-authority XBRL snapshot, as before

    groups = repository.shareholding_period_groups(INSTRUMENT_ID, limit=4)
    assert len(groups) == 1
    assert {snapshot.id for snapshot in groups[0]} == {nse.id, pdf_same_period.id}
    assert [group[0] for group in groups] == best


# ---------------------------------------------------------------------------
# McpFirstResearchCapabilityExecutor.execute_primary: NSE-first ordering,
# Yahoo-unsupported tolerance, and stale-failure reverification.
# ---------------------------------------------------------------------------

def _profile():
    return CompanyResearchProfile(
        instrument_id=INSTRUMENT_ID, company_id=uuid4(), company_name="Ready Limited",
        ticker="READY", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        provider_instrument_ids={"YAHOO_FINANCE": "READY.NS", "NSE": "READY"},
    )


def _shareholding_target():
    return ResearchRefreshTarget(
        requirement_id="SHAREHOLDING", rule_engine_area=RuleEngineArea.SHAREHOLDING,
        reason=ResearchRequirementStatus.MISSING,
        authority_policy=ProviderAuthorityRegistry.default().policy_for("SHAREHOLDING", "INDIA"),
        existing_evidence_ids=(),
    )


class _InjectingLegacy:
    """Stands in for the legacy/NSE-and-document CapabilityExecutor.

    Persists directly into the real repository instance captured at
    construction time, so a test can simulate "NSE's dedicated feed (and
    the existing targeted official-document fallback) persisted X this
    call" without wiring the real NSE discovery stack.

    After the Slice 2 follow-up (exact NSE/Yahoo/PDF order), the India
    SHAREHOLDING pre-pass in app.yahoo_mcp_acquisition no longer calls
    this double at all -- it calls repository.refresh_targeted_categories
    directly in two phases (see _PhaseScriptedRefresh below). This double
    is kept only for the non-INDIA / general-fallback regression test,
    where SHAREHOLDING is never pulled out of `targets` and the legacy
    catch-all path is unchanged by this follow-up.
    """

    def __init__(self, repository, *, snapshot=None, failures=None, satisfied=()):
        self.repository = repository
        self.calls: list[tuple] = []
        self._snapshot = snapshot
        self._failures = dict(failures or {})
        self._satisfied = tuple(satisfied)

    async def execute_primary(self, instrument_id, targets, **kwargs):
        self.calls.append((instrument_id, tuple(targets), kwargs))
        if self._snapshot is not None:
            self.repository.shareholding_snapshots[self._snapshot.id] = self._snapshot
        return CapabilityExecutionResult(
            ("LEGACY_NSE_SHAREHOLDING",) if targets else (), dict(self._failures), self._satisfied,
        )

    async def execute_approved_fallbacks(self, instrument_id, targets):
        return CapabilityExecutionResult()


class _FailingGateway:
    def __init__(self, exc=None):
        self.calls = 0
        self._exc = exc or ExternalMcpAcquisitionError("EXTERNAL_CAPABILITY_UNSUPPORTED")

    async def acquire_requirement(self, profile, **kwargs):
        self.calls += 1
        raise self._exc


class _SucceedingGateway:
    """Yahoo gateway stand-in for the 'Yahoo completes' ordering test.

    Returns a successful, non-empty outcome instead of raising, exercising
    the ordinary (no active deep-investigation repair budget) completion
    path, where a successful Yahoo acquisition is taken at face value by
    the existing per-target loop and the NSE document fallback is never
    reached. (Field-level correctness of what Yahoo actually resolves is
    exercised separately by the pure shareholding_field_merge tests above
    -- this fake isolates the ordering/skip-condition behavior this follow
    -up slice changes.)
    """

    def __init__(self):
        self.calls = 0

    async def acquire_requirement(self, profile, **kwargs):
        self.calls += 1
        return SimpleNamespace(acquisition_outcome="SUCCESS")


class _NoOpPersister:
    """Stand-in for YahooMcpResultPersister paired with _SucceedingGateway.

    The real persister expects a genuine YahooMcpNormalizedResult (identity
    checks, structured facts, etc.); _SucceedingGateway returns a bare
    SimpleNamespace, so the real persister would raise AttributeError. This
    fake just records that persist() was invoked.
    """

    def __init__(self):
        self.calls = 0

    async def persist(self, result, profile, **kwargs):
        self.calls += 1
        return 1


def _repository_with(*snapshots: ShareholdingSnapshot) -> ResearchRepository:
    import tempfile
    database = tempfile.mktemp(suffix=".db")
    settings = Settings(research_persistence_enabled=True, research_database_backend="sqlite",
                         research_database_name=database)
    repository = ResearchRepository(settings=settings)
    repository.profiles.append(_profile())
    for snapshot in snapshots:
        repository.shareholding_snapshots[snapshot.id] = snapshot
    return repository


# ---------------------------------------------------------------------------
# SLICE 2 FOLLOW-UP -- exact NSE structured / Yahoo / NSE document order.
#
# app.repository.ResearchRepository.refresh_targeted_categories(...,
# shareholding_phase=...) narrow phase selector, and the
# app.yahoo_mcp_acquisition.McpFirstResearchCapabilityExecutor.execute_primary
# India SHAREHOLDING pre-pass rewritten to call the repository directly in
# two phases (STRUCTURED_ONLY, then DOCUMENT_ONLY only if a genuine gap
# remains after Yahoo) instead of the fused legacy/CapabilityExecutor.
# ---------------------------------------------------------------------------


class _RecordingDiscovery:
    """Stand-in for OfficialNseShareholdingDiscovery / OfficialFilingDiscovery.

    Records how many times .discover(...) was called and returns an empty
    result list -- these repository-level phase-selector tests only care
    about *which* discovery object the phase selector allows to run, not
    about downstream parsing/persistence of whatever it returns (that is
    exercised by the pure shareholding_field_merge tests above and by the
    existing Slice 1/2 NSE discovery suites).
    """

    def __init__(self):
        self.calls = 0

    async def discover(self, *args, **kwargs):
        self.calls += 1
        return []


def _phase_repository():
    import tempfile
    database = tempfile.mktemp(suffix=".db")
    settings = Settings(
        research_live_enabled=True, research_persistence_enabled=True,
        research_database_backend="sqlite", research_database_name=database,
    )
    structured = _RecordingDiscovery()
    document = _RecordingDiscovery()
    repository = ResearchRepository(
        settings=settings,
        official_shareholding_discovery=structured,
        official_filing_discovery=document,
    )
    repository.profiles.append(_profile())
    return repository, structured, document


# 1. STRUCTURED_ONLY: dedicated NSE shareholding discovery executes;
#    official filing/PDF discovery does NOT execute.
@pytest.mark.asyncio
async def test_structured_only_runs_dedicated_feed_not_document_path() -> None:
    repository, structured, document = _phase_repository()

    await repository.refresh_targeted_categories(
        INSTRUMENT_ID, {"SHAREHOLDING_PATTERN"}, correlation_id="phase-1",
        allow_demo=False, shareholding_phase="STRUCTURED_ONLY",
    )

    assert structured.calls == 1
    assert document.calls == 0


# 2. DOCUMENT_ONLY: dedicated NSE shareholding discovery does NOT execute;
#    official SHAREHOLDING_PATTERN document path executes.
@pytest.mark.asyncio
async def test_document_only_runs_document_path_not_dedicated_feed() -> None:
    repository, structured, document = _phase_repository()

    await repository.refresh_targeted_categories(
        INSTRUMENT_ID, {"SHAREHOLDING_PATTERN"}, correlation_id="phase-2",
        allow_demo=False, shareholding_phase="DOCUMENT_ONLY",
    )

    assert structured.calls == 0
    assert document.calls == 1


# 3. Default/None: existing fused behavior remains unchanged -- both the
#    dedicated feed and the document path run, exactly as before this
#    phase selector existed.
@pytest.mark.asyncio
async def test_default_phase_runs_fused_dedicated_feed_and_document_path() -> None:
    repository, structured, document = _phase_repository()

    await repository.refresh_targeted_categories(
        INSTRUMENT_ID, {"SHAREHOLDING_PATTERN"}, correlation_id="phase-3", allow_demo=False,
    )

    assert structured.calls == 1
    assert document.calls == 1


# 4. FINANCIAL_RESULTS: behavior unchanged when the selector is omitted --
#    the dedicated NSE shareholding feed must never run for a
#    FINANCIAL_RESULTS-only refresh, whether the parameter is omitted or
#    explicitly passed as None.
@pytest.mark.asyncio
async def test_financial_results_unaffected_by_shareholding_phase_selector() -> None:
    repository, structured, document = _phase_repository()
    await repository.refresh_targeted_categories(
        INSTRUMENT_ID, {"FINANCIAL_RESULTS"}, correlation_id="phase-4a", allow_demo=False,
    )
    assert structured.calls == 0
    assert document.calls == 1

    repository2, structured2, document2 = _phase_repository()
    await repository2.refresh_targeted_categories(
        INSTRUMENT_ID, {"FINANCIAL_RESULTS"}, correlation_id="phase-4b", allow_demo=False,
        shareholding_phase=None,
    )
    assert structured2.calls == 0
    assert document2.calls == 1


class _PhaseScriptedRefresh:
    """Replaces ResearchRepository.refresh_targeted_categories on one
    repository instance for executor-level ordering tests.

    Records every shareholding_phase this was called with, in order, and
    per phase optionally persists a snapshot into the real repository
    and/or raises -- simulating "the dedicated NSE feed / document path
    ran and found X this call" without wiring the real NSE discovery
    stack. This is the seam the India SHAREHOLDING pre-pass in
    app.yahoo_mcp_acquisition now calls directly (it no longer calls the
    legacy/CapabilityExecutor double for SHAREHOLDING at all).
    """

    def __init__(self, repository, *, structured=None, document=None):
        self.repository = repository
        self.calls: list = []
        self._structured = structured or {}
        self._document = document or {}

    async def __call__(self, instrument_id, categories, *, correlation_id=None,
                        allow_demo=True, authority_upgrade_categories=None,
                        shareholding_phase=None):
        assert categories == {"SHAREHOLDING_PATTERN"}
        self.calls.append(shareholding_phase)
        config = self._structured if shareholding_phase == "STRUCTURED_ONLY" else self._document
        snapshot = config.get("snapshot")
        if snapshot is not None:
            self.repository.shareholding_snapshots[snapshot.id] = snapshot
        exc = config.get("raises")
        if exc is not None:
            raise exc


def _install_phase_refresh(repository, **kwargs) -> "_PhaseScriptedRefresh":
    fake = _PhaseScriptedRefresh(repository, **kwargs)
    repository.refresh_targeted_categories = fake
    return fake


# 6. NSE structured completes the requirement: Yahoo calls = 0, document
#    calls = 0 (and the structured phase is the only phase invoked).
@pytest.mark.asyncio
async def test_nse_structured_alone_satisfies_requirement_yahoo_and_document_never_called() -> None:
    repository = _repository_with()
    refresh = _install_phase_refresh(repository, structured={"snapshot": _nse_xbrl_snapshot()})
    gateway = _FailingGateway()
    legacy = _InjectingLegacy(repository)
    executor = McpFirstResearchCapabilityExecutor(legacy, repository, gateway, enabled=True)

    outcome = await executor.execute_primary(
        INSTRUMENT_ID, (_shareholding_target(),), jurisdiction="INDIA",
        correlation_id="phase-6", identity_headers={},
    )

    assert refresh.calls == ["STRUCTURED_ONLY"]
    assert gateway.calls == 0
    assert "SHAREHOLDING" in outcome.satisfied_requirement_ids
    assert outcome.failures == {}


# 7. NSE incomplete, Yahoo completes: Yahoo is called only after NSE
#    structured, and the document fallback is never reached.
@pytest.mark.asyncio
# 7. NSE structured incomplete and Yahoo is NOT a supported SHAREHOLDING
#    capability (hardcoded UNSUPPORTED): Yahoo is skipped entirely and the
#    NSE official-document phase IS still attempted as the last-authority
#    fallback -- it is never short-circuited by a provider that cannot
#    actually serve the requirement. The document phase completes the gap.
@pytest.mark.asyncio
async def test_yahoo_unsupported_means_document_phase_still_runs_and_closes_gap() -> None:
    repository = _repository_with()
    refresh = _install_phase_refresh(
        repository, document={"snapshot": _nse_xbrl_snapshot(source_identity="nse-doc-7")},
    )
    gateway = _FailingGateway()
    legacy = _InjectingLegacy(repository)
    executor = McpFirstResearchCapabilityExecutor(legacy, repository, gateway, enabled=True)

    outcome = await executor.execute_primary(
        INSTRUMENT_ID, (_shareholding_target(),), jurisdiction="INDIA",
        correlation_id="phase-7", identity_headers={},
    )

    assert refresh.calls == ["STRUCTURED_ONLY", "DOCUMENT_ONLY"]
    assert gateway.calls == 0, (
        "Yahoo must not be consulted for SHAREHOLDING while its capability is UNSUPPORTED"
    )
    assert "SHAREHOLDING" in outcome.satisfied_requirement_ids
    assert "SHAREHOLDING" not in outcome.failures


# 5 / 8. NSE incomplete, Yahoo UNSUPPORTED (verified: YahooMcpCapabilityRegistry
#    hardcodes SHAREHOLDING state=UNSUPPORTED and invoke() raises
#    EXTERNAL_CAPABILITY_UNSUPPORTED for it), NSE document completes the gap:
#    exact ordering NSE_STRUCTURED -> NSE_DOCUMENT is maintained, Yahoo is
#    NEVER consulted for SHAREHOLDING (path B: Yahoo does not genuinely
#    support the required ownership fields), the final requirement is
#    satisfied by the document phase, and no provider error leaks.
@pytest.mark.asyncio
async def test_exact_ordering_nse_structured_then_nse_document_closes_the_gap() -> None:
    repository = _repository_with()
    refresh = _install_phase_refresh(
        repository, document={"snapshot": _nse_xbrl_snapshot(source_identity="nse-doc-closing")},
    )
    gateway = _FailingGateway()  # Yahoo: EXTERNAL_CAPABILITY_UNSUPPORTED if ever called
    legacy = _InjectingLegacy(repository)
    executor = McpFirstResearchCapabilityExecutor(legacy, repository, gateway, enabled=True)

    outcome = await executor.execute_primary(
        INSTRUMENT_ID, (_shareholding_target(),), jurisdiction="INDIA",
        correlation_id="phase-8", identity_headers={},
    )

    assert refresh.calls == ["STRUCTURED_ONLY", "DOCUMENT_ONLY"]
    assert gateway.calls == 0, (
        "Yahoo MCP is not a supported SHAREHOLDING capability (hardcoded "
        "UNSUPPORTED); it must never be consulted for the SHAREHOLDING gap"
    )
    assert "SHAREHOLDING" in outcome.satisfied_requirement_ids
    assert "SHAREHOLDING" not in outcome.failures, (
        "no provider error -- intermediate or otherwise -- may leak once the "
        "NSE document phase satisfies the requirement"
    )


# 9. All sources incomplete: a genuine remaining gap stays unresolved. Under
#    path B, Yahoo is NOT consulted for SHAREHOLDING (no supported capability),
#    so the final failure is the aggregate plan-grounded verdict
#    EVIDENCE_INSUFFICIENT_WITHIN_PLAN (a PERMANENT_REASONS entry, i.e.
#    EVIDENCE_UNAVAILABLE -- genuine, not retried), NOT a bare intermediate
#    Yahoo provider error.
@pytest.mark.asyncio
async def test_all_sources_incomplete_genuine_gap_stays_reported_as_aggregate_insufficient() -> None:
    repository = _repository_with()
    refresh = _install_phase_refresh(repository)  # neither phase persists anything
    gateway = _FailingGateway(ExternalMcpAcquisitionError("EXTERNAL_CAPABILITY_UNSUPPORTED"))
    legacy = _InjectingLegacy(repository)
    executor = McpFirstResearchCapabilityExecutor(legacy, repository, gateway, enabled=True)

    outcome = await executor.execute_primary(
        INSTRUMENT_ID, (_shareholding_target(),), jurisdiction="INDIA",
        correlation_id="phase-9", identity_headers={},
    )

    assert refresh.calls == ["STRUCTURED_ONLY", "DOCUMENT_ONLY"]
    assert gateway.calls == 0, (
        "Yahoo MCP must never be consulted for SHAREHOLDING when the "
        "capability is unsupported"
    )
    assert outcome.failures.get("SHAREHOLDING") == "EVIDENCE_INSUFFICIENT_WITHIN_PLAN"
    assert "SHAREHOLDING" not in outcome.satisfied_requirement_ids


# 14a. non-INDIA jurisdictions are completely unaffected by this follow-up:
#      SHAREHOLDING is never pulled out of `targets`, so it still flows
#      through the ordinary Yahoo-then-legacy-fallback path exactly as
#      before this slice.
@pytest.mark.asyncio
async def test_non_india_shareholding_path_unchanged(caplog) -> None:
    repository = _repository_with()
    legacy = _InjectingLegacy(repository, snapshot=_nse_xbrl_snapshot(), satisfied=("SHAREHOLDING",))
    gateway = _FailingGateway()
    executor = McpFirstResearchCapabilityExecutor(legacy, repository, gateway, enabled=True)

    with caplog.at_level("INFO", logger="app.yahoo_mcp_acquisition"):
        outcome = await executor.execute_primary(
            INSTRUMENT_ID, (_shareholding_target(),), jurisdiction="USA",
            correlation_id="phase-14a", identity_headers={},
        )

    # gateway.calls may be 0 or 1 depending on whether the generic MCP-first
    # loop attempts SHAREHOLDING before falling back -- what must hold is
    # that the legacy/regional fallback still ran for it, unaffected by the
    # INDIA-only pre-pass added by this follow-up.
    assert len(legacy.calls) == 1
    assert legacy.calls[0][1] == (_shareholding_target(),)
    assert "SHAREHOLDING" in outcome.satisfied_requirement_ids
    # Issue 1 diagnostic: a non-INDIA jurisdiction must log the generic
    # (not NSE-first) route for this candidate.
    diag = next(r for r in caplog.records if r.message.startswith("shareholding_routing_decision"))
    assert "jurisdiction=USA" in diag.message
    assert "route=GENERIC_PROVIDER_LOOP" in diag.message
    # And the TRUE emission site must log the exact final reason the
    # external gateway returned for SHAREHOLDING at this jurisdiction --
    # not a routing guess, the actual safe_code from the raised error.
    final = next(r for r in caplog.records if r.message.startswith("shareholding_final_reason_emitted"))
    assert "requirement_id=SHAREHOLDING" in final.message
    assert "jurisdiction=USA" in final.message
    assert "finalReason=EXTERNAL_CAPABILITY_UNSUPPORTED" in final.message
    assert "selectedProvider=YAHOO_FINANCE_MCP" in final.message


@pytest.mark.asyncio
async def test_india_shareholding_routing_decision_is_logged_as_nse_first(caplog) -> None:
    # Issue 1 diagnostic: proves the exact SHAREHOLDING routing-decision log
    # fires with this candidate's real profile.country/profile.exchange and
    # the NSE_FIRST route whenever jurisdiction == "INDIA" -- the one
    # production fact this static-repo session cannot otherwise observe for
    # instrument 0dfbe654-9788-42ae-923c-b1ce0aceaa4b.
    repository = _repository_with()
    refresh = _install_phase_refresh(repository, structured={"snapshot": _nse_xbrl_snapshot()})
    gateway = _FailingGateway()
    legacy = _InjectingLegacy(repository)
    executor = McpFirstResearchCapabilityExecutor(legacy, repository, gateway, enabled=True)

    with caplog.at_level("INFO", logger="app.yahoo_mcp_acquisition"):
        outcome = await executor.execute_primary(
            INSTRUMENT_ID, (_shareholding_target(),), jurisdiction="INDIA",
            correlation_id="phase-diag", identity_headers={},
        )

    assert "SHAREHOLDING" in outcome.satisfied_requirement_ids
    diag = next(r for r in caplog.records if r.message.startswith("shareholding_routing_decision"))
    assert f"instrument_id={INSTRUMENT_ID}" in diag.message
    assert "country=IN" in diag.message
    assert "exchange=NSE" in diag.message
    assert "ticker=READY" in diag.message
    assert "jurisdiction=INDIA" in diag.message
    assert "nseSymbol=READY" in diag.message
    assert "route=NSE_FIRST" in diag.message
    # The dedicated NSE-first path never calls the external Yahoo gateway for
    # SHAREHOLDING, so the final-reason-emitted log (the TRUE unsupported
    # emission site) must never fire for this jurisdiction.
    assert not any(r.message.startswith("shareholding_final_reason_emitted") for r in caplog.records)


# 14b. MCP-disabled (enabled=False): the generic per-target Yahoo loop is
#      skipped entirely, but the India NSE structured/document phases are
#      NSE-direct (not MCP) and must still run exactly as when enabled.
@pytest.mark.asyncio
async def test_mcp_disabled_still_runs_nse_phases_but_never_calls_yahoo() -> None:
    repository = _repository_with()
    refresh = _install_phase_refresh(
        repository, document={"snapshot": _nse_xbrl_snapshot(source_identity="nse-doc-disabled")},
    )
    gateway = _FailingGateway()
    legacy = _InjectingLegacy(repository)
    executor = McpFirstResearchCapabilityExecutor(legacy, repository, gateway, enabled=False)

    outcome = await executor.execute_primary(
        INSTRUMENT_ID, (_shareholding_target(),), jurisdiction="INDIA",
        correlation_id="phase-14b", identity_headers={},
    )

    assert gateway.calls == 0, "Yahoo (MCP) must never be consulted while MCP-first acquisition is disabled"
    assert refresh.calls == ["STRUCTURED_ONLY", "DOCUMENT_ONLY"], (
        "the NSE structured/document phases are direct repository calls, not MCP, "
        "and must run regardless of the enabled flag, exactly as before this follow-up"
    )
    assert "SHAREHOLDING" in outcome.satisfied_requirement_ids


# ---------------------------------------------------------------------------
# PRIMARY GOAL (Objective #1) -- SHAREHOLDING final failure semantics.
#
# Yahoo MCP's capability registry hardcodes SHAREHOLDING state=UNSUPPORTED (no
# tool is registered for it), so Yahoo can never truthfully resolve a
# shareholding requirement. Routing SHAREHOLDING through the Yahoo MCP gateway
# therefore always fails with the intermediate provider error
# EXTERNAL_CAPABILITY_UNSUPPORTED, which previously leaked -- verbatim -- as
# the final SHAREHOLDING blocking reason. That is wrong aggregate semantics:
# the final outcome must be the aggregate, plan-grounded verdict
# EVIDENCE_INSUFFICIENT_WITHIN_PLAN (a PERMANENT reason -> EVIDENCE_UNAVAILABLE,
# genuine and non-retryable) once NSE structured + NSE official document
# sources have both been genuinely attempted and still left a real gap.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_shareholding_final_failure_is_aggregate_insufficient_not_yahoo_provider_error() -> None:
    repository = _repository_with()
    refresh = _install_phase_refresh(repository)  # neither NSE phase persists anything
    gateway = _FailingGateway()  # would raise EXTERNAL_CAPABILITY_UNSUPPORTED if called
    legacy = _InjectingLegacy(repository)
    executor = McpFirstResearchCapabilityExecutor(legacy, repository, gateway, enabled=True)

    outcome = await executor.execute_primary(
        INSTRUMENT_ID, (_shareholding_target(),), jurisdiction="INDIA",
        correlation_id="phase-1-path-b", identity_headers={},
    )

    # (a) Yahoo gateway.acquire_requirement is NOT called for SHAREHOLDING.
    assert gateway.calls == 0, (
        "Yahoo does not genuinely support SHAREHOLDING; it must not be invoked "
        "for the shareholding requirement"
    )
    # (b) The NSE DOCUMENT phase still ran as the last-authority fallback.
    assert refresh.calls == ["STRUCTURED_ONLY", "DOCUMENT_ONLY"]
    # (c) The final SHAREHOLDING failure is the aggregate verdict, not the
    #     bare intermediate Yahoo provider error.
    failures = outcome.failures
    assert failures.get("SHAREHOLDING") == "EVIDENCE_INSUFFICIENT_WITHIN_PLAN"
    assert failures.get("SHAREHOLDING") != "EXTERNAL_CAPABILITY_UNSUPPORTED"
    # (d) The aggregate verdict classifies as genuine evidence-absence
    #     (EVIDENCE_UNAVAILABLE), not a technical/retryable transient fault.
    from app.failure_taxonomy import classify_requirement_failures, EVIDENCE_UNAVAILABLE
    assert classify_requirement_failures(
        {"SHAREHOLDING": failures["SHAREHOLDING"]}, ("SHAREHOLDING",)
    ) == EVIDENCE_UNAVAILABLE
    assert "SHAREHOLDING" not in outcome.satisfied_requirement_ids

