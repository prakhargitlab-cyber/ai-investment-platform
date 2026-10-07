"""CURRENT_NEWS field-suggestion wiring: ExtractedDocument -> conservative,
non-binding candidate title/eventDate/sourceUrl suggestions surfaced on the
draft response as `fieldSuggestions`, exercised end-to-end through the
real HTTP draft endpoint. A TXT upload is used so the extracted text is
deterministic (no OCR variance), isolating this test to the suggestion
wiring itself -- OCR-sourced extraction is already covered by
test_current_news_draft_endpoint_regression.py.

Invariants proven here:
  - fieldSuggestions is present and populated when the text contains
    recognizable candidates
  - fieldSuggestions keys match CURRENT_NEWS_MANUAL_FIELDS exactly
    ("title", "eventDate", "sourceUrl") so the frontend can pre-fill the
    same fields the user must still explicitly submit
  - fieldSuggestions is NEVER written to proposedFacts and NEVER changes
    requiresManualFields/manualFieldSchema -- it is advisory only
  - accept() still requires the user to explicitly submit every manual
    field as `corrections`, even when a suggestion for it exists --
    suggestions never auto-fill the accepted draft
"""
from __future__ import annotations

from uuid import UUID

RELIANCE_ID = "44444444-4444-4444-4444-444444444444"

_AUTH_HEADERS = {
    "X-Aip-User-Id": "test-user",
    "X-Aip-User-Issuer": "test-issuer",
    "X-Aip-User-Subject": "test-subject",
}


def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app, headers=_AUTH_HEADERS)


_SCREENSHOT_TEXT = (
    "Home Menu Search\n"
    "Infosys Ltd.\n"
    "Market & Stock Status\n"
    "Next Earnings Date: 14 October 2026\n"
    "\n"
    "Recent Partnerships & Announcements\n"
    "Infosys announces strategic partnership with Columbia University for AI research\n"
    "Source: https://www.infosys.com/newsroom/press-releases/2026/columbia-partnership.html\n"
    "15 September 2026\n"
)


class TestCurrentNewsFieldSuggestions:
    def test_draft_surfaces_conservative_field_suggestions(self):
        client = _client()
        from app import manual_evidence
        manual_evidence._drafts.clear()

        response = client.post(
            "/api/v1/research/evidence/draft",
            params={"global_instrument_id": RELIANCE_ID, "evidence_type": "CURRENT_NEWS"},
            files={"file": ("screenshot.txt", _SCREENSHOT_TEXT.encode("utf-8"), "text/plain")},
        )
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["requiresManualFields"] is True
        assert body["proposedFacts"] == []  # suggestions never become proposed facts

        suggestions = body["fieldSuggestions"]
        assert suggestions is not None
        assert set(suggestions.keys()) <= {"title", "eventDate", "sourceUrl"}
        assert suggestions["title"] == (
            "Infosys announces strategic partnership with Columbia University for AI research"
        )
        assert suggestions["sourceUrl"] == (
            "https://www.infosys.com/newsroom/press-releases/2026/columbia-partnership.html"
        )
        assert suggestions["eventDate"] == "2026-09-15"

    def test_suggestions_never_auto_fill_accept_user_must_still_submit_corrections(self):
        client = _client()
        from app import manual_evidence
        manual_evidence._drafts.clear()

        draft_response = client.post(
            "/api/v1/research/evidence/draft",
            params={"global_instrument_id": RELIANCE_ID, "evidence_type": "CURRENT_NEWS"},
            files={"file": ("screenshot.txt", _SCREENSHOT_TEXT.encode("utf-8"), "text/plain")},
        )
        draft_id = draft_response.json()["draftId"]

        # Accept WITHOUT any corrections: even though strong suggestions
        # exist, the draft must still be rejected as missing the
        # required manual fields -- suggestions are advisory, never
        # auto-applied.
        accept_response = client.post(
            f"/api/v1/research/evidence/draft/{draft_id}/accept",
            json={"corrections": {}},
        )
        assert accept_response.status_code in (400, 409), accept_response.text

    def test_plain_text_with_no_recognizable_candidates_yields_no_suggestions_block(self):
        client = _client()
        from app import manual_evidence
        manual_evidence._drafts.clear()

        response = client.post(
            "/api/v1/research/evidence/draft",
            params={"global_instrument_id": RELIANCE_ID, "evidence_type": "CURRENT_NEWS"},
            files={"file": ("empty.txt", b"...\n---\n***\n", "text/plain")},
        )
        assert response.status_code == 200, response.text
        assert response.json()["fieldSuggestions"] is None
