from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
from uuid import UUID, uuid4

import pytest
import httpx

from app.models import (
    CompanyResearchProfile, DocumentStatus, DocumentType, ReliabilityLevel, ResearchDocument,
    ShareholdingCategory, ShareholdingSnapshot, ShareholdingSnapshotValue, SourceClassification,
    SourceMode, SourceType,
)
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository, _fair_official_filing_order
from app.research_fetching import FetchError, FetchResult, HttpResearchFetcher
from app.source_discovery import DiscoveryResult
from app.source_registry import RegisteredResearchSource
from app.settings import Settings
from app.shareholding import parse_nse_shareholding_xbrl, parse_official_shareholding
from app.source_discovery import OfficialFilingDiscovery, OfficialNseShareholdingDiscovery, SearchProviderError, _is_shareholding_announcement
from app.portfolio_orchestration import _global_master_instrument


def _snapshot(instrument_id, source="nse-filing-1"):
    return ShareholdingSnapshot(instrument_id=instrument_id, period_end=datetime(2026, 6, 30, tzinfo=timezone.utc),
        source_provider="NSE", source_type="EXCHANGE_ANNOUNCEMENT", source_identity_key=source,
        source_url="https://www.nseindia.com/files/shareholding.pdf", confidence=Decimal("0.90"),
        reliability_level=ReliabilityLevel.LEVEL_A, source_mode=SourceMode.REAL,
        values=[ShareholdingSnapshotValue(category=ShareholdingCategory.PROMOTER, percentage=Decimal("42.5"), raw_source_label="Promoter")])


class _EmptyOfficialFilingDiscovery:
    async def discover(self, *_args):
        return []


def _nse_xbrl_fixture() -> str:
    contexts = {
        "promoter": "ShareholdingOfPromoterAndPromoterGroupMember",
        "dii": "InstitutionsDomesticMember",
        "mf": "MutualFundsOrUTIMember",
        "insurance": "InsuranceCompaniesMember",
        "government": "GovernmentsMember",
        "retail": "ResidentIndividualShareholdersHoldingNominalShareCapitalUpToRsTwoLakhMember",
        "others": "OtherNonInstitutionsMember",
        "fpi1": "InstitutionsForeignPortfolioInvestorCategoryOneMember",
        "fpi2": "InstitutionsForeignPortfolioInvestorCategoryTwoMember",
        "public": "PublicShareholdingMember",
    }
    context_xml = "".join(
        f'<xbrli:context id="{key}"><xbrli:scenario><xbrldi:explicitMember>{member}</xbrldi:explicitMember></xbrli:scenario></xbrli:context>'
        for key, member in contexts.items()
    )
    ratios = {
        "promoter": "0.494", "dii": "0.0068", "mf": "0.0014", "insurance": "0.003",
        "government": "0", "retail": "0.2476", "others": "0.0168", "fpi1": "0.0789",
        "fpi2": "0.0027", "public": "0.506",
    }
    fact_xml = "".join(
        f'<n:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="{key}">{value}</n:ShareholdingAsAPercentageOfTotalNumberOfShares>'
        for key, value in ratios.items()
    )
    fact_xml += '<n:EncumberedShareUnderPledgedAsPercentageOfTotalNumberOfShares contextRef="promoter">0.4474</n:EncumberedShareUnderPledgedAsPercentageOfTotalNumberOfShares>'
    return f'<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi" xmlns:n="urn:test">{context_xml}{fact_xml}</xbrli:xbrl>'


def _legacy_nse_xbrl_snapshot(instrument_id, source: str, period: tuple[int, int, int]) -> ShareholdingSnapshot:
    snapshot = _snapshot(instrument_id, source)
    return snapshot.model_copy(update={
        "period_end": datetime(*period, tzinfo=timezone.utc),
        "source_type": "NSE_SHAREHOLDING_XBRL",
        "source_identity_key": f"NSE_SHAREHOLDING:{source}",
        "source_url": f"https://nsearchives.nseindia.com/corporate/xbrl/SHP_{source}.xml",
    })


@pytest.mark.asyncio
async def test_nse_xbrl_fetch_uses_scoped_xml_headers_without_leaking_to_generic_fetches() -> None:
    settings = Settings(research_max_retries=0)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith(".xml"):
            return httpx.Response(200, content=_nse_xbrl_fixture().encode(), headers={"content-type": "application/xml"}, request=request)
        return httpx.Response(200, text="ordinary page", headers={"content-type": "text/plain"}, request=request)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"User-Agent": settings.research_user_agent, "Accept-Encoding": "gzip, deflate, br"},
    )
    fetcher = HttpResearchFetcher(settings, client=client)
    try:
        xml = await fetcher.fetch_nse_shareholding_xbrl("https://nsearchives.nseindia.com/corporate/xbrl/fixture.xml")
        ordinary = await fetcher.fetch("https://example.com/ordinary.txt")
    finally:
        await client.aclose()

    assert xml.content_type == "application/xml"
    assert ordinary.content_type == "text/plain"
    nse_headers, generic_headers = requests[0].headers, requests[1].headers
    assert nse_headers["referer"] == "https://www.nseindia.com/"
    assert nse_headers["accept"] == "application/xml,text/xml,*/*"
    assert nse_headers["user-agent"].startswith("Mozilla/5.0")
    assert generic_headers.get("referer") is None
    assert generic_headers["user-agent"] == settings.research_user_agent


def test_nse_xbrl_normalizes_only_explicit_category_facts_with_provenance() -> None:
    values = {value.category: value for value in parse_nse_shareholding_xbrl(_nse_xbrl_fixture())}

    assert values[ShareholdingCategory.PROMOTER].percentage == Decimal("49.400")
    assert values[ShareholdingCategory.DII].percentage == Decimal("0.6800")
    assert values[ShareholdingCategory.MUTUAL_FUNDS].percentage == Decimal("0.1400")
    assert values[ShareholdingCategory.INSURANCE].percentage == Decimal("0.300")
    assert values[ShareholdingCategory.GOVERNMENT].percentage == Decimal("0")
    assert values[ShareholdingCategory.PUBLIC_RETAIL].percentage == Decimal("24.7600")
    assert values[ShareholdingCategory.OTHERS].percentage == Decimal("1.6800")
    assert values[ShareholdingCategory.FII_FPI].percentage == Decimal("8.1600")
    assert values[ShareholdingCategory.FII_FPI].metric_basis == "DERIVED_SUM_OF_MUTUALLY_EXCLUSIVE_FPI_CATEGORIES"
    assert values[ShareholdingCategory.PROMOTER_PLEDGE].percentage == Decimal("44.7400")
    assert values[ShareholdingCategory.PROMOTER_PLEDGE].metric_basis == "PERCENT_OF_PROMOTER_HOLDING"
    assert values[ShareholdingCategory.PUBLIC_RETAIL].raw_source_label != "Public Shareholding"
    assert all(value.source_locator and value.source_locator.startswith("nse-xbrl:") for value in values.values())
    assert all(value.evidence_text for value in values.values())


def test_nse_xbrl_does_not_create_categories_when_explicit_facts_are_absent() -> None:
    xml = _nse_xbrl_fixture().replace(
        '<n:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="retail">0.2476</n:ShareholdingAsAPercentageOfTotalNumberOfShares>',
        "",
    ).replace(
        '<n:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="fpi2">0.0027</n:ShareholdingAsAPercentageOfTotalNumberOfShares>',
        "",
    ).replace(
        '<n:EncumberedShareUnderPledgedAsPercentageOfTotalNumberOfShares contextRef="promoter">0.4474</n:EncumberedShareUnderPledgedAsPercentageOfTotalNumberOfShares>',
        "",
    )
    categories = {value.category for value in parse_nse_shareholding_xbrl(xml)}

    assert ShareholdingCategory.PUBLIC_RETAIL not in categories
    assert ShareholdingCategory.FII_FPI not in categories
    assert ShareholdingCategory.PROMOTER_PLEDGE not in categories
    assert ShareholdingCategory.PROMOTER in categories


def test_global_snapshot_persistence_deduplicates_source_and_values() -> None:
    persistence = SqliteResearchPersistence()
    instrument_id = uuid4()
    assert persistence.upsert_shareholding_snapshot(_snapshot(instrument_id)) is True
    assert persistence.upsert_shareholding_snapshot(_snapshot(instrument_id)) is False
    loaded = persistence.load_shareholding_snapshots()
    assert len(loaded) == 1
    assert loaded[0].instrument_id == instrument_id
    assert [(item.category, item.percentage) for item in loaded[0].values] == [(ShareholdingCategory.PROMOTER, Decimal("42.5"))]


def test_existing_official_snapshot_accepts_new_xbrl_category_values_once() -> None:
    persistence = SqliteResearchPersistence()
    instrument_id = uuid4()
    snapshot = _snapshot(instrument_id, "NSE_SHAREHOLDING:12345")
    snapshot.source_type = "NSE_SHAREHOLDING_XBRL"
    assert persistence.upsert_shareholding_snapshot(snapshot) is True

    enriched = snapshot.model_copy(update={"values": parse_nse_shareholding_xbrl(_nse_xbrl_fixture())})
    assert persistence.upsert_shareholding_snapshot(enriched) is True
    assert persistence.upsert_shareholding_snapshot(enriched) is False

    loaded = persistence.load_shareholding_snapshots()
    assert len(loaded) == 1
    values = {value.category: value for value in loaded[0].values}
    assert values[ShareholdingCategory.PROMOTER].percentage == Decimal("42.5")
    assert values[ShareholdingCategory.FII_FPI].percentage == Decimal("8.1600")
    assert values[ShareholdingCategory.PUBLIC_RETAIL].percentage == Decimal("24.7600")


def test_shareholding_persistence_accepts_native_postgres_uuid_rows_and_existing_id() -> None:
    snapshot_id, instrument_id, value_id, existing_id = uuid4(), uuid4(), uuid4(), uuid4()
    database_now = datetime(2026, 7, 20, 12, 0, 0)

    class Cursor:
        def __init__(self, *, rows=None, row=None) -> None:
            self._rows = rows or []
            self._row = row
        def fetchall(self):
            return self._rows
        def fetchone(self):
            return self._row

    class NativeUuidConnection:
        def execute(self, sql, _params=None):
            if "global_shareholding_snapshot_values" in sql:
                return Cursor(rows=[{
                    "id": value_id, "snapshot_id": snapshot_id, "category": "PROMOTER", "percentage": "49.40",
                    "metric_basis": None, "raw_source_label": "Promoter and Promoter Group",
                    "source_locator": "official:pr_and_prgrp", "evidence_text": "Promoter and Promoter Group: 49.40%",
                    "created_at": database_now,
                }])
            if "SELECT * FROM global_shareholding_snapshots" in sql:
                return Cursor(rows=[{
                    "id": snapshot_id, "instrument_id": instrument_id, "period_end": datetime(2026, 6, 30),
                    "filing_basis": None, "source_provider": "NSE", "source_type": "NSE_SHAREHOLDING_XBRL",
                    "source_identity_key": "NSE_SHAREHOLDING:123", "source_url": "https://nsearchives.nseindia.com/xbrl.xml",
                    "research_document_id": None, "published_at": database_now, "retrieved_at": database_now, "confidence": "0.95",
                    "reliability_level": "LEVEL_A", "source_mode": "REAL", "created_at": database_now, "updated_at": database_now,
                }])
            if "SELECT id FROM global_shareholding_snapshots" in sql:
                return Cursor(row={"id": existing_id})
            raise AssertionError(f"unexpected SQL: {sql}")
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False

    persistence = SqliteResearchPersistence()
    persistence._connection = NativeUuidConnection()
    loaded = persistence.load_shareholding_snapshots()
    assert len(loaded) == 1
    assert loaded[0].id == snapshot_id
    assert loaded[0].instrument_id == instrument_id
    assert loaded[0].values[0].id == value_id
    assert loaded[0].values[0].category == ShareholdingCategory.PROMOTER
    assert loaded[0].retrieved_at == database_now.replace(tzinfo=timezone.utc)
    assert loaded[0].period_end.tzinfo == timezone.utc

    duplicate = _snapshot(instrument_id, "NSE_SHAREHOLDING:123")
    assert persistence.upsert_shareholding_snapshot(duplicate) is False
    assert duplicate.id == existing_id


def test_shareholding_freshness_uses_utc_normalized_database_timestamp_without_changing_ttl() -> None:
    instrument_id = uuid4()
    persisted_at = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
    snapshot = _snapshot(instrument_id, "postgres-naive-timestamp")
    snapshot.retrieved_at = persisted_at
    repository = ResearchRepository(settings=Settings(research_shareholding_freshness_seconds=3600))
    repository.shareholding_snapshots[snapshot.id] = snapshot

    assert repository._category_is_fresh(
        instrument_id, "SHAREHOLDING_PATTERN", persisted_at + timedelta(minutes=59)
    ) is True
    assert repository._category_is_fresh(
        instrument_id, "SHAREHOLDING_PATTERN", persisted_at + timedelta(seconds=3601)
    ) is False


def test_pledge_requires_explicit_basis_and_is_not_an_ownership_residual() -> None:
    with pytest.raises(ValueError, match="PROMOTER_PLEDGE"):
        ShareholdingSnapshot(instrument_id=uuid4(), period_end=datetime(2026, 6, 30, tzinfo=timezone.utc),
            source_provider="NSE", source_type="EXCHANGE_ANNOUNCEMENT", source_identity_key="pledge-without-basis",
            source_url="https://www.nseindia.com/file.pdf", confidence=Decimal("0.90"),
            reliability_level=ReliabilityLevel.LEVEL_A, source_mode=SourceMode.REAL,
            values=[ShareholdingSnapshotValue(category=ShareholdingCategory.PROMOTER_PLEDGE, percentage=Decimal("10"))])
    pledge = ShareholdingSnapshotValue(category=ShareholdingCategory.PROMOTER_PLEDGE,
        percentage=Decimal("10"), metric_basis="PERCENT_OF_PROMOTER_HOLDING")
    snapshot = _snapshot(uuid4()).model_copy(update={"values": [_snapshot(uuid4()).values[0], pledge]})
    assert snapshot.values[-1].metric_basis == "PERCENT_OF_PROMOTER_HOLDING"


def test_official_fixture_normalizes_real_categories_without_missing_zeroes() -> None:
    instrument_id = uuid4()
    document = ResearchDocument(
        instrument_id=instrument_id, company_id=uuid4(), canonical_url="https://www.nseindia.com/file.pdf",
        original_url="https://www.nseindia.com/file.pdf", source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
        source_classification=SourceClassification.EXCHANGE, source_name="NSE", content_type="application/pdf",
        document_type=DocumentType.PDF_REFERENCE, content_hash="a" * 64, status=DocumentStatus.PROCESSED,
        reliability_level=ReliabilityLevel.LEVEL_A, entity_resolution_confidence=0.99, source_mode=SourceMode.REAL,
        normalized_text="Shareholding Pattern as on 30/06/2026. Promoter and Promoter Group: 42.50%. Foreign Portfolio Investors: 15.25%. Public Shareholders: 42.25%.",
    )
    snapshot = parse_official_shareholding(document)
    assert snapshot is not None
    values = {value.category: value.percentage for value in snapshot.values}
    assert values[ShareholdingCategory.PROMOTER] == Decimal("42.50")
    assert values[ShareholdingCategory.FII_FPI] == Decimal("15.25")
    assert ShareholdingCategory.INSURANCE not in values


def test_invalid_percentages_are_rejected_not_persisted() -> None:
    document = ResearchDocument(instrument_id=uuid4(), company_id=uuid4(), canonical_url="https://www.nseindia.com/file.pdf",
        original_url="https://www.nseindia.com/file.pdf", source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
        source_classification=SourceClassification.EXCHANGE, source_name="NSE", content_type="application/pdf",
        document_type=DocumentType.PDF_REFERENCE, content_hash="b" * 64, status=DocumentStatus.PROCESSED,
        reliability_level=ReliabilityLevel.LEVEL_A, entity_resolution_confidence=0.99, source_mode=SourceMode.REAL,
        normalized_text="Shareholding Pattern as on 30/06/2026. Promoter: 120%.")
    assert parse_official_shareholding(document) is None


@pytest.mark.parametrize("title", [
    "Shareholding Pattern",
    "Shareholding Pattern for the quarter ended June 30, 2026",
    "Share Holder Pattern",
    "Regulation 31 - Shareholding Pattern",
])
def test_nse_shareholding_classifier_accepts_only_explicit_pattern_filings(title: str) -> None:
    assert _is_shareholding_announcement(title)


@pytest.mark.parametrize("title", [
    "Change in Shareholding",
    "Promoter Shareholding Update",
    "Promoter Pledge",
    "Outcome of Board Meeting",
    "Financial Results",
    "Investor Presentation",
    "Acquisition resulting in change in shareholding",
])
def test_nse_shareholding_classifier_rejects_non_pattern_announcements(title: str) -> None:
    assert not _is_shareholding_announcement(title)


@pytest.mark.asyncio
async def test_official_discovery_excludes_unrelated_shareholding_rows() -> None:
    profile = CompanyResearchProfile(instrument_id=uuid4(), company_id=uuid4(), company_name="Generic India Equity",
        ticker="BROKER_ALIAS", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        provider_instrument_ids={"NSE": "VERIFIED_SYMBOL"})
    rows = [
        {"an_dt": "11-Aug-2026 13:52:28", "desc": "Change in Shareholding", "attchmntText": "Acquisition resulting in change in shareholding", "attchmntFile": "https://nsearchives.nseindia.com/corporate/unrelated.pdf"},
        {"an_dt": "10-Aug-2026 13:52:28", "desc": "Regulation 31 - Shareholding Pattern", "attchmntText": "Shareholding Pattern for the quarter ended June 30, 2026", "attchmntFile": "https://nsearchives.nseindia.com/corporate/pattern.pdf"},
        {"an_dt": "09-Aug-2026 13:52:28", "desc": "Promoter Pledge", "attchmntText": "Promoter shareholding update", "attchmntFile": "https://nsearchives.nseindia.com/corporate/pledge.pdf"},
    ]
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=rows, request=request)))
    try:
        discovered = await OfficialFilingDiscovery(client).discover(profile, {"SHAREHOLDING_PATTERN"}, set())
    finally:
        await client.aclose()
    assert [result.source.url for result in discovered] == ["https://nsearchives.nseindia.com/corporate/pattern.pdf"]


@pytest.mark.asyncio
async def test_dedicated_nse_shareholding_discovery_uses_trusted_mapping_and_official_xbrl_provenance() -> None:
    instrument_id = uuid4()
    profile = CompanyResearchProfile(instrument_id=instrument_id, company_id=uuid4(), company_name="Generic India Equity",
        ticker="BROKER_ALIAS", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        provider_instrument_ids={"NSE": "OFFICIAL_SYMBOL"})
    calls: list[httpx.Request] = []
    rows = [{
        "recordId": "12345", "symbol": "OFFICIAL_SYMBOL", "date": "30-JUN-2026",
        "submissionDate": "20-JUL-2026", "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/SHP_12345.xml",
        "pr_and_prgrp": "42.50", "public_val": "57.50", "employeeTrusts": "0",
    }]
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=rows, request=request)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        snapshots = await OfficialNseShareholdingDiscovery(client).discover(profile)
    finally:
        await client.aclose()
    assert len(calls) == 1
    assert calls[0].url.params["symbol"] == "OFFICIAL_SYMBOL"
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.instrument_id == instrument_id
    assert snapshot.period_end == datetime(2026, 6, 30, tzinfo=timezone.utc)
    assert snapshot.source_identity_key == "NSE_SHAREHOLDING:12345"
    assert snapshot.source_url == "https://nsearchives.nseindia.com/corporate/xbrl/SHP_12345.xml"
    assert {value.category: value.percentage for value in snapshot.values} == {
        ShareholdingCategory.PROMOTER: Decimal("42.50"),
    }
    assert ShareholdingCategory.PUBLIC_RETAIL not in {value.category for value in snapshot.values}
    assert len(snapshot.values) == 1


@pytest.mark.asyncio
async def test_dedicated_nse_shareholding_discovery_rejects_broker_alias_without_trusted_mapping() -> None:
    profile = CompanyResearchProfile(instrument_id=uuid4(), company_id=uuid4(), company_name="Generic India Equity",
        ticker="BROKER_ALIAS", exchange="NSE", mic="XNSE", country="IN", currency="INR")
    calls = 0
    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[], request=request)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await OfficialNseShareholdingDiscovery(client).discover(profile) == []
    finally:
        await client.aclose()
    assert calls == 0


@pytest.mark.asyncio
async def test_dedicated_nse_shareholding_discovery_keeps_latest_four_distinct_official_periods() -> None:
    profile = CompanyResearchProfile(instrument_id=uuid4(), company_id=uuid4(), company_name="Generic India Equity",
        ticker="BROKER_ALIAS", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        provider_instrument_ids={"NSE": "OFFICIAL_SYMBOL"})
    dates = ("30-JUN-2026", "31-MAR-2026", "31-DEC-2025", "30-SEP-2025", "30-JUN-2025")
    rows = [{
        "recordId": str(index), "symbol": "OFFICIAL_SYMBOL", "date": date,
        "submissionDate": "20-JUL-2026", "xbrl": f"https://nsearchives.nseindia.com/corporate/xbrl/SHP_{index}.xml",
        "pr_and_prgrp": "42.50", "public_val": "57.50",
    } for index, date in enumerate(reversed(dates), start=1)]
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=rows, request=request)))
    try:
        snapshots = await OfficialNseShareholdingDiscovery(client).discover(profile)
    finally:
        await client.aclose()
    assert [snapshot.period_end.date().isoformat() for snapshot in snapshots] == [
        "2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30",
    ]


@pytest.mark.asyncio
async def test_dedicated_nse_shareholding_discovery_rejects_non_quarter_as_on_date_without_displacing_quarter() -> None:
    profile = CompanyResearchProfile(instrument_id=uuid4(), company_id=uuid4(), company_name="Generic India Equity",
        ticker="BROKER_ALIAS", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        provider_instrument_ids={"NSE": "OFFICIAL_SYMBOL"})
    quarter_dates = ("30-JUN-2026", "31-MAR-2026", "31-DEC-2025", "30-SEP-2025", "30-JUN-2025")
    rows = [{
        "recordId": str(index), "symbol": "OFFICIAL_SYMBOL", "date": date,
        "submissionDate": "20-JUL-2026", "xbrl": f"https://nsearchives.nseindia.com/corporate/xbrl/SHP_{index}.xml",
        "pr_and_prgrp": "42.50", "public_val": "57.50",
    } for index, date in enumerate(quarter_dates, start=1)]
    rows.append({
        # NSE's feed labels this generic field "As on Date". With no explicit
        # quarterly period/type marker, it cannot be treated as Regulation 31
        # quarterly evidence merely because it has a filing XBRL URL.
        "recordId": "special", "symbol": "OFFICIAL_SYMBOL", "date": "18-FEB-2026",
        "submissionDate": "26-FEB-2026", "broadcastDate": "26-FEB-2026 18:57:56",
        "typeOfSubmission": None, "revisedData": "N",
        "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/SHP_special.xml",
        "pr_and_prgrp": "49.49", "public_val": "50.51",
    })
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=rows, request=request)))
    try:
        snapshots = await OfficialNseShareholdingDiscovery(client).discover(profile)
    finally:
        await client.aclose()
    assert [snapshot.period_end.date().isoformat() for snapshot in snapshots] == [
        "2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30",
    ]
    assert all(snapshot.source_identity_key != "NSE_SHAREHOLDING:special" for snapshot in snapshots)
    assert all({value.category for value in snapshot.values} == {ShareholdingCategory.PROMOTER} for snapshot in snapshots)


@pytest.mark.asyncio
async def test_dedicated_nse_shareholding_retains_distinct_revisions_for_one_quarter() -> None:
    profile = CompanyResearchProfile(instrument_id=uuid4(), company_id=uuid4(), company_name="Generic India Equity",
        ticker="BROKER_ALIAS", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        provider_instrument_ids={"NSE": "OFFICIAL_SYMBOL"})
    def row(record_id: str, submission_date: str, promoter: str) -> dict[str, str]:
        return {
            "recordId": record_id, "symbol": "OFFICIAL_SYMBOL", "date": "31-MAR-2026",
            "submissionDate": submission_date, "xbrl": f"https://nsearchives.nseindia.com/corporate/xbrl/SHP_{record_id}.xml",
            "pr_and_prgrp": promoter, "public_val": "50.00",
        }
    rows = [row("original", "20-APR-2026", "49.40"), row("revision", "22-APR-2026", "49.49")]
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=rows, request=request)))
    try:
        snapshots = await OfficialNseShareholdingDiscovery(client).discover(profile)
    finally:
        await client.aclose()
    assert [snapshot.source_identity_key for snapshot in snapshots] == [
        "NSE_SHAREHOLDING:revision", "NSE_SHAREHOLDING:original",
    ]
    assert all(snapshot.period_end == datetime(2026, 3, 31, tzinfo=timezone.utc) for snapshot in snapshots)
    repository = ResearchRepository(settings=Settings())
    for snapshot in snapshots:
        assert repository.persist_shareholding_snapshot(snapshot) is True
    latest = repository.shareholding_for(profile.instrument_id)
    assert len(latest) == 1
    assert latest[0].source_identity_key == "NSE_SHAREHOLDING:revision"


def test_existing_non_quarter_snapshot_does_not_pollute_quarterly_read_model() -> None:
    repository = ResearchRepository(settings=Settings())
    instrument_id = uuid4()
    non_quarter = _snapshot(instrument_id, "legacy-special-filing")
    non_quarter.period_end = datetime(2026, 2, 18, tzinfo=timezone.utc)
    quarter = _snapshot(instrument_id, "quarterly-filing")
    assert repository.persist_shareholding_snapshot(non_quarter) is True
    assert repository.persist_shareholding_snapshot(quarter) is True
    assert [snapshot.source_identity_key for snapshot in repository.shareholding_for(instrument_id)] == ["quarterly-filing"]


@pytest.mark.asyncio
async def test_dedicated_nse_shareholding_refresh_persists_once_and_reuses_durable_snapshot() -> None:
    profile = CompanyResearchProfile(instrument_id=uuid4(), company_id=uuid4(), company_name="Generic India Equity",
        ticker="BROKER_ALIAS", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        provider_instrument_ids={"NSE": "OFFICIAL_SYMBOL"})
    rows = [{
        "recordId": "12345", "symbol": "OFFICIAL_SYMBOL", "date": "30-JUN-2026",
        "submissionDate": "20-JUL-2026", "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/SHP_12345.xml",
        "pr_and_prgrp": "42.50", "public_val": "57.50",
    }]
    calls = 0
    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=rows, request=request)

    class XbrlFetcher:
        calls = 0

        async def fetch(self, url: str) -> FetchResult:
            self.calls += 1
            return FetchResult(
                final_url=url,
                status_code=200,
                content_type="application/xml",
                text=_nse_xbrl_fixture(),
                bytes_read=len(_nse_xbrl_fixture()),
            )

    fetcher = XbrlFetcher()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        repository = ResearchRepository(
            settings=Settings(research_live_enabled=True, research_search_enabled=False),
            fetcher=fetcher,
            official_filing_discovery=_EmptyOfficialFilingDiscovery(),
            official_shareholding_discovery=OfficialNseShareholdingDiscovery(client),
            persistence=SqliteResearchPersistence(),
        )
        repository.profiles = [profile]
        await repository._refresh_targeted(profile, set())
        await repository._refresh_targeted(profile, set())
    finally:
        await client.aclose()
    # A fresh but incomplete history remains eligible for bounded
    # reconciliation; durable source identity keeps the repeat idempotent.
    assert calls == 2
    assert fetcher.calls == 1
    snapshots = repository.shareholding_for(profile.instrument_id)
    assert len(snapshots) == 1
    assert snapshots[0].source_mode == SourceMode.REAL
    values = {value.category: value for value in snapshots[0].values}
    assert values[ShareholdingCategory.PROMOTER].percentage == Decimal("49.400")
    assert values[ShareholdingCategory.FII_FPI].percentage == Decimal("8.1600")
    assert values[ShareholdingCategory.PUBLIC_RETAIL].percentage == Decimal("24.7600")
    assert values[ShareholdingCategory.PROMOTER_PLEDGE].metric_basis == "PERCENT_OF_PROMOTER_HOLDING"
    assert repository._category_is_fresh(profile.instrument_id, "SHAREHOLDING_PATTERN", datetime.now(timezone.utc))


@pytest.mark.asyncio
async def test_fresh_incomplete_shareholding_reconciles_missing_quarter_once_and_keeps_legacy_row() -> None:
    profile = CompanyResearchProfile(instrument_id=uuid4(), company_id=uuid4(), company_name="Generic India Equity",
        ticker="BROKER_ALIAS", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        provider_instrument_ids={"NSE": "OFFICIAL_SYMBOL"})
    repository = ResearchRepository(settings=Settings(research_live_enabled=True, research_search_enabled=False))
    repository.profiles = [profile]
    for source, period in (("june", (2026, 6, 30)), ("march", (2026, 3, 31)), ("december", (2025, 12, 31))):
        snapshot = _snapshot(profile.instrument_id, source)
        snapshot.period_end = datetime(*period, tzinfo=timezone.utc)
        assert repository.persist_shareholding_snapshot(snapshot) is True
    legacy = _snapshot(profile.instrument_id, "legacy-non-quarter")
    legacy.period_end = datetime(2026, 2, 18, tzinfo=timezone.utc)
    assert repository.persist_shareholding_snapshot(legacy) is True
    september = _snapshot(profile.instrument_id, "NSE_SHAREHOLDING:203088")
    september.period_end = datetime(2025, 9, 30, tzinfo=timezone.utc)

    class ShareholdingProvider:
        def __init__(self) -> None:
            self.calls = 0
        async def discover(self, _profile):
            self.calls += 1
            return [september]
    provider = ShareholdingProvider()
    repository._official_filing_discovery = _EmptyOfficialFilingDiscovery()
    repository._official_shareholding_discovery = provider

    await repository._refresh_targeted(profile, set(), now=datetime(2026, 8, 15, tzinfo=timezone.utc))
    await repository._refresh_targeted(profile, set(), now=datetime(2026, 8, 16, tzinfo=timezone.utc))

    assert provider.calls == 1
    assert [snapshot.period_end.date().isoformat() for snapshot in repository.shareholding_for(profile.instrument_id)] == [
        "2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30",
    ]
    assert any(snapshot.source_identity_key == "legacy-non-quarter" for snapshot in repository.shareholding_snapshots.values())
    assert len([snapshot for snapshot in repository.shareholding_snapshots.values()
                if snapshot.source_identity_key == "NSE_SHAREHOLDING:203088"]) == 1


@pytest.mark.asyncio
async def test_fresh_complete_shareholding_does_not_call_structured_reconciliation_provider() -> None:
    profile = CompanyResearchProfile(instrument_id=uuid4(), company_id=uuid4(), company_name="Generic India Equity",
        ticker="BROKER_ALIAS", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        provider_instrument_ids={"NSE": "OFFICIAL_SYMBOL"})
    repository = ResearchRepository(settings=Settings(research_live_enabled=True, research_search_enabled=False))
    repository.profiles = [profile]
    for source, period in (("june", (2026, 6, 30)), ("march", (2026, 3, 31)), ("december", (2025, 12, 31)), ("september", (2025, 9, 30))):
        snapshot = _snapshot(profile.instrument_id, source)
        snapshot.period_end = datetime(*period, tzinfo=timezone.utc)
        assert repository.persist_shareholding_snapshot(snapshot) is True
    class Provider:
        calls = 0
        async def discover(self, _profile):
            self.calls += 1
            return []
    provider = Provider()
    repository._official_filing_discovery = _EmptyOfficialFilingDiscovery()
    repository._official_shareholding_discovery = provider
    await repository._refresh_targeted(profile, set(), now=datetime(2026, 8, 15, tzinfo=timezone.utc))
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_fresh_complete_legacy_nse_snapshots_are_enriched_once_without_duplicate_periods() -> None:
    profile = CompanyResearchProfile(instrument_id=uuid4(), company_id=uuid4(), company_name="Generic India Equity",
        ticker="BROKER_ALIAS", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        provider_instrument_ids={"NSE": "OFFICIAL_SYMBOL"})
    repository = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_search_enabled=False),
        persistence=SqliteResearchPersistence(),
        official_filing_discovery=_EmptyOfficialFilingDiscovery(),
    )
    repository.profiles = [profile]
    periods = ((2026, 6, 30), (2026, 3, 31), (2025, 12, 31), (2025, 9, 30))
    legacy = [_legacy_nse_xbrl_snapshot(profile.instrument_id, str(index), period) for index, period in enumerate(periods, start=1)]
    for snapshot in legacy:
        assert repository.persist_shareholding_snapshot(snapshot) is True

    class Provider:
        calls = 0
        async def discover(self, _profile):
            self.calls += 1
            return [snapshot.model_copy(update={"values": snapshot.values[:]}) for snapshot in legacy]

    class XbrlFetcher:
        calls: list[str] = []
        async def fetch_nse_shareholding_xbrl(self, url: str) -> FetchResult:
            self.calls.append(url)
            return FetchResult(url, 200, "application/xml", _nse_xbrl_fixture(), len(_nse_xbrl_fixture()))

        async def fetch(self, _url: str) -> FetchResult:
            raise AssertionError("NSE XBRL enrichment must use the scoped fetch path")

    provider, fetcher = Provider(), XbrlFetcher()
    repository._official_shareholding_discovery = provider
    repository._fetcher = fetcher

    await repository._refresh_targeted(profile, set())

    assert provider.calls == 1
    assert len(fetcher.calls) == 4
    snapshots = repository.shareholding_for(profile.instrument_id)
    assert [snapshot.source_identity_key for snapshot in snapshots] == [
        "NSE_SHAREHOLDING:1", "NSE_SHAREHOLDING:2", "NSE_SHAREHOLDING:3", "NSE_SHAREHOLDING:4",
    ]
    assert all(ShareholdingCategory.FII_FPI in {value.category for value in snapshot.values} for snapshot in snapshots)
    assert all(any((value.source_locator or "").startswith("nse-xbrl:") for value in snapshot.values) for snapshot in snapshots)
    assert len(repository.shareholding_snapshots) == 4

    # XBRL provenance, not an all-categories requirement, prevents another
    # fresh reconciliation even though optional official categories may be absent.
    await repository._refresh_targeted(profile, set())
    assert provider.calls == 1
    assert len(fetcher.calls) == 4


@pytest.mark.asyncio
async def test_legacy_xbrl_enrichment_fetch_failure_keeps_existing_promoter_values_retryable() -> None:
    profile = CompanyResearchProfile(instrument_id=uuid4(), company_id=uuid4(), company_name="Generic India Equity",
        ticker="BROKER_ALIAS", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        provider_instrument_ids={"NSE": "OFFICIAL_SYMBOL"})
    repository = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_search_enabled=False),
        persistence=SqliteResearchPersistence(),
        official_filing_discovery=_EmptyOfficialFilingDiscovery(),
    )
    repository.profiles = [profile]
    legacy = [_legacy_nse_xbrl_snapshot(profile.instrument_id, str(index), period) for index, period in enumerate(
        ((2026, 6, 30), (2026, 3, 31), (2025, 12, 31), (2025, 9, 30)), start=1)]
    for snapshot in legacy:
        assert repository.persist_shareholding_snapshot(snapshot) is True

    class Provider:
        calls = 0
        async def discover(self, _profile):
            self.calls += 1
            return legacy

    class FailingFetcher:
        calls = 0
        async def fetch(self, _url: str):
            self.calls += 1
            raise FetchError("fixture XBRL unavailable")

    provider, fetcher = Provider(), FailingFetcher()
    repository._official_shareholding_discovery = provider
    repository._fetcher = fetcher
    await repository._refresh_targeted(profile, set())

    assert provider.calls == 1
    assert fetcher.calls == 4
    assert len(repository.shareholding_snapshots) == 4
    assert all(
        [(value.category, value.percentage) for value in snapshot.values] == [(ShareholdingCategory.PROMOTER, Decimal("42.5"))]
        for snapshot in repository.shareholding_for(profile.instrument_id)
    )


@pytest.mark.asyncio
async def test_stale_complete_shareholding_rechecks_nse_and_rolls_latest_four_without_deleting_history() -> None:
    profile = CompanyResearchProfile(instrument_id=uuid4(), company_id=uuid4(), company_name="Generic India Equity",
        ticker="BROKER_ALIAS", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        provider_instrument_ids={"NSE": "OFFICIAL_SYMBOL"})
    settings = Settings(research_live_enabled=True, research_search_enabled=False, research_shareholding_freshness_seconds=3600)
    repository = ResearchRepository(settings=settings)
    repository.profiles = [profile]
    stale_at = datetime.now(timezone.utc) - timedelta(seconds=settings.research_shareholding_freshness_seconds + 1)
    for source, period in (("june", (2026, 6, 30)), ("march", (2026, 3, 31)), ("december", (2025, 12, 31)), ("september", (2025, 9, 30))):
        snapshot = _snapshot(profile.instrument_id, source)
        snapshot.period_end = datetime(*period, tzinfo=timezone.utc)
        snapshot.retrieved_at = stale_at
        assert repository.persist_shareholding_snapshot(snapshot) is True
    legacy = _snapshot(profile.instrument_id, "legacy-non-quarter")
    legacy.period_end = datetime(2026, 2, 18, tzinfo=timezone.utc)
    legacy.retrieved_at = stale_at
    assert repository.persist_shareholding_snapshot(legacy) is True
    new_june = _snapshot(profile.instrument_id, "NSE_SHAREHOLDING:new-june")
    new_june.period_end = datetime(2026, 9, 30, tzinfo=timezone.utc)

    class Provider:
        def __init__(self) -> None:
            self.calls = 0
            self.results: list[ShareholdingSnapshot] = []
        async def discover(self, _profile):
            self.calls += 1
            return self.results
    provider = Provider()
    repository._official_filing_discovery = _EmptyOfficialFilingDiscovery()
    repository._official_shareholding_discovery = provider

    # Stale + complete must query NSE even when it has no new quarter.
    await repository._refresh_targeted(profile, set())
    assert provider.calls == 1
    assert [snapshot.period_end.date().isoformat() for snapshot in repository.shareholding_for(profile.instrument_id)] == [
        "2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30",
    ]

    # Simulate the next normal refresh after the category freshness interval.
    repository._category_refresh[(profile.instrument_id, "SHAREHOLDING_PATTERN")] = datetime.now(timezone.utc) - timedelta(days=4)
    provider.results = [new_june]
    await repository._refresh_targeted(profile, set())
    assert provider.calls == 2
    assert [snapshot.period_end.date().isoformat() for snapshot in repository.shareholding_for(profile.instrument_id)] == [
        "2026-09-30", "2026-06-30", "2026-03-31", "2025-12-31",
    ]
    assert len([snapshot for snapshot in repository.shareholding_snapshots.values()
                if (snapshot.period_end.month, snapshot.period_end.day) in {(3, 31), (6, 30), (9, 30), (12, 31)}]) == 5
    assert any(snapshot.source_identity_key == "september" for snapshot in repository.shareholding_snapshots.values())
    assert all(snapshot.period_end.date().isoformat() != "2026-02-18" for snapshot in repository.shareholding_for(profile.instrument_id))

    # A later stale reconciliation may rediscover the same filing, but upsert
    # preserves one durable official source identity.
    repository._category_refresh[(profile.instrument_id, "SHAREHOLDING_PATTERN")] = datetime.now(timezone.utc) - timedelta(days=4)
    await repository._refresh_targeted(profile, set(), now=datetime(2026, 12, 5, tzinfo=timezone.utc))
    assert provider.calls == 3
    assert len([snapshot for snapshot in repository.shareholding_snapshots.values()
                if snapshot.source_identity_key == "NSE_SHAREHOLDING:new-june"]) == 1


@pytest.mark.asyncio
async def test_stale_complete_xbrl_shareholding_uses_master_check_without_redownloading_unchanged_records() -> None:
    profile = CompanyResearchProfile(instrument_id=uuid4(), company_id=uuid4(), company_name="Generic India Equity",
        ticker="BROKER_ALIAS", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        provider_instrument_ids={"NSE": "OFFICIAL_SYMBOL"})
    settings = Settings(research_live_enabled=True, research_search_enabled=False, research_shareholding_freshness_seconds=3600)
    repository = ResearchRepository(settings=settings)
    repository.profiles = [profile]
    stale_at = datetime.now(timezone.utc) - timedelta(seconds=settings.research_shareholding_freshness_seconds + 1)
    snapshots = []
    for source, period in (("june", (2026, 6, 30)), ("march", (2026, 3, 31)), ("december", (2025, 12, 31)), ("september", (2025, 9, 30))):
        snapshot = _legacy_nse_xbrl_snapshot(profile.instrument_id, source, period)
        snapshot.retrieved_at = stale_at
        snapshot.values = [ShareholdingSnapshotValue(
            category=ShareholdingCategory.PROMOTER,
            percentage=Decimal("42.5"),
            source_locator="nse-xbrl:explicit:promoter",
        )]
        assert repository.persist_shareholding_snapshot(snapshot) is True
        snapshots.append(snapshot)

    class Provider:
        calls = 0
        async def discover(self, _profile):
            self.calls += 1
            return snapshots

    class Fetcher:
        calls = 0
        async def fetch_nse_shareholding_xbrl(self, _url):
            self.calls += 1
            raise AssertionError("unchanged XBRL must not be downloaded")

    provider, fetcher = Provider(), Fetcher()
    repository._official_filing_discovery = _EmptyOfficialFilingDiscovery()
    repository._official_shareholding_discovery = provider
    repository._fetcher = fetcher

    await repository._refresh_targeted(profile, set())

    assert provider.calls == 1
    assert fetcher.calls == 0
    assert {snapshot.source_identity_key for snapshot in repository.shareholding_snapshots.values()} == {
        "NSE_SHAREHOLDING:june", "NSE_SHAREHOLDING:march", "NSE_SHAREHOLDING:december", "NSE_SHAREHOLDING:september",
    }
    assert repository._category_refresh[(profile.instrument_id, "SHAREHOLDING_PATTERN")] > stale_at


@pytest.mark.asyncio
async def test_quarterly_shareholding_gate_skips_before_next_window_then_checks_metadata_without_changing_evidence() -> None:
    profile = CompanyResearchProfile(instrument_id=uuid4(), company_id=uuid4(), company_name="Generic India Equity",
        ticker="BROKER_ALIAS", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        provider_instrument_ids={"NSE": "OFFICIAL_SYMBOL"})
    repository = ResearchRepository(settings=Settings(research_live_enabled=True, research_search_enabled=False))
    repository.profiles = [profile]
    snapshots = []
    for source, period in (("june", (2026, 6, 30)), ("march", (2026, 3, 31)), ("december", (2025, 12, 31)), ("september", (2025, 9, 30))):
        snapshot = _legacy_nse_xbrl_snapshot(profile.instrument_id, source, period)
        snapshot.retrieved_at = datetime(2026, 7, 2, tzinfo=timezone.utc)
        snapshot.values = [ShareholdingSnapshotValue(
            category=ShareholdingCategory.PROMOTER, percentage=Decimal("42.5"), source_locator="nse-xbrl:explicit:promoter",
        )]
        assert repository.persist_shareholding_snapshot(snapshot)
        snapshots.append(snapshot)

    class Provider:
        calls = 0
        async def discover(self, _profile):
            self.calls += 1
            return snapshots

    provider = Provider()
    repository._official_filing_discovery = _EmptyOfficialFilingDiscovery()
    repository._official_shareholding_discovery = provider
    evidence_at = repository.shareholding_for(profile.instrument_id, limit=1)[0].retrieved_at

    await repository._refresh_targeted(profile, set(), now=datetime(2026, 8, 15, tzinfo=timezone.utc))
    assert provider.calls == 0

    checked_at = datetime(2026, 9, 2, tzinfo=timezone.utc)
    await repository._refresh_targeted(profile, set(), now=checked_at)
    assert provider.calls == 1
    assert repository._category_refresh[(profile.instrument_id, "SHAREHOLDING_PATTERN")] == checked_at
    assert repository.shareholding_for(profile.instrument_id, limit=1)[0].retrieved_at == evidence_at

    await repository._refresh_targeted(profile, set(), now=checked_at + timedelta(days=1))
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_incomplete_fresh_shareholding_provider_failure_preserves_existing_data_and_non_nse_is_skipped() -> None:
    profile = CompanyResearchProfile(instrument_id=uuid4(), company_id=uuid4(), company_name="Generic India Equity",
        ticker="BROKER_ALIAS", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        provider_instrument_ids={"NSE": "OFFICIAL_SYMBOL"})
    repository = ResearchRepository(settings=Settings(research_live_enabled=True, research_search_enabled=False))
    repository.profiles = [profile]
    for source, period in (("june", (2026, 6, 30)), ("march", (2026, 3, 31)), ("december", (2025, 12, 31))):
        snapshot = _snapshot(profile.instrument_id, source)
        snapshot.period_end = datetime(*period, tzinfo=timezone.utc)
        assert repository.persist_shareholding_snapshot(snapshot) is True
    class UnavailableProvider:
        calls = 0
        async def discover(self, _profile):
            self.calls += 1
            raise SearchProviderError("NSE_SHAREHOLDING_OFFICIAL_UNAVAILABLE")
    provider = UnavailableProvider()
    repository._official_filing_discovery = _EmptyOfficialFilingDiscovery()
    repository._official_shareholding_discovery = provider
    await repository._refresh_targeted(profile, set())
    assert provider.calls == 1
    assert len(repository.shareholding_for(profile.instrument_id)) == 3
    assert repository._category_is_fresh(profile.instrument_id, "SHAREHOLDING_PATTERN", datetime.now(timezone.utc))

    non_nse = profile.model_copy(update={"instrument_id": uuid4(), "exchange": "NYSE", "country": "US", "provider_instrument_ids": {}})
    repository.profiles.append(non_nse)
    for source, period in (("us-june", (2026, 6, 30)), ("us-march", (2026, 3, 31)), ("us-december", (2025, 12, 31))):
        snapshot = _snapshot(non_nse.instrument_id, source)
        snapshot.period_end = datetime(*period, tzinfo=timezone.utc)
        assert repository.persist_shareholding_snapshot(snapshot) is True
    await repository._refresh_targeted(non_nse, set())
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_dedicated_nse_shareholding_empty_or_invalid_rows_remain_retryable_without_snapshot() -> None:
    profile = CompanyResearchProfile(instrument_id=uuid4(), company_id=uuid4(), company_name="Generic India Equity",
        ticker="BROKER_ALIAS", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        provider_instrument_ids={"NSE": "OFFICIAL_SYMBOL"})
    rows = [{
        "recordId": "bad", "symbol": "OFFICIAL_SYMBOL", "date": "30-JUN-2026",
        "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/SHP_bad.xml", "pr_and_prgrp": "101",
    }]
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=rows, request=request)))
    try:
        assert await OfficialNseShareholdingDiscovery(client).discover(profile) == []
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_persisted_real_official_document_reuses_before_network_after_recreation(tmp_path) -> None:
    database = tmp_path / "research.db"
    settings = Settings(research_live_enabled=True, research_persistence_enabled=True,
        research_database_backend="sqlite", research_database_name=str(database),
        research_official_document_max_attempts_per_refresh=1)
    first = ResearchRepository(settings=settings)
    profile = next(profile for profile in first.list_profiles() if profile.ticker == "RELIANCE")
    source = RegisteredResearchSource(source_id="nse-shareholding-reuse", instrument_id=profile.instrument_id,
        url="https://nsearchives.nseindia.com/corporate/shareholding-reuse.pdf",
        source_type=SourceType.EXCHANGE_ANNOUNCEMENT, source_classification=SourceClassification.EXCHANGE,
        source_name="NSE corporate announcements", publisher="NSE", reliability_level=ReliabilityLevel.LEVEL_A,
        company_id=profile.company_id, discovery_method="NSE_OFFICIAL_API", categories=("SHAREHOLDING_PATTERN",))
    persisted = first.ingest_fixture(original_url=source.url, source_type=source.source_type,
        source_classification=source.source_classification, source_name=source.source_name, publisher=source.publisher,
        content_type="text/html", body="<title>Reliance Industries Limited Shareholding Pattern</title><main>Reliance Industries Limited RELIANCE INE002A01018 Shareholding Pattern as on 30/06/2026. Promoter: 42.5%.</main>",
        reliability=source.reliability_level, source_mode=SourceMode.REAL, expected_profile=profile)
    assert persisted.status == DocumentStatus.PROCESSED

    class NoNetworkFetcher:
        calls = 0
        async def fetch(self, _url):
            self.calls += 1
            raise AssertionError("durable reuse must happen before network fetch")

    second = ResearchRepository(settings=settings)
    fetcher = NoNetworkFetcher()
    second._fetcher = fetcher
    restored_profile = second.profile(profile.instrument_id)
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("app.repository")
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        await second._fetch_official_filings(restored_profile, [DiscoveryResult("SHAREHOLDING_PATTERN", source)], set())
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    assert fetcher.calls == 0
    assert len(second.documents_for(profile.instrument_id, source_mode=SourceMode.REAL)) == 1
    assert second._reusable_official_document(profile.instrument_id, source.url) is not None
    assert any("outcome=REUSED reason=ALREADY_PERSISTED" in record.getMessage() for record in records)


@pytest.mark.asyncio
async def test_usable_historical_parsed_document_reuses_before_network_and_freshens_financial_results(tmp_path) -> None:
    database = tmp_path / "research.db"
    settings = Settings(research_live_enabled=True, research_persistence_enabled=True,
        research_database_backend="sqlite", research_database_name=str(database))
    seed = ResearchRepository(settings=Settings())
    profile = next(profile for profile in seed.list_profiles() if profile.ticker == "RELIANCE")
    source = RegisteredResearchSource(source_id="nse-parsed-reuse", instrument_id=profile.instrument_id,
        url="https://nsearchives.nseindia.com/corporate/parsed-reuse.pdf",
        source_type=SourceType.EXCHANGE_ANNOUNCEMENT, source_classification=SourceClassification.EXCHANGE,
        source_name="NSE corporate announcements", publisher="NSE", reliability_level=ReliabilityLevel.LEVEL_A,
        company_id=profile.company_id, discovery_method="NSE_OFFICIAL_API", categories=("FINANCIAL_RESULTS",))
    parsed = ResearchDocument(instrument_id=profile.instrument_id, company_id=profile.company_id,
        canonical_url=source.url, original_url=source.url, source_type=source.source_type,
        source_classification=source.source_classification, source_name=source.source_name, publisher=source.publisher,
        title="Reliance Industries Limited Financial Results", content_type="application/pdf",
        document_type=DocumentType.PDF_REFERENCE, content_hash="c" * 64, status=DocumentStatus.PARSED,
        reliability_level=ReliabilityLevel.LEVEL_A, entity_resolution_confidence=0.99, source_mode=SourceMode.REAL,
        normalized_text="Reliance Industries Limited quarterly financial results revenue 1000 crore PAT 100 crore.")
    SqliteResearchPersistence(database).upsert_document(parsed)

    class NoNetworkFetcher:
        calls = 0
        async def fetch(self, _url):
            self.calls += 1
            raise AssertionError("usable PARSED document must be reused before network fetch")

    repository = ResearchRepository(settings=settings)
    repository._fetcher = NoNetworkFetcher()
    assert repository._reusable_official_document(profile.instrument_id, source.url) is not None
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("app.repository")
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        await repository._fetch_official_filings(profile, [DiscoveryResult("FINANCIAL_RESULTS", source)], set())
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    assert repository._fetcher.calls == 0
    assert repository._qualifying_category_evidence(profile.instrument_id, "FINANCIAL_RESULTS") is not None
    assert any("outcome=REUSED reason=ALREADY_PERSISTED" in record.getMessage() for record in records)


def test_empty_parsed_document_is_not_reusable_or_financial_result_evidence() -> None:
    repository = ResearchRepository(settings=Settings(research_live_enabled=True))
    profile = next(profile for profile in repository.list_profiles() if profile.ticker == "RELIANCE")
    document = ResearchDocument(instrument_id=profile.instrument_id, company_id=profile.company_id,
        canonical_url="https://nsearchives.nseindia.com/corporate/empty-parsed.pdf",
        original_url="https://nsearchives.nseindia.com/corporate/empty-parsed.pdf", source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
        source_classification=SourceClassification.EXCHANGE, source_name="NSE", content_type="application/pdf",
        document_type=DocumentType.PDF_REFERENCE, content_hash="d" * 64, status=DocumentStatus.PARSED,
        reliability_level=ReliabilityLevel.LEVEL_A, entity_resolution_confidence=0.99, source_mode=SourceMode.REAL,
        normalized_text="")
    repository.documents[document.document_id] = document
    assert repository._reusable_official_document(profile.instrument_id, document.canonical_url) is None
    assert repository._qualifying_category_evidence(profile.instrument_id, "FINANCIAL_RESULTS") is None


@pytest.mark.asyncio
async def test_official_fetch_round_robin_prevents_category_starvation_with_bounded_budget() -> None:
    settings = Settings(research_live_enabled=True, research_official_document_max_attempts_per_refresh=3)
    repository = ResearchRepository(settings=settings)
    profile = next(profile for profile in repository.list_profiles() if profile.ticker == "RELIANCE")

    def source(category: str, suffix: str) -> RegisteredResearchSource:
        return RegisteredResearchSource(source_id=f"nse-{category}-{suffix}", instrument_id=profile.instrument_id,
            url=f"https://nsearchives.nseindia.com/corporate/{suffix}.pdf", source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
            source_classification=SourceClassification.EXCHANGE, source_name="NSE", publisher="NSE",
            reliability_level=ReliabilityLevel.LEVEL_A, company_id=profile.company_id, discovery_method="NSE_OFFICIAL_API",
            categories=(category,))

    financial = [source("FINANCIAL_RESULTS", f"financial-{index}") for index in range(3)]
    shareholding = [source("SHAREHOLDING_PATTERN", f"shareholding-{index}") for index in range(2)]

    class FailingFetcher:
        def __init__(self) -> None:
            self.urls: list[str] = []
        async def fetch(self, url):
            self.urls.append(url)
            raise FetchError("fixture failure")

    fetcher = FailingFetcher()
    repository._fetcher = fetcher
    await repository._fetch_official_filings(profile,
        [*(DiscoveryResult("FINANCIAL_RESULTS", item) for item in financial), *(DiscoveryResult("SHAREHOLDING_PATTERN", item) for item in shareholding)], set())
    assert fetcher.urls == [financial[0].url, shareholding[0].url, financial[1].url]
    assert len(fetcher.urls) == settings.research_official_document_max_attempts_per_refresh


def test_official_filing_scheduler_preserves_newest_first_within_each_category() -> None:
    profile = ResearchRepository(settings=Settings()).list_profiles()[0]
    def result(category: str, suffix: str) -> DiscoveryResult:
        source = RegisteredResearchSource(source_id=f"{category}-{suffix}", instrument_id=profile.instrument_id,
            url=f"https://nsearchives.nseindia.com/corporate/{suffix}.pdf", source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
            source_classification=SourceClassification.EXCHANGE, source_name="NSE", publisher="NSE",
            reliability_level=ReliabilityLevel.LEVEL_A, company_id=profile.company_id, categories=(category,))
        return DiscoveryResult(category, source)
    financial_new, financial_old = result("FINANCIAL_RESULTS", "financial-new"), result("FINANCIAL_RESULTS", "financial-old")
    share_new, share_old = result("SHAREHOLDING_PATTERN", "share-new"), result("SHAREHOLDING_PATTERN", "share-old")
    scheduled = _fair_official_filing_order([financial_new, financial_old, share_new, share_old])
    urls = [item.source.url for item in scheduled]
    assert urls == [financial_new.source.url, share_new.source.url, financial_old.source.url, share_old.source.url]


@pytest.mark.asyncio
async def test_reusable_official_documents_do_not_consume_category_fair_network_budget() -> None:
    settings = Settings(research_live_enabled=True, research_official_document_max_attempts_per_refresh=3)
    repository = ResearchRepository(settings=settings)
    profile = next(profile for profile in repository.list_profiles() if profile.ticker == "RELIANCE")
    def source(category: str, suffix: str) -> RegisteredResearchSource:
        return RegisteredResearchSource(source_id=f"nse-{category}-{suffix}", instrument_id=profile.instrument_id,
            url=f"https://nsearchives.nseindia.com/corporate/{suffix}.pdf", source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
            source_classification=SourceClassification.EXCHANGE, source_name="NSE", publisher="NSE",
            reliability_level=ReliabilityLevel.LEVEL_A, company_id=profile.company_id, discovery_method="NSE_OFFICIAL_API",
            categories=(category,))
    financial = [source("FINANCIAL_RESULTS", f"reuse-financial-{index}") for index in range(3)]
    shareholding = [source("SHAREHOLDING_PATTERN", f"reuse-shareholding-{index}") for index in range(2)]
    for index in (0, 1):
        document = ResearchDocument(instrument_id=profile.instrument_id, company_id=profile.company_id,
            canonical_url=financial[index].url, original_url=financial[index].url, source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
            source_classification=SourceClassification.EXCHANGE, source_name="NSE", content_type="application/pdf",
            document_type=DocumentType.PDF_REFERENCE, content_hash=(str(index + 7) * 64), status=DocumentStatus.PROCESSED,
            reliability_level=ReliabilityLevel.LEVEL_A, entity_resolution_confidence=0.99, source_mode=SourceMode.REAL)
        repository.documents[document.document_id] = document

    class FailingFetcher:
        def __init__(self) -> None:
            self.urls: list[str] = []
        async def fetch(self, url):
            self.urls.append(url)
            raise FetchError("fixture failure")

    fetcher = FailingFetcher()
    repository._fetcher = fetcher
    await repository._fetch_official_filings(profile,
        [*(DiscoveryResult("FINANCIAL_RESULTS", item) for item in financial), *(DiscoveryResult("SHAREHOLDING_PATTERN", item) for item in shareholding)], set())
    assert fetcher.urls == [shareholding[0].url, shareholding[1].url, financial[2].url]
    assert len(fetcher.urls) == settings.research_official_document_max_attempts_per_refresh


@pytest.mark.asyncio
async def test_official_discovery_requires_verified_nse_mapping() -> None:
    profile = CompanyResearchProfile(instrument_id=uuid4(), company_id=uuid4(), company_name="Generic India Equity",
        ticker="BROKER_ALIAS", exchange="NSE", mic="XNSE", country="IN", currency="INR")
    discovery = OfficialFilingDiscovery()
    assert await discovery.discover(profile, {"SHAREHOLDING_PATTERN"}, set()) == []


def test_broker_derived_nse_alias_is_not_hydrated_as_trusted_identity() -> None:
    instrument_id = uuid4()
    instrument = _global_master_instrument({
        "canonicalName": "Generic India Equity", "assetType": "EQUITY", "country": "IN", "currency": "INR",
        "primarySymbol": "BROKER_ALIAS", "primaryExchange": "NSE",
        "providerMappings": [{"provider": "NSE", "providerSymbol": "BROKER_ALIAS", "status": "VERIFIED",
                              "resolutionSource": "BROKER_IMPORT_IDENTITY"}],
    }, instrument_id)
    assert "nseSymbol" not in instrument


def test_durable_snapshot_freshness_survives_repository_recreation(tmp_path) -> None:
    database = tmp_path / "research.db"
    settings = Settings(research_persistence_enabled=True, research_database_backend="sqlite", research_database_name=str(database))
    first = ResearchRepository(settings=settings)
    profile = first.list_profiles()[0]
    assert first.persist_shareholding_snapshot(_snapshot(profile.instrument_id)) is True
    second = ResearchRepository(settings=settings)
    assert second.shareholding_for(profile.instrument_id)
    assert second._qualifying_category_evidence(profile.instrument_id, "SHAREHOLDING_PATTERN") is not None
    assert second._category_is_fresh(profile.instrument_id, "SHAREHOLDING_PATTERN", datetime.now(timezone.utc))


def test_summary_returns_latest_four_global_periods_with_provenance() -> None:
    repo = ResearchRepository(settings=Settings())
    profile = repo.list_profiles()[0]
    for index, period_end in enumerate((
        datetime(2026, 3, 31, tzinfo=timezone.utc),
        datetime(2026, 6, 30, tzinfo=timezone.utc),
        datetime(2026, 9, 30, tzinfo=timezone.utc),
        datetime(2026, 12, 31, tzinfo=timezone.utc),
        datetime(2027, 3, 31, tzinfo=timezone.utc),
    ), start=1):
        snapshot = _snapshot(profile.instrument_id, f"nse-{index}")
        snapshot.period_end = period_end
        repo.persist_shareholding_snapshot(snapshot)
    snapshots = repo.summary(profile.instrument_id).shareholding_snapshots
    assert len(snapshots) == 4
    assert all(snapshot.source_url and snapshot.values for snapshot in snapshots)
