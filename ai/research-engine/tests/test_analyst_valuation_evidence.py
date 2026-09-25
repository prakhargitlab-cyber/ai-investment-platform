"""Task 6/7/8: real analyst target-price consensus wired into
evidence_state.valuation.fair_value / bull_target.

Ground rules under test (see GlobalOpportunityOrchestrator._analyst_valuation_evidence
in app/global_opportunity_orchestration.py):
  - No new valuation formula and no score-to-price conversion: a real, eligible
    PublicAnalyst record is the only source. Absent/ineligible evidence leaves
    evidence_state.valuation empty, exactly as before this wiring existed.
  - Eligibility: finite positive target price, currency matches the instrument's
    real trading currency, freshness == 'FRESH'.
  - analyst_count is provenance-only; it never gates eligibility.
  - target_low_price is never mapped to `invalidation` -- ranges() does not gain
    that meaning from this wiring.
  - Wiring is deterministic and does not disturb the pre-existing short-term
    range computation.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.models import (
    ProvenancedValue,
    PublicAnalyst,
    StructuredInstrumentResolution,
    StructuredMarketSnapshot,
    StructuredMarketSnapshotRecord,
)
from app.recommendation_engine import ranges
from test_global_opportunity_orchestration import setup, NOW
from test_global_opportunity_ranker import inputs


def _analyst(**overrides):
    fields = dict(
        target_median_price=Decimal("100"), target_high_price=Decimal("130"),
        target_low_price=Decimal("60"), analyst_count=12, currency="INR",
        provider="YAHOO_FINANCE", provider_instrument_id="EXAMPLE.NS",
        source_name="Yahoo Finance", source_url="https://finance.test/example",
        as_of=NOW, retrieved_at=NOW, freshness="FRESH",
    )
    fields.update(overrides)
    return PublicAnalyst(**fields)


def _pv(value, unit=None):
    return ProvenancedValue(value=value, unit=unit, source_url="https://finance.test/example",
        source_name="Yahoo Finance", retrieved_at=NOW, as_of_date=NOW)


def _structured_record(gid: UUID, *, provider="YAHOO_FINANCE", last_analyst_at=NOW,
                        median=Decimal("100"), high=Decimal("130"), low=Decimal("60"),
                        currency="INR", analyst_count=Decimal("12")):
    facts = {}
    if median is not None:
        facts["publicAnalystTargetMedianPrice"] = _pv(median, currency)
    if high is not None:
        facts["publicAnalystTargetHighPrice"] = _pv(high, currency)
    if low is not None:
        facts["publicAnalystTargetLowPrice"] = _pv(low, currency)
    if analyst_count is not None:
        facts["publicAnalystCount"] = _pv(analyst_count)
    return StructuredMarketSnapshotRecord(
        instrument_id=gid, provider=provider, provider_instrument_id="EXAMPLE",
        exchange="NSE", mic="NSE", currency=currency, source_url="https://finance.test/example",
        retrieved_at=NOW, persisted_at=NOW, last_analyst_at=last_analyst_at,
        last_success_at=NOW, last_provider_attempt_at=NOW, acquisition_status="SUCCESS",
        snapshot=StructuredMarketSnapshot(
            resolution=StructuredInstrumentResolution(
                provider="NSE", provider_ticker="EXAMPLE.NS", company_name="Example Ltd.",
                exchange="NSE", currency=currency, confidence=0.9, resolved_at=NOW),
            status="SUCCESS", retrieved_at=NOW, market_as_of=NOW,
            source_url="https://finance.test/example", facts=facts,
        ),
    )


def test_A_fresh_median_sets_fair_value(monkeypatch):
    service, *_ = setup(monkeypatch, 1)
    result = service._analyst_valuation_evidence(_analyst(target_high_price=None), "INR")
    assert result["fair_value"] == 100.0
    assert "bull_target" not in result
    assert result["provenance"]["fields_used"] == ["target_median_price"]


def test_B_median_and_high_sets_bull_target(monkeypatch):
    service, *_ = setup(monkeypatch, 1)
    result = service._analyst_valuation_evidence(_analyst(), "INR")
    assert result["fair_value"] == 100.0
    assert result["bull_target"] == 130.0
    assert result["provenance"]["fields_used"] == ["target_median_price", "target_high_price"]
    assert result["provenance"]["analyst_count"] == 12
    assert result["provenance"]["provider"] == "YAHOO_FINANCE"
    assert result["provenance"]["freshness"] == "FRESH"


def test_C_no_target_price_still_insufficient(monkeypatch):
    service, *_ = setup(monkeypatch, 1)
    assert service._analyst_valuation_evidence(
        _analyst(target_median_price=None, target_high_price=None), "INR") == {}
    assert service._analyst_valuation_evidence(None, "INR") == {}


def test_D_stale_analyst_evidence_not_used(monkeypatch):
    service, *_ = setup(monkeypatch, 1)
    assert service._analyst_valuation_evidence(_analyst(freshness="STALE"), "INR") == {}


def test_E_non_positive_target_price_not_used(monkeypatch):
    service, *_ = setup(monkeypatch, 1)
    assert service._analyst_valuation_evidence(
        _analyst(target_median_price=Decimal("0"), target_high_price=None), "INR") == {}
    assert service._analyst_valuation_evidence(
        _analyst(target_median_price=Decimal("-10"), target_high_price=None), "INR") == {}


def test_F_currency_mismatch_not_used(monkeypatch):
    service, *_ = setup(monkeypatch, 1)
    assert service._analyst_valuation_evidence(_analyst(currency="USD"), "INR") == {}
    # No instrument currency on record (e.g. technical snapshot never resolved one)
    # means currency-compatibility can never be confirmed, so evidence is skipped too.
    assert service._analyst_valuation_evidence(_analyst(), None) == {}


def test_G_no_score_to_price_conversion(monkeypatch):
    service, *_ = setup(monkeypatch, 1)
    enriched, rule = inputs(1)
    for area in rule.area_scores:
        area.raw_score = 100  # A maxed-out VALUATION area score alone must not yield a price.
    enriched.technical_feature_snapshot.currency = "INR"
    evidence = service._evidence_state(enriched, rule, None)
    assert evidence["valuation"] == {}


@pytest.mark.asyncio
async def test_H_wiring_is_deterministic_across_repeated_runs(monkeypatch):
    service, rows, pairs, _ = setup(monkeypatch, 1)
    pairs[UUID(int=1)][0].technical_feature_snapshot.currency = "INR"
    record = _structured_record(UUID(int=1))
    fetch = AsyncMock(return_value={UUID(int=1): [record]})
    service.repository.structured_market_snapshots_for_instruments = fetch
    first = await service.run(rows, as_of=NOW)
    second = await service.run(rows, as_of=NOW)
    assert first.model_dump(exclude={"generated_at"}) == second.model_dump(exclude={"generated_at"})
    valuation = first.top_n[0].evidence_state["valuation"]
    assert valuation["fair_value"] == 100.0 and valuation["bull_target"] == 130.0
    # Fetched once per run, batched -- not once per candidate.
    assert fetch.await_count == 2


@pytest.mark.asyncio
async def test_prefers_nse_structured_over_yahoo_and_skips_ineligible_candidates(monkeypatch):
    service, rows, pairs, _ = setup(monkeypatch, 2)
    for n in (1, 2):
        pairs[UUID(int=n)][0].technical_feature_snapshot.currency = "INR"
    nse_record = _structured_record(UUID(int=1), provider="NSE_STRUCTURED", median=Decimal("111"), high=None)
    yahoo_record = _structured_record(UUID(int=1), provider="YAHOO_FINANCE", median=Decimal("999"), high=None)
    stale_record = _structured_record(UUID(int=2), last_analyst_at=NOW - timedelta(days=30))
    service.repository.structured_market_snapshots_for_instruments = AsyncMock(return_value={
        UUID(int=1): [yahoo_record, nse_record], UUID(int=2): [stale_record],
    })
    result = await service.run(rows, as_of=NOW)
    by_id = {e.global_instrument_id: e for e in result.top_n}
    assert by_id[UUID(int=1)].evidence_state["valuation"]["fair_value"] == 111.0
    assert by_id[UUID(int=2)].evidence_state["valuation"] == {}


def test_I_short_term_ranges_unaffected_by_valuation_evidence(monkeypatch):
    service, *_ = setup(monkeypatch, 1)
    technical = {"support_level": 90, "resistance_level": 110, "atr14": 2}
    without_valuation = ranges({"evidence_state": {"technical": technical, "valuation": {}},
                                 "current_price": 95})
    with_valuation = ranges({"evidence_state": {"technical": technical,
                              "valuation": service._analyst_valuation_evidence(_analyst(), "INR")},
                              "current_price": 95})
    short_keys = ["short_entry_low", "short_entry_high", "short_target_1", "short_target_2", "short_invalidation"]
    assert {k: without_valuation[k] for k in short_keys} == {k: with_valuation[k] for k in short_keys}
    assert with_valuation["long_fair_value"] == 100.0 and without_valuation["long_fair_value"] is None


def test_J_analyst_low_target_never_becomes_invalidation(monkeypatch):
    service, *_ = setup(monkeypatch, 1)
    valuation = service._analyst_valuation_evidence(_analyst(), "INR")
    assert "invalidation" not in valuation
    # target_low_price (60) is well inside what would have been treated as a
    # valid long_invalidation (< fair_value * .8 == 80) had it been mapped --
    # confirm ranges() still reports no long-term invalidation level at all.
    result = ranges({"evidence_state": {"technical": {}, "valuation": valuation}, "current_price": 95})
    assert result["long_invalidation"] is None
