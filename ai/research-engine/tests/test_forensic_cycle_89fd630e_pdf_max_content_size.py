"""Forensic analysis of controlled cycle 89fd630e-5212-4018-ba6d-09889f83822e.

Issue 6 -- PDF MAX-CONTENT-SIZE CLASSIFICATION:
FetchError("Maximum content size exceeded") / outcome=EXTRACTION_ERROR was
observed for a 100,710,365-byte PDF and several multi-MB PDFs.

Root cause: app/repository.py's `_fetch_rejection_reason()` already had a
correctly-classified, PERMANENT_REASONS-listed canonical reason for exactly
this condition -- "DOCUMENT_SIZE_LIMIT_EXCEEDED" (raised as a typed
`DocumentSizeLimitExceeded(FetchError)` by the early Content-Length/
streaming-time check in app/research_fetching.py) -- but that literal
string was missing from `_fetch_rejection_reason`'s recognized-passthrough
set, so BOTH that typed exception AND the separate post-buffer backstop
check's plain `FetchError("Maximum content size exceeded")` (for a response
with no/unreliable Content-Length header) fell through to the generic
default, "PARSER_FAILED" -- a TECHNICAL_RETRYABLE reason indistinguishable
from an actual corrupt-PDF parsing exception. A document's byte count is a
deterministic property of that exact document: an identical retry against
the identical URL will exceed the identical limit every time, so treating
it as retryable wastes a repair attempt that can never succeed (and
increasing the size limit to "fix" it is explicitly out of scope).

Fix: `_fetch_rejection_reason` now (1) includes "DOCUMENT_SIZE_LIMIT_
EXCEEDED" in its recognized-literal passthrough set, and (2) maps the
backstop check's "Maximum content size exceeded" message to that SAME
existing canonical reason, rather than inventing a second name for one
condition. Nothing about `research_max_content_bytes`, concurrency, or
timeouts was changed.

Also verifies (Issue 7, TIMEOUT CAUSALITY): a genuine NETWORK_TIMEOUT is
NOT reclassified by this change, and a content-size failure is never
reported as a network timeout.
"""
from __future__ import annotations

import pytest

from app.failure_taxonomy import EVIDENCE_UNAVAILABLE, TECHNICAL_RETRYABLE, classify_reason
from app.repository import _fetch_rejection_reason, _is_transport_fetch_failure
from app.research_fetching import DocumentSizeLimitExceeded, FetchError, TransportFetchError


def test_document_size_limit_exceeded_typed_exception_classifies_correctly() -> None:
    exc = DocumentSizeLimitExceeded(content_length=100_710_365, max_bytes=20_000_000)
    reason = _fetch_rejection_reason(exc)
    assert reason == "DOCUMENT_SIZE_LIMIT_EXCEEDED"
    assert classify_reason(reason) == EVIDENCE_UNAVAILABLE, (
        "a document's byte count cannot change on retry -- this must not be TECHNICAL_RETRYABLE"
    )


def test_backstop_maximum_content_size_message_maps_to_the_same_canonical_reason() -> None:
    exc = FetchError("Maximum content size exceeded")
    reason = _fetch_rejection_reason(exc)
    assert reason == "DOCUMENT_SIZE_LIMIT_EXCEEDED"
    assert classify_reason(reason) == EVIDENCE_UNAVAILABLE


def test_content_size_failure_is_never_reported_as_a_network_timeout() -> None:
    exc = FetchError("Maximum content size exceeded")
    reason = _fetch_rejection_reason(exc)
    assert reason != "NETWORK_TIMEOUT"
    assert "TIMEOUT" not in reason


def test_content_size_failure_does_not_trip_transport_host_cooldown() -> None:
    """A too-large document is not a transport/network problem with the
    host -- it must not count toward the per-host transport-failure/
    cooldown bookkeeping the way a real connection failure does."""
    exc = DocumentSizeLimitExceeded(content_length=50_000_000, max_bytes=20_000_000)
    assert not _is_transport_fetch_failure(exc)


def test_genuine_network_timeout_is_unaffected_and_stays_technical_retryable() -> None:
    """Issue 7 regression: this fix must not touch genuine timeout
    classification at all."""
    exc = TransportFetchError("NETWORK_TIMEOUT")
    reason = _fetch_rejection_reason(exc)
    assert reason == "NETWORK_TIMEOUT"
    assert classify_reason(reason) == TECHNICAL_RETRYABLE
    assert _is_transport_fetch_failure(exc)


def test_bare_unqualified_parser_failure_remains_distinct_and_retryable() -> None:
    """An actual corrupt-PDF parse exception (not a size issue) must keep
    getting the generic, retryable PARSER_FAILED classification -- this fix
    only carves out the specific size-limit message, nothing else."""
    exc = FetchError("some pypdf internal error")
    reason = _fetch_rejection_reason(exc)
    assert reason == "PARSER_FAILED"
    assert classify_reason(reason) == TECHNICAL_RETRYABLE
