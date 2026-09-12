"""Provider-neutral financial fact identity and precedence rules.

This is deliberately independent of document parsing.  Every future financial
ingester must normalize into ``FinancialFact`` and call ``merge_fact`` at its
durable upsert boundary instead of making provider-specific replacement choices.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from uuid import UUID

from app.models import ProvenancedValue, SourceMode


class FactSourceTier(IntEnum):
    SEARCH = 1
    YAHOO = 2
    # 3 is durable historical storage for official NSE facts. Never reuse it.
    OFFICIAL_NSE = 3
    STRUCTURED_FUNDAMENTALS = 4
    OFFICIAL_REGULATORY = 5


# Provider-neutral list of tiers eligible for financial read projections.
SUPPORTED_FINANCIAL_SOURCE_TIERS = frozenset({
    FactSourceTier.YAHOO,
    FactSourceTier.STRUCTURED_FUNDAMENTALS,
    FactSourceTier.OFFICIAL_NSE,
    FactSourceTier.OFFICIAL_REGULATORY,
})


def fact_source_authority(tier: FactSourceTier) -> int:
    """Return authority without changing durable source-tier identities."""
    return {
        FactSourceTier.SEARCH: 1,
        FactSourceTier.YAHOO: 2,
        FactSourceTier.STRUCTURED_FUNDAMENTALS: 3,
        FactSourceTier.OFFICIAL_NSE: 4,
        FactSourceTier.OFFICIAL_REGULATORY: 5,
    }[tier]


@dataclass(frozen=True)
class FinancialFactKey:
    instrument_id: UUID
    metric: str
    period_end: str | None
    period_type: str
    reporting_basis: str | None = None


@dataclass(frozen=True)
class FinancialFact:
    key: FinancialFactKey
    value: ProvenancedValue
    source_tier: FactSourceTier
    source_provider: str
    source_identity: str
    source_mode: SourceMode = SourceMode.REAL


def merge_fact(existing: FinancialFact | None, incoming: FinancialFact | None, *,
               allow_same_tier_correction: bool = False) -> FinancialFact | None:
    """Return the accepted fact without letting a fallback erase evidence.

    A correction is intentionally opt-in and only valid for a same-tier official
    fact with the same canonical fact identity. Retrieval time never controls
    precedence.
    """
    if incoming is None or _missing(incoming.value.value):
        return existing
    if existing is None or _missing(existing.value.value):
        return incoming
    if existing.key != incoming.key:
        raise ValueError("Financial facts with different canonical identities cannot be merged")
    if fact_source_authority(incoming.source_tier) > fact_source_authority(existing.source_tier):
        return incoming
    if fact_source_authority(incoming.source_tier) < fact_source_authority(existing.source_tier):
        return existing
    if allow_same_tier_correction and incoming.source_tier == FactSourceTier.OFFICIAL_NSE:
        return incoming
    return existing


def _missing(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())
