"""Regression pin: DisabledResearchPersistence must implement every public
method the ResearchPersistence Protocol declares.

Root cause (introduced regression): the staged feature added
``upsert_acquisition_observation``/``load_acquisition_observations`` to the
real (SQLite/Postgres) persistence backend's interface, called from
``app.repository.record_document_size_failure`` via
``record_acquisition_observation``, but never added the matching no-op
stubs to ``DisabledResearchPersistence`` -- every other ``upsert_*``/
``load_*`` method on that class IS a no-op stub. Any research-engine
deployment configured with persistence disabled then raised
``AttributeError`` the first time an oversized-document failure was
recorded, because ``ResearchPersistence`` is a ``typing.Protocol`` (no
runtime enforcement), so nothing caught the missing method until it was
actually called.

This test makes that gap structural: it enumerates every public method
``ResearchPersistence`` declares and asserts ``DisabledResearchPersistence``
has a same-named, callable attribute, so a future method added to the real
interface without a matching disabled-persistence stub fails fast here
instead of as a production AttributeError.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timezone
from uuid import uuid4

from app.persistence import DisabledResearchPersistence, ResearchPersistence


def _protocol_public_methods(protocol: type) -> set[str]:
    return {
        name
        for name, member in vars(protocol).items()
        if not name.startswith("_") and inspect.isfunction(member)
    }


def test_disabled_persistence_implements_every_protocol_method():
    required = _protocol_public_methods(ResearchPersistence)
    disabled = DisabledResearchPersistence()
    missing = sorted(
        name for name in required
        if not callable(getattr(disabled, name, None))
    )
    assert not missing, (
        "DisabledResearchPersistence is missing no-op stubs for: "
        f"{missing} -- every other ResearchPersistence method on this class "
        "is a no-op stub; a disabled-persistence deployment will raise "
        "AttributeError the first time production code calls one of these."
    )


def test_disabled_persistence_acquisition_observation_methods_are_noop():
    """Direct pin for the specific method pair this regression was about."""
    disabled = DisabledResearchPersistence()
    result = disabled.upsert_acquisition_observation(
        uuid4(), "SOME_REQUIREMENT", "YAHOO_FINANCE", "FAILED",
        datetime.now(timezone.utc), "https://example.com",
    )
    assert result is None
    assert disabled.load_acquisition_observations(uuid4()) == []


def test_disabled_persistence_reconcile_financial_facts_for_source_is_noop():
    """Direct pin: reconcile_financial_facts_for_source (a pre-existing gap,
    present on baseline too, not one of the 27 introduced regressions) must
    be a safe no-op consistent with every other upsert_*/reconcile_* stub on
    this class, returning 0 (no facts reconciled) rather than raising
    AttributeError."""
    disabled = DisabledResearchPersistence()
    result = disabled.reconcile_financial_facts_for_source(
        uuid4(), "some-source-identity", [],
    )
    assert result == 0
