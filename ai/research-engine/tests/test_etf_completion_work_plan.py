"""Synthetic regression tests for ETF single-pass remediation planning."""
from copy import deepcopy

import pytest

from app.etf_completion_work_plan import build_etf_remediation_plan


def sample():
    return {
        "status": "READ_ONLY_ETF_COMPLETION_ACCEPTANCE",
        "universe_count": 359,
        "ready_for_analysis": 351,
        "blocked_for_analysis": 8,
        "as_of": "2026-11-04T17:00:11+00:00",
        "first_25_blocked": [
            {
                "ticker": "BANKCASE", "instrument_id": "uuid-bank",
                "blockers": [
                    {"requirement": "IDENTITY", "status": "TECHNICAL_FAILURE",
                     "reason": "NSE_METADATA_IDENTITY_CONFLICT"},
                    {"requirement": "MARKET_PRICE", "status": "TECHNICAL_FAILURE",
                     "reason": "NSE_ACQUISITION_FAILED:NSE_METADATA_IDENTITY_CONFLICT"},
                ],
            },
            *[
                {"ticker": ticker, "instrument_id": f"uuid-{ticker}",
                 "blockers": [
                     {"requirement": "MARKET_PRICE", "status": "TECHNICAL_FAILURE",
                      "reason": "NSE_ACQUISITION_FAILED:UNVERIFIED_OR_DUPLICATE_MARKET_SYMBOL"},
                     {"requirement": "TRADING_VOLUME", "status": "TECHNICAL_FAILURE",
                      "reason": "NSE_ACQUISITION_FAILED:UNVERIFIED_OR_DUPLICATE_MARKET_SYMBOL"},
                 ]}
                for ticker in ("LTGILTCASE", "NIFTYCASE", "NXT100CASE", "SILVERCASE", "SML100CASE")
            ],
            *[
                {"ticker": ticker, "instrument_id": f"uuid-{ticker}",
                 "blockers": [{"requirement": "MARKET_PRICE", "status": "MISSING",
                               "reason": None}]}
                for ticker in ("GSEC10ABSL", "LIQUIDBETA")
            ],
        ],
        "supporting_statuses": [
            {"requirement": "NAV", "status": "READY_STALE", "etfs": 349},
            {"requirement": "NAV", "status": "MISSING", "etfs": 10},
            {"requirement": "UNDERLYING_HOLDINGS", "status": "MISSING", "etfs": 359},
            {"requirement": "AUM", "status": "MISSING", "etfs": 359},
            {"requirement": "EXPENSE_RATIO", "status": "MISSING", "etfs": 359},
            {"requirement": "INDEX_CONSTITUENTS", "status": "NOT_IMPLEMENTED", "etfs": 359},
            {"requirement": "INDEX_VALUATION", "status": "NOT_IMPLEMENTED", "etfs": 359},
        ],
    }


def test_real_shape_plans_exactly_eight_without_untrusted_retry():
    result = build_etf_remediation_plan(sample())
    assert result["ready_for_analysis"] == 351
    assert result["blocked_for_analysis"] == 8
    assert result["mandatory_action_counts"] == {
        "P0_BOUNDED_OFFICIAL_MARKET_RETRY": 2,
        "P0_SOURCE_IDENTITY_REVIEW": 6,
    }
    assert len(result["mandatory_actions"]) == 8
    assert result["bounded_operator_quote_retry_batches"] == [{
        "instrument_ids": ["uuid-GSEC10ABSL", "uuid-LIQUIDBETA"],
        "endpoint": "POST /api/v1/etf-radar/evidence",
        "operator_authorization_required": True,
        "automatic_execution": False,
    }]
    assert all(not action["automated_resolution_approved"]
               for action in result["mandatory_actions"])
    assert result["source_onboarding"]["missing_or_stale_etfs_by_requirement"] == {
        "AUM": 359, "EXPENSE_RATIO": 359, "INDEX_CONSTITUENTS": 359,
        "INDEX_VALUATION": 359, "NAV": 359, "UNDERLYING_HOLDINGS": 359,
    }
    assert result["source_onboarding"]["not_implemented_etfs_by_requirement"] == {
        "INDEX_CONSTITUENTS": 359, "INDEX_VALUATION": 359,
    }
    assert result["automatic_market_retries_executed"] == 0
    assert result["provider_requests_executed"] == 0
    assert result["database_writes_executed"] == 0
    assert result["source_onboarding"]["sbi_nsdl_code_to_isin_approved"] is False


@pytest.mark.parametrize("mutation,reason", [
    (lambda r: r.update(status="OTHER"), "REQUIRES_APPLICATION_ACCEPTANCE"),
    (lambda r: r.update(ready_for_analysis=350), "COUNTS_INVALID"),
    (lambda r: r.pop("first_25_blocked"), "BLOCKER_SCOPE_INCOMPLETE"),
    (lambda r: r["first_25_blocked"].pop(), "BLOCKER_SCOPE_INCOMPLETE"),
    (lambda r: r["first_25_blocked"][1].update(instrument_id="uuid-bank"), "DUPLICATE_OR_INVALID_ID"),
    (lambda r: r["first_25_blocked"][0]["blockers"][0].update(requirement="TER"), "BLOCKER_INVALID"),
    (lambda r: r["supporting_statuses"][0].update(etfs=-1), "SUPPORTING_SCOPE_INVALID"),
    (lambda r: r["supporting_statuses"].append(
        {"requirement": "NAV", "status": "MISSING", "etfs": 11}), "SUPPORTING_DUPLICATE"),
])
def test_inconsistent_or_partial_acceptance_rejected(mutation, reason):
    r = deepcopy(sample())
    mutation(r)
    with pytest.raises(ValueError, match=reason):
        build_etf_remediation_plan(r)


def test_unexpected_market_failure_is_not_auto_retried():
    r = sample()
    r["first_25_blocked"][6]["blockers"][0].update(
        status="TECHNICAL_FAILURE", reason="NSE_HTTP_503"
    )
    r["first_25_blocked"][7]["blockers"][0].update(
        status="TECHNICAL_FAILURE", reason="NSE_HTTP_503"
    )
    result = build_etf_remediation_plan(r)
    assert result["mandatory_action_counts"]["P0_TECHNICAL_INVESTIGATION"] == 2
    assert not any(item["automated_resolution_approved"]
                   for item in result["mandatory_actions"])


def test_plan_requires_complete_blocker_scope_not_just_first_25():
    r = sample()
    r["universe_count"] = 380
    r["blocked_for_analysis"] = 29
    r["ready_for_analysis"] = 351
    with pytest.raises(ValueError, match="BLOCKER_SCOPE_INCOMPLETE"):
        build_etf_remediation_plan(r)
