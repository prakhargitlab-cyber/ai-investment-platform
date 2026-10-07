"""ETF Radar cycle tests: no company-specific acquisition, no silent
candidate loss, deterministic reproducible ranking, terminal accounting."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.etf_evidence import EtfAuthority, EtfFact, EtfListing, EtfMetric, EtfProvenance
from app.etf_opportunity_cycle import (
    ETF_RADAR_VERSION, EtfCycleDisposition, run_etf_radar_cycle,
)
from app.etf_rule_engine import EtfRecommendation
from app.models import DailyMarketBar, SourceMode
from app.persistence import SqliteResearchPersistence

NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
ID_A = UUID(int=101)
ID_B = UUID(int=102)


def provenance(**updates):
    data = dict(provider="NSE", source_type="EXCHANGE_ANNOUNCEMENT", source_identity="document:1",
        source_url="https://nsearchives.nseindia.com/document", authority=EtfAuthority.OFFICIAL_EXCHANGE,
        retrieved_at=NOW, reliability_level="LEVEL_A")
    return EtfProvenance(**(data | updates))


@pytest.fixture
def store(tmp_path):
    result = SqliteResearchPersistence(tmp_path / "cycle.sqlite")
    yield result
    result._connection.close()


def _row(instrument_id, symbol):
    return dict(globalInstrumentId=str(instrument_id), symbol=symbol, exchange="NSE", status="ACTIVE", assetType="ETF")


def _admit(store, instrument_id, symbol, *, with_price=True, with_history=False):
    store.save_etf_evidence(EtfListing(instrument_id=instrument_id, isin="IN0000000001" if instrument_id == ID_A
        else "IN0000000002", symbol=symbol, name=f"{symbol} ETF", provenance=provenance()))
    if with_price:
        store.save_etf_evidence(EtfFact(instrument_id=instrument_id, metric=EtfMetric.MARKET_PRICE,
            value=Decimal("100"), unit="INR", as_of_date=NOW.date(),
            provenance=provenance(provider="YAHOO_FINANCE", authority=EtfAuthority.SECONDARY)))
    if with_history:
        for i in range(260):
            store.upsert_daily_market_bar(DailyMarketBar(global_instrument_id=instrument_id,
                trading_date=NOW.date() - timedelta(days=260 - i), close=Decimal("100") + Decimal(i) * Decimal("0.05"),
                currency="INR", provider="NSE", source_mode=SourceMode.REAL,
                source_url="https://nsearchives.nseindia.com/bar", retrieved_at=NOW))


class TestNoSilentCandidateLoss:
    def test_every_row_produces_exactly_one_candidate_result(self, store):
        rows = [_row(ID_A, "NIFTYBEES"), _row(uuid4(), "RANDOM"), dict(symbol="NOID", exchange="NSE",
            status="ACTIVE", assetType="ETF")]
        result = run_etf_radar_cycle(rows, store, now=NOW)
        assert len(result.candidates) == len(rows)

    def test_equity_row_is_excluded_not_evaluated(self, store):
        row = dict(globalInstrumentId=str(ID_A), symbol="RELIANCE", exchange="NSE", status="ACTIVE", assetType="EQUITY")
        result = run_etf_radar_cycle([row], store, now=NOW)
        assert result.candidates[0].disposition == EtfCycleDisposition.INELIGIBLE
        assert result.candidates[0].reason == "FAILED_NSE_ETF_ACTIVE_PREFILTER"


class TestTerminalDispositions:
    def test_unverified_mapping_is_insufficient_data(self, store):
        result = run_etf_radar_cycle([_row(ID_A, "NIFTYBEES")], store, now=NOW)
        assert result.candidates[0].disposition == EtfCycleDisposition.INSUFFICIENT_DATA

    def test_admitted_candidate_is_evaluated(self, store):
        _admit(store, ID_A, "NIFTYBEES")
        result = run_etf_radar_cycle([_row(ID_A, "NIFTYBEES")], store, now=NOW)
        assert result.candidates[0].disposition == EtfCycleDisposition.EVALUATED
        assert result.candidates[0].rule_engine_result is not None
        assert result.candidates[0].recommendation is not None

    def test_technical_failure_from_broken_store_is_reported_not_raised(self, store, monkeypatch):
        _admit(store, ID_A, "NIFTYBEES")

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated persistence outage")
        monkeypatch.setattr(store, "load_daily_market_bars", _boom)
        result = run_etf_radar_cycle([_row(ID_A, "NIFTYBEES")], store, now=NOW)
        assert result.candidates[0].disposition == EtfCycleDisposition.TECHNICAL_FAILURE
        assert "simulated persistence outage" in result.candidates[0].reason


class TestRankingIsDeterministic:
    def test_only_evaluated_candidates_are_ranked(self, store):
        _admit(store, ID_A, "NIFTYBEES")
        result = run_etf_radar_cycle([_row(ID_A, "NIFTYBEES"), _row(ID_B, "GOLDBEES")], store, now=NOW)
        assert len(result.ranked) == 1
        assert result.ranked[0].global_instrument_id == ID_A
        assert result.ranked[0].rank == 1

    def test_repeated_runs_produce_identical_ranking(self, store):
        _admit(store, ID_A, "NIFTYBEES", with_history=True)
        _admit(store, ID_B, "GOLDBEES", with_history=True)
        rows = [_row(ID_A, "NIFTYBEES"), _row(ID_B, "GOLDBEES")]
        first = run_etf_radar_cycle(rows, store, now=NOW)
        second = run_etf_radar_cycle(rows, store, now=NOW)
        assert [c.global_instrument_id for c in first.ranked] == [c.global_instrument_id for c in second.ranked]

    def test_top_n_truncates(self, store):
        _admit(store, ID_A, "NIFTYBEES")
        _admit(store, ID_B, "GOLDBEES")
        result = run_etf_radar_cycle([_row(ID_A, "NIFTYBEES"), _row(ID_B, "GOLDBEES")], store, now=NOW, top_n=1)
        assert len(result.ranked) == 1


class TestExcludedByReason:
    def test_counts_are_tallied_and_evaluated_is_excluded_from_tally(self, store):
        _admit(store, ID_A, "NIFTYBEES")
        rows = [_row(ID_A, "NIFTYBEES"), _row(ID_B, "GOLDBEES"),
            dict(globalInstrumentId=str(uuid4()), symbol="X", exchange="BSE", status="ACTIVE", assetType="ETF")]
        result = run_etf_radar_cycle(rows, store, now=NOW)
        assert result.excluded_by_reason.get("UNVERIFIED_MAPPING") == 1
        # A non-NSE row fails the admission pre-filter before it ever reaches
        # admit_etf_candidate's own UNSUPPORTED_MARKET check.
        assert result.excluded_by_reason.get("FAILED_NSE_ETF_ACTIVE_PREFILTER") == 1
        assert "EVALUATED" not in result.excluded_by_reason


class TestVersionAndIdentity:
    def test_radar_version_is_pinned(self, store):
        result = run_etf_radar_cycle([], store, now=NOW)
        assert result.radar_version == ETF_RADAR_VERSION

    def test_cycle_id_is_generated_when_not_supplied(self, store):
        result = run_etf_radar_cycle([], store, now=NOW)
        assert isinstance(result.cycle_id, UUID)

    def test_explicit_cycle_id_is_preserved(self, store):
        given = uuid4()
        result = run_etf_radar_cycle([], store, now=NOW, cycle_id=given)
        assert result.cycle_id == given


class TestCyclePersistence:
    def test_save_and_load_latest_cycle_round_trips(self, store):
        from app.etf_opportunity_cycle import etf_cycle_result_to_dict
        _admit(store, ID_A, "NIFTYBEES", with_history=True)
        result = run_etf_radar_cycle([_row(ID_A, "NIFTYBEES")], store, now=NOW)
        payload = etf_cycle_result_to_dict(result)
        store.save_etf_radar_cycle(result.cycle_id, result.radar_version, result.correlation_id, result.as_of, payload)
        loaded = store.latest_etf_radar_cycle()
        assert loaded["cycle_id"] == str(result.cycle_id)
        assert loaded["radar_version"] == ETF_RADAR_VERSION
        assert len(loaded["candidates"]) == 1
        assert loaded["candidates"][0]["recommendation"] in {r.value for r in EtfRecommendation}

    def test_load_specific_cycle_by_id(self, store):
        from app.etf_opportunity_cycle import etf_cycle_result_to_dict
        result = run_etf_radar_cycle([], store, now=NOW)
        store.save_etf_radar_cycle(result.cycle_id, result.radar_version, result.correlation_id, result.as_of,
            etf_cycle_result_to_dict(result))
        loaded = store.etf_radar_cycle(result.cycle_id)
        assert loaded["cycle_id"] == str(result.cycle_id)

    def test_no_cycle_persisted_returns_none(self, store):
        assert store.latest_etf_radar_cycle() is None
        assert store.etf_radar_cycle(uuid4()) is None

    def test_latest_returns_most_recently_as_of_cycle(self, store):
        from app.etf_opportunity_cycle import etf_cycle_result_to_dict
        earlier = run_etf_radar_cycle([], store, now=NOW - timedelta(days=1))
        later = run_etf_radar_cycle([], store, now=NOW)
        store.save_etf_radar_cycle(earlier.cycle_id, earlier.radar_version, earlier.correlation_id, earlier.as_of,
            etf_cycle_result_to_dict(earlier))
        store.save_etf_radar_cycle(later.cycle_id, later.radar_version, later.correlation_id, later.as_of,
            etf_cycle_result_to_dict(later))
        assert store.latest_etf_radar_cycle()["cycle_id"] == str(later.cycle_id)
