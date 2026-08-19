from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

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
