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
import pytest

from app.background_task_registry import current_news_background_tasks


@pytest.fixture(autouse=True)
def _reset_background_task_registry():
    current_news_background_tasks.reset()
    yield
    current_news_background_tasks.reset()
