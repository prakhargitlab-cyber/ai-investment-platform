"""Regression coverage for GlobalOpportunityOrchestrator._run_blocking -- the
caller-side offload boundary added for the GLOBAL SCANNER EVENT-LOOP SAFETY
follow-up fix.

_run_blocking is deliberately a thin reuse of ResearchRepository's existing,
already thread-safety-audited executor boundary
(ResearchRepository._run_blocking_persistence), not a new threading/locking
mechanism. These tests prove:

  1. When the repository exposes _run_blocking_persistence (the normal
     case), _run_blocking delegates to it with the exact operation/args/
     kwargs, rather than re-implementing offload logic.
  2. When it does not (e.g. a minimal test double), _run_blocking falls
     back to safe inline execution -- never silently drops the call.
  3. self.persistence (handed to GlobalScanner and used throughout run())
     is literally the same persistence object repository.persistence
     exposes, and therefore the same object repository's
     _persistence_worker_lock is scoped to -- confirming the offload reuse
     is safe by construction, not by assumption.
"""
from unittest.mock import AsyncMock

import pytest

from app.global_opportunity_orchestration import GlobalOpportunityOrchestrator
from app.persistence import SqliteResearchPersistence
from app.repository import ResearchRepository


def _make_orchestrator(repository, persistence):
    return GlobalOpportunityOrchestrator(
        repository, persistence,
        profile_hydrator=lambda *a, **k: True,
    )


def test_persistence_is_the_same_object_the_repository_lock_protects():
    """GlobalOpportunityOrchestrator is always constructed (in
    run_global_opportunity_cycle) as
    GlobalOpportunityOrchestrator(repository, repository.persistence, ...).
    self.persistence must therefore be identical (is, not ==) to
    repository.persistence / repository._persistence -- the object
    repository._persistence_worker_lock serializes access to -- otherwise
    reusing that lock via _run_blocking_persistence would not actually
    serialize access to the connection GlobalScanner reads from."""
    store = SqliteResearchPersistence()
    repo = ResearchRepository(persistence=store)
    orchestrator = _make_orchestrator(repo, repo.persistence)

    assert orchestrator.persistence is repo.persistence
    assert orchestrator.persistence is repo._persistence
    assert orchestrator.persistence is store


@pytest.mark.asyncio
async def test_run_blocking_delegates_to_repository_boundary_when_present():
    store = SqliteResearchPersistence()
    repo = ResearchRepository(persistence=store)
    orchestrator = _make_orchestrator(repo, repo.persistence)

    repo._run_blocking_persistence = AsyncMock(return_value="delegated-result")

    def operation(a, b, kw=None):
        return (a, b, kw)

    result = await orchestrator._run_blocking(operation, 1, 2, kw="x")

    assert result == "delegated-result"
    repo._run_blocking_persistence.assert_awaited_once_with(operation, 1, 2, kw="x")


@pytest.mark.asyncio
async def test_run_blocking_executes_the_real_boundary_correctly():
    """Without any monkeypatching: for the SQLite backend, the real
    ResearchRepository._run_blocking_persistence runs the operation inline
    and returns its actual result unchanged."""
    store = SqliteResearchPersistence()
    repo = ResearchRepository(persistence=store)
    orchestrator = _make_orchestrator(repo, repo.persistence)

    result = await orchestrator._run_blocking(lambda x: x * 2, 21)

    assert result == 42


@pytest.mark.asyncio
async def test_run_blocking_falls_back_to_inline_when_boundary_absent():
    """A minimal repository stand-in without _run_blocking_persistence (as
    might appear in a lightweight test double) must not break -- the call
    still executes, just inline, rather than raising AttributeError."""
    class _BareRepository:
        pass

    store = SqliteResearchPersistence()
    orchestrator = _make_orchestrator(_BareRepository(), store)

    result = await orchestrator._run_blocking(lambda x, y: x + y, 3, 4)

    assert result == 7
