"""Tests for app.evidence_interpretation -- the EVIDENCE INTERPRETATION
layer, independent of app.manual_evidence and app.document_extraction.

These tests exercise interpret_shareholding/interpret_current_news
directly against an ExtractedDocument-shaped input (a bare object with a
``.text`` attribute is sufficient -- the interpreters never depend on
app.document_extraction.ExtractedDocument's concrete type, only on the
``text`` attribute, matching the documented reusability contract).
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from app.evidence_interpretation import (
    CurrentNewsCandidate,
    ShareholdingInterpretation,
    interpret_current_news,
    interpret_shareholding,
)
from app.models import ShareholdingCategory


class TestInterpretShareholding:
    def test_extracts_latest_period_values_from_official_style_text(self):
        """Matches the task's concrete example values (Jun 2026 period):
        Promoters 74.24%, FIIs 1.87%, DIIs 0.11%, Public 23.78%."""
        text = (
            "Shareholding as on 30/06/2026\n\n"
            "Promoter and promoter group: 74.24%\n"
            "Foreign institutional investors: 1.87%\n"
            "Domestic institutional investors: 0.11%\n"
            "Public shareholders: 23.78%\n"
        )
        result = interpret_shareholding(
            SimpleNamespace(text=text),
            instrument_id=uuid4(),
            content_digest="deadbeef",
            filename="shareholding.png",
            mime="image/png",
        )
        assert isinstance(result, ShareholdingInterpretation)
        assert not result.errors
        assert result.period_end is not None
        assert result.period_end.year == 2026 and result.period_end.month == 6
        by_category = {v.category: v.percentage for v in result.values}
        assert by_category[ShareholdingCategory.PROMOTER] == Decimal("74.24")
        assert by_category[ShareholdingCategory.FII_FPI] == Decimal("1.87")
        assert by_category[ShareholdingCategory.DII] == Decimal("0.11")
        assert by_category[ShareholdingCategory.PUBLIC_RETAIL] == Decimal("23.78")

    def test_noisy_table_with_no_as_on_phrase_is_parsed_by_the_table_interpreter(self):
        """A bare multi-column table dump (the task's literal screenshot
        shape: 'Sep 2023 Dec 2023 ... Jun 2026 / Promoters / FIIs ...'
        with no 'as on'/'ended' narrative phrase) is outside what the
        existing prose-based deterministic parser was built to recognize,
        but the multi-column TABLE interpreter (app.evidence_interpretation's
        _parse_shareholding_table) now handles exactly this shape: it
        detects the period-column header, selects the LATEST column
        (Jun 2026, not the first/any other column), and maps only the
        rows that are actually present -- DII/Public are absent from this
        table and must be left unset, never invented."""
        text = (
            "Shareholding Pattern\n"
            "Sep 2023 Dec 2023 Mar 2024 Jun 2026\n"
            "Promoters 75.1 74.9 74.5 74.24\n"
            "FIIs 1.2 1.5 1.7 1.87\n"
        )
        result = interpret_shareholding(
            SimpleNamespace(text=text),
            instrument_id=uuid4(),
            content_digest="cafef00d",
            filename="shareholding.png",
            mime="image/png",
        )
        assert not result.errors
        by_category = {v.category: v.percentage for v in result.values}
        assert by_category == {
            ShareholdingCategory.PROMOTER: Decimal("74.24"),
            ShareholdingCategory.FII_FPI: Decimal("1.87"),
        }
        # Jun 2026 is MONTH precision -> normalized to the first day of
        # the month (2026-06-01), the platform's common reporting-period
        # rule -- never invented as a quarter-end/month-end day.
        assert result.period_end is not None
        assert result.period_end.year == 2026 and result.period_end.month == 6 and result.period_end.day == 1
        assert result.period_precision == "MONTH"

    def test_empty_text_degrades_to_no_values_not_a_crash(self):
        result = interpret_shareholding(
            SimpleNamespace(text=""),
            instrument_id=None,
            content_digest="abc123",
            filename="blank.png",
            mime="image/png",
        )
        assert result.values == []
        assert result.errors


class TestInterpretCurrentNews:
    def test_extracts_candidate_fields_from_realistic_noisy_screenshot_text(self):
        """Matches the task's concrete CURRENT_NEWS example: a screenshot
        with UI chrome, a forward-looking 'Next Earnings Date' (must NOT
        be suggested as the event date), a real past announcement with a
        source URL and its own date."""
        text = (
            "Home Menu Search\n"
            "Infosys Ltd.\n"
            "Market & Stock Status\n"
            "Share Price: Rs 1,842.50\n"
            "Market Cap: Rs 7.65 Lakh Cr\n"
            "Next Earnings Date: 14 October 2026\n"
            "Sector Trend: IT Services\n"
            "\n"
            "Recent Partnerships & Announcements\n"
            "Infosys announces strategic partnership with Columbia University for AI research\n"
            "Source: https://www.infosys.com/newsroom/press-releases/2026/columbia-partnership.html\n"
            "15 September 2026\n"
            "\n"
            "ABN AMRO collaboration expands to cover digital banking transformation\n"
            "AI Agents launch planned for Q4\n"
        )
        candidate = interpret_current_news(SimpleNamespace(text=text))
        assert isinstance(candidate, CurrentNewsCandidate)
        assert candidate.title == (
            "Infosys announces strategic partnership with Columbia University for AI research"
        )
        assert candidate.source_url == (
            "https://www.infosys.com/newsroom/press-releases/2026/columbia-partnership.html"
        )
        # The forward-looking "Next Earnings Date" must never be suggested
        # as the event date -- only the real past announcement date.
        assert candidate.event_date == "2026-09-15"

    def test_never_invents_a_future_date(self):
        text = "Breaking news about upcoming product launch\nNext Earnings Date: 14 October 2026\n"
        candidate = interpret_current_news(SimpleNamespace(text=text))
        assert candidate.event_date is None

    def test_pure_ui_noise_yields_no_title_suggestion(self):
        text = "Home\nMenu\nSearch\nBack\nNext\n"
        candidate = interpret_current_news(SimpleNamespace(text=text))
        assert candidate.title is None

    def test_empty_text_yields_all_fields_none(self):
        candidate = interpret_current_news(SimpleNamespace(text=""))
        assert candidate == CurrentNewsCandidate(title=None, source_url=None, event_date=None)

    def test_never_fabricates_source_without_url_or_label(self):
        text = "Infosys announces strategic partnership with Columbia University for AI research\n"
        candidate = interpret_current_news(SimpleNamespace(text=text))
        assert candidate.title is not None
        assert candidate.source_url is None
        assert candidate.event_date is None
