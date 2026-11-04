"""Fail-closed, one-pass ETF remediation plan from real application readiness.

This is deliberately not an automatic market-data retry mechanism or a
document-source approval. Never turn a source name/symbol guess into an ISIN,
and never treat MISSING/NOT_IMPLEMENTED as a numeric value or approval.
"""
from collections import Counter
from datetime import datetime
from typing import Any

WORK_PLAN_VERSION = "ETF_REMEDIATION_V1"
MAX_BLOCKERS = 500
SUPPORTED_REQUIREMENTS = frozenset({
    "IDENTITY", "MARKET_PRICE", "TRADING_VOLUME",
})
VERIFICATION_REQUIRED = frozenset({
    "NSE_METADATA_IDENTITY_CONFLICT",
    "UNVERIFIED_OR_DUPLICATE_MARKET_SYMBOL",
    "UNVERIFIED_MARKET_WATCH_SYMBOL",
    "DUPLICATE_MARKET_WATCH_SYMBOL",
    "AMBIGUOUS_OFFICIAL_SYMBOL",
    "UNSUPPORTED_NSE_LISTING",
    "ETF_LISTING_IDENTITY_CONFLICT",
})
OFFICIAL_SOURCE_SUPPORTING = (
    "NAV", "UNDERLYING_HOLDINGS", "EXPENSE_RATIO", "AUM",
    "TRACKING_ERROR", "TRACKING_DIFFERENCE", "BENCHMARK_INDEX",
    "AMC_FUND_HOUSE", "REPLICATION_METHOD", "FUND_INCEPTION_DATE",
    "BID_ASK_SPREAD", "INDEX_CONSTITUENTS", "INDEX_VALUATION",
)
NO_AUTOMATIC_SOURCE_APPROVAL = True


def _source_issue(blocker: dict) -> bool:
    reason = blocker.get("reason") or ""
    if not isinstance(reason, str):
        raise ValueError("ETF_REMEDIATION_INVALID_REASON")
    code = reason.removeprefix("NSE_ACQUISITION_FAILED:").split(":", 1)[0]
    return code in VERIFICATION_REQUIRED or code.startswith("NSE_METADATA_IDENTITY_CONFLICT")


def _disposition(blockers: list[dict]) -> tuple[str, str]:
    # Any identity ambiguity wins over missing quote evidence; no retries
    # until NSE master/canonical and current exchange metadata are reconciled.
    if any(_source_issue(blocker) for blocker in blockers):
        return ("P0_SOURCE_IDENTITY_REVIEW",
                "Require independently verified NSE master, market-watch and "
                "metadata identity evidence. Do not select duplicate rows, "
                "change ticker aliases or persist speculative facts.")
    if any(blocker["requirement"] in ("MARKET_PRICE", "TRADING_VOLUME")
           and blocker["status"] == "MISSING" for blocker in blockers):
        return ("P0_BOUNDED_OFFICIAL_MARKET_RETRY",
                "Eligible only for an explicitly authorized <=5-instrument "
                "canonical NSE request using existing ingest_quotes; retry "
                "outcome may remain MISSING and must be reported.")
    return ("P0_TECHNICAL_INVESTIGATION",
            "Preserve original failure and inspect official acquisition "
            "attempts before authorizing any further run.")


def build_etf_remediation_plan(acceptance: dict) -> dict[str, Any]:
    """Compile operator actions, not instrument mappings or ingestion results.

    The first_25_blocked subset is sufficient only when blocked count <=25.
    Fail closed for larger outages instead of silently planning a subset.
    """
    if acceptance.get("status") != "READ_ONLY_ETF_COMPLETION_ACCEPTANCE":
        raise ValueError("ETF_REMEDIATION_REQUIRES_APPLICATION_ACCEPTANCE")
    universe = acceptance.get("universe_count")
    ready = acceptance.get("ready_for_analysis")
    blocked = acceptance.get("blocked_for_analysis")
    if (not all(type(n) is int for n in (universe, ready, blocked))
            or not 1 <= universe <= MAX_BLOCKERS
            or not 0 <= ready <= universe or blocked != universe - ready):
        raise ValueError("ETF_REMEDIATION_COUNTS_INVALID")
    candidates = acceptance.get("first_25_blocked")
    if not isinstance(candidates, list) or len(candidates) != blocked:
        raise ValueError("ETF_REMEDIATION_BLOCKER_SCOPE_INCOMPLETE")
    if any(not isinstance(row, dict) for row in candidates):
        raise ValueError("ETF_REMEDIATION_BLOCKER_INVALID")
    seen = set()
    actions = []
    for row in sorted(candidates, key=lambda r: (r.get("ticker", ""), r.get("instrument_id", ""))):
        if not isinstance(row.get("ticker"), str) or not row["ticker"].strip():
            raise ValueError("ETF_REMEDIATION_BLOCKER_INVALID")
        key = row.get("instrument_id")
        if not isinstance(key, str) or not key or key in seen:
            raise ValueError("ETF_REMEDIATION_DUPLICATE_OR_INVALID_ID")
        seen.add(key)
        blockers = row.get("blockers")
        if not isinstance(blockers, list) or not blockers:
            raise ValueError("ETF_REMEDIATION_BLOCKER_INVALID")
        if any(not isinstance(b, dict)
               or b.get("requirement") not in SUPPORTED_REQUIREMENTS
               or b.get("status") not in ("MISSING", "READY_STALE", "TECHNICAL_FAILURE")
               for b in blockers):
            raise ValueError("ETF_REMEDIATION_BLOCKER_INVALID")
        disposition, instruction = _disposition(blockers)
        actions.append({
            "symbol": row["ticker"], "instrument_id": key,
            "priority": disposition, "action": instruction,
            "blockers": [
                {"requirement": b["requirement"], "status": b["status"],
                 "reason": b.get("reason")}
                for b in sorted(blockers, key=lambda b: b["requirement"])
            ],
            "automated_resolution_approved": False,
        })
    supporting = acceptance.get("supporting_statuses")
    if not isinstance(supporting, list):
        raise ValueError("ETF_REMEDIATION_SUPPORTING_SCOPE_INVALID")
    source_gaps = Counter()
    unsupported = Counter()
    for row in supporting:
        if (not isinstance(row, dict) or not isinstance(row.get("requirement"), str)
                or not isinstance(row.get("status"), str)
                or type(row.get("etfs")) is not int
                or not 0 <= row["etfs"] <= universe):
            raise ValueError("ETF_REMEDIATION_SUPPORTING_SCOPE_INVALID")
        if row["requirement"] in OFFICIAL_SOURCE_SUPPORTING:
            if row["status"] in ("MISSING", "READY_STALE", "TECHNICAL_FAILURE", "NOT_IMPLEMENTED"):
                source_gaps[row["requirement"]] += row["etfs"]
            if row["status"] == "NOT_IMPLEMENTED":
                unsupported[row["requirement"]] += row["etfs"]
    if any(n > universe for n in source_gaps.values()):
        raise ValueError("ETF_REMEDIATION_SUPPORTING_DUPLICATE")
    counts = Counter(action["priority"] for action in actions)
    return {
        "version": WORK_PLAN_VERSION,
        "as_of": acceptance.get("as_of"),
        "universe_count": universe,
        "ready_for_analysis": ready,
        "blocked_for_analysis": blocked,
        "mandatory_action_counts": dict(sorted(counts.items())),
        "mandatory_actions": actions,
        "source_onboarding": {
            "missing_or_stale_etfs_by_requirement": dict(sorted(source_gaps.items())),
            "not_implemented_etfs_by_requirement": dict(sorted(unsupported.items())),
            "source_policy": (
                "Exact official URL, provenance, publication date, canonical "
                "ISIN and scheme-section identity required; accessible "
                "official data only. No name-only joins, unsupported ticker "
                "aliases, invented figures or unverified NSDL-code-to-ISIN."
            ),
            "sbi_nsdl_code_to_isin_approved": False,
            "automatic_document_approval": False,
            "automatic_ter_persistence": False,
        },
        "automatic_market_retries_executed": 0,
        "provider_requests_executed": 0,
        "database_writes_executed": 0,
        "status": "PLAN_ONLY_REQUIRES_SOURCE_EVIDENCE",
    }
