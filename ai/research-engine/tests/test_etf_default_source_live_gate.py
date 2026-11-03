"""Guardrail: no default reviewed ETF PDF is silently enabled for live fetching.

This test should change only after an operator validates a new exact public
PDF URL and independently confirms the content meets the existing identity
and metric parsers. No HTTP calls occur in this test.
"""
from app.etf_official_document_sources import AccessTier, default_bounded_registry
from app.etf_official_document_pipeline import MAX_BOUNDED_INSTRUMENTS


def test_seeded_sources_are_not_live_approved():
    registry = default_bounded_registry()
    assert registry.instrument_count() <= MAX_BOUNDED_INSTRUMENTS
    assert registry.all_sources(), "Keep known restricted sources visible for audit"
    assert all(s.access_tier is AccessTier.REQUIRES_AUTHENTICATION
               for s in registry.all_sources())
    assert {s.ticker for s in registry.all_sources()} == {"NIFTYBEES", "JUNIORBEES"}
    assert all(s.approval_reference for s in registry.all_sources())
