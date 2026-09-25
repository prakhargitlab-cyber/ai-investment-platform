"""Focused coverage for the macro event-calendar foundation (FOMC, RBI MPC):
acquisition + persistence + read model only. Nothing here is consumed by
the Rule Engine, readiness, or ranking, and nothing here computes a
consensus/expected value or a surprise.
"""
import asyncio
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from app.persistence import SqliteResearchPersistence
from app.global_data_provider import SingleFlightTTLCache
from app.macro_observation import FED_POLICY_RATE, RBI_REPO_RATE, MacroProviderConfigurationError, MacroProviderError
from app.macro_event import CENTRAL_BANK_MEETING, MacroEvent, macro_event_id
from app.macro_event_acquisition import get_macro_events
from app.fomc_calendar_provider import FomcCalendarProvider, parse_fomc_calendar_text
from app.rbi_mpc_calendar_provider import RbiMpcCalendarProvider

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

# The verbatim 2026/2027 meeting dates confirmed against the official FOMC
# calendar page during this iteration's discovery pass (see the report),
# arranged into a plain-text fixture representative of what
# app.normalization.extract_text would hand back after stripping HTML.
FOMC_FIXTURE_TEXT = """
FOMC Meeting Calendars

2026
January 27-28
March 17-18*
April 28-29
June 16-17*
July 28-29
September 15-16*
October 27-28
December 8-9*

2027
January 26-27
March 16-17*
April 27-28
June 8-9*
July 27-28
September 14-15*
October 26-27
December 7-8*

Each meeting date is tentative until confirmed at the meeting immediately
preceding it.
"""


# -- 1. FOMC official calendar parsing -----------------------------------------

def test_fomc_calendar_text_parses_all_verified_meeting_dates():
    candidates = parse_fomc_calendar_text(FOMC_FIXTURE_TEXT)
    assert len(candidates) == 16  # 8 meetings x 2 years
    first = candidates[0]
    assert first.start_date == date(2026, 1, 27) and first.end_date == date(2026, 1, 28)
    last = candidates[-1]
    assert last.start_date == date(2027, 12, 7) and last.end_date == date(2027, 12, 8)
    # A meeting entirely within 2027 is never assigned to 2026 or vice versa.
    years = {c.start_date.year for c in candidates}
    assert years == {2026, 2027}


def test_fomc_provider_parses_fixture_into_macro_events():
    provider = FomcCalendarProvider()
    events = provider.parse(FOMC_FIXTURE_TEXT, now=NOW)
    assert len(events) == 16
    first = events[0]
    assert first.indicator == FED_POLICY_RATE and first.region == "US"
    assert first.event_type == CENTRAL_BANK_MEETING
    assert first.status == "SCHEDULED"
    assert first.provenance == "OFFICIAL_GOVERNMENT"
    assert first.scheduled_at == date(2026, 1, 27)
    assert first.period == "2026-01-27/2026-01-28"
    assert first.source_url == "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"


@pytest.mark.asyncio
async def test_fomc_provider_fetch_uses_real_transport_and_parser():
    def handler(request):
        return httpx.Response(200, text=f"<html><body><pre>{FOMC_FIXTURE_TEXT}</pre></body></html>")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = FomcCalendarProvider(client=client)
    events = await provider.fetch(NOW)
    assert len(events) == 16


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502])
@pytest.mark.asyncio
async def test_fomc_provider_http_failures_never_fabricate_events(status):
    def handler(request):
        return httpx.Response(status)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = FomcCalendarProvider(client=client)
    with pytest.raises(MacroProviderError):
        await provider.fetch(NOW)


def test_fomc_parser_never_fabricates_an_impossible_date():
    # February 30 cannot exist; it must never become a record.
    candidates = parse_fomc_calendar_text("2026\nFebruary 30-31")
    assert candidates == []


# -- 2. RBI official fixture parsing (boundary only, per instruction) ----------

@pytest.mark.asyncio
async def test_rbi_mpc_calendar_provider_is_a_boundary_only_and_never_fabricates():
    provider = RbiMpcCalendarProvider()
    with pytest.raises(MacroProviderConfigurationError):
        await provider.fetch(NOW)


# -- 3. persistence / idempotent refresh ---------------------------------------

def test_upsert_macro_event_is_idempotent():
    repo = SqliteResearchPersistence()
    event = MacroEvent(
        id=macro_event_id(CENTRAL_BANK_MEETING, FED_POLICY_RATE, "US", date(2026, 1, 27)),
        indicator=FED_POLICY_RATE, region="US", scheduled_at=date(2026, 1, 27), period="2026-01-27/2026-01-28",
        source="Fed", source_url="https://example.test/fomc", provider="FOMC_CALENDAR_FEDERAL_RESERVE",
        observed_at=NOW,
    )
    repo.upsert_macro_event(event)
    first = repo.load_macro_event(event.id)
    repo.upsert_macro_event(event.model_copy(update={"observed_at": NOW + timedelta(days=1)}))
    second = repo.load_macro_event(event.id)
    assert first.id == second.id
    all_rows = repo._connection.execute("SELECT COUNT(*) AS n FROM macro_events").fetchone()["n"]
    assert all_rows == 1  # re-acquiring the same meeting never duplicates the row
    assert first.created_at == second.created_at  # created_at set once, never touched again


# -- 4. 25 simulated consumers -> no duplicate acquisition ---------------------

@pytest.mark.asyncio
async def test_twenty_five_concurrent_consumers_fetch_fomc_calendar_once():
    calls = []
    class CountingProvider:
        async def fetch(self, now):
            calls.append(1)
            await asyncio.sleep(0.01)
            return [MacroEvent(
                id=macro_event_id(CENTRAL_BANK_MEETING, FED_POLICY_RATE, "US", date(2026, 1, 27)),
                indicator=FED_POLICY_RATE, region="US", scheduled_at=date(2026, 1, 27),
                period="2026-01-27/2026-01-28", source="Fed", source_url="https://example.test/fomc",
                provider="FOMC_CALENDAR_FEDERAL_RESERVE", observed_at=now,
            )]

    repo = SqliteResearchPersistence()
    cache = SingleFlightTTLCache(ttl_seconds=60)
    provider = CountingProvider()
    results = await asyncio.gather(*[
        get_macro_events(repo, provider, cache, CENTRAL_BANK_MEETING, FED_POLICY_RATE, "US", now=NOW)
        for _ in range(25)
    ])
    assert len(calls) == 1
    assert all(len(r) == 1 for r in results)
    assert repo._connection.execute("SELECT COUNT(*) AS n FROM macro_events").fetchone()["n"] == 1


# -- 5. fresh cache reuse --------------------------------------------------------

@pytest.mark.asyncio
async def test_fresh_calendar_is_reused_without_fetch():
    repo = SqliteResearchPersistence()
    repo.upsert_macro_event(MacroEvent(
        id=macro_event_id(CENTRAL_BANK_MEETING, FED_POLICY_RATE, "US", date(2026, 1, 27)),
        indicator=FED_POLICY_RATE, region="US", scheduled_at=date(2026, 1, 27), period="2026-01-27/2026-01-28",
        source="Fed", source_url="https://example.test/fomc", provider="FOMC_CALENDAR_FEDERAL_RESERVE",
        observed_at=NOW,
    ))
    calls = []
    class ShouldNotBeCalled:
        async def fetch(self, now):
            calls.append(1)
            raise AssertionError("provider must not be called for a fresh calendar")

    cache = SingleFlightTTLCache(ttl_seconds=60)
    results = await get_macro_events(repo, ShouldNotBeCalled(), cache, CENTRAL_BANK_MEETING, FED_POLICY_RATE, "US",
        now=NOW + timedelta(hours=1), ttl_seconds=604_800)
    assert calls == []
    assert len(results) == 1


# -- 6. provider failure preserves existing events ------------------------------

@pytest.mark.asyncio
async def test_provider_failure_preserves_previously_persisted_future_events():
    repo = SqliteResearchPersistence()
    original = MacroEvent(
        id=macro_event_id(CENTRAL_BANK_MEETING, FED_POLICY_RATE, "US", date(2026, 1, 27)),
        indicator=FED_POLICY_RATE, region="US", scheduled_at=date(2026, 1, 27), period="2026-01-27/2026-01-28",
        source="Fed", source_url="https://example.test/fomc", provider="FOMC_CALENDAR_FEDERAL_RESERVE",
        observed_at=NOW - timedelta(days=10),
    )
    repo.upsert_macro_event(original)

    class FailingProvider:
        async def fetch(self, now):
            raise MacroProviderError("MACRO_PROVIDER_UNAVAILABLE:http_status_502")

    cache = SingleFlightTTLCache(ttl_seconds=60)
    results = await get_macro_events(repo, FailingProvider(), cache, CENTRAL_BANK_MEETING, FED_POLICY_RATE, "US",
        now=NOW, ttl_seconds=1)  # ttl already expired -> attempts a refresh
    assert len(results) == 1
    assert results[0].status == "SCHEDULED"  # never falsely marked completed/cancelled
    persisted = repo.load_macro_event(original.id)
    assert persisted.observed_at == original.observed_at  # row itself untouched by the failed attempt


@pytest.mark.asyncio
async def test_provider_failure_with_no_prior_events_raises():
    repo = SqliteResearchPersistence()
    class FailingProvider:
        async def fetch(self, now):
            raise MacroProviderError("MACRO_PROVIDER_UNAVAILABLE")
    cache = SingleFlightTTLCache(ttl_seconds=60)
    with pytest.raises(MacroProviderError):
        await get_macro_events(repo, FailingProvider(), cache, CENTRAL_BANK_MEETING, FED_POLICY_RATE, "US", now=NOW)


# -- 7. next-event query ---------------------------------------------------------

def test_next_macro_event_query():
    repo = SqliteResearchPersistence()
    for day in (date(2026, 1, 27), date(2026, 3, 17), date(2025, 12, 1)):  # includes a past one
        repo.upsert_macro_event(MacroEvent(
            id=macro_event_id(CENTRAL_BANK_MEETING, FED_POLICY_RATE, "US", day),
            indicator=FED_POLICY_RATE, region="US", scheduled_at=day, period=f"{day.isoformat()}/{day.isoformat()}",
            source="Fed", source_url="https://example.test/fomc", provider="FOMC_CALENDAR_FEDERAL_RESERVE",
            observed_at=NOW,
        ))
    next_event = repo.next_macro_event(FED_POLICY_RATE, "US", NOW)
    assert next_event.scheduled_at == date(2026, 1, 27)


# -- 8. date-range query -----------------------------------------------------------

def test_macro_events_between_query():
    repo = SqliteResearchPersistence()
    for day in (date(2026, 1, 27), date(2026, 3, 17), date(2026, 6, 16)):
        repo.upsert_macro_event(MacroEvent(
            id=macro_event_id(CENTRAL_BANK_MEETING, FED_POLICY_RATE, "US", day),
            indicator=FED_POLICY_RATE, region="US", scheduled_at=day, period=f"{day.isoformat()}/{day.isoformat()}",
            source="Fed", source_url="https://example.test/fomc", provider="FOMC_CALENDAR_FEDERAL_RESERVE",
            observed_at=NOW,
        ))
    in_range = repo.macro_events_between(date(2026, 1, 1), date(2026, 4, 1), region="US")
    assert [e.scheduled_at for e in in_range] == [date(2026, 1, 27), date(2026, 3, 17)]


def test_upcoming_macro_events_by_region():
    repo = SqliteResearchPersistence()
    repo.upsert_macro_event(MacroEvent(
        id=macro_event_id(CENTRAL_BANK_MEETING, FED_POLICY_RATE, "US", date(2026, 1, 27)),
        indicator=FED_POLICY_RATE, region="US", scheduled_at=date(2026, 1, 27), period="2026-01-27/2026-01-27",
        source="Fed", source_url="https://example.test/fomc", provider="FOMC_CALENDAR_FEDERAL_RESERVE",
        observed_at=NOW,
    ))
    assert len(repo.upcoming_macro_events(region="US", now=NOW)) == 1
    assert len(repo.upcoming_macro_events(region="IN", now=NOW)) == 0


# -- 9. date-only event does not receive an invented time ----------------------

def test_scheduled_at_is_date_only_never_a_fabricated_clock_time():
    event = MacroEvent(
        id=macro_event_id(CENTRAL_BANK_MEETING, FED_POLICY_RATE, "US", date(2026, 1, 27)),
        indicator=FED_POLICY_RATE, region="US", scheduled_at=date(2026, 1, 27), period="2026-01-27/2026-01-28",
        source="Fed", source_url="https://example.test/fomc", provider="FOMC_CALENDAR_FEDERAL_RESERVE",
        observed_at=NOW,
    )
    assert type(event.scheduled_at) is date  # not datetime -- no hour/minute/second exists to invent
    repo = SqliteResearchPersistence()
    repo.upsert_macro_event(event)
    row = repo._connection.execute("SELECT scheduled_at FROM macro_events WHERE id=?", (event.id,)).fetchone()
    assert row["scheduled_at"] == "2026-01-27"  # exactly a date string, no time component persisted


def test_macro_event_id_rejects_a_mismatched_date():
    with pytest.raises(Exception):
        MacroEvent(
            id=macro_event_id(CENTRAL_BANK_MEETING, FED_POLICY_RATE, "US", date(2026, 1, 27)),
            indicator=FED_POLICY_RATE, region="US", scheduled_at=date(2026, 1, 28),  # mismatched
            period="2026-01-27/2026-01-28", source="Fed", source_url="https://example.test/fomc",
            provider="FOMC_CALENDAR_FEDERAL_RESERVE", observed_at=NOW,
        )


# -- 10. no macro_observations contamination ------------------------------------

def test_macro_events_never_enter_macro_observations_or_financial_facts():
    repo = SqliteResearchPersistence()
    repo.upsert_macro_event(MacroEvent(
        id=macro_event_id(CENTRAL_BANK_MEETING, RBI_REPO_RATE, "IN", date(2026, 2, 6)),
        indicator=RBI_REPO_RATE, region="IN", scheduled_at=date(2026, 2, 6), period="2026-02-04/2026-02-06",
        source="RBI", source_url="https://example.test/rbi", provider="RBI_MPC_CALENDAR_RBI", observed_at=NOW,
    ))
    assert repo._connection.execute("SELECT COUNT(*) AS n FROM macro_observations").fetchone()["n"] == 0
    from uuid import UUID
    assert repo.load_financial_facts({UUID(int=1)}) == []
    columns = {row["name"] for row in repo._connection.execute("PRAGMA table_info(macro_events)").fetchall()}
    assert "instrument_id" not in columns
    assert "actual_value" not in columns
    assert "expected_value" not in columns


# -- 11. no Rule Engine/ranking/readiness behavior changes ----------------------

def test_macro_event_modules_are_not_imported_by_rule_engine_or_ranking():
    # A pre-existing local variable in stock_rule_engine.py is literally named
    # `macro_events` (proven-macro-exposure events, unrelated to this iteration's
    # MacroEvent/macro_events TABLE) -- so this checks for actual imports of the
    # new modules, not a bare substring match.
    import app.stock_rule_engine as sre
    import app.global_opportunity_orchestration as goo
    import app.global_opportunity_ranker as ranker
    forbidden_imports = ("app.macro_event", "app.macro_event_persistence",
        "app.macro_event_acquisition", "app.fomc_calendar_provider", "app.rbi_mpc_calendar_provider")
    for module in (sre, goo, ranker):
        text = open(module.__file__, encoding="utf-8").read()
        for name in forbidden_imports:
            assert f"import {name}" not in text
            assert f"from {name} " not in text
        assert "MacroEvent" not in text
        assert "FomcCalendarProvider" not in text
        assert "RbiMpcCalendarProvider" not in text
