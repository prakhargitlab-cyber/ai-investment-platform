"""Forensic analysis of controlled cycle 89fd630e-5212-4018-ba6d-09889f83822e
(correlation_id 5866a2ff-4110-499f-8b4a-51f9910bc4a2).

Issue 1 -- READINESS/PERSISTENCE AMPLIFICATION:
ResearchReadinessService.assess was called 1147 times for 25 Stage-2
candidates (~46/candidate), each one re-running
RepositoryResearchReadinessAdapter.load_by_global_instrument_id -- eight
separate repository reads -- via `_run_blocking_persistence`'s thread-
dispatch path (total_exec_ms=141024, total_wait_ms=66763).

Root cause: `ResearchReadinessRuntime.read()` already memoizes evidence_only
reads using `repository._readiness_mutation_generation` (bumped on every
persistence write) as the staleness sentinel -- but six call sites inside
`McpFirstResearchCapabilityExecutor.execute_primary`
(app/yahoo_mcp_acquisition.py) and two inside
`ExistingResearchCapabilityExecutor.execute_primary`
(app/research_readiness_runtime.py) each construct their OWN throwaway
`ResearchReadinessService(RepositoryResearchReadinessAdapter(self.repository))`
and call `.assess` directly through `_run_blocking_persistence`, bypassing
that cache entirely. Two such calls with no durable write committed in
between therefore pay the full reload cost twice for identical state.

Fix: `memoized_assess()` (new, app/research_readiness_runtime.py) applies the
exact same already-trusted generation-counter invalidation rule, scoped to a
`memo` dict each execute_primary call creates fresh for itself (never shared
across candidates or calls, never module/global state). A cached result is
returned only when the mutation generation has not advanced since it was
captured and `now` was not explicitly pinned; any committed write
immediately invalidates it. All six yahoo_mcp_acquisition.py sites and both
remaining research_readiness_runtime.py sites were switched to call this
helper instead of constructing ResearchReadinessService directly.
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

import app.research_readiness_runtime as rrr
from app.research_readiness_runtime import memoized_assess

INSTRUMENT_ID = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")


class _CountingReadinessService:
    """Stand-in for ResearchReadinessService that counts real assess() calls
    without needing a full fake repository (RepositoryResearchReadinessAdapter
    is still constructed for real -- cheap, does no I/O at __init__ -- only
    the expensive `.assess()` call itself is replaced)."""

    calls = 0

    def __init__(self, adapter) -> None:
        self.adapter = adapter

    def assess(self, instrument_id, *, jurisdiction, evidence_only=False, now=None):
        type(self).calls += 1
        return SimpleNamespace(
            instrument_id=instrument_id, jurisdiction=jurisdiction,
            evidence_only=evidence_only, now=now, token=type(self).calls,
        )


class _FakeRepository:
    def __init__(self) -> None:
        self._readiness_mutation_generation = 0

    async def _run_blocking_persistence(self, operation, *args, **kwargs):
        return operation(*args, **kwargs)


@pytest.fixture(autouse=True)
def _patch_service(monkeypatch):
    _CountingReadinessService.calls = 0
    monkeypatch.setattr(rrr, "ResearchReadinessService", _CountingReadinessService)
    yield


@pytest.mark.asyncio
async def test_repeated_calls_with_no_write_between_them_reuse_one_load() -> None:
    repo = _FakeRepository()
    memo: dict = {}

    first = await memoized_assess(repo, memo, INSTRUMENT_ID, jurisdiction="INDIA")
    second = await memoized_assess(repo, memo, INSTRUMENT_ID, jurisdiction="INDIA")
    third = await memoized_assess(repo, memo, INSTRUMENT_ID, jurisdiction="INDIA")

    assert _CountingReadinessService.calls == 1, "three identical reads with no intervening write must load once"
    assert first is second is third


@pytest.mark.asyncio
async def test_a_committed_write_between_calls_forces_a_fresh_reload() -> None:
    """Final readiness must still come from current durable evidence
    (Invariant 1) -- a write between two memoized_assess calls must never be
    masked by the memo."""
    repo = _FakeRepository()
    memo: dict = {}

    first = await memoized_assess(repo, memo, INSTRUMENT_ID, jurisdiction="INDIA")
    repo._readiness_mutation_generation += 1  # simulates a committed persistence write
    second = await memoized_assess(repo, memo, INSTRUMENT_ID, jurisdiction="INDIA")

    assert _CountingReadinessService.calls == 2
    assert first is not second


@pytest.mark.asyncio
async def test_evidence_only_and_durable_reads_are_not_conflated() -> None:
    """A different `evidence_only` mode is semantically a different read and
    must not reuse the other mode's cached result."""
    repo = _FakeRepository()
    memo: dict = {}

    durable = await memoized_assess(repo, memo, INSTRUMENT_ID, jurisdiction="INDIA", evidence_only=False)
    evidence_only = await memoized_assess(repo, memo, INSTRUMENT_ID, jurisdiction="INDIA", evidence_only=True)

    assert _CountingReadinessService.calls == 2
    assert durable is not evidence_only


@pytest.mark.asyncio
async def test_two_candidates_never_share_a_memo() -> None:
    """Memo dicts are created fresh per execute_primary call -- never shared
    across candidates (no cross-candidate caching)."""
    repo = _FakeRepository()
    other_instrument = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")

    memo_a: dict = {}
    memo_b: dict = {}
    await memoized_assess(repo, memo_a, INSTRUMENT_ID, jurisdiction="INDIA")
    await memoized_assess(repo, memo_b, other_instrument, jurisdiction="INDIA")

    assert _CountingReadinessService.calls == 2


@pytest.mark.asyncio
async def test_an_explicit_now_is_never_served_from_cache() -> None:
    """A caller pinning an explicit `now` wants that exact evaluation
    instant; the memo must not silently substitute an earlier snapshot."""
    repo = _FakeRepository()
    memo: dict = {}
    from datetime import datetime, timezone

    pinned = datetime(2026, 1, 1, tzinfo=timezone.utc)
    await memoized_assess(repo, memo, INSTRUMENT_ID, jurisdiction="INDIA")
    await memoized_assess(repo, memo, INSTRUMENT_ID, jurisdiction="INDIA", now=pinned)
    await memoized_assess(repo, memo, INSTRUMENT_ID, jurisdiction="INDIA", now=pinned)

    assert _CountingReadinessService.calls == 3, "an explicit `now` always reloads and is never itself cached"


@pytest.mark.asyncio
async def test_missing_generation_counter_disables_caching_safely() -> None:
    """A repository with no `_readiness_mutation_generation` attribute (a
    minimal/legacy fixture) must still work correctly -- just without the
    optimization -- never raise and never serve stale state."""
    class _NoGenerationRepo:
        async def _run_blocking_persistence(self, operation, *args, **kwargs):
            return operation(*args, **kwargs)

    repo = _NoGenerationRepo()
    memo: dict = {}
    first = await memoized_assess(repo, memo, INSTRUMENT_ID, jurisdiction="INDIA")
    second = await memoized_assess(repo, memo, INSTRUMENT_ID, jurisdiction="INDIA")

    assert _CountingReadinessService.calls == 2
    assert first is not second
