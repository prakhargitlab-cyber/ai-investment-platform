"""Repository-level tests proving Shareholding reporting-period grouping
is SEMANTIC (year, month), not keyed by the exact period_end datetime --
so a MONTH-normalized period_end (e.g. 2026-06-01, from a "Jun 2026"
source) and an explicit-date period_end for the same reporting period
(e.g. 2026-06-30, from an explicit "30 Jun 2026" source) are recognized
as the SAME Jun-2026 reporting period, never as two independent periods
merely because their day differs.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from app.models import (
    ReliabilityLevel, ShareholdingCategory, ShareholdingSnapshot,
    ShareholdingSnapshotValue, SourceMode,
)
from app.repository import (
    ResearchRepository, _is_quarter_end, _is_shareholding_quarter_period,
    _shareholding_period_key,
)
from app.settings import Settings


def _snapshot(instrument_id, period_end, provider, promoter_pct="74.24"):
    return ShareholdingSnapshot(
        instrument_id=instrument_id, period_end=period_end, source_mode=SourceMode.REAL,
        source_provider=provider, source_type="MANUAL_UPLOAD", reliability_level=ReliabilityLevel.LEVEL_D,
        source_identity_key=f"{provider}-{period_end.isoformat()}",
        published_at=None, retrieved_at=datetime.now(timezone.utc),
        source_url="https://example.test/manual", confidence=0.5,
        values=[ShareholdingSnapshotValue(category=ShareholdingCategory.PROMOTER, percentage=Decimal(promoter_pct))],
    )


class TestIsShareholdingQuarterPeriod:
    def test_accepts_explicit_quarter_end_day(self):
        assert _is_shareholding_quarter_period(datetime(2026, 6, 30, tzinfo=timezone.utc)) is True
        assert _is_shareholding_quarter_period(datetime(2026, 3, 31, tzinfo=timezone.utc)) is True

    def test_accepts_month_normalized_first_of_month(self):
        assert _is_shareholding_quarter_period(datetime(2026, 6, 1, tzinfo=timezone.utc)) is True

    def test_rejects_arbitrary_mid_month_day(self):
        assert _is_shareholding_quarter_period(datetime(2026, 6, 15, tzinfo=timezone.utc)) is False

    def test_rejects_non_quarter_month(self):
        assert _is_shareholding_quarter_period(datetime(2026, 7, 1, tzinfo=timezone.utc)) is False

    def test_generic_is_quarter_end_is_unaffected_by_this_change(self):
        """The generic, exact-day _is_quarter_end helper (used by other,
        non-shareholding-grouping callers) must keep its original, exact
        semantics -- this task adds a shareholding-specific adaptation,
        it does not change the generic helper."""
        assert _is_quarter_end(datetime(2026, 6, 1, tzinfo=timezone.utc)) is False
        assert _is_quarter_end(datetime(2026, 6, 30, tzinfo=timezone.utc)) is True


class TestShareholdingPeriodKey:
    def test_month_normalized_and_explicit_date_share_the_same_key(self):
        assert _shareholding_period_key(datetime(2026, 6, 1, tzinfo=timezone.utc)) == \
            _shareholding_period_key(datetime(2026, 6, 30, tzinfo=timezone.utc)) == (2026, 6)

    def test_different_months_have_different_keys(self):
        assert _shareholding_period_key(datetime(2026, 3, 1, tzinfo=timezone.utc)) != \
            _shareholding_period_key(datetime(2026, 6, 1, tzinfo=timezone.utc))


class TestSemanticGroupingThroughRealRepository:
    def test_month_normalized_and_explicit_date_snapshots_are_one_group(self):
        repo = ResearchRepository(settings=Settings())
        instrument_id = uuid4()
        repo.persist_shareholding_snapshot(
            _snapshot(instrument_id, datetime(2026, 6, 1, tzinfo=timezone.utc), "USER_UPLOAD")
        )
        repo.persist_shareholding_snapshot(
            _snapshot(instrument_id, datetime(2026, 6, 30, tzinfo=timezone.utc), "USER_UPLOAD")
        )

        groups = repo.shareholding_period_groups(instrument_id, limit=4)
        assert len(groups) == 1
        assert len(groups[0]) == 2

    def test_month_normalized_evidence_alone_remains_visible_to_shareholding_for(self):
        """Proves a MONTH-normalized (2026-06-01) Shareholding snapshot,
        entirely on its own (no explicit-date companion), is NOT dropped
        by the real repository read path -- the exact regression this
        closure fixes."""
        repo = ResearchRepository(settings=Settings())
        instrument_id = uuid4()
        repo.persist_shareholding_snapshot(
            _snapshot(instrument_id, datetime(2026, 6, 1, tzinfo=timezone.utc), "USER_UPLOAD")
        )

        latest = repo.shareholding_for(instrument_id, limit=4)
        assert len(latest) == 1
        assert latest[0].period_end == datetime(2026, 6, 1, tzinfo=timezone.utc)

    def test_distinct_months_remain_distinct_groups(self):
        repo = ResearchRepository(settings=Settings())
        instrument_id = uuid4()
        repo.persist_shareholding_snapshot(
            _snapshot(instrument_id, datetime(2026, 3, 1, tzinfo=timezone.utc), "USER_UPLOAD")
        )
        repo.persist_shareholding_snapshot(
            _snapshot(instrument_id, datetime(2026, 6, 1, tzinfo=timezone.utc), "USER_UPLOAD")
        )
        groups = repo.shareholding_period_groups(instrument_id, limit=4)
        assert len(groups) == 2

    def test_duplicate_same_month_precedence_uses_existing_authority_rules(self):
        """When both an explicit-date and a MONTH-normalized snapshot
        exist for the same semantic period, the existing authority/
        provenance tie-breakers (never touched by this change) still
        decide ordering WITHIN that one group -- this test only proves
        they land in the same group and both remain visible, not that
        USER_UPLOAD authority rules changed."""
        repo = ResearchRepository(settings=Settings())
        instrument_id = uuid4()
        repo.persist_shareholding_snapshot(
            _snapshot(instrument_id, datetime(2026, 6, 1, tzinfo=timezone.utc), "USER_UPLOAD", promoter_pct="74.00")
        )
        repo.persist_shareholding_snapshot(
            _snapshot(instrument_id, datetime(2026, 6, 30, tzinfo=timezone.utc), "USER_UPLOAD", promoter_pct="74.24")
        )
        groups = repo.shareholding_period_groups(instrument_id, limit=4)
        assert len(groups) == 1
        assert {s.period_end for s in groups[0]} == {
            datetime(2026, 6, 1, tzinfo=timezone.utc), datetime(2026, 6, 30, tzinfo=timezone.utc),
        }
