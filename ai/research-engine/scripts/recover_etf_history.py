"""Bounded ETF history recovery. Default: PostgreSQL-enforced read-only plan.

Run from ai/research-engine with ``python -m scripts.recover_etf_history --help``.
Apply requires both approval flags, existing live/non-demo configuration, and an
environment-only AIP_ETF_RECOVERY_BEARER_TOKEN. No application worker is started.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, timezone
import json
import os
import re
import sys
import threading
from uuid import UUID

CONFIRMATION = "RECOVER_ETF_HISTORY"
_REASONS = frozenset({
    "YAHOO_EXPLICIT_CHART_ERROR", "YAHOO_MALFORMED_RESPONSE", "YAHOO_RATE_LIMITED",
    "YAHOO_SESSION_BACKOFF_ACTIVE", "YAHOO_DOWNSTREAM_TIMEOUT", "YAHOO_FETCH_FAILED",
    "EXTERNAL_PROVIDER_RATE_LIMITED", "EXTERNAL_PROVIDER_UNAVAILABLE", "DOWNSTREAM_TIMEOUT",
    "YAHOO_ACCESS_RESTRICTED", "HISTORICAL_PRICE_PROVIDER_UNAVAILABLE",
    "VERIFIED_HISTORICAL_IDENTITY_MISMATCH", "PROVIDER_IDENTITY_UNAVAILABLE",
    "HISTORICAL_RANGE_INCOMPLETE", "HISTORICAL_SESSION_CALENDAR_UNAVAILABLE",
    "HISTORICAL_SESSION_HOURS_UNVERIFIED",
    "ETF_REQUEST_BUDGET_EXHAUSTED", "ETF_HTTP_BUDGET_EXHAUSTED", "ETF_RECOVERY_STOPPED",
    "ETF_HISTORY_INVALID_OBSERVATION", "ETF_HISTORY_INVALID_RANGE",
    "ETF_HISTORY_INVALID_RETRIEVAL_CLOCK", "ETF_HISTORY_LISTING_NOT_VERIFIED",
    "ETF_RECOVERY_CLI_ARGUMENTS_INVALID", "ETF_RECOVERY_APPROVAL_REQUIRED",
    "ETF_RECOVERY_RESUME_REQUIRES_APPLY", "ETF_RECOVERY_POSTGRES_REQUIRED",
    "ETF_RECOVERY_RESEARCH_SCHEMA_REQUIRED", "ETF_RECOVERY_LIVE_NON_DEMO_REQUIRED",
    "ETF_RECOVERY_PERSISTENCE_ENABLED_REQUIRED",
    "ETF_RECOVERY_BEARER_TOKEN_REQUIRED", "ETF_RECOVERY_BEARER_TOKEN_INVALID",
    "ETF_RECOVERY_EXPLICIT_UNIQUE_SCOPE_REQUIRED_MAX_5", "ETF_RECOVERY_AWARE_CLOCK_REQUIRED",
    "ETF_RECOVERY_RECENT_RANGE_REQUIRED_MAX_731_DAYS", "ETF_RECOVERY_BUDGET_OUT_OF_BOUNDS",
    "ETF_RECOVERY_COMPLETED_ETF_SOURCE_REQUIRED", "ETF_RECOVERY_SOURCE_UNIVERSE_UNAVAILABLE",
    "ETF_RECOVERY_AMBIGUOUS_OR_UNKNOWN_CANONICAL_ID", "ETF_RECOVERY_CANONICAL_IDENTITY_NOT_VERIFIED",
    "ETF_RECOVERY_ONLY_FAILED_HISTORY_ALLOWED", "ETF_RECOVERY_UNRESOLVED_PROVIDER_IDENTITY_CONFLICT",
    "ETF_RECOVERY_CURRENT_CANONICAL_IDENTITY_CONFLICT", "ETF_RECOVERY_RESUME_CONTRACT_MISMATCH",
    "ETF_RECOVERY_OTHER_OWNER_ACTIVE", "ETF_RECOVERY_PERSISTED_BUDGET_MISMATCH",
    "ETF_RECOVERY_CHECKPOINT_PAYLOAD_CONFLICT", "ETF_RECOVERY_CHECKPOINT_COVERAGE_CHANGED",
    "RESEARCH_SCHEMA_NOT_MIGRATED",
})
_STATUSES = frozenset({"RECOVERED_APPROVED_RANGE", "UNRESOLVED", "SUCCESS_WITH_DATA",
    "SUCCESS_EMPTY", "TECHNICAL_FAILURE", "COMPLETED_APPROVED_RANGE", "PARTIAL_FAILURE"})
_RECOVERY_OUTCOMES = frozenset({"RECOVERED_APPROVED_RANGE", "HISTORICAL_RANGE_INCOMPLETE",
    "TECHNICAL_FAILURE", "UNRESOLVED"})
_PROVIDER_STATUSES = frozenset({"SUCCESS_WITH_DATA", "SUCCESS_EMPTY", "TECHNICAL_FAILURE"})
_STOP_REASONS = frozenset({"ETF_REQUEST_BUDGET_EXHAUSTED", "ETF_HTTP_BUDGET_EXHAUSTED",
    "ETF_RECOVERY_STOPPED"})
_INCOMPLETE_DETAILS = ("LATEST_SESSION_NOT_RETURNED", "PROVIDER_HISTORY_STARTS_AFTER_RANGE_START",
    "INTERIOR_SESSIONS_MISSING")


class UsageError(ValueError):
    pass


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally repeats raw argv, which might contain a pasted token.
        raise UsageError("ETF_RECOVERY_CLI_ARGUMENTS_INVALID")


def parser():
    p = SafeArgumentParser(description=__doc__, allow_abbrev=False)
    p.add_argument("--source-cycle-id", type=UUID, required=True)
    p.add_argument("--instrument-id", type=UUID, action="append", required=True,
                   help="Repeat for 1 to 5 explicit failed-history canonical ETF UUIDs.")
    p.add_argument("--start", type=date.fromisoformat, required=True, help="Inclusive YYYY-MM-DD.")
    p.add_argument("--end", type=date.fromisoformat, required=True,
                   help="Exclusive YYYY-MM-DD; recent range at most 731 days.")
    p.add_argument("--max-requests", type=int, default=5, help="Provider windows, 1..15 (default 5).")
    p.add_argument("--max-http-requests", type=int, default=40, help="Actual HTTP requests, 1..60 (default 40).")
    p.add_argument("--apply", action="store_true", help="Explicitly execute the approved bounded repair.")
    p.add_argument("--confirm", help="Apply requires the exact token RECOVER_ETF_HISTORY.")
    p.add_argument("--recovery-id", type=UUID,
                   help="Resume an identical ACCEPTED or RUNNING recovery; apply only.")
    return p


class RecoveryRepository:
    """Only the persistence bridge required by the existing fenced history writer."""
    def __init__(self, persistence):
        self.persistence = persistence
        self._persistence_worker_lock = threading.RLock()
        self._dispatch = asyncio.Semaphore(1)

    async def _run_blocking_persistence(self, operation, *args, **kwargs):
        def call():
            with self._persistence_worker_lock:
                return operation(*args, **kwargs)
        async with self._dispatch:
            task = asyncio.create_task(asyncio.to_thread(call))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # A cancelled DB thread must drain before cleanup or another call.
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                if not task.cancelled():
                    task.exception()
                raise


def _settings():
    from app.settings import Settings
    return Settings()


def _open_store(settings):
    from app.postgres_persistence import PostgresResearchPersistence
    return PostgresResearchPersistence(settings)  # Never SQLite initialization or migrations.


def _api():
    from app.etf_history_recovery import HistoryRecoveryRequest, plan_history_recovery, recover_etf_history
    return HistoryRecoveryRequest, plan_history_recovery, recover_etf_history


def _read_only(store):
    store._connection.execute("SET default_transaction_read_only = on")
    store._connection.commit()


def _close_store(store):
    connection = store._connection.connection
    try:
        connection.rollback()
    finally:
        connection.close()


def _reason(value):
    if value is None:
        return None
    code, _, detail = str(value).partition(":")
    if code == "HISTORICAL_PRICE_PROVIDER_UNAVAILABLE" and detail.partition(":")[0] == "YFPricesMissingError":
        # That exception alone proves neither a chart-error body nor absent history.
        return "YAHOO_FETCH_FAILED"
    if (code == "VERIFIED_HISTORICAL_IDENTITY_MISMATCH"
            and detail.partition(":")[0] == "PROVIDER_IDENTITY_UNAVAILABLE"):
        return code + ":PROVIDER_IDENTITY_UNAVAILABLE"
    return code if code in _REASONS else "UNCLASSIFIED_TECHNICAL_FAILURE"


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _iso(value):
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        return None


def _windows(values):
    return [{"start": _iso(v.get("start")), "end_exclusive": _iso(v.get("end_exclusive"))}
            for v in values if isinstance(v, dict)] if isinstance(values, list) else []


def _missing_session_windows(values):
    """Whitelist the structured actual-missing-session windows (dates and counts only)."""
    return [{"start": _iso(v.get("start")), "end_exclusive": _iso(v.get("end_exclusive")),
             "first_missing_session": _iso(v.get("first_missing_session")),
             "last_missing_session": _iso(v.get("last_missing_session")),
             "missing_session_count": _count(v.get("missing_session_count"))}
            for v in values if isinstance(v, dict)] if isinstance(values, list) else []


def _rejected_close_report(value):
    # Lazy import keeps --help free of application/provider initialization.
    # The same bounded whitelist and strict counters serve checkpoint and CLI.
    from app.etf_history_recovery import rejected_close_diagnostics_report
    return rejected_close_diagnostics_report([value])


def _provider_windows(values):
    """Separate provider acquisition evidence from approved-range completeness."""
    rows = []
    for value in values if isinstance(values, list) else []:
        if not isinstance(value, dict):
            continue
        status = value.get("provider_status", value.get("status"))
        row = {"start": _iso(value.get("start")), "end_exclusive": _iso(value.get("end_exclusive")),
            "provider_status": status if status in _PROVIDER_STATUSES else "UNRESOLVED"}
        # Keep the existing per-window status/observations aliases for consumers.
        row["status"] = row["provider_status"]
        for key in ("observations", "preserved_conflicts", "accepted_provider_bar_count"):
            if key in value:
                row[key] = _count(value[key])
        row["persisted_bar_count"] = _count(value.get("persisted_bar_count", value.get("observations")))
        for key in ("first_date", "last_date", "accepted_provider_first_date", "accepted_provider_last_date"):
            if key in value:
                row[key] = _iso(value[key])
        if "rejected_invalid_close_count" in value:
            row["rejected_invalid_close_count"] = _count(value["rejected_invalid_close_count"])
        row.update(_rejected_close_report(value))
        if value.get("provider") == "YAHOO_FINANCE":
            row["provider"] = "YAHOO_FINANCE"
        if "reason" in value:
            row["reason_class"] = _reason(value["reason"])
        rows.append(row)
    return rows


def _recovery_outcome(item):
    """Read additive outcomes and conservatively explain older checkpoints."""
    outcome = item.get("recovery_outcome")
    reason = _reason(item.get("reason"))
    if reason in _STOP_REASONS:
        return "UNRESOLVED"
    windows = item.get("windows")
    technical_window = any(
        window.get("provider_status", window.get("status")) == "TECHNICAL_FAILURE"
        for window in windows if isinstance(window, dict)) if isinstance(windows, list) else False
    if (item.get("status") == "TECHNICAL_FAILURE"
            or reason not in (None, "HISTORICAL_RANGE_INCOMPLETE")
            or technical_window):
        return "TECHNICAL_FAILURE"
    if item.get("remaining_windows") or reason == "HISTORICAL_RANGE_INCOMPLETE":
        return "HISTORICAL_RANGE_INCOMPLETE"
    # A persisted positive flag cannot override unresolved gaps or actual
    # technical failures. Retain conservative additive outcomes unchanged.
    if outcome in _RECOVERY_OUTCOMES:
        return outcome
    if item.get("status") == "RECOVERED_APPROVED_RANGE":
        return "RECOVERED_APPROVED_RANGE"
    return "UNRESOLVED"


def sanitized_report(result, request, *, apply):
    """Whitelist output fields; never serialize raw errors, source bodies or headers."""
    output = {"mode": "APPLY" if apply else "PLAN", "kind": "ETF_HISTORY_RECOVERY",
        "parameters": request.parameters(), "write_policy": "INSERT_ONLY",
        "publication": "UNCHANGED", "recommendation": "NOT_EVALUATED"}
    source = list((result.get("results") or {}).values()) if apply else result.get("instruments", [])
    allowed = {str(key) for key in request.instrument_ids}
    rows = []
    for item in source:
        if not isinstance(item, dict) or str(item.get("instrument_id")) not in allowed:
            continue
        row = {"instrument_id": str(item["instrument_id"])}
        for key, pattern in (("isin", r"IN[A-Z0-9]{9}[0-9]"), ("symbol", r"[A-Z0-9&._-]{1,50}")):
            value = item.get(key)
            if isinstance(value, str) and re.fullmatch(pattern, value):
                row[key] = value
        for key in ("existing_valid_sessions", "observations", "valid_sessions"):
            if key in item:
                row[key] = _count(item[key])
        if apply:
            row["persisted_bar_count"] = _count(item.get("persisted_bar_count", item.get("observations")))
            row["recovery_outcome"] = _recovery_outcome(item)
            row["windows"] = _provider_windows(item.get("windows"))
            # Additive diagnostics; absent from older checkpoints and then simply omitted.
            for key in ("expected_completed_sessions", "rejected_invalid_close_count"):
                if key in item:
                    row[key] = _count(item[key])
            for key in ("expected_latest_completed_session", "provider_first_accepted_date",
                        "provider_last_accepted_date"):
                if key in item:
                    row[key] = _iso(item[key])
            if isinstance(item.get("incomplete_detail"), list):
                row["incomplete_detail"] = [v for v in _INCOMPLETE_DETAILS if v in item["incomplete_detail"]]
            if "missing_session_windows" in item:
                row["missing_session_windows"] = _missing_session_windows(item["missing_session_windows"])
            row.update(_rejected_close_report(item))
        for key in ("original_failure", "reason"):
            if key in item:
                row[key + "_class"] = _reason(item[key])
        if "status" in item:
            row["status"] = item["status"] if item["status"] in _STATUSES else "UNRESOLVED"
        if "canonical_listing_date" in item:
            row["canonical_listing_date"] = _iso(item["canonical_listing_date"])
            row["listing_boundary"] = "NOT_INDEPENDENTLY_VERIFIED_NOT_USED"
        for key in ("missing_windows", "remaining_windows"):
            if key in item:
                row[key] = _windows(item[key])
        row["older_history"] = "UNRESOLVED_OUTSIDE_APPROVED_RANGE"
        rows.append(row)
    output["instruments"] = rows
    if apply:
        attempted_ids = {str(row["instrument_id"]) for row in rows}
        output["unattempted_instrument_ids"] = [str(key) for key in request.instrument_ids
                                               if str(key) not in attempted_ids]
        output["recovery_id"] = str(UUID(str(result["recovery_id"])))
        output["status"] = result.get("status") if result.get("status") in _STATUSES else "PARTIAL_FAILURE"
        output["stop_reason_class"] = _reason(result.get("stop_reason"))
        output["request_reservations"] = _count(result.get("request_reservations"))
        output["http_reservations"] = _count(result.get("http_reservations"))
        output["provider_window_reservations"] = output["request_reservations"]
        output["provider_window_budget"] = request.max_requests
        output["http_budget"] = request.max_http_requests
        counts = [row["persisted_bar_count"] for row in rows]
        output["persisted_bar_count"] = sum(counts) if all(value is not None for value in counts) else None
    return output


async def run(args, *, now=None):
    if ((args.apply and args.confirm != CONFIRMATION)
            or (not args.apply and args.confirm is not None)):
        raise UsageError("ETF_RECOVERY_APPROVAL_REQUIRED")
    if args.recovery_id is not None and not args.apply:
        raise UsageError("ETF_RECOVERY_RESUME_REQUIRES_APPLY")
    request_type, plan, recover = _api()
    instant = now or datetime.now(timezone.utc)
    request = request_type(str(args.source_cycle_id), tuple(args.instrument_id), args.start, args.end,
                           args.max_requests, args.max_http_requests)
    request.validate(instant)
    settings = _settings()
    if settings.research_database_backend != "postgres":
        raise UsageError("ETF_RECOVERY_POSTGRES_REQUIRED")
    if settings.research_database_schema != "research":
        raise UsageError("ETF_RECOVERY_RESEARCH_SCHEMA_REQUIRED")
    token = None
    if args.apply:
        if not settings.research_persistence_enabled:
            raise UsageError("ETF_RECOVERY_PERSISTENCE_ENABLED_REQUIRED")
        if not settings.research_live_enabled or settings.research_demo_enabled:
            raise UsageError("ETF_RECOVERY_LIVE_NON_DEMO_REQUIRED")
        token = os.environ.get("AIP_ETF_RECOVERY_BEARER_TOKEN")
        if not token:
            raise UsageError("ETF_RECOVERY_BEARER_TOKEN_REQUIRED")
        if len(token) > 16384 or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token):
            raise UsageError("ETF_RECOVERY_BEARER_TOKEN_INVALID")
    repository = RecoveryRepository(None)
    def open_store():
        repository.persistence = _open_store(settings)
    try:
        await repository._run_blocking_persistence(open_store)
        store = repository.persistence
        if not args.apply:
            await repository._run_blocking_persistence(_read_only, store)
            result = await plan(repository, request, now=instant)
        else:
            import httpx
            from app.portfolio_orchestration import PortfolioResearchOrchestrator
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0), follow_redirects=False) as client:
                orchestrator = PortfolioResearchOrchestrator(repository, settings, client=client,
                                                              structured_provider=object())
                result = await recover(repository, orchestrator, request, now=instant,
                    identity_headers={"Authorization": "Bearer " + token}, recovery_id=args.recovery_id)
        return sanitized_report(result, request, apply=args.apply)
    finally:
        if repository.persistence is not None:
            await repository._run_blocking_persistence(_close_store, repository.persistence)


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        result = asyncio.run(run(args))  # asyncio.run also drains its default thread pool.
    except KeyboardInterrupt:
        print(json.dumps({"error": "ETF_RECOVERY_INTERRUPTED"}), file=sys.stderr)
        return 130
    except Exception as error:
        print(json.dumps({"error": _reason(error)}), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 3 if result.get("status") == "PARTIAL_FAILURE" else 0


if __name__ == "__main__":
    raise SystemExit(main())
