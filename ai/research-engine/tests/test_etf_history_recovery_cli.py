"""CLI safety tests. All database/HTTP/apply operations are mocked; no live repair."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest

from app.etf_history_recovery import HistoryRecoveryRequest
from scripts import recover_etf_history as cli

NOW = datetime(2026, 11, 6, 9, tzinfo=timezone.utc)
SOURCE = "17c48038-507b-4924-b902-20f3b7df4091"
INSTRUMENT = str(UUID(int=7))
RECOVERY = str(UUID(int=8))
SECRET = "synthetic-token.never-print-me"


def argv(*extra):
    return ["--source-cycle-id", SOURCE, "--instrument-id", INSTRUMENT,
            "--start", "2026-10-01", "--end", "2026-11-06", *extra]


@pytest.fixture
def environment(monkeypatch):
    connection = Mock()
    adapter = Mock(connection=connection)
    store = SimpleNamespace(_connection=adapter)
    settings = SimpleNamespace(research_database_backend="postgres", research_database_schema="research",
        research_live_enabled=True, research_demo_enabled=False, research_persistence_enabled=True)
    settings_call = Mock(return_value=settings)
    open_store = Mock(return_value=store)
    plan = AsyncMock(return_value={"instruments": [{"instrument_id": INSTRUMENT,
        "isin": "INF204KB14I2", "symbol": "NIFTYBEES", "existing_valid_sessions": 4,
        "original_failure": "YAHOO_EXPLICIT_CHART_ERROR:raw private content",
        "missing_windows": [{"start": "2026-10-01", "end_exclusive": "2026-11-06"}]}]})
    recover = AsyncMock(return_value={"recovery_id": RECOVERY, "status": "COMPLETED_APPROVED_RANGE",
        "request_reservations": 1, "http_reservations": 4,
        "results": {"INF204KB14I2": {"instrument_id": INSTRUMENT, "isin": "INF204KB14I2",
            "status": "RECOVERED_APPROVED_RANGE", "observations": 3, "valid_sessions": 7,
            "reason": None, "remaining_windows": []}}})
    monkeypatch.setattr(cli, "_settings", settings_call)
    monkeypatch.setattr(cli, "_open_store", open_store)
    monkeypatch.setattr(cli, "_api", lambda: (HistoryRecoveryRequest, plan, recover))
    monkeypatch.delenv("AIP_ETF_RECOVERY_BEARER_TOKEN", raising=False)
    return SimpleNamespace(store=store, adapter=adapter, connection=connection, settings=settings,
                           settings_call=settings_call, open_store=open_store, plan=plan, recover=recover)


def test_help_needs_no_settings_database_or_http(environment, capsys):
    with pytest.raises(SystemExit) as stop:
        cli.main(["--help"])
    assert stop.value.code == 0
    assert "--max-http-requests" in capsys.readouterr().out
    environment.settings_call.assert_not_called()
    environment.open_store.assert_not_called()


def test_help_imports_no_application_main_or_yahoo_constructor():
    # A fresh process also verifies help doesn't import the application runtime.
    probe = """import sys
from scripts import recover_etf_history as c
try: c.main(['--help'])
except SystemExit as e: assert e.code == 0
assert 'app.main' not in sys.modules
assert 'app.etf_history_recovery' not in sys.modules
assert 'app.structured_market' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", probe], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("extra,reason", [
    (["--apply"], "ETF_RECOVERY_APPROVAL_REQUIRED"),
    (["--confirm", "RECOVER_ETF_HISTORY"], "ETF_RECOVERY_APPROVAL_REQUIRED"),
    (["--apply", "--confirm", "wrong"], "ETF_RECOVERY_APPROVAL_REQUIRED"),
    (["--recovery-id", RECOVERY], "ETF_RECOVERY_RESUME_REQUIRES_APPLY"),
    (["--max-requests", "0"], "ETF_RECOVERY_BUDGET_OUT_OF_BOUNDS"),
    (["--max-requests", "16"], "ETF_RECOVERY_BUDGET_OUT_OF_BOUNDS"),
    (["--max-http-requests", "61"], "ETF_RECOVERY_BUDGET_OUT_OF_BOUNDS"),
    (["--instrument-id", INSTRUMENT], "ETF_RECOVERY_EXPLICIT_UNIQUE_SCOPE_REQUIRED_MAX_5"),
    (["--start", "2024-01-01"], "ETF_RECOVERY_RECENT_RANGE_REQUIRED_MAX_731_DAYS"),
    (["--end", "2026-12-01"], "ETF_RECOVERY_RECENT_RANGE_REQUIRED_MAX_731_DAYS"),
    (["--end", "2026-10-01"], "ETF_RECOVERY_RECENT_RANGE_REQUIRED_MAX_731_DAYS"),
])
def test_invalid_or_unapproved_requests_refused_before_database(environment, extra, reason):
    with pytest.raises(ValueError, match=reason):
        asyncio.run(cli.run(cli.parser().parse_args(argv(*extra)), now=NOW))
    environment.open_store.assert_not_called()
    environment.recover.assert_not_awaited()


def test_six_instruments_refused_before_database(environment):
    extra = [x for n in range(10, 15) for x in ("--instrument-id", str(UUID(int=n)))]
    with pytest.raises(ValueError, match="MAX_5"):
        asyncio.run(cli.run(cli.parser().parse_args(argv(*extra)), now=NOW))
    environment.open_store.assert_not_called()


@pytest.mark.parametrize("bad_args", [[], ["--source-cycle-id", SECRET], argv("--bearer-token", SECRET),
    argv("--start", SECRET), argv("--max-requests", SECRET),
    argv("--ap", "--confirm", cli.CONFIRMATION)])
def test_argument_errors_do_not_echo_arbitrary_values(environment, capsys, bad_args):
    assert cli.main(bad_args) == 2
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert json.loads(output.err) == {"error": "ETF_RECOVERY_CLI_ARGUMENTS_INVALID"}
    environment.open_store.assert_not_called()


def test_default_plan_enforces_read_only_and_never_constructs_http(environment, monkeypatch):
    import httpx
    client = Mock(side_effect=AssertionError("No HTTP in a plan"))
    monkeypatch.setattr(httpx, "AsyncClient", client)
    result = asyncio.run(cli.run(cli.parser().parse_args(argv()), now=NOW))
    environment.adapter.execute.assert_called_once_with("SET default_transaction_read_only = on")
    environment.adapter.commit.assert_called_once_with()
    environment.plan.assert_awaited_once()
    environment.recover.assert_not_awaited()
    client.assert_not_called()
    environment.connection.rollback.assert_called_once_with()
    environment.connection.close.assert_called_once_with()
    assert result["mode"] == "PLAN" and result["parameters"]["max_requests"] == 5
    assert result["parameters"]["max_http_requests"] == 40
    assert result["instruments"][0]["original_failure_class"] == "YAHOO_EXPLICIT_CHART_ERROR"
    assert "raw private content" not in json.dumps(result)


def test_maximum_explicit_scope_and_budgets_are_preserved_in_plan(environment):
    extra = [x for n in range(10, 14) for x in ("--instrument-id", str(UUID(int=n)))]
    result = asyncio.run(cli.run(cli.parser().parse_args(argv(*extra, "--max-requests", "15",
        "--max-http-requests", "60")), now=NOW))
    assert len(result["parameters"]["instrument_ids"]) == 5
    assert result["parameters"]["max_requests"] == 15
    assert result["parameters"]["max_http_requests"] == 60
    environment.recover.assert_not_awaited()


def test_read_only_plan_is_allowed_with_persistence_disabled(environment):
    environment.settings.research_persistence_enabled = False
    result = asyncio.run(cli.run(cli.parser().parse_args(argv()), now=NOW))
    assert result["mode"] == "PLAN"
    environment.adapter.execute.assert_called_once_with("SET default_transaction_read_only = on")
    environment.recover.assert_not_awaited()


@pytest.mark.parametrize("field,value,reason", [
    ("research_database_backend", "sqlite", "ETF_RECOVERY_POSTGRES_REQUIRED"),
    ("research_database_schema", "portfolio", "ETF_RECOVERY_RESEARCH_SCHEMA_REQUIRED"),
    ("research_live_enabled", False, "ETF_RECOVERY_LIVE_NON_DEMO_REQUIRED"),
    ("research_demo_enabled", True, "ETF_RECOVERY_LIVE_NON_DEMO_REQUIRED"),
    ("research_persistence_enabled", False, "ETF_RECOVERY_PERSISTENCE_ENABLED_REQUIRED"),
])
def test_unsafe_configuration_refused_before_database(environment, monkeypatch, field, value, reason):
    monkeypatch.setenv("AIP_ETF_RECOVERY_BEARER_TOKEN", SECRET)
    setattr(environment.settings, field, value)
    with pytest.raises(ValueError, match=reason):
        asyncio.run(cli.run(cli.parser().parse_args(argv("--apply", "--confirm", cli.CONFIRMATION)), now=NOW))
    environment.open_store.assert_not_called()


@pytest.mark.parametrize("token,reason", [(None, "REQUIRED"), ("Bearer injected", "INVALID"),
    ("bad\r\nAuthorization: value", "INVALID")])
def test_apply_requires_environment_only_valid_bearer(environment, monkeypatch, token, reason):
    if token is not None:
        monkeypatch.setenv("AIP_ETF_RECOVERY_BEARER_TOKEN", token)
    with pytest.raises(ValueError, match="ETF_RECOVERY_BEARER_TOKEN_" + reason):
        asyncio.run(cli.run(cli.parser().parse_args(argv("--apply", "--confirm", cli.CONFIRMATION)), now=NOW))
    environment.open_store.assert_not_called()


def test_approved_apply_uses_existing_orchestrator_without_yahoo_or_http_calls(environment, monkeypatch):
    import httpx
    import app.portfolio_orchestration as portfolio
    client = SimpleNamespace()
    context = AsyncMock()
    context.__aenter__.return_value = client
    http = Mock(return_value=context)
    orchestrator = Mock()
    factory = Mock(return_value=orchestrator)
    monkeypatch.setattr(httpx, "AsyncClient", http)
    monkeypatch.setattr(portfolio, "PortfolioResearchOrchestrator", factory)
    monkeypatch.setenv("AIP_ETF_RECOVERY_BEARER_TOKEN", SECRET)
    result = asyncio.run(cli.run(cli.parser().parse_args(argv("--apply", "--confirm", cli.CONFIRMATION,
        "--recovery-id", RECOVERY)), now=NOW))
    environment.plan.assert_not_awaited()
    environment.recover.assert_awaited_once()
    call = environment.recover.call_args
    assert call.args[1] is orchestrator
    assert call.kwargs["identity_headers"] == {"Authorization": "Bearer " + SECRET}
    assert call.kwargs["recovery_id"] == UUID(RECOVERY)
    assert factory.call_args.kwargs["client"] is client
    assert factory.call_args.kwargs["structured_provider"] is not None
    assert SECRET not in json.dumps(result)
    assert result["recovery_id"] == RECOVERY and result["recommendation"] == "NOT_EVALUATED"
    environment.adapter.execute.assert_not_called()
    environment.connection.close.assert_called_once_with()
    context.__aexit__.assert_awaited_once()


def test_plan_failure_closes_store_and_prints_only_stable_error(environment, capsys):
    environment.plan.side_effect = RuntimeError("postgresql://user:" + SECRET + "@host/database")
    assert cli.main(argv()) == 2
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err and "postgresql" not in output.err
    assert json.loads(output.err)["error"] == "UNCLASSIFIED_TECHNICAL_FAILURE"
    environment.connection.close.assert_called_once_with()


def test_partial_failure_is_reported_with_nonzero_exit(monkeypatch, capsys):
    monkeypatch.setattr(cli, "run", AsyncMock(return_value={"mode": "APPLY", "status": "PARTIAL_FAILURE"}))
    assert cli.main(argv("--apply", "--confirm", cli.CONFIRMATION)) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "PARTIAL_FAILURE"


@pytest.mark.asyncio
async def test_persistence_bridge_serializes_and_drains_cancelled_thread():
    bridge = cli.RecoveryRepository(object())
    started, release = threading.Event(), threading.Event()
    order = []
    def first():
        order.append("first_start")
        started.set()
        assert release.wait(3)
        order.append("first_end")
    task = asyncio.create_task(bridge._run_blocking_persistence(first))
    while not started.is_set():
        await asyncio.sleep(0)
    task.cancel()
    second = asyncio.create_task(bridge._run_blocking_persistence(lambda: order.append("second")))
    await asyncio.sleep(0)
    assert order == ["first_start"]
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await second
    assert order == ["first_start", "first_end", "second"]


@pytest.mark.asyncio
async def test_cancellation_while_opening_store_still_closes_connection(environment, monkeypatch):
    started, release = threading.Event(), threading.Event()
    def open_store(_settings):
        started.set()
        assert release.wait(3)
        return environment.store
    monkeypatch.setattr(cli, "_open_store", open_store)
    task = asyncio.create_task(cli.run(cli.parser().parse_args(argv()), now=NOW))
    while not started.is_set():
        await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    environment.connection.close.assert_called_once_with()
    environment.plan.assert_not_awaited()


def test_report_omits_untrusted_payloads_and_other_instrument_ids():
    request = HistoryRecoveryRequest(SOURCE, (UUID(INSTRUMENT),), NOW.date(), NOW.date())
    result = {"instruments": [{"instrument_id": INSTRUMENT, "symbol": SECRET,
        "original_failure": SECRET, "headers": {"Authorization": SECRET}},
        {"instrument_id": str(UUID(int=999)), "symbol": "EQUITY"}], "secret": SECRET}
    sanitized = cli.sanitized_report(result, request, apply=False)
    assert len(sanitized["instruments"]) == 1
    assert sanitized["instruments"][0]["original_failure_class"] == "UNCLASSIFIED_TECHNICAL_FAILURE"
    assert SECRET not in json.dumps(sanitized) and "EQUITY" not in json.dumps(sanitized)


@pytest.mark.parametrize("reason,classification", [
    ("HISTORICAL_PRICE_PROVIDER_UNAVAILABLE:YFPricesMissingError:" + SECRET, "YAHOO_FETCH_FAILED"),
    ("HISTORICAL_PRICE_PROVIDER_UNAVAILABLE:DifferentException", "HISTORICAL_PRICE_PROVIDER_UNAVAILABLE"),
    ("YAHOO_EXPLICIT_CHART_ERROR:" + SECRET, "YAHOO_EXPLICIT_CHART_ERROR"),
    ("EXTERNAL_PROVIDER_RATE_LIMITED:" + SECRET, "EXTERNAL_PROVIDER_RATE_LIMITED"),
    ("DOWNSTREAM_TIMEOUT:" + SECRET, "DOWNSTREAM_TIMEOUT"),
    ("YAHOO_ACCESS_RESTRICTED:" + SECRET, "YAHOO_ACCESS_RESTRICTED"),
    ("HISTORICAL_SESSION_HOURS_UNVERIFIED", "HISTORICAL_SESSION_HOURS_UNVERIFIED"),
    ("ETF_RECOVERY_CHECKPOINT_PAYLOAD_CONFLICT", "ETF_RECOVERY_CHECKPOINT_PAYLOAD_CONFLICT"),
    ("ETF_RECOVERY_CHECKPOINT_COVERAGE_CHANGED", "ETF_RECOVERY_CHECKPOINT_COVERAGE_CHANGED"),
    ("VERIFIED_HISTORICAL_IDENTITY_MISMATCH:PROVIDER_IDENTITY_UNAVAILABLE:" + SECRET,
     "VERIFIED_HISTORICAL_IDENTITY_MISMATCH:PROVIDER_IDENTITY_UNAVAILABLE"),
    ("unstructured malformed payload " + SECRET, "UNCLASSIFIED_TECHNICAL_FAILURE"),
])
def test_reason_classification_preserves_verified_prefixes_without_inventing_absence(reason, classification):
    assert cli._reason(reason) == classification
    assert SECRET not in cli._reason(reason)


def test_global_stop_reports_remaining_explicit_scope_without_fabricated_progress():
    requested = (UUID(INSTRUMENT), UUID(int=9), UUID(int=10))
    request = HistoryRecoveryRequest(SOURCE, requested, NOW.date(), NOW.date())
    result = {"recovery_id": RECOVERY, "status": "PARTIAL_FAILURE",
        "stop_reason": "EXTERNAL_PROVIDER_RATE_LIMITED", "request_reservations": 1,
        "http_reservations": 2, "results": {"INF204KB14I2": {
            "instrument_id": INSTRUMENT, "isin": "INF204KB14I2", "status": "UNRESOLVED",
            "observations": 0, "reason": "EXTERNAL_PROVIDER_RATE_LIMITED"}}}
    report = cli.sanitized_report(result, request, apply=True)
    assert report["status"] == "PARTIAL_FAILURE"
    assert report["unattempted_instrument_ids"] == [str(key) for key in requested[1:]]
    assert len(report["instruments"]) == 1
    assert report["instruments"][0]["instrument_id"] == INSTRUMENT
    assert report["instruments"][0]["status"] == "UNRESOLVED"
    assert report["stop_reason_class"] == "EXTERNAL_PROVIDER_RATE_LIMITED"


def test_synthetic_pilot_report_keeps_provider_success_distinct_from_incomplete_range():
    """Synthetic reproduction of the pilot counts; never live market evidence."""
    requested = (UUID(INSTRUMENT), UUID(int=9))
    request = HistoryRecoveryRequest(SOURCE, requested, NOW.date(), NOW.date(), 2, 12)
    result = {"recovery_id": RECOVERY, "status": "PARTIAL_FAILURE", "stop_reason": None,
        "request_reservations": 2, "http_reservations": 11, "results": {}}
    for index, (identifier, count) in enumerate(zip(requested, (40, 3))):
        result["results"][str(index)] = {"instrument_id": str(identifier), "status": "UNRESOLVED",
            "observations": count, "valid_sessions": count, "reason": "HISTORICAL_RANGE_INCOMPLETE",
            "remaining_windows": [{"start": "2026-10-01", "end_exclusive": "2026-10-02"}],
            "windows": [{"start": "2026-10-01", "end_exclusive": "2026-11-06",
                "status": "SUCCESS_WITH_DATA", "observations": count, "provider": "YAHOO_FINANCE",
                "first_date": "2026-10-05", "last_date": "2026-11-05", "preserved_conflicts": 0}]}
    report = cli.sanitized_report(result, request, apply=True)
    assert report["status"] == "PARTIAL_FAILURE"
    assert report["persisted_bar_count"] == 43
    assert report["provider_window_reservations"] == report["request_reservations"] == 2
    assert report["provider_window_budget"] == 2
    assert report["http_reservations"] == 11 and report["http_budget"] == 12
    assert report["publication"] == "UNCHANGED" and report["recommendation"] == "NOT_EVALUATED"
    assert [row["persisted_bar_count"] for row in report["instruments"]] == [40, 3]
    for row in report["instruments"]:
        assert row["recovery_outcome"] == "HISTORICAL_RANGE_INCOMPLETE"
        assert row["reason_class"] == "HISTORICAL_RANGE_INCOMPLETE"
        assert row["remaining_windows"]
        assert row["windows"][0]["provider_status"] == row["windows"][0]["status"] == "SUCCESS_WITH_DATA"
        assert row["windows"][0]["preserved_conflicts"] == 0
        assert row["windows"][0]["first_date"] == "2026-10-05"
        assert row["windows"][0]["last_date"] == "2026-11-05"


@pytest.mark.parametrize("status,reason,windows,remaining,outcome", [
    ("RECOVERED_APPROVED_RANGE", None, [{"status": "SUCCESS_WITH_DATA", "observations": 2}], [],
     "RECOVERED_APPROVED_RANGE"),
    ("UNRESOLVED", "YAHOO_EXPLICIT_CHART_ERROR:" + SECRET,
     [{"status": "TECHNICAL_FAILURE", "reason": "YAHOO_EXPLICIT_CHART_ERROR:" + SECRET,
       "observations": 0}], [{"start": "2026-10-01", "end_exclusive": "2026-11-06"}],
     "TECHNICAL_FAILURE"),
    ("UNRESOLVED", "ETF_REQUEST_BUDGET_EXHAUSTED", [],
     [{"start": "2026-10-01", "end_exclusive": "2026-11-06"}], "UNRESOLVED"),
    ("UNRESOLVED", "ETF_HTTP_BUDGET_EXHAUSTED",
     [{"status": "TECHNICAL_FAILURE", "reason": "ETF_HTTP_BUDGET_EXHAUSTED", "observations": 0}],
     [{"start": "2026-10-01", "end_exclusive": "2026-11-06"}], "UNRESOLVED"),
    ("UNRESOLVED", None, [{"status": "SUCCESS_EMPTY", "observations": 0}],
     [{"start": "2026-10-01", "end_exclusive": "2026-11-06"}], "HISTORICAL_RANGE_INCOMPLETE"),
])
def test_legacy_checkpoint_outcomes_distinguish_completion_technical_failure_and_stops(
        status, reason, windows, remaining, outcome):
    request = HistoryRecoveryRequest(SOURCE, (UUID(INSTRUMENT),), NOW.date(), NOW.date())
    result = {"recovery_id": RECOVERY, "status": "PARTIAL_FAILURE", "results": {"one": {
        "instrument_id": INSTRUMENT, "status": status, "reason": reason, "observations": 0,
        "windows": windows, "remaining_windows": remaining}}}
    report = cli.sanitized_report(result, request, apply=True)
    assert report["instruments"][0]["status"] == status
    assert report["instruments"][0]["recovery_outcome"] == outcome
    assert SECRET not in json.dumps(report)


def test_additive_recovery_outcome_and_persisted_count_do_not_require_checkpoint_migration():
    request = HistoryRecoveryRequest(SOURCE, (UUID(INSTRUMENT),), NOW.date(), NOW.date())
    result = {"recovery_id": RECOVERY, "status": "PARTIAL_FAILURE", "results": {"one": {
        "instrument_id": INSTRUMENT, "status": "UNRESOLVED", "observations": 4,
        "persisted_bar_count": 4, "recovery_outcome": "HISTORICAL_RANGE_INCOMPLETE",
        "windows": [{"status": "SUCCESS_WITH_DATA", "provider_status": "SUCCESS_WITH_DATA",
            "observations": 4, "persisted_bar_count": 4}]}}}
    report = cli.sanitized_report(result, request, apply=True)
    assert report["persisted_bar_count"] == 4
    assert report["instruments"][0]["observations"] == 4
    assert report["instruments"][0]["recovery_outcome"] == "HISTORICAL_RANGE_INCOMPLETE"
    assert report["instruments"][0]["windows"][0]["persisted_bar_count"] == 4


def test_provider_window_report_rejects_untrusted_fields_and_invalid_counts():
    request = HistoryRecoveryRequest(SOURCE, (UUID(INSTRUMENT),), NOW.date(), NOW.date())
    result = {"recovery_id": RECOVERY, "status": "PARTIAL_FAILURE", "results": {"one": {
        "instrument_id": INSTRUMENT, "status": "UNRESOLVED", "observations": True,
        "windows": [{"start": SECRET, "end_exclusive": "2026-11-06", "status": SECRET,
            "observations": -1, "preserved_conflicts": True, "first_date": SECRET,
            "last_date": "2026-11-05", "provider": SECRET, "reason": SECRET,
            "headers": {"Authorization": SECRET}, "body": SECRET}]}}}
    report = cli.sanitized_report(result, request, apply=True)
    row = report["instruments"][0]
    assert report["persisted_bar_count"] is None and row["persisted_bar_count"] is None
    window = row["windows"][0]
    assert window["provider_status"] == "UNRESOLVED" and window["reason_class"] == "UNCLASSIFIED_TECHNICAL_FAILURE"
    assert window["observations"] is None and window["preserved_conflicts"] is None
    assert window["start"] is None and window["first_date"] is None
    assert "provider" not in window and "headers" not in window and "body" not in window
    assert SECRET not in json.dumps(report)


@pytest.mark.parametrize("reason,windows,remaining,outcome", [
    (None, [{"status": "SUCCESS_WITH_DATA", "observations": 1}],
     [{"start": "2026-10-01", "end_exclusive": "2026-10-02"}], "HISTORICAL_RANGE_INCOMPLETE"),
    (None, [{"status": "TECHNICAL_FAILURE", "reason": "YAHOO_EXPLICIT_CHART_ERROR",
             "observations": 0}], [], "TECHNICAL_FAILURE"),
    ("HISTORICAL_RANGE_INCOMPLETE", [{"status": "TECHNICAL_FAILURE", "observations": 0}],
     [{"start": "2026-10-01", "end_exclusive": "2026-10-02"}], "TECHNICAL_FAILURE"),
    ("ETF_REQUEST_BUDGET_EXHAUSTED", [],
     [{"start": "2026-10-01", "end_exclusive": "2026-10-02"}], "UNRESOLVED"),
])
def test_contradictory_positive_outcome_cannot_override_unresolved_evidence(reason, windows, remaining, outcome):
    request = HistoryRecoveryRequest(SOURCE, (UUID(INSTRUMENT),), NOW.date(), NOW.date())
    result = {"recovery_id": RECOVERY, "status": "PARTIAL_FAILURE", "results": {"one": {
        "instrument_id": INSTRUMENT, "status": "UNRESOLVED", "observations": 0,
        "recovery_outcome": "RECOVERED_APPROVED_RANGE", "reason": reason,
        "windows": windows, "remaining_windows": remaining}}}
    report = cli.sanitized_report(result, request, apply=True)
    assert report["instruments"][0]["recovery_outcome"] == outcome
    assert report["recommendation"] == "NOT_EVALUATED"


@pytest.mark.parametrize("outcome", ["HISTORICAL_RANGE_INCOMPLETE", "UNRESOLVED"])
def test_conservative_additive_outcome_never_upgraded_from_legacy_positive_status(outcome):
    assert cli._recovery_outcome({"recovery_outcome": outcome,
        "status": "RECOVERED_APPROVED_RANGE", "remaining_windows": [], "reason": None}) == outcome


def test_valid_provider_bars_preserved_by_insert_only_still_report_provider_success():
    """Synthetic collision: two accepted provider bars, zero inserted rows."""
    request = HistoryRecoveryRequest(SOURCE, (UUID(INSTRUMENT),), NOW.date(), NOW.date())
    result = {"recovery_id": RECOVERY, "status": "COMPLETED_APPROVED_RANGE", "results": {"one": {
        "instrument_id": INSTRUMENT, "status": "RECOVERED_APPROVED_RANGE", "observations": 0,
        "persisted_bar_count": 0, "remaining_windows": [], "reason": None,
        "windows": [{"status": "SUCCESS_EMPTY", "provider_status": "SUCCESS_WITH_DATA",
            "observations": 0, "persisted_bar_count": 0, "accepted_provider_bar_count": 2,
            "preserved_conflicts": 2, "provider": "YAHOO_FINANCE"}]}}}
    report = cli.sanitized_report(result, request, apply=True)
    window = report["instruments"][0]["windows"][0]
    assert window["provider_status"] == "SUCCESS_WITH_DATA"
    assert window["accepted_provider_bar_count"] == 2
    assert window["persisted_bar_count"] == window["observations"] == 0
    assert window["preserved_conflicts"] == 2
    assert report["persisted_bar_count"] == 0
    assert report["instruments"][0]["recovery_outcome"] == "RECOVERED_APPROVED_RANGE"
