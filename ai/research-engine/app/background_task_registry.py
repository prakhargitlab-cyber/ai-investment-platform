"""Application/runtime-owned registry for bounded, tracked background work
that must outlive the single async call/phase that created it.

Today's only user is Stage-2's optional CURRENT_NEWS acquisition (see
app/deep_investigation.py's `investigate(..., background_tasks=...)` and
app/global_opportunity_orchestration.py's Stage-2 phase). Before this
module existed, CURRENT_NEWS acquisitions were tracked in a *phase-local*
`set` that the Stage-2 phase's own `finally:` block synchronously drained
(`await asyncio.gather(*pending_news_tasks, ...)`) before considering
itself -- and therefore the whole cycle -- complete. That kept every
correctness property (nothing orphaned, every exception observed, every
outcome persisted before shutdown) but meant a cycle's *mandatory*
completion still waited on the *slowest* of its optional per-candidate news
searches, which is exactly what CURRENT_NEWS's optional/non-blocking
contract (see StockRuleEngineEligibilityPolicy._current_news_is_optional_
and_non_blocking) says must not happen.

This registry keeps the same correctness properties without that wait:

* A task added here is never garbage-collected mid-flight -- a live
  reference is retained (in `_tasks`) until it finishes.
* Its outcome is always observed: a `done_callback` retrieves any
  exception and logs it, so nothing becomes an unhandled background-task
  exception, whether or not anything ever explicitly awaits the task.
* Nothing that adds a task here is ever itself awaited by the code that
  added it -- so mandatory Stage-2/cycle completion never blocks on it.
* It is bounded: `has_capacity()` lets a caller check, *before* creating a
  task and choosing to background it, whether the registry is already at
  its cap. The cap never cancels or drops a task that is already running;
  it only stops *new* work from being added to unbounded background
  growth. The established fallback at the call site (see
  deep_investigation.py) is to acquire synchronously instead when at
  capacity -- self-throttling without ever losing evidence.
* `drain()` lets the *process* (not any single cycle) wait for whatever is
  still outstanding, with an optional timeout so shutdown can't hang
  forever. See app/main.py's `research_lifespan` for the shutdown call
  site, which mirrors the pre-existing `research_readiness_runtime._flights`
  drain pattern already used there for a different kind of tracked task.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Safety valve against unbounded memory growth if a very large production
# universe cycle submits background CURRENT_NEWS acquisitions faster than
# they complete. Not a concurrency limit on any other resource (provider
# rate limits / cooldowns are enforced independently, unchanged by this
# module) -- purely a cap on how many tasks this registry will track at
# once before callers are expected to fall back to inline acquisition.
DEFAULT_MAX_TRACKED_TASKS = 32


class BoundedBackgroundTaskRegistry:
    """Tracks fire-and-forget-but-observed asyncio tasks. See module
    docstring for the full contract."""

    def __init__(self, max_tasks: int = DEFAULT_MAX_TRACKED_TASKS):
        self._max_tasks = max_tasks
        self._tasks: set = set()

    def has_capacity(self) -> bool:
        return len(self._tasks) < self._max_tasks

    def __len__(self) -> int:
        return len(self._tasks)

    def add(self, task):
        """Track `task` until it completes. Deliberately matches the
        `set.add(task)` interface (single positional arg, no return value
        required to be used) so this registry is a drop-in replacement
        anywhere a caller-owned `set` was previously passed as
        `background_tasks=` -- no call-site changes needed beyond
        constructing the task the same way as before."""
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("background_task_exception task=%r error=%r", task.get_name(), exc)

    def reset(self) -> None:
        """Drop every tracked reference without awaiting or cancelling any
        of them. NEVER call this in production -- it does not observe
        outcomes or let anything persist, which is exactly what this
        registry exists to guarantee. It exists only for test-process
        hygiene: an asyncio.Task belongs to the event loop it was created
        on, and test suites that use a fresh event loop per test (the
        pytest-asyncio default) will otherwise leave this process-lifetime
        singleton holding references to tasks whose loop has already
        closed -- inert, but permanently occupying registry capacity across
        the rest of that test process. See tests/conftest.py."""
        self._tasks.clear()

    async def drain(self, *, timeout: Optional[float] = None) -> None:
        """Await every currently-tracked task. For process shutdown only --
        calling this from any per-cycle code path reintroduces the exact
        synchronous phase-end wait this registry exists to avoid."""
        pending = list(self._tasks)
        if not pending:
            return
        if timeout is None:
            await asyncio.gather(*pending, return_exceptions=True)
            return
        _done, still_pending = await asyncio.wait(pending, timeout=timeout)
        for task in still_pending:
            logger.warning("background_task_shutdown_timeout task=%r", task.get_name())


# Process-lifetime singleton. Deliberately module-level rather than owned by
# GlobalOpportunityOrchestrator: that orchestrator is constructed fresh for
# every cycle (see global_opportunity_cycle.py) and torn down as soon as
# that cycle's `run()` returns, so an instance attribute there cannot
# outlive a single phase any better than the local `set` it would replace.
current_news_background_tasks = BoundedBackgroundTaskRegistry()
