"""Read-only shortlist of missing ETF histories from one completed source cycle.

Run from ai/research-engine with ``python -m scripts.discover_etf_history_recovery
--help``. This command has no execution or authentication flags, creates no
checkpoint, and makes no provider or portfolio-service requests.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
import json
import re
import sys
from uuid import UUID

from scripts import recover_etf_history as recovery

_ERRORS = frozenset({"ETF_DISCOVERY_CLI_ARGUMENTS_INVALID", "ETF_DISCOVERY_LIMIT_OUT_OF_BOUNDS",
    "ETF_DISCOVERY_PAIRED_DATES_REQUIRED", "ETF_DISCOVERY_INVALID_RESULT",
    "ETF_DISCOVERY_INVALID_RECOVERY_METADATA", "READ_ONLY_SNAPSHOT_REQUIRES_IDLE_CONNECTION",
    "READ_ONLY_SNAPSHOT_NOT_ENFORCED"})
_SUMMARY = ("source_universe", "original_failed_history_candidates", "eligible_candidates",
    "excluded_candidates", "partially_recovered_candidates", "returned_shortlist_count")
_PRIORITY_POLICY = "VERIFIED_PARTIAL_THEN_MISSING_SESSIONS_DESC_SYMBOL_ISIN_UUID"
_PRIORITY_REASONS = {"VERIFIED_PARTIAL_COVERAGE_WITH_PRIOR_RECOVERY",
                     "MISSING_COMPLETED_SESSION_COVERAGE"}


class SafeArgumentParser(recovery.SafeArgumentParser):
    def error(self, message):
        raise recovery.UsageError("ETF_DISCOVERY_CLI_ARGUMENTS_INVALID")


def parser():
    p = SafeArgumentParser(description=__doc__, allow_abbrev=False)
    p.add_argument("--source-cycle-id", type=UUID, required=True)
    p.add_argument("--limit", type=int, default=5, help="Shortlist size, 1..5 (default 5).")
    p.add_argument("--start", type=date.fromisoformat, help="Optional inclusive YYYY-MM-DD; requires --end.")
    p.add_argument("--end", type=date.fromisoformat,
                   help="Optional exclusive YYYY-MM-DD; requires --start; recent range at most 731 days.")
    return p


def _api():
    from app.etf_history_discovery import discover_etf_history_recovery
    return discover_etf_history_recovery


def _fail():
    raise ValueError("ETF_DISCOVERY_INVALID_RESULT")


def _uuid(value, *, nullable=False):
    if value is None and nullable:
        return None
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        _fail()


def _date(value, *, nullable=False):
    if value is None and nullable:
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (ValueError, TypeError):
        _fail()


def _count(value, *, nullable=False):
    if value is None and nullable:
        return None
    if type(value) is not int or value < 0:
        _fail()
    return value


def _enum(value, allowed, *, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or value not in allowed:
        _fail()
    return value


def _identity_text(value, pattern):
    # Malformed identities are retained as excluded assessments, without printing raw text.
    return value if isinstance(value, str) and re.fullmatch(pattern, value) else None


def _reason(value):
    code = str(value).partition(":")[0]
    if code in _ERRORS:
        return code
    return recovery._reason(value)


def _windows(value):
    if not isinstance(value, list):
        _fail()
    result = []
    for window in value:
        if not isinstance(window, dict):
            _fail()
        start = _date(window.get("start"))
        end = _date(window.get("end_exclusive"))
        if start >= end:
            _fail()
        result.append({"start": start, "end_exclusive": end})
    return result


def _previous(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or type(value.get("terminal")) is not bool
            or value.get("execution_authorized") is not False):
        _fail()
    return {"recovery_id": _uuid(value.get("recovery_id"), nullable=True),
        "checkpoint_status": _enum(value.get("checkpoint_status"), {"ACCEPTED", "RUNNING", "PUBLISHED",
            "CANCEL_REQUESTED", "COMPLETED", "FAILED", "CANCELLED", "UNKNOWN"}),
        "recovery_status": _enum(value.get("recovery_status"),
            {"PARTIAL_FAILURE", "COMPLETED_APPROVED_RANGE"}, nullable=True),
        "instrument_status": _enum(value.get("instrument_status"), {"RECOVERED_APPROVED_RANGE",
            "UNRESOLVED", "TECHNICAL_FAILURE", "HISTORICAL_RANGE_INCOMPLETE"}, nullable=True),
        "reason_classification": _reason(value.get("reason_classification")),
        "terminal": value["terminal"], "execution_authorized": False}


def _safe_row(row, source_cycle_id):
    if not isinstance(row, dict) or _uuid(row.get("source_cycle_id")) != source_cycle_id:
        _fail()
    output = {"source_cycle_id": source_cycle_id,
        "instrument_id": _uuid(row.get("instrument_id"), nullable=True),
        "isin": _identity_text(row.get("isin"), r"IN[A-Z0-9]{9}[0-9]"),
        "primary_symbol": _identity_text(row.get("primary_symbol"), r"[A-Z0-9&._-]{1,50}"),
        "original_failure_classification": _reason(row.get("original_failure_classification")),
        "coverage_scope": "APPROVED_RANGE_ONLY",
        "canonical_listing_date": _date(row.get("canonical_listing_date"), nullable=True),
        "listing_date_verification_status": _enum(row.get("listing_date_verification_status"),
            {"MISSING", "UNVERIFIED_NOT_USED"}),
        "missing_historical_windows": _windows(row.get("missing_historical_windows")),
        "eligibility_status": _enum(row.get("eligibility_status"), {"ELIGIBLE", "EXCLUDED"}),
        "recommended_priority": _count(row.get("recommended_priority"), nullable=True),
        "priority_rationale": _enum(row.get("priority_rationale"), _PRIORITY_REASONS, nullable=True),
        "previous_recovery": _previous(row.get("previous_recovery")),
        "older_history": "UNRESOLVED_OUTSIDE_APPROVED_RANGE"}
    for key in ("existing_valid_bar_count", "missing_completed_sessions", "expected_completed_sessions"):
        output[key] = _count(row.get(key), nullable=True)
    for key in ("first_available_historical_date", "last_available_historical_date"):
        output[key] = _date(row.get(key), nullable=True)
    exclusion = row.get("exclusion_reason")
    output["exclusion_reason"] = ("HISTORICAL_RANGE_ALREADY_COVERED" if exclusion ==
        "HISTORICAL_RANGE_ALREADY_COVERED" else _reason(exclusion))
    if output["eligibility_status"] == "ELIGIBLE" and (
            not output["instrument_id"] or not output["isin"] or not output["primary_symbol"]
            or not output["recommended_priority"] or output["exclusion_reason"] is not None):
        _fail()
    return output


def sanitized_report(result, args):
    if (not isinstance(result, dict) or result.get("kind") != "ETF_HISTORY_RECOVERY_DISCOVERY"
            or result.get("mode") != "READ_ONLY" or result.get("execution_authorized") is not False
            or type(result.get("provider_requests")) is not int or result.get("provider_requests") != 0
            or result.get("priority_policy") != _PRIORITY_POLICY
            or result.get("priority_is_provider_success_prediction") is not False):
        _fail()
    source = str(args.source_cycle_id)
    if _uuid(result.get("source_cycle_id")) != source or result.get("limit") != args.limit:
        _fail()
    if not isinstance(result.get("summary"), dict) or not isinstance(result.get("range"), dict):
        _fail()
    try:
        instant = datetime.fromisoformat(str(result.get("as_of")))
        if instant.tzinfo is None or instant.utcoffset() is None:
            _fail()
    except ValueError:
        _fail()
    summary = {key: _count(result["summary"].get(key)) for key in _SUMMARY}
    bounds = {"start": _date(result["range"].get("start")),
              "end_exclusive": _date(result["range"].get("end_exclusive"))}
    if bounds["start"] >= bounds["end_exclusive"]:
        _fail()
    if args.start is not None and bounds != {
            "start": args.start.isoformat(), "end_exclusive": args.end.isoformat()}:
        _fail()
    output = {"kind": "ETF_HISTORY_RECOVERY_DISCOVERY", "mode": "READ_ONLY",
        "source_cycle_id": source, "as_of": instant.isoformat(), "range": bounds,
        "limit": args.limit, "summary": summary, "publication": "UNCHANGED",
        "recommendation": "NOT_EVALUATED", "execution_authorized": False, "provider_requests": 0}
    output["priority_policy"] = _PRIORITY_POLICY
    output["priority_is_provider_success_prediction"] = False
    for key in ("candidates", "assessments"):
        if not isinstance(result.get(key), list):
            _fail()
        output[key] = [_safe_row(row, source) for row in result[key]]
    if (len(output["candidates"]) > args.limit
            or summary["returned_shortlist_count"] != len(output["candidates"])
            or summary["original_failed_history_candidates"] != len(output["assessments"])
            or summary["eligible_candidates"] + summary["excluded_candidates"] != len(output["assessments"])):
        _fail()
    return output


async def run(args, *, now=None):
    if type(args.limit) is not int or not 1 <= args.limit <= 5:
        raise recovery.UsageError("ETF_DISCOVERY_LIMIT_OUT_OF_BOUNDS")
    if (args.start is None) != (args.end is None):
        raise recovery.UsageError("ETF_DISCOVERY_PAIRED_DATES_REQUIRED")
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise recovery.UsageError("ETF_RECOVERY_AWARE_CLOCK_REQUIRED")
    if args.start is not None and (args.start >= args.end
            or args.end > instant.date() + timedelta(days=1)
            or args.start < instant.date() - timedelta(days=731)
            or (args.end - args.start).days > 731):
        raise recovery.UsageError("ETF_RECOVERY_RECENT_RANGE_REQUIRED_MAX_731_DAYS")
    settings = recovery._settings()
    if settings.research_database_backend != "postgres":
        raise recovery.UsageError("ETF_RECOVERY_POSTGRES_REQUIRED")
    if settings.research_database_schema != "research":
        raise recovery.UsageError("ETF_RECOVERY_RESEARCH_SCHEMA_REQUIRED")
    repository = recovery.RecoveryRepository(None)
    def open_store():
        repository.persistence = recovery._open_store(settings)
    try:
        await repository._run_blocking_persistence(open_store)
        await repository._run_blocking_persistence(recovery._read_only, repository.persistence)
        result = await _api()(repository, str(args.source_cycle_id), limit=args.limit,
                              start=args.start, end=args.end, now=instant)
        return sanitized_report(result, args)
    finally:
        if repository.persistence is not None:
            await repository._run_blocking_persistence(recovery._close_store, repository.persistence)


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        result = asyncio.run(run(args))
    except KeyboardInterrupt:
        print(json.dumps({"error": "ETF_DISCOVERY_INTERRUPTED"}), file=sys.stderr)
        return 130
    except Exception as error:
        print(json.dumps({"error": _reason(error)}), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
