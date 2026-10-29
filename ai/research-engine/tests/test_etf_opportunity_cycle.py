"""ETF Radar cycle tests: no company-specific acquisition, no silent
candidate loss, deterministic reproducible ranking, terminal accounting."""
import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from threading import RLock
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.etf_evidence import EtfAuthority, EtfFact, EtfListing, EtfMetric, EtfProvenance
from app.etf_opportunity_cycle import (
    ETF_HISTORY_MAX_STALE_DAYS, ETF_HISTORY_REQUIRED_SESSIONS, ETF_RADAR_VERSION, EtfCycleDisposition,
    evaluate_admitted_etf, run_etf_radar_cycle, run_etf_radar_cycle_async, _ensure_etf_historical_coverage,
)
from app.etf_rule_engine import EtfRecommendation
from app.etf_universe import EtfAdmissionDecision, EtfAdmissionOutcome
from app.models import DailyMarketBar, SourceMode
from app.nse_historical_daily import NseHistoricalResult
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
    store.save_etf_evidence(EtfFact(instrument_id=instrument_id, metric=EtfMetric.TRADING_VOLUME,
        value=Decimal("500000"), unit="shares", as_of_date=NOW.date(),
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


def _bars_for_window(instrument_id, start, end):
    days = (end - start).days + 1
    return [DailyMarketBar(global_instrument_id=instrument_id, trading_date=start + timedelta(days=i),
        close=Decimal("100"), currency="INR", provider="NSE", source_mode=SourceMode.REAL,
        source_url="https://nsearchives.nseindia.com/bar", retrieved_at=NOW) for i in range(days)]


def _fake_repository(store):
    async def dispatch(fn):
        return fn()
    return SimpleNamespace(persistence=store, _persistence_worker_lock=RLock(), _run_blocking_persistence=dispatch)


class TestHistoricalCoverageAcquisition:
    """_ensure_etf_historical_coverage(): bounded, gap-only, resumable
    acquisition that gates historical scoring. Exercised directly against
    the real sqlite store so persistence and re-read are genuine."""

    @pytest.mark.asyncio
    async def test_zero_bars_triggers_bounded_acquisition(self, store):
        _admit(store, ID_A, "GOLDBEES")

        async def populate(instrument_id, *, start, end, identity_headers, correlation_id):
            bars = _bars_for_window(instrument_id, start, end)
            for bar in bars:
                store.upsert_daily_market_bar(bar)
            return NseHistoricalResult(instrument_id, start, end, status="SUCCESS", persisted_rows=len(bars))

        jobs = SimpleNamespace(settings=SimpleNamespace(nse_historical_request_window_days=250),
            populate_daily_bars=AsyncMock(side_effect=populate))
        failures = await _ensure_etf_historical_coverage(_fake_repository(store), [_row(ID_A, "GOLDBEES")],
            jobs=jobs, identity_headers={}, correlation_id="c1")
        assert jobs.populate_daily_bars.await_count == 1
        assert failures == {}
        # Successful acquisition must be re-read, not merely trusted from
        # the provider's own attempt report.
        usable = store.load_daily_market_bars({ID_A})
        assert len(usable) >= 200

    @pytest.mark.asyncio
    async def test_sufficient_existing_history_skips_acquisition(self, store):
        # Coverage freshness is judged against the real clock (there is no
        # `now` parameter to this function -- see TestHistoricalCoverageStalenessIsNotCountOnly),
        # so "current" bars for this test must be anchored to today, not the
        # module's fixed NOW fixture.
        _admit(store, ID_A, "GOLDBEES")
        real_today = datetime.now(timezone.utc).date()
        for i in range(260):
            store.upsert_daily_market_bar(DailyMarketBar(global_instrument_id=ID_A,
                trading_date=real_today - timedelta(days=260 - i), close=Decimal("100") + Decimal(i) * Decimal("0.05"),
                currency="INR", provider="NSE", source_mode=SourceMode.REAL,
                source_url="https://nsearchives.nseindia.com/bar", retrieved_at=NOW))
        jobs = SimpleNamespace(settings=SimpleNamespace(nse_historical_request_window_days=30),
            populate_daily_bars=AsyncMock())
        failures = await _ensure_etf_historical_coverage(_fake_repository(store), [_row(ID_A, "GOLDBEES")],
            jobs=jobs, identity_headers={}, correlation_id="c1")
        jobs.populate_daily_bars.assert_not_called()
        assert failures == {}

    @pytest.mark.asyncio
    async def test_partial_history_acquires_only_the_missing_range(self, store):
        _admit(store, ID_A, "GOLDBEES")
        oldest_existing = NOW.date() - timedelta(days=50)
        for i in range(50):
            store.upsert_daily_market_bar(DailyMarketBar(global_instrument_id=ID_A,
                trading_date=oldest_existing + timedelta(days=i), close=Decimal("100"), currency="INR",
                provider="NSE", source_mode=SourceMode.REAL, source_url="https://nsearchives.nseindia.com/bar",
                retrieved_at=NOW))
        windows_called = []

        async def populate(instrument_id, *, start, end, identity_headers, correlation_id):
            windows_called.append((start, end))
            bars = _bars_for_window(instrument_id, start, end)
            for bar in bars:
                store.upsert_daily_market_bar(bar)
            return NseHistoricalResult(instrument_id, start, end, status="SUCCESS", persisted_rows=len(bars))

        jobs = SimpleNamespace(settings=SimpleNamespace(nse_historical_request_window_days=100),
            populate_daily_bars=AsyncMock(side_effect=populate))
        await _ensure_etf_historical_coverage(_fake_repository(store), [_row(ID_A, "GOLDBEES")], jobs=jobs,
            identity_headers={}, correlation_id="c1")
        assert windows_called, "expected at least one acquisition window"
        # The acquired window must end strictly before the oldest bar we
        # already had -- the existing 50 days are never re-requested.
        assert windows_called[0][1] < oldest_existing

    @pytest.mark.asyncio
    async def test_provider_technical_failure_is_recorded_not_disguised_and_not_persisted(self, store):
        _admit(store, ID_A, "GOLDBEES")

        async def populate(instrument_id, *, start, end, identity_headers, correlation_id):
            return NseHistoricalResult(instrument_id, start, end, status="UNAVAILABLE",
                failure_reason="HISTORICAL_TIMEOUT")

        jobs = SimpleNamespace(settings=SimpleNamespace(nse_historical_request_window_days=30),
            populate_daily_bars=AsyncMock(side_effect=populate))
        failures = await _ensure_etf_historical_coverage(_fake_repository(store), [_row(ID_A, "GOLDBEES")],
            jobs=jobs, identity_headers={}, correlation_id="c1")
        assert failures == {ID_A: "HISTORICAL_TIMEOUT"}
        assert store.load_daily_market_bars({ID_A}) == []

    @pytest.mark.asyncio
    async def test_identity_mismatch_fails_closed(self, store):
        _admit(store, ID_A, "GOLDBEES")

        async def populate(instrument_id, *, start, end, identity_headers, correlation_id):
            return NseHistoricalResult(instrument_id, start, end, status="UNAVAILABLE",
                failure_reason="IDENTITY_MISMATCH")

        jobs = SimpleNamespace(settings=SimpleNamespace(nse_historical_request_window_days=30),
            populate_daily_bars=AsyncMock(side_effect=populate))
        failures = await _ensure_etf_historical_coverage(_fake_repository(store), [_row(ID_A, "GOLDBEES")],
            jobs=jobs, identity_headers={}, correlation_id="c1")
        assert failures == {ID_A: "IDENTITY_MISMATCH"}

    @pytest.mark.asyncio
    async def test_genuine_short_history_is_not_a_technical_failure(self, store):
        """Provider succeeds technically but the listing itself has no
        history in the window -- this is ordinary absence, not a defect,
        and must fall through to INSUFFICIENT_DATA rather than being
        recorded as a failure."""
        _admit(store, ID_A, "GOLDBEES")

        async def populate(instrument_id, *, start, end, identity_headers, correlation_id):
            return NseHistoricalResult(instrument_id, start, end, status="UNAVAILABLE",
                failure_reason="NO_VALID_HISTORY", persisted_rows=0)

        jobs = SimpleNamespace(settings=SimpleNamespace(nse_historical_request_window_days=30),
            populate_daily_bars=AsyncMock(side_effect=populate))
        failures = await _ensure_etf_historical_coverage(_fake_repository(store), [_row(ID_A, "GOLDBEES")],
            jobs=jobs, identity_headers={}, correlation_id="c1")
        assert failures == {}

    @pytest.mark.asyncio
    async def test_repeated_scans_do_not_duplicate_bars(self, store):
        _admit(store, ID_A, "GOLDBEES")

        async def populate(instrument_id, *, start, end, identity_headers, correlation_id):
            bars = _bars_for_window(instrument_id, start, end)
            for bar in bars:
                store.upsert_daily_market_bar(bar)
            return NseHistoricalResult(instrument_id, start, end, status="SUCCESS", persisted_rows=len(bars))

        jobs = SimpleNamespace(settings=SimpleNamespace(nse_historical_request_window_days=250),
            populate_daily_bars=AsyncMock(side_effect=populate))
        repository = _fake_repository(store)
        await _ensure_etf_historical_coverage(repository, [_row(ID_A, "GOLDBEES")], jobs=jobs,
            identity_headers={}, correlation_id="c1")
        first_count = len(store.load_daily_market_bars({ID_A}))
        first_calls = jobs.populate_daily_bars.await_count
        await _ensure_etf_historical_coverage(repository, [_row(ID_A, "GOLDBEES")], jobs=jobs,
            identity_headers={}, correlation_id="c1")
        assert jobs.populate_daily_bars.await_count == first_calls, "already-sufficient coverage must not re-fetch"
        assert len(store.load_daily_market_bars({ID_A})) == first_count

    @pytest.mark.asyncio
    async def test_one_instruments_acquisition_failure_does_not_block_another(self, store):
        _admit(store, ID_A, "GOLDBEES")
        _admit(store, ID_B, "NIFTYBEES")

        async def populate(instrument_id, *, start, end, identity_headers, correlation_id):
            if instrument_id == ID_A:
                return NseHistoricalResult(instrument_id, start, end, status="UNAVAILABLE",
                    failure_reason="IDENTITY_MISMATCH")
            bars = _bars_for_window(instrument_id, start, end)
            for bar in bars:
                store.upsert_daily_market_bar(bar)
            return NseHistoricalResult(instrument_id, start, end, status="SUCCESS", persisted_rows=len(bars))

        jobs = SimpleNamespace(settings=SimpleNamespace(nse_historical_request_window_days=250),
            populate_daily_bars=AsyncMock(side_effect=populate))
        failures = await _ensure_etf_historical_coverage(_fake_repository(store),
            [_row(ID_A, "GOLDBEES"), _row(ID_B, "NIFTYBEES")], jobs=jobs, identity_headers={}, correlation_id="c1")
        assert failures == {ID_A: "IDENTITY_MISMATCH"}
        assert len(store.load_daily_market_bars({ID_B})) >= 200


class TestEvaluateAdmittedEtfHistoricalShortCircuit:
    def test_historical_failure_reason_short_circuits_before_touching_bars_or_scoring(self, store, monkeypatch):
        _admit(store, ID_A, "GOLDBEES", with_history=True)
        decision = EtfAdmissionDecision(ID_A, "GOLDBEES", EtfAdmissionOutcome.ADMITTED, "ADMITTED",
            _row(ID_A, "GOLDBEES"))

        def _boom(*args, **kwargs):
            raise AssertionError("bars must not be read once acquisition already failed for this instrument")
        monkeypatch.setattr(store, "load_daily_market_bars", _boom)
        result = evaluate_admitted_etf(decision, store, now=NOW, historical_failure_reason="IDENTITY_MISMATCH")
        assert result.disposition == EtfCycleDisposition.TECHNICAL_FAILURE
        assert result.reason == "HISTORICAL_ACQUISITION_FAILED:IDENTITY_MISMATCH"
        assert result.rule_engine_result is None
        assert result.recommendation is None

    def test_absent_historical_failure_reason_behaves_exactly_as_before(self, store):
        _admit(store, ID_A, "GOLDBEES")
        decision = EtfAdmissionDecision(ID_A, "GOLDBEES", EtfAdmissionOutcome.ADMITTED, "ADMITTED",
            _row(ID_A, "GOLDBEES"))
        result = evaluate_admitted_etf(decision, store, now=NOW)
        assert result.disposition == EtfCycleDisposition.EVALUATED


class TestHistoricalAcquisitionIsOptInAndWired:
    """The new acquisition pre-step must never run on paths that predate
    this feature, and must thread its result into exactly the affected
    candidate when it does run."""

    @pytest.mark.asyncio
    async def test_unverified_or_jobless_path_never_calls_acquisition(self, store, monkeypatch):
        import app.etf_opportunity_cycle as cycle_module
        spy = AsyncMock(return_value={})
        monkeypatch.setattr(cycle_module, "_ensure_etf_historical_coverage", spy)
        repository = _fake_repository(store)
        await run_etf_radar_cycle_async(repository, SimpleNamespace(), candidate_ids=[ID_A], jobs=SimpleNamespace())
        spy.assert_not_called()

        await run_etf_radar_cycle_async(repository, SimpleNamespace(), candidate_ids=[ID_A],
            verify_canonical_candidates=False, jobs=None)
        spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_acquisition_failure_marks_only_the_affected_candidate(self, store, monkeypatch):
        import app.etf_ingestion as ingestion_module
        # run_etf_radar_cycle_async does not thread a fixed `now` through to
        # run_etf_radar_cycle (pre-existing, unchanged behavior), so ID_B's
        # evidence must be fresh against the real clock, not the module's
        # fixed NOW, or it is correctly treated as stale by readiness checks
        # unrelated to the thing this test is isolating.
        real_now = datetime.now(timezone.utc)
        _admit(store, ID_A, "GOLDBEES")
        store.save_etf_evidence(EtfListing(instrument_id=ID_B, isin="IN0000000002", symbol="NIFTYBEES",
            name="NIFTYBEES ETF", provenance=provenance(retrieved_at=real_now)))
        store.save_etf_evidence(EtfFact(instrument_id=ID_B, metric=EtfMetric.MARKET_PRICE, value=Decimal("100"),
            unit="INR", as_of_date=real_now.date(),
            provenance=provenance(provider="YAHOO_FINANCE", authority=EtfAuthority.SECONDARY, retrieved_at=real_now)))
        store.save_etf_evidence(EtfFact(instrument_id=ID_B, metric=EtfMetric.TRADING_VOLUME, value=Decimal("500000"),
            unit="shares", as_of_date=real_now.date(),
            provenance=provenance(provider="YAHOO_FINANCE", authority=EtfAuthority.SECONDARY, retrieved_at=real_now)))
        for i in range(260):
            store.upsert_daily_market_bar(DailyMarketBar(global_instrument_id=ID_B,
                trading_date=real_now.date() - timedelta(days=260 - i), close=Decimal("100") + Decimal(i) * Decimal("0.05"),
                currency="INR", provider="NSE", source_mode=SourceMode.REAL,
                source_url="https://nsearchives.nseindia.com/bar", retrieved_at=real_now))
        rows = [_row(ID_A, "GOLDBEES") | {"isin": "IN0000000001", "primarySymbol": "GOLDBEES"},
                _row(ID_B, "NIFTYBEES") | {"isin": "IN0000000002", "primarySymbol": "NIFTYBEES"}]
        monkeypatch.setattr(ingestion_module, "canonical_etfs", AsyncMock(return_value=rows))
        monkeypatch.setattr(ingestion_module, "etf_preflight", lambda *a, **k: {"status": "READY"})
        jobs = SimpleNamespace()
        repository = _fake_repository(store)

        async def fake_coverage(repository, rows, *, jobs, identity_headers, correlation_id):
            return {ID_A: "IDENTITY_MISMATCH"}
        import app.etf_opportunity_cycle as cycle_module
        monkeypatch.setattr(cycle_module, "_ensure_etf_historical_coverage", fake_coverage)

        outcome = await run_etf_radar_cycle_async(repository, SimpleNamespace(), candidate_ids=[ID_A, ID_B],
            verify_canonical_candidates=True, identity_headers={}, correlation_id="c1", jobs=jobs)
        loaded = store.etf_radar_cycle(UUID(outcome["cycle_id"]))
        candidates = {c["global_instrument_id"]: c for c in loaded["candidates"]}
        assert candidates[str(ID_A)]["disposition"] == "TECHNICAL_FAILURE"
        assert candidates[str(ID_A)]["reason"] == "HISTORICAL_ACQUISITION_FAILED:IDENTITY_MISMATCH"
        assert candidates[str(ID_B)]["disposition"] == "EVALUATED"


class TestHistoricalCoverageStalenessIsNotCountOnly:
    """200 usable sessions that stopped advancing months ago must not be
    treated as permanently done -- nothing else in this pipeline ever
    appends a bar going forward on its own, so a count-only check would
    silently freeze an instrument's history forever after its first
    successful top-up."""

    @pytest.mark.asyncio
    async def test_deep_but_stale_history_still_triggers_forward_catch_up(self, store):
        _admit(store, ID_A, "GOLDBEES")
        stale_newest = NOW.date() - timedelta(days=ETF_HISTORY_MAX_STALE_DAYS + 30)
        for i in range(ETF_HISTORY_REQUIRED_SESSIONS + 10):
            store.upsert_daily_market_bar(DailyMarketBar(global_instrument_id=ID_A,
                trading_date=stale_newest - timedelta(days=(ETF_HISTORY_REQUIRED_SESSIONS + 10) - i),
                close=Decimal("100"), currency="INR", provider="NSE", source_mode=SourceMode.REAL,
                source_url="https://nsearchives.nseindia.com/bar", retrieved_at=NOW))
        windows_called = []

        async def populate(instrument_id, *, start, end, identity_headers, correlation_id):
            windows_called.append((start, end))
            bars = _bars_for_window(instrument_id, start, end)
            for bar in bars:
                store.upsert_daily_market_bar(bar)
            return NseHistoricalResult(instrument_id, start, end, status="SUCCESS", persisted_rows=len(bars))

        jobs = SimpleNamespace(settings=SimpleNamespace(nse_historical_request_window_days=30),
            populate_daily_bars=AsyncMock(side_effect=populate))
        failures = await _ensure_etf_historical_coverage(_fake_repository(store), [_row(ID_A, "GOLDBEES")],
            jobs=jobs, identity_headers={}, correlation_id="c1")
        assert windows_called, "stale-but-deep coverage must still trigger acquisition"
        # Must walk FORWARD from the newest existing bar, not re-request the
        # depth that is already covered.
        assert windows_called[0][0] == stale_newest  # seeding loop's newest bar is stale_newest - 1 day
        assert failures == {}

    @pytest.mark.asyncio
    async def test_deep_and_current_history_skips_acquisition(self, store):
        _admit(store, ID_A, "GOLDBEES")
        real_today = datetime.now(timezone.utc).date()
        for i in range(ETF_HISTORY_REQUIRED_SESSIONS + 10):
            store.upsert_daily_market_bar(DailyMarketBar(global_instrument_id=ID_A,
                trading_date=real_today - timedelta(days=(ETF_HISTORY_REQUIRED_SESSIONS + 10) - i),
                close=Decimal("100"), currency="INR", provider="NSE", source_mode=SourceMode.REAL,
                source_url="https://nsearchives.nseindia.com/bar", retrieved_at=NOW))
        jobs = SimpleNamespace(settings=SimpleNamespace(nse_historical_request_window_days=30),
            populate_daily_bars=AsyncMock())
        failures = await _ensure_etf_historical_coverage(_fake_repository(store), [_row(ID_A, "GOLDBEES")],
            jobs=jobs, identity_headers={}, correlation_id="c1")
        jobs.populate_daily_bars.assert_not_called()
        assert failures == {}

    @pytest.mark.asyncio
    async def test_one_cycle_from_empty_cannot_reach_200_sessions_and_reports_insufficient_not_success(self, store):
        """3 windows x 30 calendar days is ~90 calendar days -- nowhere near
        the ~280 calendar days of NSE trading needed for 200 sessions. A
        single cycle from zero must end up INSUFFICIENT_DATA, never a false
        SUCCESS, and must not be misreported as a technical failure either."""
        _admit(store, ID_A, "GOLDBEES")

        async def populate(instrument_id, *, start, end, identity_headers, correlation_id):
            bars = _bars_for_window(instrument_id, start, end)
            for bar in bars:
                store.upsert_daily_market_bar(bar)
            return NseHistoricalResult(instrument_id, start, end, status="SUCCESS", persisted_rows=len(bars))

        jobs = SimpleNamespace(settings=SimpleNamespace(nse_historical_request_window_days=30),
            populate_daily_bars=AsyncMock(side_effect=populate))
        failures = await _ensure_etf_historical_coverage(_fake_repository(store), [_row(ID_A, "GOLDBEES")],
            jobs=jobs, identity_headers={}, correlation_id="c1")
        assert jobs.populate_daily_bars.await_count == 3, "the full 3-window budget is used, never more"
        assert failures == {}
        usable = store.load_daily_market_bars({ID_A})
        assert len(usable) < ETF_HISTORY_REQUIRED_SESSIONS

    @pytest.mark.asyncio
    async def test_a_second_cycle_resumes_backward_and_can_eventually_reach_200_sessions(self, store):
        _admit(store, ID_A, "GOLDBEES")

        async def populate(instrument_id, *, start, end, identity_headers, correlation_id):
            bars = _bars_for_window(instrument_id, start, end)
            for bar in bars:
                store.upsert_daily_market_bar(bar)
            return NseHistoricalResult(instrument_id, start, end, status="SUCCESS", persisted_rows=len(bars))

        jobs = SimpleNamespace(settings=SimpleNamespace(nse_historical_request_window_days=30),
            populate_daily_bars=AsyncMock(side_effect=populate))
        repository = _fake_repository(store)
        for _ in range(6):
            await _ensure_etf_historical_coverage(repository, [_row(ID_A, "GOLDBEES")], jobs=jobs,
                identity_headers={}, correlation_id="c1")
            if len(store.load_daily_market_bars({ID_A})) >= ETF_HISTORY_REQUIRED_SESSIONS:
                break
        usable = store.load_daily_market_bars({ID_A})
        assert len(usable) >= ETF_HISTORY_REQUIRED_SESSIONS, "repeated authorized scans must eventually reach depth"
        dates = sorted(bar.trading_date for bar in usable)
        assert len(dates) == len(set(dates)), "no duplicate trading dates across repeated scans"


class TestHistoricalCoverageTimeoutTruthfulness:
    """A cancellation that lands in populate_daily_bars' own existing
    post-persistence pacing delay (inside its try/finally, after the bars
    were already durably written) must never be reported as a failure for
    coverage that, once re-verified, is actually already there.

    These exercise the REAL asyncio.wait_for timeout machinery (with the
    module's timeout monkeypatched to a few milliseconds) rather than a
    manually raised CancelledError, because wait_for only converts a
    cancellation it caused itself into TimeoutError -- it does not
    reinterpret an arbitrary CancelledError raised by the awaited
    coroutine for some unrelated reason. A fake that just raises
    CancelledError would test a different (and less representative)
    scenario, so it is deliberately not used here."""

    @pytest.mark.asyncio
    async def test_timeout_after_bars_were_actually_persisted_is_not_reported_as_a_failure(self, store, monkeypatch):
        import app.etf_opportunity_cycle as cycle_module
        monkeypatch.setattr(cycle_module, "ETF_HISTORY_ACQUISITION_TIMEOUT_SECONDS", 0.05)
        _admit(store, ID_A, "GOLDBEES")
        real_today = datetime.now(timezone.utc).date()
        for i in range(ETF_HISTORY_REQUIRED_SESSIONS - 5):
            store.upsert_daily_market_bar(DailyMarketBar(global_instrument_id=ID_A,
                trading_date=real_today - timedelta(days=(ETF_HISTORY_REQUIRED_SESSIONS - 5) - i),
                close=Decimal("100"), currency="INR", provider="NSE", source_mode=SourceMode.REAL,
                source_url="https://nsearchives.nseindia.com/bar", retrieved_at=NOW))

        async def populate(instrument_id, *, start, end, identity_headers, correlation_id):
            # Simulate populate_daily_bars' own real shape: persistence
            # completes successfully and only afterward (in its own
            # try/finally pacing sleep, per app/market_data_population.py)
            # does the real wait_for timeout actually cancel it.
            bars = _bars_for_window(instrument_id, start, end)
            for bar in bars:
                store.upsert_daily_market_bar(bar)
            await asyncio.sleep(10)

        jobs = SimpleNamespace(settings=SimpleNamespace(nse_historical_request_window_days=30),
            populate_daily_bars=AsyncMock(side_effect=populate))
        failures = await _ensure_etf_historical_coverage(_fake_repository(store), [_row(ID_A, "GOLDBEES")],
            jobs=jobs, identity_headers={}, correlation_id="c1")
        assert len(store.load_daily_market_bars({ID_A})) >= ETF_HISTORY_REQUIRED_SESSIONS
        assert failures == {}, "coverage that is truthfully sufficient after re-read must not be a reported failure"

    @pytest.mark.asyncio
    async def test_timeout_with_no_persisted_progress_is_still_reported(self, store, monkeypatch):
        import app.etf_opportunity_cycle as cycle_module
        monkeypatch.setattr(cycle_module, "ETF_HISTORY_ACQUISITION_TIMEOUT_SECONDS", 0.05)
        _admit(store, ID_A, "GOLDBEES")

        async def populate(instrument_id, *, start, end, identity_headers, correlation_id):
            await asyncio.sleep(10)

        jobs = SimpleNamespace(settings=SimpleNamespace(nse_historical_request_window_days=30),
            populate_daily_bars=AsyncMock(side_effect=populate))
        failures = await _ensure_etf_historical_coverage(_fake_repository(store), [_row(ID_A, "GOLDBEES")],
            jobs=jobs, identity_headers={}, correlation_id="c1")
        assert failures == {ID_A: "HISTORICAL_ACQUISITION_TIMEOUT"}


class TestSameInstrumentPartialSuccessThenFailure:
    @pytest.mark.asyncio
    async def test_a_successful_window_is_kept_even_if_the_next_window_for_the_same_instrument_fails(self, store):
        _admit(store, ID_A, "GOLDBEES")
        calls = []

        async def populate(instrument_id, *, start, end, identity_headers, correlation_id):
            calls.append((start, end))
            if len(calls) == 1:
                bars = _bars_for_window(instrument_id, start, end)
                for bar in bars:
                    store.upsert_daily_market_bar(bar)
                return NseHistoricalResult(instrument_id, start, end, status="SUCCESS", persisted_rows=len(bars))
            return NseHistoricalResult(instrument_id, start, end, status="UNAVAILABLE",
                failure_reason="HISTORICAL_HTTP_500")

        jobs = SimpleNamespace(settings=SimpleNamespace(nse_historical_request_window_days=30),
            populate_daily_bars=AsyncMock(side_effect=populate))
        failures = await _ensure_etf_historical_coverage(_fake_repository(store), [_row(ID_A, "GOLDBEES")],
            jobs=jobs, identity_headers={}, correlation_id="c1")
        assert len(calls) == 2, "must stop at the first failure, not keep spending the window budget"
        assert failures == {ID_A: "HISTORICAL_HTTP_500"}
        # The first window's bars must survive the second window's failure.
        assert len(store.load_daily_market_bars({ID_A})) == 30


def _admit_fresh(store, instrument_id, symbol, *, with_history=False):
    """Like _admit(), but anchored to the real clock: run_etf_radar_cycle_async
    (unlike the synchronous run_etf_radar_cycle most tests in this file call
    directly with now=NOW) threads no fixed `now` through, so evidence meant
    to read as currently fresh in these async tests must be dated against
    datetime.now(timezone.utc), not the module's fixed NOW fixture."""
    real_now = datetime.now(timezone.utc)
    store.save_etf_evidence(EtfListing(instrument_id=instrument_id,
        isin="IN0000000001" if instrument_id == ID_A else "IN0000000002", symbol=symbol, name=f"{symbol} ETF",
        provenance=provenance(retrieved_at=real_now)))
    store.save_etf_evidence(EtfFact(instrument_id=instrument_id, metric=EtfMetric.MARKET_PRICE,
        value=Decimal("100"), unit="INR", as_of_date=real_now.date(),
        provenance=provenance(provider="YAHOO_FINANCE", authority=EtfAuthority.SECONDARY, retrieved_at=real_now)))
    store.save_etf_evidence(EtfFact(instrument_id=instrument_id, metric=EtfMetric.TRADING_VOLUME,
        value=Decimal("500000"), unit="shares", as_of_date=real_now.date(),
        provenance=provenance(provider="YAHOO_FINANCE", authority=EtfAuthority.SECONDARY, retrieved_at=real_now)))
    if with_history:
        for i in range(260):
            store.upsert_daily_market_bar(DailyMarketBar(global_instrument_id=instrument_id,
                trading_date=real_now.date() - timedelta(days=260 - i), close=Decimal("100") + Decimal(i) * Decimal("0.05"),
                currency="INR", provider="NSE", source_mode=SourceMode.REAL,
                source_url="https://nsearchives.nseindia.com/bar", retrieved_at=real_now))


class TestBoundedCycleScreeningEnrichment:
    """The bounded (controlled-cycles) path must surface the SAME trusted,
    already-persisted fund-quality fields the full-universe cycle already
    publishes (app.etf_full_universe), by reusing app.etf_screening's own
    screening_evidence()/enrich_candidate() verbatim -- no new computation,
    no acquisition, no change to scoring or full-universe behavior."""

    @pytest.mark.asyncio
    async def test_trading_volume_and_actual_drawdown_metric_appear_from_persisted_evidence(self, store, monkeypatch):
        import app.etf_ingestion as ingestion_module
        _admit_fresh(store, ID_A, "GOLDBEES", with_history=True)
        rows = [_row(ID_A, "GOLDBEES") | {"isin": "IN0000000001", "primarySymbol": "GOLDBEES"}]
        monkeypatch.setattr(ingestion_module, "canonical_etfs", AsyncMock(return_value=rows))
        monkeypatch.setattr(ingestion_module, "etf_preflight", lambda *a, **k: {"status": "READY"})
        repository = _fake_repository(store)
        outcome = await run_etf_radar_cycle_async(repository, SimpleNamespace(), candidate_ids=[ID_A],
            verify_canonical_candidates=True, identity_headers={}, correlation_id="c1", jobs=None)
        loaded = store.etf_radar_cycle(UUID(outcome["cycle_id"]))
        candidate = loaded["candidates"][0]
        assert candidate["disposition"] == "EVALUATED"

        volume_fact = store.etf_facts(ID_A, EtfMetric.TRADING_VOLUME)[0]
        bars = store.load_daily_market_bars({ID_A})
        from app.etf_metrics import compute_etf_deterministic_metrics, EtfMetricId
        actual_drawdown = compute_etf_deterministic_metrics(ID_A, bars).get(EtfMetricId.MAX_DRAWDOWN)

        metrics = candidate["screening_metrics"]
        assert metrics["trading_volume"]["quality"] == "VALID"
        assert Decimal(metrics["trading_volume"]["value"]) == volume_fact.value
        assert metrics["drawdown"]["quality"] == "VALID"
        assert Decimal(metrics["drawdown"]["value"]) == actual_drawdown.value
        # Must be the real metric, never the normalized 0-100 rule-engine score.
        drawdown_factor = next(f for f in candidate["rule_engine_result"]["factor_results"]
                                if f["factor"] == "RISK_DRAWDOWN_VOLATILITY")
        assert Decimal(metrics["drawdown"]["value"]) != Decimal(str(drawdown_factor["normalized_score"]))
        assert candidate["history_observations"] == len(bars)

    @pytest.mark.asyncio
    async def test_evidence_that_was_never_acquired_stays_unavailable_not_zero_or_fabricated(self, store, monkeypatch):
        import app.etf_ingestion as ingestion_module
        _admit_fresh(store, ID_A, "GOLDBEES", with_history=True)  # no expense_ratio/tracking/holdings facts seeded
        rows = [_row(ID_A, "GOLDBEES") | {"isin": "IN0000000001", "primarySymbol": "GOLDBEES"}]
        monkeypatch.setattr(ingestion_module, "canonical_etfs", AsyncMock(return_value=rows))
        monkeypatch.setattr(ingestion_module, "etf_preflight", lambda *a, **k: {"status": "READY"})
        repository = _fake_repository(store)
        outcome = await run_etf_radar_cycle_async(repository, SimpleNamespace(), candidate_ids=[ID_A],
            verify_canonical_candidates=True, identity_headers={}, correlation_id="c1", jobs=None)
        loaded = store.etf_radar_cycle(UUID(outcome["cycle_id"]))
        candidate = loaded["candidates"][0]
        metrics = candidate["screening_metrics"]
        for name in ("expense_ratio", "aum", "tracking_error", "tracking_difference", "bid_ask_spread"):
            assert metrics[name]["quality"] == "UNAVAILABLE"
            assert metrics[name]["value"] is None
        assert candidate["fund_house"] == "UNKNOWN"
        assert candidate["replication_method"] == "UNKNOWN"
        assert candidate["holdings_coverage"] is None

    def test_synchronous_run_etf_radar_cycle_is_unaffected_by_the_async_enrichment_step(self, store):
        """Backward compatibility: direct synchronous callers (most existing
        tests in this file) never go through run_etf_radar_cycle_async, so
        their result dict must be exactly as before -- no screening_metrics
        key appears unless the async wrapper's enrichment step ran."""
        from app.etf_opportunity_cycle import etf_cycle_result_to_dict
        _admit(store, ID_A, "GOLDBEES", with_history=True)
        result = run_etf_radar_cycle([_row(ID_A, "GOLDBEES")], store, now=NOW)
        payload = etf_cycle_result_to_dict(result)
        assert "screening_metrics" not in payload["candidates"][0]

    @pytest.mark.asyncio
    async def test_a_technical_failure_candidate_is_still_enriched_with_whatever_evidence_exists(self, store, monkeypatch):
        """Enrichment is unconditional on disposition (matching
        app.etf_full_universe's own behavior) -- it only ever reveals
        already-trusted evidence, so a TECHNICAL_FAILURE candidate (e.g.
        from a historical-acquisition failure) still shows what market
        evidence it does have, rather than hiding it behind the failure."""
        import app.etf_ingestion as ingestion_module
        _admit_fresh(store, ID_A, "GOLDBEES")
        rows = [_row(ID_A, "GOLDBEES") | {"isin": "IN0000000001", "primarySymbol": "GOLDBEES"}]
        monkeypatch.setattr(ingestion_module, "canonical_etfs", AsyncMock(return_value=rows))
        monkeypatch.setattr(ingestion_module, "etf_preflight", lambda *a, **k: {"status": "READY"})

        async def fake_coverage(repository, rows, *, jobs, identity_headers, correlation_id):
            return {ID_A: "IDENTITY_MISMATCH"}
        import app.etf_opportunity_cycle as cycle_module
        monkeypatch.setattr(cycle_module, "_ensure_etf_historical_coverage", fake_coverage)

        repository = _fake_repository(store)
        outcome = await run_etf_radar_cycle_async(repository, SimpleNamespace(), candidate_ids=[ID_A],
            verify_canonical_candidates=True, identity_headers={}, correlation_id="c1", jobs=SimpleNamespace())
        loaded = store.etf_radar_cycle(UUID(outcome["cycle_id"]))
        candidate = loaded["candidates"][0]
        assert candidate["disposition"] == "TECHNICAL_FAILURE"
        # TRADING_VOLUME was seeded by _admit() and is unrelated to the
        # historical-acquisition failure -- it must still surface.
        assert candidate["screening_metrics"]["trading_volume"]["quality"] == "VALID"
