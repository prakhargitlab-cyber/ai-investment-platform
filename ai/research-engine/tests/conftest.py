"""Test-process hygiene for app.background_task_registry's process-lifetime
singleton (app.background_task_registry.current_news_background_tasks).

In production there is exactly one persistent event loop for the life of
the process, so tasks added to this singleton always belong to the loop
that is later used to await/drain them. This test suite instead gives each
`@pytest.mark.asyncio` test its own event loop (the pytest-asyncio
default), so a task added to the singleton during one test belongs to a
loop that is closed by the time the next test runs; it can never complete
or fire its done-callback, so it would otherwise sit in the registry
forever, eventually exhausting its bounded capacity partway through a long
combined test run (see the consolidated-fix-pass report, Section H/I).

Resetting the singleton before each test is purely about dropping those
now-inert stale references -- it has no bearing on the actual production
correctness properties (nothing-orphaned, exceptions-observed,
shutdown-drains-safely), which are covered directly by
tests/test_background_task_registry.py and
tests/test_background_task_registry_shutdown.py against a *freshly
constructed* registry instance, not this shared one.
"""
import logging
from datetime import datetime, timezone

import pytest

from app.background_task_registry import current_news_background_tasks


@pytest.fixture
def financial_fixture_clock(monkeypatch, request):
    """Replay captured statements at their test's explicit observation date.

    Opt in per module so market-session and freshness-boundary tests retain
    their own clocks. Patch the consumers, never the freshness policies.
    """
    instant = getattr(request.module, "NOW", datetime(2026, 10, 3, tzinfo=timezone.utc))

    class FixtureDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

    for name in ("app.repository", "app.research_readiness", "app.research_readiness_runtime",
                 "app.yahoo_mcp_acquisition"):
        monkeypatch.setattr(f"{name}.datetime", FixtureDatetime)
    if hasattr(request.module, "datetime"):
        monkeypatch.setattr(request.module, "datetime", FixtureDatetime)


@pytest.fixture
def news_fixture_clock(monkeypatch):
    class NewsDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = datetime(2026, 9, 19, tzinfo=timezone.utc)
            return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

    monkeypatch.setattr("app.evidence_interpretation.datetime", NewsDatetime)


@pytest.fixture(autouse=True)
def _reset_background_task_registry():
    current_news_background_tasks.reset()
    yield
    current_news_background_tasks.reset()


@pytest.fixture(autouse=True)
def _isolate_app_logger_configuration():
    """Test-isolation only; does not touch app/main.py or app/settings.py.

    app.main's module-import side effect (`configure_application_logging`,
    called at module scope) permanently sets logging.getLogger("app")'s
    level to the configured research_log_level (WARNING by default) and
    its propagate flag to False -- a deliberate, one-time production
    startup decision (avoid duplicate output, respect the configured
    verbosity). That is correct in a real process with exactly one
    startup, but in this test session it fires the first time ANY test
    imports app.main (directly, or transitively e.g. via `from app.main
    import app` for a FastAPI TestClient) and then stays in effect,
    process-wide, for every test that runs afterward.

    That silently breaks any later test that uses `caplog` against an
    "app.*" child logger (e.g. app.etf_history, app.historical_market_data):
    the child's effective level climbs the hierarchy and gets clipped to
    WARNING at "app" regardless of the test's own `caplog.at_level(INFO)`,
    and even once level is accounted for, "app".propagate=False stops the
    record from ever reaching the root logger where caplog's own handler
    lives, so `caplog.records` comes back empty even though the code
    under test ran its logging statement. The effect is order-dependent:
    a test passes alone (app.main not yet imported in that process) and
    fails only when run after whichever test module first imports
    app.main -- this is exactly the symptom this fixture removes.
    One other test (test_financial_evidence_plan.py) already had to work
    around the propagate half of this ad hoc with its own
    `monkeypatch.setattr(logging.getLogger("app"), "propagate", True)`;
    this fixture generalizes that so no future caplog-based test on an
    app.* logger needs its own copy of the workaround.

    Scope is deliberately narrow: only the "app" logger's own `level` and
    `propagate` attributes are saved and restored around each test (NOT
    reset to some fixed value) -- NOTSET + propagate=True is restored to
    whatever the test needs to use as a baseline, and whatever
    configure_application_logging left behind is restored exactly
    afterward, so a test that explicitly calls configure_application_logging
    itself (as test_logging_configuration.py's own tests do) is unaffected:
    it still observes and restores its own before/after state, it only no
    longer inherits unrelated state some other, earlier test file's import
    happened to leave behind. No other logger, no handler list, and no
    production code is touched.
    """
    app_logger = logging.getLogger("app")
    original_level, original_propagate = app_logger.level, app_logger.propagate
    app_logger.setLevel(logging.NOTSET)
    app_logger.propagate = True
    yield
    app_logger.setLevel(original_level)
    app_logger.propagate = original_propagate
