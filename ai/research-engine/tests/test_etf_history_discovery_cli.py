"""Discovery CLI boundaries; database and discovery API are mocked throughout."""
import asyncio
import copy
from datetime import date, datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest

from scripts import discover_etf_history_recovery as cli

SOURCE = "17c48038-507b-4924-b902-20f3b7df4091"
INSTRUMENT = str(UUID(int=7))
RECOVERY = str(UUID(int=8))
NOW = datetime(2026, 11, 6, 9, tzinfo=timezone.utc)
SECRET = "synthetic.secret-never-print"


def argv(*extra):
    return ["--source-cycle-id", SOURCE, *extra]


def report():
    eligible = {"source_cycle_id": SOURCE, "instrument_id": INSTRUMENT,
        "isin": "INF204KB14I2", "primary_symbol": "NIFTYBEES",
        "original_failure_classification": "YAHOO_FETCH_FAILED", "existing_valid_bar_count": 4,
        "first_available_historical_date": "2026-10-01", "last_available_historical_date": "2026-10-06",
        "coverage_scope": "APPROVED_RANGE_ONLY", "canonical_listing_date": None,
        "listing_date_verification_status": "MISSING",
        "missing_historical_windows": [{"start": "2026-10-07", "end_exclusive": "2026-11-06"}],
        "missing_completed_sessions": 21, "expected_completed_sessions": 25,
        "previous_recovery": {"recovery_id": RECOVERY, "checkpoint_status": "FAILED",
            "recovery_status": "PARTIAL_FAILURE",
            "instrument_status": "UNRESOLVED", "reason_classification": "EXTERNAL_PROVIDER_RATE_LIMITED",
            "terminal": True, "execution_authorized": False},
        "eligibility_status": "ELIGIBLE", "exclusion_reason": None, "recommended_priority": 1,
        "priority_rationale": "VERIFIED_PARTIAL_COVERAGE_WITH_PRIOR_RECOVERY",
        "older_history": "UNRESOLVED_OUTSIDE_APPROVED_RANGE"}
    excluded = {**copy.deepcopy(eligible), "instrument_id": None, "isin": None, "primary_symbol": None,
        "previous_recovery": None, "eligibility_status": "EXCLUDED",
        "exclusion_reason": "ETF_RECOVERY_CANONICAL_IDENTITY_NOT_VERIFIED", "recommended_priority": None,
        "priority_rationale": None, "existing_valid_bar_count": None, "first_available_historical_date": None,
        "last_available_historical_date": None, "missing_completed_sessions": None,
        "expected_completed_sessions": None, "missing_historical_windows": []}
    return {"kind": "ETF_HISTORY_RECOVERY_DISCOVERY", "mode": "READ_ONLY", "source_cycle_id": SOURCE,
        "as_of": NOW.isoformat(), "range": {"start": "2026-10-01", "end_exclusive": "2026-11-06"},
        "limit": 5, "summary": {"source_universe": 359, "original_failed_history_candidates": 2,
            "eligible_candidates": 1, "excluded_candidates": 1, "partially_recovered_candidates": 1,
            "returned_shortlist_count": 1}, "candidates": [eligible], "assessments": [eligible, excluded],
        "priority_policy": "VERIFIED_PARTIAL_THEN_MISSING_SESSIONS_DESC_SYMBOL_ISIN_UUID",
        "priority_is_provider_success_prediction": False, "execution_authorized": False,
        "provider_requests": 0, "publication": "UNCHANGED", "recommendation": "NOT_EVALUATED"}


@pytest.fixture
def environment(monkeypatch):
    connection = Mock()
    adapter = Mock(connection=connection)
    store = SimpleNamespace(_connection=adapter)
    # Read-only discovery needs no live/demo/persistence-enable/auth settings.
    settings = SimpleNamespace(research_database_backend="postgres", research_database_schema="research")
    settings_call = Mock(return_value=settings)
    open_store = Mock(return_value=store)
    api = AsyncMock(return_value=report())
    monkeypatch.setattr(cli.recovery, "_settings", settings_call)
    monkeypatch.setattr(cli.recovery, "_open_store", open_store)
    monkeypatch.setattr(cli, "_api", lambda: api)
    return SimpleNamespace(store=store, adapter=adapter, connection=connection, settings=settings,
        settings_call=settings_call, open_store=open_store, api=api)


def test_help_uses_no_database_settings_or_api(environment, capsys):
    with pytest.raises(SystemExit) as stop:
        cli.main(["--help"])
    assert stop.value.code == 0
    text = capsys.readouterr().out
    assert "--limit" in text and "--source-cycle-id" in text
    assert "--apply" not in text and "--confirm" not in text and "--token" not in text
    environment.settings_call.assert_not_called()
    environment.open_store.assert_not_called()
    environment.api.assert_not_awaited()


def test_help_in_fresh_process_imports_no_database_provider_or_worker():
    probe = """import sys
from scripts import discover_etf_history_recovery as c
try: c.main(['--help'])
except SystemExit as e: assert e.code == 0
for name in ('app.main', 'app.postgres_persistence', 'app.settings',
             'app.etf_history_discovery', 'app.etf_history_recovery', 'app.structured_market'):
    assert name not in sys.modules, name
"""
    process = subprocess.run([sys.executable, "-c", probe], cwd=Path(__file__).resolve().parents[1],
                             capture_output=True, text=True, timeout=10)
    assert process.returncode == 0, process.stderr


@pytest.mark.parametrize("arguments", [[], ["--source-cycle-id", SECRET],
    argv("--apply"), argv("--confirm", SECRET), argv("--recovery-id", RECOVERY),
    argv("--bearer-token", SECRET), argv("--instrument-id", INSTRUMENT), argv("--sou", SOURCE),
    argv("--start", SECRET), argv("--limit", SECRET)])
def test_bad_or_execution_flags_are_safely_refused_without_database(environment, capsys, arguments):
    assert cli.main(arguments) == 2
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert json.loads(captured.err) == {"error": "ETF_DISCOVERY_CLI_ARGUMENTS_INVALID"}
    environment.open_store.assert_not_called()
    environment.api.assert_not_awaited()


@pytest.mark.parametrize("extra,code", [
    (["--limit", "0"], "ETF_DISCOVERY_LIMIT_OUT_OF_BOUNDS"),
    (["--limit", "6"], "ETF_DISCOVERY_LIMIT_OUT_OF_BOUNDS"),
    (["--start", "2026-10-01"], "ETF_DISCOVERY_PAIRED_DATES_REQUIRED"),
    (["--end", "2026-11-06"], "ETF_DISCOVERY_PAIRED_DATES_REQUIRED"),
    (["--start", "2026-10-01", "--end", "2026-10-01"], "ETF_RECOVERY_RECENT_RANGE_REQUIRED_MAX_731_DAYS"),
    (["--start", "2024-01-01", "--end", "2026-11-06"], "ETF_RECOVERY_RECENT_RANGE_REQUIRED_MAX_731_DAYS"),
    (["--start", "2026-10-01", "--end", "2026-12-01"], "ETF_RECOVERY_RECENT_RANGE_REQUIRED_MAX_731_DAYS"),
])
def test_invalid_bounds_refused_before_database(environment, extra, code):
    with pytest.raises(ValueError, match=code):
        asyncio.run(cli.run(cli.parser().parse_args(argv(*extra)), now=NOW))
    environment.settings_call.assert_not_called()
    environment.open_store.assert_not_called()


@pytest.mark.parametrize("field,value,code", [
    ("research_database_backend", "sqlite", "ETF_RECOVERY_POSTGRES_REQUIRED"),
    ("research_database_schema", "portfolio", "ETF_RECOVERY_RESEARCH_SCHEMA_REQUIRED"),
])
def test_config_requires_existing_postgres_research_schema(environment, field, value, code):
    setattr(environment.settings, field, value)
    with pytest.raises(ValueError, match=code):
        asyncio.run(cli.run(cli.parser().parse_args(argv()), now=NOW))
    environment.open_store.assert_not_called()


def test_default_discovery_is_read_only_no_network_and_preserves_excluded_assessments(environment, monkeypatch):
    import httpx
    import socket
    import urllib.request
    denied = Mock(side_effect=AssertionError("No network in discovery"))
    monkeypatch.setattr(httpx, "AsyncClient", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(urllib.request, "urlopen", denied)
    result = asyncio.run(cli.run(cli.parser().parse_args(argv()), now=NOW))
    environment.api.assert_awaited_once()
    call = environment.api.call_args
    assert call.args[1] == SOURCE
    assert call.kwargs == {"limit": 5, "start": None, "end": None, "now": NOW}
    environment.adapter.execute.assert_called_once_with("SET default_transaction_read_only = on")
    environment.adapter.commit.assert_called_once_with()
    environment.connection.rollback.assert_called_once_with()
    environment.connection.close.assert_called_once_with()
    denied.assert_not_called()
    assert result["mode"] == "READ_ONLY" and result["execution_authorized"] is False
    assert result["provider_requests"] == 0 and result["summary"]["source_universe"] == 359
    assert len(result["assessments"]) == 2
    assert result["assessments"][1]["instrument_id"] is None
    assert result["assessments"][1]["eligibility_status"] == "EXCLUDED"
    assert result["candidates"][0]["previous_recovery"]["terminal"] is True
    assert result["candidates"][0]["previous_recovery"]["checkpoint_status"] == "FAILED"
    assert result["candidates"][0]["previous_recovery"]["recovery_status"] == "PARTIAL_FAILURE"
    assert result["candidates"][0]["priority_rationale"] == "VERIFIED_PARTIAL_COVERAGE_WITH_PRIOR_RECOVERY"


def test_explicit_dates_and_limit_are_passed_to_database_only_api(environment):
    environment.api.return_value["limit"] = 1
    result = asyncio.run(cli.run(cli.parser().parse_args(argv("--limit", "1", "--start", "2026-10-01",
        "--end", "2026-11-06")), now=NOW))
    assert environment.api.call_args.kwargs["start"] == date(2026, 10, 1)
    assert environment.api.call_args.kwargs["end"] == date(2026, 11, 6)
    assert environment.api.call_args.kwargs["limit"] == result["limit"] == 1


def test_successful_main_prints_one_structured_sanitized_json(environment, capsys):
    assert cli.main(argv()) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert captured.err == "" and result["kind"] == "ETF_HISTORY_RECOVERY_DISCOVERY"
    assert result["publication"] == "UNCHANGED" and result["recommendation"] == "NOT_EVALUATED"


@pytest.mark.parametrize("code", ["ETF_RECOVERY_COMPLETED_ETF_SOURCE_REQUIRED", "RESEARCH_SCHEMA_NOT_MIGRATED",
    "ETF_DISCOVERY_INVALID_RECOVERY_METADATA", "READ_ONLY_SNAPSHOT_REQUIRES_IDLE_CONNECTION",
    "READ_ONLY_SNAPSHOT_NOT_ENFORCED"])
def test_expected_errors_retain_safe_classification_and_close_connection(environment, capsys, code):
    environment.api.side_effect = ValueError(code + ":" + SECRET)
    assert cli.main(argv()) == 2
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert json.loads(captured.err)["error"] == code
    environment.connection.close.assert_called_once_with()


def test_unexpected_errors_do_not_print_database_credentials_or_tokens(environment, capsys):
    environment.api.side_effect = RuntimeError("postgresql://user:" + SECRET + "@host/database Authorization=" + SECRET)
    assert cli.main(argv()) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and SECRET not in captured.err and "postgresql" not in captured.err
    assert json.loads(captured.err)["error"] == "UNCLASSIFIED_TECHNICAL_FAILURE"
    environment.connection.close.assert_called_once_with()


def test_unknown_fields_raw_failure_bodies_and_bad_identity_text_are_never_emitted():
    value = report()
    value["Authorization"] = SECRET
    value["candidates"][0]["headers"] = {"Authorization": SECRET}
    value["candidates"][0]["original_failure_classification"] = "YAHOO_EXPLICIT_CHART_ERROR:" + SECRET
    value["assessments"][1]["isin"] = SECRET
    value["assessments"][1]["primary_symbol"] = SECRET
    result = cli.sanitized_report(value, cli.parser().parse_args(argv()))
    assert SECRET not in json.dumps(result)
    assert result["candidates"][0]["original_failure_classification"] == "YAHOO_EXPLICIT_CHART_ERROR"
    assert result["assessments"][1]["isin"] is None and result["assessments"][1]["primary_symbol"] is None


def test_older_previous_recovery_without_selection_status_preserves_unknown():
    value = report()
    value["candidates"][0]["previous_recovery"].pop("recovery_status")
    result = cli.sanitized_report(value, cli.parser().parse_args(argv()))
    previous = result["candidates"][0]["previous_recovery"]
    assert previous["recovery_status"] is None
    assert previous["checkpoint_status"] == "FAILED"


@pytest.mark.parametrize("mutate", [
    lambda x: x.update(provider_requests=1),
    lambda x: x.update(execution_authorized=True),
    lambda x: x.update(mode="APPLY"),
    lambda x: x.update(source_cycle_id=str(UUID(int=999))),
    lambda x: x.update(assessments=None),
    lambda x: x["summary"].update(eligible_candidates=2),
    lambda x: x["range"].update(start="not-a-date"),
    lambda x: x["candidates"][0].update(priority_rationale=SECRET),
    lambda x: x["candidates"][0]["previous_recovery"].update(checkpoint_status=SECRET),
    lambda x: x["candidates"][0]["previous_recovery"].update(recovery_status=SECRET),
])
def test_invalid_response_contract_is_explicit_not_fabricated_empty_result(environment, mutate, capsys):
    mutate(environment.api.return_value)
    assert cli.main(argv()) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and SECRET not in captured.err
    assert json.loads(captured.err)["error"] == "ETF_DISCOVERY_INVALID_RESULT"
    environment.connection.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_cancelled_database_open_drains_and_closes_connection(environment, monkeypatch):
    started, release = threading.Event(), threading.Event()
    def open_store(_settings):
        started.set()
        assert release.wait(3)
        return environment.store
    monkeypatch.setattr(cli.recovery, "_open_store", open_store)
    task = asyncio.create_task(cli.run(cli.parser().parse_args(argv()), now=NOW))
    while not started.is_set():
        await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    environment.api.assert_not_awaited()
    environment.connection.close.assert_called_once_with()
