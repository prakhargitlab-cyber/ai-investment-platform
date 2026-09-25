"""Global (non-instrument-scoped) macro event-calendar model: future/past
scheduled central-bank meetings (FOMC, RBI MPC).

Deliberately kept separate from app/macro_observation.py: a macro_event has
no actual_value and no consensus/expected/surprise fields -- it is a
calendar row, not a released observation. Nothing here is consumed by the
Rule Engine, readiness, or ranking.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import AwareDatetime, model_validator

from app.macro_observation import RBI_REPO_RATE, FED_POLICY_RATE
from app.models import ResearchBaseModel

# The only event type modelled in this iteration.
CENTRAL_BANK_MEETING = "CENTRAL_BANK_MEETING"

SUPPORTED_EVENT_INDICATORS = frozenset({RBI_REPO_RATE, FED_POLICY_RATE})


def macro_event_id(event_type: str, indicator: str, region: str, scheduled_at: date) -> str:
    """Deterministic id -- (event_type, indicator, region, scheduled_at) is
    the natural key, so re-acquiring the same meeting from the same source
    always upserts the same row instead of duplicating it."""
    return f"{event_type}:{indicator}:{region}:{scheduled_at.isoformat()}"


class MacroEvent(ResearchBaseModel):
    id: str
    event_type: Literal["CENTRAL_BANK_MEETING"] = CENTRAL_BANK_MEETING
    indicator: str
    region: str
    # A meeting date is calendar-date truth, not an instant -- no official
    # source publishes (or this codebase invents) a clock time for "the
    # meeting", so this is modelled explicitly as a DATE, never a fabricated
    # AwareDatetime with an invented hour.
    scheduled_at: date
    period: str
    status: Literal["SCHEDULED", "COMPLETED", "CANCELLED"] = "SCHEDULED"
    source: str
    source_url: str
    provider: str
    provenance: Literal["OFFICIAL_GOVERNMENT"] = "OFFICIAL_GOVERNMENT"
    # True instants -- these ARE timezone-aware timestamps, unlike scheduled_at.
    observed_at: AwareDatetime
    created_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _deterministic_id_matches(self):
        expected = macro_event_id(self.event_type, self.indicator, self.region, self.scheduled_at)
        if self.id != expected:
            raise ValueError("MACRO_EVENT_ID_NOT_DETERMINISTIC")
        return self
