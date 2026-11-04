"""Contract tests for full-universe ETF readiness completion (synthetic only)."""
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

import app.etf_completion_acceptance as completion
from app.etf_readiness import (
    ETC3_MANDATORY, ETC3_SUPPORTING, ETC3_CONTEXTUAL,
    EtfReadinessStatus, EtfRequirementStatus,
)

NOW = datetime(2026, 11, 4, tzinfo=timezone.utc)


def canonical(n=2):
    return [
        {"globalInstrumentId": str(UUID(int=i + 1)), "isin": f"INF9999999{i}",
         "primarySymbol": f"TESTETF{i}", "primaryExchange": "NSE",
         "assetType": "ETF", "status": "ACTIVE"}
        for i in range(n)
    ]


def result_for(*, ready):
    statuses = []
    for name in sorted(ETC3_MANDATORY):
        status = (EtfReadinessStatus.READY_FRESH if ready or name != "MARKET_PRICE"
                  else EtfReadinessStatus.MISSING)
        statuses.append(EtfRequirementStatus(
            requirement=name, role="MANDATORY", applicability="APPLICABLE",
            status=status,
        ))
    for name in sorted(ETC3_SUPPORTING):
        statuses.append(EtfRequirementStatus(
            requirement=name, role="SUPPORTING", applicability="APPLICABLE",
            status=(EtfReadinessStatus.NOT_IMPLEMENTED if name == "INDEX_VALUATION"
                    else EtfReadinessStatus.MISSING),
        ))
    for name in sorted(ETC3_CONTEXTUAL):
        statuses.append(EtfRequirementStatus(
            requirement=name, role="CONTEXTUAL", applicability="APPLICABLE",
            status=EtfReadinessStatus.MISSING,
        ))
    return statuses, SimpleNamespace(ready_for_analysis=ready)


def test_acceptance_uses_existing_readiness_results_and_keeps_gaps(monkeypatch):
    called = []

    def fake_evaluate(store, uuid, *, now):
        called.append((uuid, now))
        return result_for(ready=uuid.int == 1)

    monkeypatch.setattr(completion, "evaluate_etf_readiness", fake_evaluate)
    out = completion.evaluate_etf_completion(canonical(), object(), now=NOW)
    assert len(called) == 2
    assert out["ready_for_analysis"] == 1
    assert out["blocked_for_analysis"] == 1
    assert out["mandatory_blockers"] == [
        {"requirement": "MARKET_PRICE", "status": "MISSING", "etfs": 1}
    ]
    assert out["supporting_not_fresh"]["UNDERLYING_HOLDINGS"] == 2
    assert out["supporting_not_fresh"]["EXPENSE_RATIO"] == 2
    assert out["supporting_not_fresh"]["INDEX_VALUATION"] == 2
    assert out["source_approval_granted"] is False
    assert out["ter_ingestion_authorized"] is False
    assert out["production_evidence_written"] is False
    assert out["radar_cycle_executed"] is False


@pytest.mark.parametrize("rows,reason", [
    ([], "SCOPE_INVALID"),
    (canonical(501), "SCOPE_INVALID"),
    (canonical(1) * 2, "CANONICAL_AMBIGUOUS"),
    ([{**canonical(1)[0], "assetType": "EQUITY"}], "CANONICAL_ROW_INVALID"),
    ([{**canonical(1)[0], "globalInstrumentId": "oops"}], "UUID_INVALID"),
    ([{**canonical(1)[0], "primarySymbol": ""}], "ROW_INVALID"),
])
def test_invalid_universe_fails_closed(rows, reason):
    with pytest.raises(ValueError, match=reason):
        completion.evaluate_etf_completion(rows, object(), now=NOW)


def test_incomplete_status_contract_fails_closed(monkeypatch):
    monkeypatch.setattr(completion, "evaluate_etf_readiness",
                        lambda *args, **kwargs: ([], SimpleNamespace(ready_for_analysis=True)))
    with pytest.raises(ValueError, match="CONTRACT_INCOMPLETE"):
        completion.evaluate_etf_completion(canonical(1), object(), now=NOW)


def test_inconsistent_summary_fails_closed(monkeypatch):
    monkeypatch.setattr(completion, "evaluate_etf_readiness",
                        lambda *args, **kwargs: (
                            result_for(ready=True)[0],
                            SimpleNamespace(ready_for_analysis=False),
                        ))
    with pytest.raises(ValueError, match="SUMMARY_CONFLICT"):
        completion.evaluate_etf_completion(canonical(1), object(), now=NOW)


def test_cache_reuses_evidence_calls_instead_of_refetching():
    class Reader:
        def __init__(self):
            self.count = {}

        def mark(self, label):
            self.count[label] = self.count.get(label, 0) + 1
            return []

        def etf_listings(self):
            return self.mark("listings")

        def etf_attempts(self, key):
            return self.mark("attempts")

        def etf_facts(self, key, metric=None):
            return self.mark("facts")

        def etf_navs(self, key):
            return self.mark("navs")

        def etf_holdings(self, key):
            return self.mark("holdings")

        def load_daily_market_bars(self, ids, **kw):
            return self.mark("bars")

    raw = Reader()
    store = completion._ReadThroughEtfCache(raw)
    for _ in range(4):
        store.etf_listings()
        store.etf_attempts(UUID(int=1))
        store.etf_facts(UUID(int=1), "MARKET_PRICE")
        store.etf_navs(UUID(int=1))
        store.etf_holdings(UUID(int=1))
        store.load_daily_market_bars({UUID(int=1)})
    assert raw.count == {
        "listings": 1, "attempts": 1, "facts": 1,
        "navs": 1, "holdings": 1, "bars": 1,
    }
