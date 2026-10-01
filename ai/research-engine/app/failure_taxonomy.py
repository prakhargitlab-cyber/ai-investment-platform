"""Candidate-level classification of acquisition failure reasons (Slice 4).

The per-requirement reason strings are the DI-20H.4 taxonomy produced by
deep_investigation / research_readiness_runtime / repository and are preserved
verbatim. This module only answers one question for the orchestrator:
is a blocked candidate's gap TECHNICAL (retry within a bounded repair budget;
never a business rejection) or GENUINE (evidence unavailable; terminal, not
retried)?

Unknown reasons -- including bare exception class names recorded by the
readiness runtime (``type(exc).__name__``) -- are treated as TECHNICAL: a
failure we cannot positively identify as "the evidence does not exist" must
not be turned into a terminal outcome.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

TECHNICAL_RETRYABLE = "TECHNICAL_RETRYABLE"
EVIDENCE_UNAVAILABLE = "EVIDENCE_UNAVAILABLE"

# Positively genuine: the source refuses access by policy, the content is
# not usable for this instrument, or discovery found nothing to acquire.
PERMANENT_REASONS = frozenset({
    "ROBOTS_OR_ACCESS_BLOCKED",
    "DOMAIN_BLOCKED",
    "DOMAIN_VALIDATION_FAILED",
    "SOURCE_QUALITY_REJECTED",
    "COMPANY_RELEVANCE_FAILED",
    "DOCUMENT_SIZE_LIMIT_EXCEEDED",
    "CONTENT_EMPTY",
    "CONTENT_TOO_SHORT",
    "UNSUPPORTED_CONTENT_TYPE",
    "RESULTS_REJECTED",
    "DISCOVERY_NO_FINANCIAL_RESULTS_CANDIDATES",
    "DISCOVERY_NO_CANDIDATES",
    "DISCOVERY_ALL_CANDIDATES_REUSED_OR_UNSUPPORTED",
    "EVIDENCE_INSUFFICIENT_WITHIN_PLAN",
    "PRIMARY_FINANCIAL_PROVIDER_RETURNED_NO_FACTS",
    "NOT_APPLICABLE",
    # The requirement's most recent AUTHORITATIVE (NSE) acquisition attempt
    # genuinely completed and found nothing (SUCCESS_EMPTY) -- not "a
    # scheduling decision skipped this cycle" (that stays ACQUISITION_NOT_DUE
    # / ACQUISITION_BACKOFF, which are deliberately technical: see below).
    # research_readiness_runtime.py already treats a completed SUCCESS_EMPTY
    # NSE check the same way (AUTHORITATIVE_UNAVAILABLE / NO_USABLE_EVIDENCE
    # diagnostics, _completed_authoritative_check); deep_investigation's
    # fallback classification was the one place still discarding that signal
    # and reporting a bare ACQUISITION_NOT_DUE (technical/retryable) even when
    # the last real check already ran to completion with nothing found.
    "PRIOR_ACQUISITION_EMPTY",
})

# Positively technical (listed for documentation/review; anything not in
# PERMANENT_REASONS is technical anyway).
TECHNICAL_REASONS = frozenset({
    # A refresh scheduling decision is not an authoritative absence check.
    # Nor does readiness/scorer disagreement prove that evidence is absent.
    "ACQUISITION_NOT_DUE", "RULE_AREA_UNSCORABLE",
    "NETWORK_TIMEOUT", "HTTP_FETCH_FAILED", "DOCUMENT_FETCH_FAILED",
    "PDF_EXTRACTION_TIMEOUT", "PDF_EXTRACTION_QUEUE_TIMEOUT", "PDF_TEXT_EXTRACTION_FAILED",
    "EXTRACTION_FAILED", "PARSER_FAILED", "RECONCILE_FAILED",
    "DOCUMENT_PERSIST_FAILED", "SHAREHOLDING_PERSIST_FAILED", "PERSISTENCE_FAILED",
    "ACQUISITION_TIMEOUT", "DOWNSTREAM_TIMEOUT",
    "EXTERNAL_PROVIDER_UNAVAILABLE", "SOURCE_UNAVAILABLE", "AUTHORITATIVE_UNAVAILABLE",
    "SEARCH_PROVIDER_UNAVAILABLE", "SEARCH_PROVIDER_TIMEOUT", "SEARCH_PROVIDER_RATE_LIMITED",
    "GOOGLE_PROVIDER_UNAVAILABLE", "NSE_FINANCIAL_UPGRADE_UNAVAILABLE",
    "NSE_SHAREHOLDING_OFFICIAL_UNAVAILABLE",
    # A spent per-refresh budget says nothing about whether evidence exists;
    # a repair attempt gets a fresh budget.
    "DOCUMENT_BUDGET_EXHAUSTED", "DISCOVERY_QUERY_BUDGET_EXHAUSTED", "BUDGET_EXHAUSTED",
})


def _components(reason: str | None) -> list[str]:
    return [part.strip() for part in str(reason or "").split("|") if part.strip()]


def classify_reason(reason: str | None) -> str | None:
    parts = _components(reason)
    if not parts:
        return None
    if any(part not in PERMANENT_REASONS for part in parts):
        return TECHNICAL_RETRYABLE
    return EVIDENCE_UNAVAILABLE


def classify_requirement_failures(failures: Mapping[str, str] | None,
                                  blocking_requirements: Iterable[str] = ()) -> str | None:
    """Classify only failures on the requirements that actually block the
    candidate (a technical failure on an optional requirement does not make
    a genuine mandatory gap retryable). No recorded failure -> None."""
    failures = dict(failures or {})
    blocking = set(blocking_requirements or ())
    relevant = {k: v for k, v in failures.items() if not blocking or k in blocking}
    classes = {classify_reason(v) for v in relevant.values()} - {None}
    if not classes:
        return None
    return TECHNICAL_RETRYABLE if TECHNICAL_RETRYABLE in classes else EVIDENCE_UNAVAILABLE


def blocking_failures(failures: Mapping[str, str] | None, blocking_requirements: Iterable[str] = ()) -> dict[str, str]:
    failures = dict(failures or {})
    blocking = set(blocking_requirements or ())
    return {str(k): str(v) for k, v in sorted(failures.items()) if not blocking or k in blocking}
