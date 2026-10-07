"""Canonical ETF universe selection and cheap admission.

Mirrors app.global_scanner.CanonicalEquityUniverse but scoped to
assetType == ETF against the same Global Instrument Master (portfolio
service) endpoint, so the Equity and ETF universes are always disjoint
server-side queries -- never a shared row set split by a local heuristic.

Admission here is deliberately cheap: no provider acquisition, no document
fetch, no readiness computation. It only consults the canonical row itself
and evidence already persisted by ETF-1/2/3 (listings, market-price facts).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

import httpx

from app.etf_evidence import EtfMetric


class EtfUniverse(Protocol):
    async def active_global_etfs(self, **kwargs) -> list[dict]: ...


class CanonicalEtfUniverse:
    """Read canonical ETF metadata only; no portfolio ownership or provider acquisition."""

    def __init__(self, client: httpx.AsyncClient, base_url: str):
        self.client, self.base_url = client, base_url.rstrip("/")

    async def active_global_etfs(self, *, correlation_id=None, identity_headers=None):
        headers = {k: v for k, v in (identity_headers or {}).items() if v}
        if correlation_id:
            headers["X-Correlation-Id"] = correlation_id
        values, page = [], 0
        while True:
            response = await self.client.get(
                f"{self.base_url}/api/v1/instruments",
                params={"status": "ACTIVE", "assetType": "ETF", "page": page, "size": 500},
                headers=headers or None,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("instruments"), list):
                raise ValueError("Invalid canonical ETF universe response")
            batch = payload["instruments"]
            total = int(payload.get("totalElements", len(values) + len(batch)))
            values.extend(batch)
            if len(values) >= total:
                return values
            if not batch:
                raise ValueError("Incomplete canonical ETF universe pagination")
            page += 1


def nse_etfs(rows: list[dict]) -> list[dict]:
    """Defense-in-depth re-check mirroring app.global_opportunity_cycle.nse_equities.

    The server-side assetType=ETF filter is trusted but re-verified here from
    the same canonical fields -- never re-derived from a name/symbol heuristic.
    """
    return [r for r in rows if (r.get("exchange") or r.get("primaryExchange")) == "NSE"
            and r.get("status") == "ACTIVE" and r.get("assetType") == "ETF"]


class EtfAdmissionOutcome(StrEnum):
    ADMITTED = "ADMITTED"
    INELIGIBLE = "INELIGIBLE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    UNSUPPORTED = "UNSUPPORTED"
    TECHNICAL_FAILURE = "TECHNICAL_FAILURE"


@dataclass(frozen=True)
class EtfAdmissionDecision:
    global_instrument_id: UUID | None
    symbol: str | None
    outcome: EtfAdmissionOutcome
    reason: str
    candidate: dict


def _parse_instrument_id(row: dict) -> UUID | None:
    raw = row.get("globalInstrumentId") or row.get("instrumentId")
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


def admit_etf_candidate(row: dict, store) -> EtfAdmissionDecision:
    """One explicit, auditable decision per candidate. Never raises for an
    ordinary data problem -- those become TECHNICAL_FAILURE/INELIGIBLE/
    INSUFFICIENT_DATA decisions instead, so one bad row never aborts a batch.
    """
    symbol = row.get("symbol")
    if row.get("assetType") != "ETF":
        return EtfAdmissionDecision(_parse_instrument_id(row), symbol, EtfAdmissionOutcome.INELIGIBLE, "NOT_ETF", row)
    if row.get("status") != "ACTIVE":
        return EtfAdmissionDecision(_parse_instrument_id(row), symbol, EtfAdmissionOutcome.INELIGIBLE, "INACTIVE", row)
    exchange = row.get("exchange") or row.get("primaryExchange")
    if exchange != "NSE":
        return EtfAdmissionDecision(_parse_instrument_id(row), symbol, EtfAdmissionOutcome.UNSUPPORTED,
            "UNSUPPORTED_MARKET", row)
    instrument_id = _parse_instrument_id(row)
    if instrument_id is None:
        return EtfAdmissionDecision(None, symbol, EtfAdmissionOutcome.INELIGIBLE, "INVALID_IDENTITY", row)
    try:
        listings = [listing for listing in store.etf_listings() if listing.instrument_id == instrument_id]
    except Exception as exc:
        return EtfAdmissionDecision(instrument_id, symbol, EtfAdmissionOutcome.TECHNICAL_FAILURE,
            f"IDENTITY_LOOKUP_FAILED:{type(exc).__name__}", row)
    if not listings:
        return EtfAdmissionDecision(instrument_id, symbol, EtfAdmissionOutcome.INSUFFICIENT_DATA,
            "UNVERIFIED_MAPPING", row)
    try:
        price_facts = store.etf_facts(instrument_id, EtfMetric.MARKET_PRICE)
    except Exception as exc:
        return EtfAdmissionDecision(instrument_id, symbol, EtfAdmissionOutcome.TECHNICAL_FAILURE,
            f"MARKET_DATA_LOOKUP_FAILED:{type(exc).__name__}", row)
    if not price_facts:
        return EtfAdmissionDecision(instrument_id, symbol, EtfAdmissionOutcome.INSUFFICIENT_DATA,
            "MISSING_USABLE_PRICE", row)
    return EtfAdmissionDecision(instrument_id, symbol, EtfAdmissionOutcome.ADMITTED, "ADMITTED", row)


def admit_etf_candidates(rows: list[dict], store) -> list[EtfAdmissionDecision]:
    """Admit every row exactly once. The result always has the same length
    as `rows` -- a row failing the NSE/ETF/ACTIVE pre-filter still gets an
    explicit INELIGIBLE decision rather than silently disappearing."""
    prefiltered_ids = {id(row) for row in nse_etfs(rows)}
    decisions = []
    for row in rows:
        if id(row) in prefiltered_ids:
            decisions.append(admit_etf_candidate(row, store))
        else:
            decisions.append(EtfAdmissionDecision(_parse_instrument_id(row), row.get("symbol"),
                EtfAdmissionOutcome.INELIGIBLE, "FAILED_NSE_ETF_ACTIVE_PREFILTER", row))
    return decisions
