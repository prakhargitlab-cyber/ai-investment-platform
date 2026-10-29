"""Offline diagnostics contracts: no live providers or production persistence."""
import logging
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from app.repository import ResearchRepository
from app.research_fetching import DocumentSizeLimitExceeded, HttpResearchFetcher
from app.settings import Settings
from app.structured_financial import parse_nse_financial_xbrl
from test_historical_financial_discovery import setup_with_history
from test_nse_structured_financial import FIXTURES, NOW, ROWS, setup


XML = (FIXTURES / "ghcl_20260630.xml").read_bytes()
OLD_XML = XML.replace(b"2026-01-31/in-capmkt", b"2025-01-01/in-capmkt")
SECRET = "SENSITIVE_TEST_TOKEN"


@pytest.fixture(autouse=True)
def diagnostic_logs(caplog):
    for name in ("app.structured_financial", "app.repository", "app.research_fetching"):
        caplog.set_level(logging.INFO, logger=name)


def messages(caplog, event):
    return [record.getMessage() for record in caplog.records
            if record.getMessage().startswith(event + " ")]


def row(period, basis="Consolidated", suffix=""):
    return {**ROWS[0], "qe_Date": period, "consolidated": basis,
            "xbrl": f"https://nsearchives.nseindia.com/corporate/xbrl/{period}_{basis}{suffix}.xml"}


def provider_for(pages):
    repo, profile, provider, executor, xml, calls = setup_with_history(pages)
    for page in pages:
        for filing in page:
            xml[filing["xbrl"]] = OLD_XML
    return profile, provider, calls


def test_parse_success_reports_identity_taxonomy_and_count_without_contents_or_tokens(caplog):
    _, profile, _, _ = setup()
    source = ROWS[0]["xbrl"] + f"?token={SECRET}#{SECRET}"
    xml = XML.replace(b"<xbrli:xbrl ", (
        f'<xbrli:xbrl xmlns:diagnostic="https://user:{SECRET}@example.com/taxonomy?token={SECRET}#{SECRET}" '
    ).encode()).replace(b"</xbrli:xbrl>", b"<in-capmkt:Unmapped>PRIVATE_DOCUMENT_TEXT</in-capmkt:Unmapped></xbrli:xbrl>")
    facts = parse_nse_financial_xbrl(xml, profile, {**ROWS[0], "xbrl": source}, retrieved_at=NOW)
    assert facts
    event, = messages(caplog, "official_structured_financial_parse")
    assert "reported_period=2026-06-30 reporting_basis=STANDALONE" in event
    assert "http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt" in event
    assert "http://www.xbrl.org/2003/instance" in event
    assert "https://example.com/taxonomy" in event
    assert f"parsed_fact_count={len(facts)} persisted_fact_count=UNKNOWN reason=PARSED" in event
    assert SECRET not in caplog.text and "PRIVATE_DOCUMENT_TEXT" not in caplog.text
    assert facts[0].source_identity == source  # Redaction is logging-only.


@pytest.mark.parametrize("xml,reason,counter", [
    (OLD_XML, "UNSUPPORTED_TAXONOMY", None),
    (XML.replace(b'unitRef="INR"', b'unitRef="bad"').replace(b'unitRef="INRPerShare"', b'unitRef="bad"'),
     "NO_ELIGIBLE_FACTS", "FACT_UNIT_UNSUPPORTED"),
    (XML.replace(b"2026-04-01", b"2026-01-01"), "NO_ELIGIBLE_CONTEXTS", "CONTEXT_DURATION_UNSUPPORTED"),
], ids=["unsupported-taxonomy", "unsupported-units", "unsupported-duration"])
def test_zero_parse_results_have_explicit_reasons_and_preserve_rejection(xml, reason, counter, caplog):
    _, profile, _, _ = setup()
    assert parse_nse_financial_xbrl(xml, profile, ROWS[0], retrieved_at=NOW) == []
    event, = messages(caplog, "official_structured_financial_parse")
    assert "parsed_fact_count=0 persisted_fact_count=0" in event
    assert f"reason={reason}" in event
    if counter:
        assert counter in event
    else:
        assert "http://www.sebi.gov.in/xbrl/2025-01-01/in-capmkt" in event


@pytest.mark.asyncio
async def test_page_counts_and_empty_page_stop(caplog):
    filing = row("31-Dec-2025")
    invalid = {**filing, "qe_Date": None}
    profile, provider, calls = provider_for([[invalid, filing]])
    assert await provider.collect(profile) == []
    assert [page for page, _ in calls["pages"]] == [1, 2]
    assert calls["xml_urls"] == [filing["xbrl"]]
    start, = messages(caplog, "official_structured_financial_discovery")
    assert "page_limit=5" in start
    page = messages(caplog, "official_structured_financial_page")[0]
    assert "page=1 raw_rows=2 valid_rows=1 discovered_periods=['2025-12-31'] empty_page=False" in page
    stop, = messages(caplog, "official_structured_financial_pagination_stop")
    assert "page=2 reason=EMPTY_PAGE" in stop


@pytest.mark.asyncio
async def test_empty_page_stops_discovery_without_fetching_unadvertised_history(caplog):
    filing = row("31-Dec-2025")
    profile, provider, calls = provider_for([[], [filing]])
    await provider.collect(profile)
    assert [page for page, _ in calls["pages"]] == [1]
    assert calls["xml_urls"] == []
    assert "page=1 raw_rows=0 valid_rows=0 discovered_periods=[] empty_page=True" in messages(
        caplog, "official_structured_financial_page")[0]
    assert "page=1 reason=EMPTY_PAGE" in messages(caplog, "official_structured_financial_pagination_stop")[0]


@pytest.mark.asyncio
async def test_distinct_history_keeps_five_page_limit(caplog):
    pages = [[row(f"31-Dec-{year}")] for year in range(2025, 2019, -1)]
    profile, provider, calls = provider_for(pages)
    assert await provider.collect(profile) == []
    assert [page for page, _ in calls["pages"]] == [1, 2, 3, 4, 5]
    assert len(calls["xml_urls"]) == 4
    stop, = messages(caplog, "official_structured_financial_pagination_stop")
    assert "page=5 reason=PAGE_LIMIT" in stop
    selection, = messages(caplog, "official_structured_financial_selection")
    assert "reason=DOCUMENT_LIMIT" in selection


@pytest.mark.asyncio
async def test_repeated_page_stops_without_refetching_duplicate_documents(caplog):
    filing = row("31-Dec-2025")
    profile, provider, calls = provider_for([[filing], [dict(filing)]])
    await provider.collect(profile)
    assert [page for page, _ in calls["pages"]] == [1, 2]
    assert calls["xml_urls"] == [filing["xbrl"]]
    assert "reason=NO_NEW_FILINGS" in messages(caplog, "official_structured_financial_pagination_stop")[0]


def quarterly_rows():
    return [row(period, basis) for period in ("31-Dec-2025", "30-Sep-2025")
            for basis in ("Consolidated", "Standalone")]


@pytest.mark.asyncio
async def test_selection_identifies_a_discovered_annual_target_excluded_by_four_xml_limit(caplog):
    quarters = quarterly_rows()
    annual = row("31-Mar-2024")
    profile, provider, calls = provider_for([quarters + [annual]])
    await provider.collect(profile)
    assert len(calls["pages"]) == 2
    assert len(calls["xml_urls"]) == 4
    assert set(calls["xml_urls"]) == {filing["xbrl"] for filing in quarters}
    selection, = messages(caplog, "official_structured_financial_selection")
    skipped = selection.split("document_limit_skipped=", 1)[1]
    assert annual["xbrl"] in skipped and "'reporting_basis': 'CONSOLIDATED'" in skipped
    assert "reason=DOCUMENT_LIMIT" in selection
    assert len(messages(caplog, "official_structured_financial_parse")) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("tail", ["absent", "duplicate", "revision", "older"])
async def test_document_limit_only_reports_eligible_excluded_filings(tail, caplog):
    quarters = quarterly_rows()
    remaining = {"absent": [], "duplicate": [dict(quarters[-1])],
                 "revision": [{**row("31-Mar-2024"), "type_Sub": "Revised"}],
                 "older": [row("31-Mar-2023")] }[tail]
    profile, provider, calls = provider_for([quarters + remaining])
    await provider.collect(profile)
    assert len(calls["xml_urls"]) == 4
    selection, = messages(caplog, "official_structured_financial_selection")
    if tail == "older":
        assert "reason=DOCUMENT_LIMIT" in selection and remaining[0]["xbrl"] in selection
    else:
        assert "document_limit_skipped=[] reason=SELECTION_COMPLETE" in selection


@pytest.mark.asyncio
async def test_filing_failure_preserves_exception_without_logging_its_sensitive_message(caplog):
    filing = row("31-Dec-2025")
    filing["xbrl"] += f"?token={SECRET}#{SECRET}"
    profile, provider, _ = provider_for([[filing]])
    error = ValueError(f"provider body {SECRET}")
    provider.fetcher.fetch_network.side_effect = error
    with pytest.raises(ValueError) as caught:
        await provider.collect(profile)
    assert caught.value is error
    event, = messages(caplog, "official_structured_financial_filing_failed")
    assert "FETCH_OR_PARSE_ERROR error_type=ValueError" in event
    assert "'reported_period': '2025-12-31'" in event
    assert SECRET not in caplog.text


def test_persistence_reports_actual_upsert_acceptance_per_source_without_extra_writes(caplog):
    _, profile, _, _ = setup()
    facts = parse_nse_financial_xbrl(XML, profile, ROWS[0], retrieved_at=NOW)[:2]
    other_url = "https://nsearchives.nseindia.com/corporate/xbrl/other.xml"
    other = replace(facts[0], source_identity=other_url,
                    value=facts[0].value.model_copy(update={"source_url": other_url + f"?token={SECRET}"}))
    submitted = [*facts, other]
    upsert = Mock(side_effect=[True, False, False])
    repository = SimpleNamespace(_persistence=SimpleNamespace(upsert_financial_fact=upsert))
    assert ResearchRepository.persist_international_financial_facts(repository, submitted) == 1
    assert [call.args[0] for call in upsert.call_args_list] == submitted
    first, second = messages(caplog, "official_structured_financial_persist")
    assert "parsed_fact_count=2 persisted_fact_count=1 reason=PERSISTED" in first
    assert "parsed_fact_count=1 persisted_fact_count=0 reason=NO_FACTS_ACCEPTED_BY_PERSISTENCE" in second
    assert other_url in second and SECRET not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("declared_length,check", [(100, "HEADER"), (None, "STREAMING"), (5, "STREAMING")])
async def test_size_rejection_logs_limit_received_bytes_and_check_without_body_or_headers(declared_length, check, caplog):
    chunks = [b"%PDF", b"PRIVATE_DOCUMENT_TEXT"]
    delivered = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in chunks:
                delivered.append(chunk)
                yield chunk

    def handle(request):
        headers = {"content-type": "application/pdf"}
        if declared_length is not None:
            headers["content-length"] = str(declared_length)
        return httpx.Response(200, headers=headers, stream=Stream())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        fetcher = HttpResearchFetcher(Settings(research_max_retries=0), client)
        with pytest.raises(DocumentSizeLimitExceeded) as caught:
            await fetcher.fetch_network(
                f"https://nsearchives.nseindia.com/corporate/oversize.pdf?token={SECRET}",
                headers={"Authorization": f"Bearer {SECRET}"}, max_bytes=6)
    assert caught.value.max_bytes == 6
    assert caught.value.content_length == declared_length
    event, = messages(caplog, "fetch_size_rejected")
    received = 0 if check == "HEADER" else sum(map(len, chunks))
    assert f"configured_limit_bytes=6 declared_content_length={declared_length}" in event
    assert f"bytes_received={received} rejection_check={check}" in event
    assert len(delivered) == (0 if check == "HEADER" else 2)
    assert SECRET not in caplog.text and "PRIVATE_DOCUMENT_TEXT" not in caplog.text
