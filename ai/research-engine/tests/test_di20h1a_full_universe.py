"""Full-universe Radar coverage, using only fake providers and in-memory stores."""
from dataclasses import replace
from uuid import UUID

import pytest

from app.global_scanner import GlobalScanner, GlobalPreScore
from test_global_opportunity_baseline import setup_acquisition
from test_global_opportunity_ranker import inputs
from test_global_scanner import NOW
from test_stock_rule_engine import _readiness


@pytest.mark.asyncio
async def test_late_unready_nominee_reaches_rules_and_wins_despite_earlier_failure(monkeypatch):
    service, rows, pairs, _, tracker = setup_acquisition(monkeypatch, count=40)
    late = UUID(int=40)
    pairs[late] = inputs(40, core=100)
    original_score = GlobalPreScore.score

    def score(self, row, *args, **kwargs):
        candidate = original_score(self, row, *args, **kwargs)
        if candidate.global_instrument_id == late:
            # Preliminary market readiness must not exclude a stock whose
            # actual mandatory applicable evidence permits rule evaluation.
            return candidate.model_copy(update={'eligible_for_deep_analysis': False, 'pre_score': 0})
        return candidate

    monkeypatch.setattr(GlobalPreScore, 'score', score)
    monkeypatch.setattr(GlobalScanner, 'enrich_candidates', lambda self, scan, **kw:
        [pairs[c.global_instrument_id][0] for c in scan.candidates])
    considered = []

    async def read(key, **kwargs):
        if not considered or considered[-1] != key:
            considered.append(key)
        if key == UUID(int=2):
            raise RuntimeError('one failed investigation')
        return replace(_readiness(), global_instrument_id=key)

    service.readiness.read = read
    result = await service.run(rows, as_of=NOW, shortlist_limit=25, top_n=1, discovery_v2=True)
    assert result.shortlist_count == 25
    assert result.deep_attempted_count == 40
    assert set(considered) == {UUID(int=n) for n in range(1, 41)}
    assert considered.index(late) >= 25
    assert result.investigation_matrix[str(late)]['rule_evaluated']
    assert result.top_n[0].global_instrument_id == late
    assert result.rule_analyzed_count == 39
    assert result.stage2_internal_error_count == 1
    assert all(row['disposition'] for row in result.investigation_matrix.values())
    # The fully fresh deep evidence requires no additional ensure calls.
    assert len(tracker.calls) == len(tracker.baseline_calls) == 40
