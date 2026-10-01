"""Consolidated fix pass -- Section H/I: process shutdown must safely drain
the bounded background task registry (app.background_task_registry) rather
than orphaning outstanding optional CURRENT_NEWS work. Exercises the actual
wiring in app.main.research_lifespan (not a reimplementation of it).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import app.main as main_module
from app.background_task_registry import current_news_background_tasks


@pytest.mark.asyncio
async def test_shutdown_drains_outstanding_background_task():
    # research_lifespan is a plain @asynccontextmanager function -- drive it
    # directly on this test's own event loop (rather than through
    # TestClient, whose synchronous wrapper runs the ASGI lifespan on a
    # separate loop/thread via a portal, which would make a task created
    # here un-awaitable from there -- an artifact of that test harness, not
    # of the real single-event-loop ASGI server this code actually runs
    # under). This still exercises the real wiring in app.main, not a
    # reimplementation of it.
    finished = {"flag": False}

    async def _slow():
        await asyncio.sleep(0.05)
        finished["flag"] = True

    current_news_background_tasks.add(asyncio.ensure_future(_slow()))
    assert len(current_news_background_tasks) == 1

    application = SimpleNamespace(state=SimpleNamespace())
    async with main_module.research_lifespan(application):
        pass  # startup then immediate shutdown

    assert finished["flag"] is True
    assert len(current_news_background_tasks) == 0
