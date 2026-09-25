from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from app.models import SourceType


@dataclass(frozen=True)
class ResearchScheduleRule:
    source_type: SourceType
    interval: timedelta
    max_documents_per_run: int


def default_schedule_rules() -> list[ResearchScheduleRule]:
    return [
        ResearchScheduleRule(SourceType.EXCHANGE_ANNOUNCEMENT, timedelta(hours=2), 20),
        ResearchScheduleRule(SourceType.REGULATORY_FILING, timedelta(hours=4), 20),
        ResearchScheduleRule(SourceType.INVESTOR_RELATIONS, timedelta(days=1), 10),
        ResearchScheduleRule(SourceType.COMPANY_WEBSITE, timedelta(days=3), 10),
        ResearchScheduleRule(SourceType.RSS, timedelta(hours=6), 20),
        ResearchScheduleRule(SourceType.NEWS, timedelta(hours=12), 20),
    ]


class OpportunitySchedulePhase(StrEnum):
    """Market-session phases for the system-owned global opportunity scan."""
    MARKET_HOURS = "MARKET_HOURS"
    POST_CLOSE = "POST_CLOSE"


@dataclass(frozen=True)
class OpportunityScheduleRule:
    """NSE market-session-aware scheduling for the global opportunity engine.

    Unlike ResearchScheduleRule (which polls a single source type), this rule
    governs when the system-owned full-market cycle is triggered. The ``phase``
    distinguishes between intra-session hourly scans and the single post-close
    review run.
    """
    market_code: str
    phase: OpportunitySchedulePhase
    interval: timedelta
    window: timedelta | None = None


def nse_opportunity_schedule() -> list[OpportunityScheduleRule]:
    return [
        OpportunityScheduleRule('NSE', OpportunitySchedulePhase.MARKET_HOURS, timedelta(hours=1)),
        OpportunityScheduleRule('NSE', OpportunitySchedulePhase.POST_CLOSE, timedelta(days=1), window=timedelta(hours=2)),
    ]
