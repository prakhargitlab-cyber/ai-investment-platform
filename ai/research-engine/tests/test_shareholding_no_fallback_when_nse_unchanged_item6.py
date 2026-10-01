"""Stage2 Final Fix -- Item 6: shareholding lightweight check / web fallback.

Runtime evidence: NSE official shareholding discovery returns ZERO_RESULTS,
research_refresh_gate logs LIGHTWEIGHT_CHECK_UNCHANGED, and a subsequent
targeted ensure() for SHAREHOLDING (the plain /ensure route, which calls
repository.refresh_targeted_categories() directly -- never through
deep_investigation.investigate(), so no acquisition_budget() contextvar is
ever set on this path) could still reach the generic search fallback and
eventually fail with SEARCH_PROVIDER_UNAVAILABLE / EXTERNAL_CAPABILITY_UNSUPPORTED.

NSE is authoritative for SHAREHOLDING_PATTERN. _refresh_targeted already
tracks `shareholding_check_succeeded` -- True whenever the dedicated NSE
shareholding feed call completed without raising this refresh (ZERO_RESULTS
and LIGHTWEIGHT_CHECK_UNCHANGED both still mean "NSE was checked", not
"never attempted"). The due_categories gate previously only excluded
SHAREHOLDING_PATTERN from the generic search fallback when a targeted-repair
budget was active (`budget is not None`) -- a proxy for which CALLER path
was used, not for whether NSE was actually, truthfully checked this refresh.
This left the no-budget /ensure path free to fall through to a generic web
search merely to try to manufacture shareholding evidence, even immediately
after NSE itself was checked and found unchanged.

This test proves that generic search is no longer invoked for
SHAREHOLDING_PATTERN in that exact scenario, while NOT touching the
budget-present (deep_investigation/targeted-repair) path's existing,
unmodified behavior (covered by tests/test_di12c_shareholding_precedence.py,
which still passes unmodified).
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.models import CompanyResearchProfile
from app.repository import ResearchRepository
from app.settings import Settings

from tests.test_di12c_shareholding_precedence import (
    _FakePersistence,
    _FakeSearchDiscovery,
)

INSTRUMENT_ID = UUID("00000000-0000-0000-0000-00000000c0de")


class _ZeroResultShareholdingDiscovery:
    """Mimics NSE official shareholding discovery returning ZERO_RESULTS
    (no snapshots at all) -- the feed call itself completes without raising,
    which is exactly what makes shareholding_check_succeeded True."""

    def __init__(self) -> None:
        self.calls = 0

    async def discover(self, profile):
        self.calls += 1
        return []


class _RecordingApprovedSourceDiscovery:
    """Records every category passed to the non-search registered-source
    discovery pass, so the test can assert SHAREHOLDING_PATTERN's search
    category is never requested."""

    def __init__(self) -> None:
        self.discover_calls: list[set[str]] = []

    def discover(self, profile, categories, seen_urls):
        self.discover_calls.append(set(categories))
        return []


class _EmptyOfficialFilingDiscovery:
    """Stands in for the separate NSE announcements/filings feed (a
    different discovery path from the dedicated shareholding feed) so this
    test performs no real network access for official_due_categories."""

    def __init__(self) -> None:
        self.calls: list[set[str]] = []

    async def discover(self, profile, missing_categories, already_seen_urls):
        self.calls.append(set(missing_categories))
        return []


def _nse_profile():
    return CompanyResearchProfile(
        instrument_id=INSTRUMENT_ID, company_name="Shareholding Example", company_id=uuid4(),
        ticker="SHAREX", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        provider_instrument_ids={"NSE": "SHAREX"},
    )


def _repo(*, official_shareholding_discovery, search_discovery, discovery):
    repo = ResearchRepository(
        settings=Settings(research_live_enabled=True, research_demo_enabled=False),
        persistence=_FakePersistence(),
        discovery=discovery,
        search_discovery=search_discovery,
        official_filing_discovery=_EmptyOfficialFilingDiscovery(),  # type: ignore[arg-type]
        official_shareholding_discovery=official_shareholding_discovery,  # type: ignore[arg-type]
    )
    repo.profiles = [_nse_profile()]
    return repo


@pytest.mark.asyncio
async def test_no_budget_path_skips_generic_search_when_nse_shareholding_unchanged() -> None:
    official_shareholding = _ZeroResultShareholdingDiscovery()
    search_discovery = _FakeSearchDiscovery()
    discovery = _RecordingApprovedSourceDiscovery()
    repo = _repo(
        official_shareholding_discovery=official_shareholding,
        search_discovery=search_discovery,
        discovery=discovery,
    )

    # No app.deep_investigation._scope budget is set anywhere in this test --
    # this reproduces the plain /ensure route's call shape exactly.
    await repo.refresh_targeted_categories(INSTRUMENT_ID, {"SHAREHOLDING_PATTERN"})

    # NSE's dedicated shareholding feed WAS actually checked this refresh.
    assert official_shareholding.calls == 1
    # Generic search was never invoked for SHAREHOLDING_PATTERN.
    assert search_discovery.discover_called is False
    for categories in discovery.discover_calls:
        assert "Shareholding Pattern" not in categories
    # The requirement lands truthfully as not-ready, never a false READY,
    # and with no fabricated evidence.
    assert repo.shareholding_for(INSTRUMENT_ID, limit=4) == []


@pytest.mark.asyncio
async def test_no_budget_path_still_falls_back_on_a_genuine_nse_outage() -> None:
    """A real NSE shareholding-feed failure (not merely zero-results) must
    NOT be silently treated as "checked, nothing new" -- it is not covered
    by this item's exclusion, so existing fallback/failure behavior for a
    genuine outage is preserved."""

    class _FailingShareholdingDiscovery:
        async def discover(self, profile):
            raise ValueError("NSE_SHAREHOLDING_TRANSIENT_OUTAGE")

    search_discovery = _FakeSearchDiscovery()
    discovery = _RecordingApprovedSourceDiscovery()
    repo = _repo(
        official_shareholding_discovery=_FailingShareholdingDiscovery(),
        search_discovery=search_discovery,
        discovery=discovery,
    )

    await repo.refresh_targeted_categories(INSTRUMENT_ID, {"SHAREHOLDING_PATTERN"})

    # The genuine outage is recorded as a real, truthful failure rather than
    # a manufactured success -- and, unlike the unchanged/zero-results case
    # above, is not excluded from whatever fallback already applied before
    # this item (i.e. this item does not change outage behavior).
    assert "NSE_SHAREHOLDING_UNAVAILABLE" in (repo.last_live_error.get(INSTRUMENT_ID) or "")
