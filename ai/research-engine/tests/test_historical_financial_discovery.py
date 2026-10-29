"""End-to-end offline tests for historical financial acquisition: FY2024 annual
and missing historical quarterly periods reach source discovery and persistence
through the BOUNDED Equity Radar acquisition workflow.

No live providers, no database mutations outside the in-memory test repo,
no Radar execution, no deployment, no Helm changes.
"""
import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.fact_precedence import FactSourceTier, FinancialFact, FinancialFactKey
from app.financial_authority import (annual_income_history_gaps,
                                     quarterly_income_history_gaps)
from app.financial_gap_fill import load_financial_gap_state
from app.models import ProvenancedValue, SourceMode
from app.research_readiness import ResearchReadinessService, ResearchRequirementStatus
from app.research_readiness_runtime import (ExistingResearchCapabilityExecutor,
                                            RepositoryResearchReadinessAdapter)
from app.structured_financial import NseOfficialFinancialProvider
from test_di15_financial_authority_upgrade import _repo
from test_research_readiness_runtime import _target

FIXTURES = Path(__file__).parent / 'fixtures' / 'nse_financial_xbrl'
ROWS = json.loads((FIXTURES / 'filings.json').read_text())
NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)

# Pre-load the two XML fixtures so we can generate period-specific variants
_QUARTERLY_XML = (FIXTURES / 'ghcl_20260630.xml').read_bytes()
_ANNUAL_XML = (FIXTURES / 'ghcl_20260331.xml').read_bytes()


def _make_historical_xml(base_xml: bytes, new_start: str, new_end: str) -> bytes:
    """Replace context duration dates and reporting-period declaration nodes
    in the XML to simulate a filing for a different fiscal period."""
    xml = base_xml
    xml = re.sub(rb'(<xbrli:startDate>)[^<]+',
                 rb'\g<1>' + new_start.encode(), xml, count=1)
    xml = re.sub(rb'(<xbrli:endDate>)[^<]+',
                 rb'\g<1>' + new_end.encode(), xml, count=1)
    xml = re.sub(rb'(DateOfStartOfReportingPeriod[^>]*>)[^<]+',
                 rb'\g<1>' + new_start.encode(), xml, count=1)
    xml = re.sub(rb'(DateOfEndOfReportingPeriod[^>]*>)[^<]+',
                 rb'\g<1>' + new_end.encode(), xml, count=1)
    return xml


def _historical_row(symbol: str, scrip: str, qe_date: str, broadcast: str,
                    isin='INE539A01019', consolidated='Standalone', xbrl_url=None):
    """Build an NSE API filing row for a historical period."""
    return {
        'symbol': symbol, 'scripCode': scrip, 'isin': isin,
        'broadcast_Date': broadcast, 'consolidated': consolidated,
        'type_Sub': 'Original', 'revised_Date': None, 'category': 'Annual',
        'type': 'Integrated Filing- Financials',
        'qe_Date': qe_date,
        'xbrl': xbrl_url or f'https://nsearchives.nseindia.com/corporate/xbrl/HIST_{qe_date}.xml',
    }


@pytest.fixture(autouse=True)
def fixed_readiness_clock(monkeypatch):
    # Captured filing periods stay real; replay readiness at the capture date.
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)
    monkeypatch.setattr('app.research_readiness.datetime', FixedDatetime)
    monkeypatch.setattr('app.research_readiness_runtime.datetime', FixedDatetime)


def setup_with_history(page_rows: list):
    """Like setup() but allows custom NSE API page responses and XML content.

    ``page_rows`` is a list of lists: each inner list is the rows returned
    for NSE API page N (page index 0 = page 1).  Returns (repo, profile,
    provider, executor, xml_content, call_log).
    """
    repo, profile = _repo()
    profile = profile.model_copy(update={
        'isin': 'INE539A01019',
        'provider_instrument_ids': {'NSE': 'GHCL'},
    })
    repo.profiles = [profile]

    call_log = {'pages': [], 'xml_urls': []}
    async def get(url, params=None, **kwargs):
        page = (params or {}).get('page', 1)
        idx = page - 1
        if idx < len(page_rows) and page_rows[idx]:
            data = page_rows[idx]
        else:
            data = []
        call_log['pages'].append((page, dict(params or {})))
        return httpx.Response(200, json={'data': data},
                              request=httpx.Request('GET', url))
    client = SimpleNamespace(get=get)

    xml_content = {}
    async def fetch(url, **kwargs):
        call_log['xml_urls'].append(url)
        content = xml_content.get(url, b'')
        return SimpleNamespace(final_url=url, status_code=200,
                               content_type='application/xml', content=content)
    fetcher = SimpleNamespace(settings=repo.settings, fetch_network=AsyncMock(side_effect=fetch))
    provider = NseOfficialFinancialProvider(client, fetcher, clock=lambda: NOW)
    repo.refresh_targeted_categories = AsyncMock()
    executor = ExistingResearchCapabilityExecutor(
        repo, SimpleNamespace(), None, official_financial_provider=provider)
    return repo, profile, provider, executor, xml_content, call_log


# ---------------------------------------------------------------------------
# XML fixtures for historical periods
# ---------------------------------------------------------------------------

# Annual XML for FY2024: duration 2023-04-01 to 2024-03-31
ANNUAL_2024_XML = _make_historical_xml(_ANNUAL_XML, '2023-04-01', '2024-03-31')
# Annual XML for FY2025: duration 2024-04-01 to 2025-03-31
ANNUAL_2025_XML = _make_historical_xml(_ANNUAL_XML, '2024-04-01', '2025-03-31')
# Quarterly XML for Q1 FY2026 (Jun): duration 2026-04-01 to 2026-06-30
QUARTERLY_JUN2026_XML = _make_historical_xml(_QUARTERLY_XML, '2026-04-01', '2026-06-30')
# Quarterly XML for Q4 FY2026 (Mar): duration 2026-01-01 to 2026-03-31
QUARTERLY_MAR2026_XML = _make_historical_xml(_QUARTERLY_XML, '2026-01-01', '2026-03-31')
# Quarterly XML for Q3 FY2026 (Dec): duration 2025-10-01 to 2025-12-31
QUARTERLY_DEC2025_XML = _make_historical_xml(_QUARTERLY_XML, '2025-10-01', '2025-12-31')
# Quarterly XML for Q2 FY2026 (Sep): duration 2025-07-01 to 2025-09-30
QUARTERLY_SEP2025_XML = _make_historical_xml(_QUARTERLY_XML, '2025-07-01', '2025-09-30')

# NSE filing rows for historical periods (with proper broadcast_Date format)
ROW_ANNUAL_2024 = _historical_row('GHCL', '500171', '31-Mar-2024', '15-May-2024 00:00:00')
ROW_ANNUAL_2025 = _historical_row('GHCL', '500171', '31-Mar-2025', '01-Jun-2025 00:00:00')
ROW_QUARTERLY_JUN2026 = _historical_row('GHCL', '500171', '30-Jun-2026', '01-Aug-2026 16:04:06')
ROW_QUARTERLY_MAR2026 = _historical_row('GHCL', '500171', '31-Mar-2026', '05-May-2026 18:43:19')
ROW_QUARTERLY_DEC2025 = _historical_row('GHCL', '500171', '31-Dec-2025', '01-Jan-2026 00:00:00')
ROW_QUARTERLY_SEP2025 = _historical_row('GHCL', '500171', '30-Sep-2025', '01-Nov-2025 00:00:00')


def _seed_facts(repo, instrument_id, annual_years, quarter_periods):
    """Seed pre-existing accepted facts into the repository to simulate a
    prior acquisition cycle. Only seeds revenue and pat (the metrics that
    matter for readiness).  Uses STANDALONE to match the GHCL XML fixture's
    ``consolidated=Standalone`` declaration."""
    facts = []
    for year in annual_years:
        period = f'{year}-03-31'
        for metric in ('revenue', 'pat'):
            facts.append(FinancialFact(
                FinancialFactKey(instrument_id, metric, period, 'ANNUAL', 'STANDALONE'),
                ProvenancedValue(value=Decimal('9999999'), unit='INR',
                                 source_url=f'https://nsearchives.nseindia.com/seed-{period}.xml',
                                 source_name='NSE', retrieved_at=NOW,
                                 as_of_date=datetime.fromisoformat(period).replace(tzinfo=timezone.utc)),
                FactSourceTier.OFFICIAL_NSE, 'NSE', f'seed-{period}', SourceMode.REAL))
    for period in quarter_periods:
        for metric in ('revenue', 'pat'):
            facts.append(FinancialFact(
                FinancialFactKey(instrument_id, metric, period, 'QUARTERLY', 'STANDALONE'),
                ProvenancedValue(value=Decimal('8888888'), unit='INR',
                                 source_url=f'https://nsearchives.nseindia.com/seed-q-{period}.xml',
                                 source_name='NSE', retrieved_at=NOW,
                                 as_of_date=datetime.fromisoformat(period).replace(tzinfo=timezone.utc)),
                FactSourceTier.OFFICIAL_NSE, 'NSE', f'seed-q-{period}', SourceMode.REAL))
    repo.persist_international_financial_facts(facts)
    return facts


# ---------------------------------------------------------------------------
# Test 1: FY2024 missing → historical FY2024 filing discovery → facts persisted
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_historical_fy2024_annual_discovery_and_persistence():
    """Missing FY2024 annual fact → acquire_official_financials computes target
    → provider paginates to historical page → FY2024 filing discovered → facts
    persisted through normal contracts."""
    # NSE API returns latest filings on page 1, historical FY2024 on page 2
    page1_rows = [ROW_QUARTERLY_JUN2026, ROW_QUARTERLY_MAR2026,
                  ROW_QUARTERLY_DEC2025, ROW_QUARTERLY_SEP2025,
                  ROW_ANNUAL_2025]
    page2_rows = [ROW_ANNUAL_2024]
    repo, profile, provider, executor, xml_content, call_log = setup_with_history(
        [page1_rows, page2_rows, [], [], []])

    # Seed facts: have FY2025, FY2026 annual + all 4 quarters, missing FY2024
    _seed_facts(repo, profile.instrument_id,
                annual_years=[2025, 2026],
                quarter_periods=['2026-06-30', '2026-03-31', '2025-12-31', '2025-09-30'])

    # Wire XML responses for filings that may be fetched
    xml_content[ROW_ANNUAL_2024['xbrl']] = ANNUAL_2024_XML
    xml_content[ROW_QUARTERLY_JUN2026['xbrl']] = QUARTERLY_JUN2026_XML
    xml_content[ROW_QUARTERLY_MAR2026['xbrl']] = QUARTERLY_MAR2026_XML
    xml_content[ROW_QUARTERLY_DEC2025['xbrl']] = QUARTERLY_DEC2025_XML
    xml_content[ROW_QUARTERLY_SEP2025['xbrl']] = QUARTERLY_SEP2025_XML
    xml_content[ROW_ANNUAL_2025['xbrl']] = ANNUAL_2025_XML

    # Run execute_primary with financial requirements
    outcome = await executor.execute_primary(
        profile.instrument_id,
        [_target('GROWTH_FACTS'), _target('QUARTERLY_FINANCIALS')],
        jurisdiction='INDIA', correlation_id=None, identity_headers=None)

    assert not outcome.failures, f"Failures: {outcome.failures}"

    # Verify FY2024 annual fact was persisted through the normal contract
    facts = repo.financial_facts_for(profile.instrument_id)
    fy2024_revenue = next((f for f in facts
                           if f.key.metric == 'revenue'
                           and f.key.period_end == '2024-03-31'
                           and f.key.period_type == 'ANNUAL'), None)
    assert fy2024_revenue is not None, "FY2024 annual revenue fact was not persisted"
    assert fy2024_revenue.value.value == Decimal('7905100000')
    assert fy2024_revenue.source_provider == 'NSE'
    assert fy2024_revenue.source_tier == FactSourceTier.OFFICIAL_NSE
    assert fy2024_revenue.key.reporting_basis == 'STANDALONE'

    # Verify the gap state no longer reports FY2024 as missing
    state = load_financial_gap_state(repo, profile.instrument_id)
    assert not any(k.period_end == '2024-03-31' for k in state.missing_annual_fact_keys)


# ---------------------------------------------------------------------------
# Test 2: Missing older quarter → historical filing discovery → facts persisted
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_older_quarter_historical_discovery_and_persistence():
    """With only 2 of 4 required quarters present, the 2 missing older quarters
    are discovered via historical pagination and facts persisted."""
    page1_rows = [ROW_QUARTERLY_JUN2026, ROW_QUARTERLY_MAR2026,
                  ROW_ANNUAL_2025]
    page2_rows = [ROW_QUARTERLY_DEC2025, ROW_QUARTERLY_SEP2025]
    page3_rows = [ROW_ANNUAL_2024]
    repo, profile, provider, executor, xml_content, call_log = setup_with_history(
        [page1_rows, page2_rows, page3_rows, [], []])

    # Seed: 3 annual + only 2 quarters (Jun2026, Mar2026)
    _seed_facts(repo, profile.instrument_id,
                annual_years=[2024, 2025, 2026],
                quarter_periods=['2026-06-30', '2026-03-31'])

    xml_content[ROW_QUARTERLY_DEC2025['xbrl']] = QUARTERLY_DEC2025_XML
    xml_content[ROW_QUARTERLY_SEP2025['xbrl']] = QUARTERLY_SEP2025_XML

    outcome = await executor.execute_primary(
        profile.instrument_id,
        [_target('QUARTERLY_FINANCIALS')],
        jurisdiction='INDIA', correlation_id=None, identity_headers=None)

    assert not outcome.failures, f"Failures: {outcome.failures}"

    facts = repo.financial_facts_for(profile.instrument_id)
    # Verify the missing historical quarters were discovered and persisted
    for period in ('2025-12-31', '2025-09-30'):
        rev = next((f for f in facts
                    if f.key.metric == 'revenue'
                    and f.key.period_end == period
                    and f.key.period_type == 'QUARTERLY'), None)
        assert rev is not None, f"Quarterly revenue for {period} was not persisted"
        assert rev.value.value == Decimal('7742600000')

    # Verify all 4 quarters now present
    state = load_financial_gap_state(repo, profile.instrument_id)
    assert state.missing_quarterly_fact_keys == frozenset()


# ---------------------------------------------------------------------------
# Test 3: 2 of 4 quarters → PARTIAL and acquisition eligible
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_two_of_four_quarters_is_partial_and_acquisition_eligible():
    """With 2 of 4 required quarters, readiness is PARTIAL and acquisition is
    eligible — the provider is invoked with historical targets."""
    page1_rows = [ROW_QUARTERLY_JUN2026, ROW_QUARTERLY_MAR2026,
                  ROW_QUARTERLY_DEC2025, ROW_QUARTERLY_SEP2025,
                  ROW_ANNUAL_2025]
    repo, profile, provider, executor, xml_content, call_log = setup_with_history(
        [page1_rows, []])

    # Seed: 3 annual + 2 quarters only → quarterly coverage incomplete
    _seed_facts(repo, profile.instrument_id,
                annual_years=[2024, 2025, 2026],
                quarter_periods=['2026-06-30', '2026-03-31'])

    xml_content[ROW_QUARTERLY_JUN2026['xbrl']] = QUARTERLY_JUN2026_XML
    xml_content[ROW_QUARTERLY_MAR2026['xbrl']] = QUARTERLY_MAR2026_XML
    xml_content[ROW_QUARTERLY_DEC2025['xbrl']] = QUARTERLY_DEC2025_XML
    xml_content[ROW_QUARTERLY_SEP2025['xbrl']] = QUARTERLY_SEP2025_XML

    # Verify readiness BEFORE acquisition — should be PARTIAL
    readiness_before = ResearchReadinessService(
        RepositoryResearchReadinessAdapter(repo)).assess(
            profile.instrument_id, jurisdiction='INDIA', now=NOW)
    qstatus = readiness_before.for_requirement('QUARTERLY_FINANCIALS').status
    assert qstatus in ('PARTIAL', 'MISSING'), f"Expected PARTIAL/MISSING, got {qstatus}"

    outcome = await executor.execute_primary(
        profile.instrument_id,
        [_target('QUARTERLY_FINANCIALS')],
        jurisdiction='INDIA', correlation_id=None, identity_headers=None)
    assert not outcome.failures

    # Verify the provider was called (acquisition attempted)
    assert call_log['pages'], "Provider was not invoked for missing quarters"

    # Verify historical quarters were discovered and persisted
    facts = repo.financial_facts_for(profile.instrument_id)
    for period in ('2025-12-31', '2025-09-30'):
        rev = next((f for f in facts
                    if f.key.metric == 'revenue'
                    and f.key.period_end == period
                    and f.key.period_type == 'QUARTERLY'), None)
        assert rev is not None, f"Quarter {period} not persisted"

    # Verify readiness improved to READY (facts persisted and sufficient)
    readiness_after = ResearchReadinessService(
        RepositoryResearchReadinessAdapter(repo)).assess(
            profile.instrument_id, jurisdiction='INDIA', now=NOW)
    assert readiness_after.for_requirement('QUARTERLY_FINANCIALS').status in ('READY_FRESH', 'READY_STALE')


# ---------------------------------------------------------------------------
# Test 4: 4 of 4 quarters → coverage complete, no repeat fetch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_four_of_four_quarters_complete_no_repeat_fetch():
    """With all 4 quarters + 3 annual facts already present, no historical
    acquisition is attempted — coverage is complete and no fetch occurs."""
    repo, profile, provider, executor, xml_content, call_log = setup_with_history([[]])

    _seed_facts(repo, profile.instrument_id,
                annual_years=[2024, 2025, 2026],
                quarter_periods=['2026-06-30', '2026-03-31', '2025-12-31', '2025-09-30'])

    # Verify gap state has no missing annual or quarterly fact keys
    state = load_financial_gap_state(repo, profile.instrument_id)
    assert state.missing_annual_fact_keys == frozenset()
    assert state.missing_quarterly_fact_keys == frozenset()

    outcome = await executor.execute_primary(
        profile.instrument_id,
        [_target('QUARTERLY_FINANCIALS'), _target('GROWTH_FACTS')],
        jurisdiction='INDIA', correlation_id=None, identity_headers=None)
    assert not outcome.failures

    # Verify only page 1 was fetched (no historical pagination)
    pages = [p for p, _ in call_log['pages']]
    assert pages == [1], "Only page 1 should be fetched when coverage is complete (no historical pagination)"


# ---------------------------------------------------------------------------
# Test 5: Official provider unable to retrieve historical source → bounded backoff
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_provider_historical_retrieval_failure_bounded_backoff():
    """When the NSE API does not return historical filings for missing periods
    within the bounded page budget, the gap is recorded and no facts are
    fabricated. Limited quarterly readiness does not certify TTM/YoY history."""
    # Page 1: latest filings. Pages 2-5: empty (historical not available)
    page1_rows = [ROW_QUARTERLY_JUN2026, ROW_QUARTERLY_MAR2026,
                  ROW_ANNUAL_2025]
    repo, profile, provider, executor, xml_content, call_log = setup_with_history(
        [page1_rows, [], [], [], []])

    # Seed: 3 annual + 2 quarters → missing 2 older quarters
    _seed_facts(repo, profile.instrument_id,
                annual_years=[2024, 2025, 2026],
                quarter_periods=['2026-06-30', '2026-03-31'])

    xml_content[ROW_QUARTERLY_JUN2026['xbrl']] = QUARTERLY_JUN2026_XML
    xml_content[ROW_QUARTERLY_MAR2026['xbrl']] = QUARTERLY_MAR2026_XML
    xml_content[ROW_ANNUAL_2025['xbrl']] = ANNUAL_2025_XML

    outcome = await executor.execute_primary(
        profile.instrument_id,
        [_target('QUARTERLY_FINANCIALS')],
        jurisdiction='INDIA', correlation_id=None, identity_headers=None)

    assert not outcome.failures

    # Verify the provider paginated through the bounded page budget
    pages_requested = [p for p, _ in call_log['pages']]
    assert len(pages_requested) <= 5, "Should not exceed bounded page budget"
    assert 2 in pages_requested, "Should have paginated to page 2 for historical discovery"

    # Verify no fabricated facts for missing periods
    facts = repo.financial_facts_for(profile.instrument_id)
    for period in ('2025-12-31', '2025-09-30'):
        new_facts = [f for f in facts
                     if f.key.period_end == period
                     and f.key.reporting_basis == 'STANDALONE'
                     and f.source_provider == 'NSE']
        assert len(new_facts) == 0, f"Facts for {period} should not be fabricated"

    # Two quarters support the existing limited assessment. They cannot close
    # the separate TTM/YoY plan, even after fresh EPS arrives from the latest XML.
    readiness = ResearchReadinessService(
        RepositoryResearchReadinessAdapter(repo)).assess(
            profile.instrument_id, jurisdiction='INDIA', now=NOW)
    qstatus = readiness.for_requirement('QUARTERLY_FINANCIALS').status
    assert qstatus == ResearchRequirementStatus.READY_FRESH
    assert {key.period_end for key in quarterly_income_history_gaps(facts)} == {
        '2025-12-31', '2025-09-30'}
    from app.financial_evidence_plan import build_financial_evidence_plan
    plan = build_financial_evidence_plan(profile, facts, {
        'QUARTERLY_FINANCIALS': {'COMPARABLE_QUARTERS'}}, as_of=NOW)
    assert {need.period_end for need in plan.missing(facts)} == {
        '2025-12-31', '2025-09-30', '2025-06-30'}
    receipt = repo._latest_acquisition_observation(profile.instrument_id, "FINANCIAL_PLAN", "NSE")
    assert receipt["outcome"] == "COMPLETED" and receipt["evidence_count"] > 0
    assert receipt["failure_reason"] == "NO_ELIGIBLE_FILINGS"


@pytest.mark.asyncio
@pytest.mark.parametrize("planned", [False, True])
@pytest.mark.parametrize("invalid_xml", [b"", b"<broken", b"<not-a-financial-statement/>"])
async def test_invalid_xml_preserves_other_document_facts(planned, invalid_xml):
    repo, profile, provider, executor, xml_content, call_log = setup_with_history([
        [ROW_QUARTERLY_DEC2025, ROW_QUARTERLY_SEP2025], []])
    _seed_facts(repo, profile.instrument_id, annual_years=[2024, 2025, 2026],
                quarter_periods=['2026-06-30', '2026-03-31'])
    xml_content[ROW_QUARTERLY_DEC2025['xbrl']] = QUARTERLY_DEC2025_XML
    xml_content[ROW_QUARTERLY_SEP2025['xbrl']] = invalid_xml

    if planned:
        await executor.acquire_official_financials(profile.instrument_id, {'QUARTERLY_FINANCIALS'})
    else:
        repo.persist_international_financial_facts(await provider.collect(profile))

    facts = repo.financial_facts_for(profile.instrument_id)
    assert any(f.key.metric == 'revenue' and f.key.period_end == '2025-12-31'
               and f.value.value == Decimal('7742600000') for f in facts)
    assert not any(f.key.period_end == '2025-09-30' for f in facts)
    assert len(call_log['xml_urls']) == 2
    assert set(call_log['xml_urls']) == {
        ROW_QUARTERLY_DEC2025['xbrl'], ROW_QUARTERLY_SEP2025['xbrl']}
    assert len(call_log['pages']) <= 5
    if planned:
        await executor.acquire_official_financials(profile.instrument_id, {'QUARTERLY_FINANCIALS'})
        assert len(call_log['xml_urls']) == 2  # Durable per-document receipts.
