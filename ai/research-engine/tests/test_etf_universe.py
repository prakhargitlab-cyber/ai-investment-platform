"""ETF universe/admission tests: canonical-identity-only, no name/symbol
heuristics, no silent candidate loss."""
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest

from app.etf_evidence import EtfAuthority, EtfFact, EtfListing, EtfMetric, EtfProvenance
from app.etf_universe import (
    CanonicalEtfUniverse, EtfAdmissionOutcome, admit_etf_candidate, admit_etf_candidates, nse_etfs,
)
from app.persistence import SqliteResearchPersistence

NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
ID = UUID(int=42)


def provenance(**updates):
    data = dict(provider="NSE", source_type="EXCHANGE_ANNOUNCEMENT", source_identity="document:1",
        source_url="https://nsearchives.nseindia.com/document", authority=EtfAuthority.OFFICIAL_EXCHANGE,
        retrieved_at=NOW, reliability_level="LEVEL_A")
    return EtfProvenance(**(data | updates))


@pytest.fixture
def store(tmp_path):
    result = SqliteResearchPersistence(tmp_path / "universe.sqlite")
    yield result
    result._connection.close()


def _etf_row(**overrides):
    defaults = dict(globalInstrumentId=str(ID), symbol="NIFTYBEES", exchange="NSE", status="ACTIVE", assetType="ETF")
    return defaults | overrides


def _admit_listing(store, instrument_id=ID):
    store.save_etf_evidence(EtfListing(instrument_id=instrument_id, isin="IN0000000001", symbol="NIFTYBEES",
        name="Nifty BeES ETF", provenance=provenance()))


def _admit_price(store, instrument_id=ID):
    store.save_etf_evidence(EtfFact(instrument_id=instrument_id, metric=EtfMetric.MARKET_PRICE, value=Decimal("100"),
        unit="INR", as_of_date=NOW.date(), provenance=provenance(provider="YAHOO_FINANCE", authority=EtfAuthority.SECONDARY)))


class TestNseEtfsPrefilter:
    def test_keeps_only_nse_active_etf_rows(self):
        rows = [_etf_row(), _etf_row(exchange="BSE"), _etf_row(status="INACTIVE"), _etf_row(assetType="EQUITY")]
        assert nse_etfs(rows) == [rows[0]]

    def test_equity_row_never_passes_etf_prefilter(self):
        rows = [_etf_row(assetType="EQUITY", symbol="RELIANCE")]
        assert nse_etfs(rows) == []


class TestAdmitEtfCandidate:
    def test_not_etf_is_ineligible(self, store):
        decision = admit_etf_candidate(_etf_row(assetType="EQUITY"), store)
        assert decision.outcome == EtfAdmissionOutcome.INELIGIBLE
        assert decision.reason == "NOT_ETF"

    def test_inactive_is_ineligible(self, store):
        decision = admit_etf_candidate(_etf_row(status="INACTIVE"), store)
        assert decision.outcome == EtfAdmissionOutcome.INELIGIBLE
        assert decision.reason == "INACTIVE"

    def test_non_nse_is_unsupported_not_ineligible(self, store):
        decision = admit_etf_candidate(_etf_row(exchange="BSE"), store)
        assert decision.outcome == EtfAdmissionOutcome.UNSUPPORTED
        assert decision.reason == "UNSUPPORTED_MARKET"

    def test_invalid_identity_is_ineligible(self, store):
        decision = admit_etf_candidate(_etf_row(globalInstrumentId="not-a-uuid"), store)
        assert decision.outcome == EtfAdmissionOutcome.INELIGIBLE
        assert decision.reason == "INVALID_IDENTITY"

    def test_missing_identity_is_ineligible(self, store):
        row = _etf_row()
        del row["globalInstrumentId"]
        decision = admit_etf_candidate(row, store)
        assert decision.outcome == EtfAdmissionOutcome.INELIGIBLE
        assert decision.reason == "INVALID_IDENTITY"

    def test_no_verified_listing_is_insufficient_data_not_ineligible(self, store):
        decision = admit_etf_candidate(_etf_row(), store)
        assert decision.outcome == EtfAdmissionOutcome.INSUFFICIENT_DATA
        assert decision.reason == "UNVERIFIED_MAPPING"
        assert decision.global_instrument_id == ID

    def test_verified_listing_without_price_is_insufficient_data(self, store):
        _admit_listing(store)
        decision = admit_etf_candidate(_etf_row(), store)
        assert decision.outcome == EtfAdmissionOutcome.INSUFFICIENT_DATA
        assert decision.reason == "MISSING_USABLE_PRICE"

    def test_verified_listing_and_price_is_admitted(self, store):
        _admit_listing(store)
        _admit_price(store)
        decision = admit_etf_candidate(_etf_row(), store)
        assert decision.outcome == EtfAdmissionOutcome.ADMITTED
        assert decision.global_instrument_id == ID

    def test_listing_for_a_different_instrument_does_not_admit(self, store):
        _admit_listing(store, instrument_id=uuid4())
        decision = admit_etf_candidate(_etf_row(), store)
        assert decision.outcome == EtfAdmissionOutcome.INSUFFICIENT_DATA
        assert decision.reason == "UNVERIFIED_MAPPING"


class TestAdmitEtfCandidates:
    def test_every_row_gets_exactly_one_decision_nothing_silently_dropped(self, store):
        _admit_listing(store)
        _admit_price(store)
        rows = [_etf_row(), _etf_row(exchange="BSE"), _etf_row(assetType="EQUITY"), _etf_row(status="INACTIVE")]
        decisions = admit_etf_candidates(rows, store)
        assert len(decisions) == len(rows)
        assert [d.outcome for d in decisions] == [
            EtfAdmissionOutcome.ADMITTED, EtfAdmissionOutcome.INELIGIBLE,
            EtfAdmissionOutcome.INELIGIBLE, EtfAdmissionOutcome.INELIGIBLE,
        ]

    def test_prefilter_rejections_carry_an_explicit_reason(self, store):
        decisions = admit_etf_candidates([_etf_row(assetType="EQUITY")], store)
        assert decisions[0].reason == "FAILED_NSE_ETF_ACTIVE_PREFILTER"


class TestCanonicalEtfUniverse:
    @pytest.mark.asyncio
    async def test_queries_assettype_etf_and_paginates(self):
        calls = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(dict(request.url.params))
            page = int(request.url.params["page"])
            assert request.url.params["assetType"] == "ETF"
            assert request.url.params["status"] == "ACTIVE"
            if page == 0:
                return httpx.Response(200, json={"instruments": [_etf_row()], "totalElements": 2})
            return httpx.Response(200, json={"instruments": [_etf_row(symbol="GOLDBEES")], "totalElements": 2})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        universe = CanonicalEtfUniverse(client, "https://portfolio-service.internal")
        rows = await universe.active_global_etfs()
        assert len(rows) == 2
        assert all(row["assetType"] == "ETF" for row in rows)
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_never_requests_assettype_equity(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.params["assetType"] != "EQUITY"
            return httpx.Response(200, json={"instruments": [], "totalElements": 0})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        rows = await CanonicalEtfUniverse(client, "https://portfolio-service.internal").active_global_etfs()
        assert rows == []
