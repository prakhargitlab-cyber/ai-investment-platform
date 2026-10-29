"""Regression: Stage-2 deep-phase "completed" progress must count distinct
candidates, never raw attempts.

Root cause (confirmed via a real bounded Equity Radar run, Ellenbarrie
Industrial Gases, cycle 492a9e68-27b7-4624-b172-be6cfffe7910): a single
admitted candidate that DEEP_ACQUISITION_TIMEOUTs on its first attempt and
is then repaired logged "stage2_deep_progress: completed=2/1" -- impossible
for a cycle with exactly one admitted candidate. `deep_durable_completed_count`
(app/global_opportunity_orchestration.py) was a plain int incremented once
per completed acquisition BODY (`_checkpoint_deep_outcome` call), and a
repaired candidate's body completes once per attempt -- once for the
original (failing) attempt and again for the repair attempt -- so one
candidate repaired once pushed the counter to 2 against a denominator
(`len(deep_candidates)`) that only ever counts distinct candidates (1).

The fix tracks a *set* of distinct completed candidate keys instead of an
int, so completing the same candidate's body twice (original + repair)
still contributes exactly one member.

Provider-free: no real NSE/Yahoo/network calls, no deploy.
"""
from __future__ import annotations

import logging
from collections import Counter

import pytest

from app.cycle_checkpoint import CycleCheckpoint
from app.research_readiness import ResearchRequirementStatus
from app.research_readiness_runtime import TargetedEnsureResult

from test_global_opportunity_acquisition import acquisition_service
from test_global_scanner import NOW, instrument, persisted
from test_stock_rule_engine import _readiness

MANDATORY_REQ = "VALUATION_INPUTS"


def _parse_completed_over_total(message: str) -> tuple[int, int] | None:
    marker = "completed="
    if marker not in message:
        return None
    fragment = message.split(marker, 1)[1].split()[0]
    completed_str, total_str = fragment.split("/")
    return int(completed_str), int(total_str)


@pytest.mark.asyncio
async def test_single_repaired_candidate_never_reports_completed_above_one(monkeypatch):
    """One admitted candidate: first attempt fails technically (retryable),
    repair pass succeeds. Every "stage2_deep_progress" log line for this
    cycle must show completed<=1 (never 2) against total=1."""
    service, runtime, store = acquisition_service(monkeypatch)
    not_ready = _readiness(overrides={MANDATORY_REQ: ResearchRequirementStatus.MISSING}, critical_pct=50)
    ready = _readiness()
    calls = Counter()

    async def ensure(key, **kwargs):
        persisted(store, instrument(key.int))
        if kwargs.get("requirement_ids") is not None:
            return TargetedEnsureResult(None, (), ())
        attempt = calls[key]
        calls[key] += 1
        if attempt == 0:
            return TargetedEnsureResult(not_ready, (), (), failures={MANDATORY_REQ: "NETWORK_TIMEOUT"})
        return TargetedEnsureResult(ready, (), ())

    runtime.ensure.side_effect = ensure

    store.create_cycle_run("test-cycle-deep-progress", {})
    assert store.claim_cycle_run("test-cycle-deep-progress", "test-owner")
    checkpoint = CycleCheckpoint(store, "test-cycle-deep-progress", "test-owner", run_blocking=None)
    await checkpoint.load()

    import app.global_opportunity_orchestration as goo
    seen: list[str] = []
    original_info = goo.logger.info

    def recording_info(message, *args, **kwargs):
        if isinstance(message, str) and message.startswith("stage2_deep_progress"):
            seen.append(message % args if args else message)
        return original_info(message, *args, **kwargs)

    monkeypatch.setattr(goo.logger, "info", recording_info)

    result = await service.run([instrument(1)], as_of=NOW, shortlist_limit=1, checkpoint=checkpoint)

    assert len(result.diagnostics) == 1
    assert result.deep_repair_attempted_count == 1
    assert result.deep_repair_recovered_count == 1
    assert seen, "expected at least one stage2_deep_progress log line"
    for message in seen:
        parsed = _parse_completed_over_total(message)
        assert parsed is not None, message
        completed, total = parsed
        assert total == 1, message
        assert completed <= 1, f"completed exceeded the distinct-candidate total: {message}"
