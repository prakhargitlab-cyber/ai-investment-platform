"""NSE integrated-filing XBRL whose contexts are identified by trading Symbol.

Production root cause (Equity FULL, 2026-10-29): NSE integrated financial
filings for several companies (e.g. SEJALLTD, VERTOZ, NPST, MOREPENLAB)
identify every xbrli:context entity by the company's NSE trading symbol,
under the scheme ``http://www.sebi.gov.in/in-capmkt/Symbol`` (2026 taxonomy)
or ``http://www.bseindia.com/in-capmkt/Symbol`` (2025 taxonomy), instead of
by the numeric ScripCode. parse_nse_financial_xbrl only accepted ScripCode
schemes, silently dropped every context, and reported NO_ELIGIBLE_CONTEXTS
with zero facts -- even though the filings carry clean, non-dimensional
consolidated revenue/PAT/balance-sheet totals. Zero official facts then
surfaced as PARSER_FAILED:NO_SUPPORTED_FINANCIAL_FACTS (a permanent verdict)
for BUSINESS_QUALITY_FACTS / GROWTH_FACTS / QUARTERLY_FINANCIALS.

Fixtures are derived deterministically from the verified GHCL filings by
rewriting ONLY the context identifiers; no financial value is invented.
"""
import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from app.structured_financial import parse_nse_financial_xbrl
# fixed_readiness_clock is autouse: readiness replays at the fixtures' capture date.
from test_nse_structured_financial import NOW, fixed_readiness_clock, setup  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures" / "nse_financial_xbrl"
ROWS = json.loads((FIXTURES / "filings.json").read_text())
_SCRIP_IDENTIFIER = re.compile(
    rb'<xbrli:identifier scheme="http://www\.sebi\.gov\.in/in-capmkt/ScripCode">500171</xbrli:identifier>')


def _symbol_identified(xml: bytes, scheme: bytes, symbol: bytes = b"GHCL") -> bytes:
    rewritten, count = _SCRIP_IDENTIFIER.subn(
        b'<xbrli:identifier scheme="' + scheme + b'">' + symbol + b"</xbrli:identifier>", xml)
    assert count > 0, "fixture no longer contains the expected ScripCode identifiers"
    return rewritten


def _projection(facts):
    return sorted((f.key.metric, f.key.period_end, f.key.period_type, f.key.reporting_basis,
                   f.value.value, f.value.unit) for f in facts)


@pytest.mark.parametrize("fixture,row", [("ghcl_20260630.xml", 0), ("ghcl_20260331.xml", 1)])
@pytest.mark.parametrize("scheme", [b"http://www.sebi.gov.in/in-capmkt/Symbol",
                                    b"http://www.bseindia.com/in-capmkt/Symbol"])
def test_symbol_identified_contexts_parse_the_same_facts_as_scripcode_contexts(fixture, row, scheme):
    _, profile, _, _ = setup()
    original = (FIXTURES / fixture).read_bytes()
    baseline = parse_nse_financial_xbrl(original, profile, ROWS[row], retrieved_at=NOW)
    assert baseline, "the verified ScripCode fixture must itself parse"
    parsed = parse_nse_financial_xbrl(_symbol_identified(original, scheme), profile, ROWS[row], retrieved_at=NOW)
    assert _projection(parsed) == _projection(baseline)
    assert {f.source_provider for f in parsed} == {"NSE"}


def test_symbol_identified_annual_filing_yields_annual_revenue_and_pat():
    """The annual (Q4) filing is what BUSINESS_QUALITY_FACTS / GROWTH_FACTS need."""
    _, profile, _, _ = setup()
    xml = _symbol_identified((FIXTURES / "ghcl_20260331.xml").read_bytes(),
                             b"http://www.sebi.gov.in/in-capmkt/Symbol")
    facts = parse_nse_financial_xbrl(xml, profile, ROWS[1], retrieved_at=NOW)
    annual = {f.key.metric for f in facts if f.key.period_type == "ANNUAL" and f.key.period_end == "2026-03-31"}
    assert {"revenue", "pat"} <= annual


@pytest.mark.parametrize("scheme,symbol", [
    # Another company's symbol under an accepted scheme: identity must not match.
    (b"http://www.sebi.gov.in/in-capmkt/Symbol", b"OTHER"),
    # The ScripCode value presented under a Symbol scheme is not the symbol.
    (b"http://www.sebi.gov.in/in-capmkt/Symbol", b"500171"),
    # An unrecognised identifier scheme stays unsupported.
    (b"http://example.com/in-capmkt/Symbol", b"GHCL"),
])
def test_symbol_identified_contexts_still_fail_closed_on_identity(scheme, symbol):
    _, profile, _, _ = setup()
    xml = _symbol_identified((FIXTURES / "ghcl_20260630.xml").read_bytes(), scheme, symbol)
    assert parse_nse_financial_xbrl(xml, profile, ROWS[0], retrieved_at=NOW) == []


def test_document_level_identity_checks_still_apply_to_symbol_identified_filings():
    _, profile, _, _ = setup()
    xml = _symbol_identified((FIXTURES / "ghcl_20260630.xml").read_bytes(),
                             b"http://www.sebi.gov.in/in-capmkt/Symbol")
    with pytest.raises(ValueError, match="IDENTITY_MISMATCH"):
        parse_nse_financial_xbrl(xml.replace(b">INE539A01019<", b">INE000000000<"),
                                 profile, ROWS[0], retrieved_at=NOW)


@pytest.mark.asyncio
async def test_symbol_identified_filings_satisfy_quarterly_readiness_end_to_end():
    """Through the real executor/persistence/readiness path: Symbol-identified
    filings now persist official NSE facts, so QUARTERLY_FINANCIALS is not left
    with zero official facts (the PARSER_FAILED:NO_SUPPORTED_FINANCIAL_FACTS
    precondition) and no PDF fallback is needed."""
    from app.research_readiness import ResearchReadinessService
    from app.research_readiness_runtime import RepositoryResearchReadinessAdapter
    from test_research_readiness_runtime import _target

    repo, profile, provider, executor = setup()
    original = provider.fetcher.fetch_network.side_effect

    async def symbol_identified_fetch(url, **kwargs):
        network = await original(url, **kwargs)
        network.content = _symbol_identified(network.content, b"http://www.sebi.gov.in/in-capmkt/Symbol")
        return network

    provider.fetcher.fetch_network.side_effect = symbol_identified_fetch
    outcome = await executor.execute_primary(profile.instrument_id, [_target("QUARTERLY_FINANCIALS")],
                                             jurisdiction="INDIA", correlation_id=None, identity_headers=None)
    assert not outcome.failures
    repo.refresh_targeted_categories.assert_not_awaited()
    readiness = ResearchReadinessService(RepositoryResearchReadinessAdapter(repo)).assess(
        profile.instrument_id, jurisdiction="INDIA", now=NOW)
    assert readiness.for_requirement("QUARTERLY_FINANCIALS").status == "READY_FRESH"
    revenue = next(f for f in repo.financial_facts_for(profile.instrument_id)
                   if f.key.metric == "revenue" and f.key.period_end == "2026-06-30")
    assert revenue.value.value == Decimal("7742600000") and revenue.value.unit == "INR"
    assert revenue.source_provider == "NSE" and revenue.source_identity == ROWS[0]["xbrl"]
