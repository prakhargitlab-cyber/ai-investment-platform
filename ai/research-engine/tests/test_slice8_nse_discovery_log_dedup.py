"""Guardian Review Slice 8 -- NSE discovery safe local improvement.

Preserves all existing, already-correct behavior verified by
tests/test_slice6_acquisition_efficiency.py:
  - the corporate-announcements endpoint is symbol-scoped (one row list per
    NSE symbol);
  - a per-symbol TTL cache and same-symbol single-flight already prevent a
    second physical NSE fetch when multiple requirement groups investigate
    the same instrument close together (test_one_instrument_investigation_
    fetches_nse_announcements_once already proves zero duplicate network
    calls -- not re-proven here);
  - discovery/classification/acceptance semantics are UNCHANGED: every row
    that was accepted before this change is still accepted, and every
    NO_ATTACHMENT row is still rejected (never added to the returned
    candidates) -- only the LOGGING for that one rejection reason changed.

The one defect this fixes: app.source_discovery.OfficialFilingDiscovery.
discover() previously emitted one logger.info(...) line per NO_ATTACHMENT
row, every time discover() runs -- and because one investigation calls
discover() multiple times (once per capability group: FINANCIALS;
SHAREHOLDING/ORDER_BOOK/GOVERNANCE) sharing the SAME cached rows list, a
symbol with many placeholder-attachment announcements produced repeated,
purely-volume log lines carrying no new information. Rejected rows are now
counted during the loop and reported as ONE aggregated line per discover()
call (only when the count is nonzero, to avoid a noisy count=0 line),
instead of one line per row.
"""
from __future__ import annotations

import logging
from uuid import UUID

import httpx
import pytest

from app.models import CompanyResearchProfile
from app.source_discovery import OfficialFilingDiscovery

_FINANCIAL_ROW = {
    "an_dt": "14-Aug-2026 18:53:06", "desc": "Financial Results", "category": "Financial Results",
    "attchmntText": "Unaudited financial results for the quarter ended June 30, 2026",
    "attchmntFile": "https://nsearchives.nseindia.com/corporate/X_results_jun.pdf",
}


def _no_attachment_financial_row(n: int) -> dict:
    return {
        "an_dt": f"1{n}-Aug-2026 09:00:00", "desc": "Financial Results", "category": "Financial Results",
        "attchmntText": f"Unaudited financial results addendum {n}",
        "attchmntFile": "-",  # placeholder -- no real attachment
    }


def _no_attachment_governance_row(n: int) -> dict:
    return {
        "an_dt": f"0{n}-Aug-2026 09:00:00", "desc": "Appointment of Director",
        "attchmntText": "Appointment of director to the board",
        "attchmntFile": "NA",  # placeholder -- no real attachment
    }


def _profile():
    return CompanyResearchProfile(
        instrument_id=UUID(int=77), company_id=UUID(int=78), company_name="Example Industries Limited",
        isin="INE000X01010", ticker="EXAMPLEX", exchange="NSE", mic="XNSE", country="IN", currency="INR",
        provider_instrument_ids={"NSE": "EXAMPLEX"})


def _discovery(rows):
    def handle(request):
        return httpx.Response(200, json=rows, request=request)
    return OfficialFilingDiscovery(httpx.AsyncClient(transport=httpx.MockTransport(handle)))


# 1 -- many NO_ATTACHMENT rows collapse to exactly one aggregated log line
# with the correct count, instead of one line per row -----------------------
@pytest.mark.asyncio
async def test_no_attachment_rejections_are_aggregated_into_one_log_line(caplog):
    rows = [_FINANCIAL_ROW] + [_no_attachment_financial_row(n) for n in range(1, 6)]  # 5 placeholders
    discovery = _discovery(rows)
    with caplog.at_level(logging.INFO, logger="app.source_discovery"):
        accepted = await discovery.discover(_profile(), {"FINANCIAL_RESULTS"}, set())
    # Acceptance semantics unchanged: only the real attachment is accepted.
    assert [r.source.url.rsplit("/", 1)[1] for r in accepted] == ["X_results_jun.pdf"]
    no_attachment_records = [rec for rec in caplog.records if "reason=NO_ATTACHMENT" in rec.message
                              and "official_candidate_rejected" in rec.message]
    assert len(no_attachment_records) == 1, [rec.message for rec in no_attachment_records]
    assert "count=5" in no_attachment_records[0].message


# 2 -- across the multiple discover() calls one investigation makes (one per
# capability group, all sharing the cached rows list), each call emits its
# OWN single aggregated line for its own rejections -- never one line per
# row, and never a line double-counting another group's rejections --------
@pytest.mark.asyncio
async def test_log_volume_stays_one_line_per_call_across_multiple_groups(caplog):
    rows = (
        [_FINANCIAL_ROW]
        + [_no_attachment_financial_row(n) for n in range(1, 4)]  # 3 for the FINANCIALS call
        + [_no_attachment_governance_row(n) for n in range(1, 3)]  # 2 for the GOVERNANCE call
    )
    discovery = _discovery(rows)
    profile = _profile()
    with caplog.at_level(logging.INFO, logger="app.source_discovery"):
        financial = await discovery.discover(profile, {"FINANCIAL_RESULTS"}, set())
        governance = await discovery.discover(profile, {"RISKS", "REGULATORY", "MANAGEMENT"}, set())
    assert [r.source.url.rsplit("/", 1)[1] for r in financial] == ["X_results_jun.pdf"]
    assert governance == []  # the two placeholder rows are rejected, nothing else qualifies
    no_attachment_records = [rec for rec in caplog.records if "reason=NO_ATTACHMENT" in rec.message
                              and "official_candidate_rejected" in rec.message]
    # Exactly two aggregated lines total (one per discover() call), not five
    # (one per rejected row) and not one merged/miscounted line.
    assert len(no_attachment_records) == 2
    counts = sorted(int(rec.message.rsplit("count=", 1)[1]) for rec in no_attachment_records)
    assert counts == [2, 3]


# 3 -- a call with zero NO_ATTACHMENT rejections stays silent on this reason
# (no noisy count=0 line) ----------------------------------------------------
@pytest.mark.asyncio
async def test_zero_rejections_emits_no_aggregated_line(caplog):
    discovery = _discovery([_FINANCIAL_ROW])
    with caplog.at_level(logging.INFO, logger="app.source_discovery"):
        accepted = await discovery.discover(_profile(), {"FINANCIAL_RESULTS"}, set())
    assert len(accepted) == 1
    assert not any("reason=NO_ATTACHMENT" in rec.message for rec in caplog.records)
