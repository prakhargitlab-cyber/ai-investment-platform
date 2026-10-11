"""ETF Radar cycle: universe -> admission -> persisted-data baseline ->
ETF-specific readiness -> deterministic metrics -> ETF_RULE_ENGINE_V1 ->
risk gates -> recommendation -> ranking -> terminal accounting.

Deliberately separate from app.global_opportunity_cycle (which is
equity-specific: see its nse_equities() filter). This module never calls
into company-specific research acquisition -- no financial-PDF, quarterly
statement, shareholding, order-book, CAPEX, or governance evidence fetch at
any stage. Admission and readiness here only ever read evidence that is
already persisted (ETF-1/2/3); this cycle function itself performs no
provider acquisition of its own, bounded or otherwise.
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Callable
from uuid import UUID, uuid4

from app.etf_domain import classify_etf
from app.etf_evidence import EtfMetric
from app.etf_metrics import compute_etf_deterministic_metrics
from app.etf_readiness import evaluate_etf_readiness
from app.etf_rule_engine import (
    ETF_RULE_ENGINE_VERSION, EtfRecommendation, EtfRiskGate, EtfRuleEngineInput, EtfRuleEngineResult,
    derive_etf_recommendation, evaluate_etf_risk_gates, score_etf,
)
from app.etf_universe import EtfAdmissionDecision, EtfAdmissionOutcome, admit_etf_candidates

ETF_RADAR_VERSION = "ETF_RADAR_V1"


class EtfCycleDisposition:
    EVALUATED = "EVALUATED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    INELIGIBLE = "INELIGIBLE"
    UNSUPPORTED = "UNSUPPORTED"
    TECHNICAL_FAILURE = "TECHNICAL_FAILURE"
    CANCELLED = "CANCELLED"


# Every admission outcome maps to exactly one terminal disposition; a bare
# dict literal (rather than an if/elif chain) makes an unmapped outcome a
# KeyError instead of a silently-wrong default.
_ADMISSION_TO_DISPOSITION = {
    EtfAdmissionOutcome.INELIGIBLE: EtfCycleDisposition.INELIGIBLE,
    EtfAdmissionOutcome.INSUFFICIENT_DATA: EtfCycleDisposition.INSUFFICIENT_DATA,
    EtfAdmissionOutcome.UNSUPPORTED: EtfCycleDisposition.UNSUPPORTED,
    EtfAdmissionOutcome.TECHNICAL_FAILURE: EtfCycleDisposition.TECHNICAL_FAILURE,
}


@dataclass(frozen=True)
class EtfCycleCandidateResult:
    global_instrument_id: UUID | None
    symbol: str | None
    disposition: str
    reason: str
    subtype: str | None = None
    rule_engine_result: EtfRuleEngineResult | None = None
    risk_gates: tuple[EtfRiskGate, ...] = ()
    recommendation: str | None = None
    rank: int | None = None


@dataclass(frozen=True)
class EtfCycleResult:
    radar_version: str
    cycle_id: UUID
    correlation_id: str | None
    as_of: datetime
    candidates: tuple[EtfCycleCandidateResult, ...]
    ranked: tuple[EtfCycleCandidateResult, ...]
    excluded_by_reason: dict[str, int]


def _json_default(obj):
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    raise TypeError(f"Not JSON serializable: {type(obj)!r}")


def etf_cycle_result_to_dict(result: EtfCycleResult) -> dict:
    """A plain, JSON-round-trippable dict -- the canonical persisted shape
    for a cycle result. dataclasses.asdict() handles the nested-dataclass
    structure; json.dumps's default handles the remaining leaf types
    (UUID/Decimal/datetime/Enum) that asdict leaves untouched."""
    return json.loads(json.dumps(dataclasses.asdict(result), default=_json_default))


def _bars(store, instrument_id: UUID):
    return [bar for bar in store.load_daily_market_bars({instrument_id}) if bar.global_instrument_id == instrument_id]


def _fact(store, instrument_id: UUID, metric: EtfMetric):
    facts = store.etf_facts(instrument_id, metric)
    return facts[0] if facts else None


def _nav(store, instrument_id: UUID):
    navs = store.etf_navs(instrument_id)
    return navs[0] if navs else None


def _holdings(store, instrument_id: UUID):
    snapshots = store.etf_holdings(instrument_id)
    return snapshots[0] if snapshots else None


def _subtype_for(decision: EtfAdmissionDecision, profile_lookup) -> str | None:
    """Classify from canonical fund metadata only, never from name/symbol.

    profile_lookup is an optional ``UUID -> EtfResearchProfile | None``
    callable supplied by the caller (e.g. backed by
    ResearchRepository.etf_profile). When absent, or when no profile is on
    file, subtype is reported as None (unclassified) rather than guessed --
    classify_etf() itself already degrades unmatched/conflicting metadata to
    EtfSubtype.OTHER, so None here specifically means "no profile to ask".
    """
    if profile_lookup is None or decision.global_instrument_id is None:
        return None
    profile = profile_lookup(decision.global_instrument_id)
    if profile is None:
        return None
    try:
        return str(classify_etf(profile))
    except Exception:
        return None


def evaluate_admitted_etf(decision: EtfAdmissionDecision, store, *, now: datetime,
                           profile_lookup=None) -> EtfCycleCandidateResult:
    """Score a single ADMITTED candidate. Never raises for an ordinary data
    gap -- those become EVALUATED-with-INSUFFICIENT_DATA-recommendation
    rather than aborting the candidate, except for a genuine exception from
    the persistence layer itself, which is reported as TECHNICAL_FAILURE."""
    instrument_id = decision.global_instrument_id
    try:
        bars = _bars(store, instrument_id)
        statuses, summary = evaluate_etf_readiness(store, instrument_id)
        metrics = compute_etf_deterministic_metrics(instrument_id, bars)
        rule_input = EtfRuleEngineInput(
            instrument_id=instrument_id, readiness_statuses=statuses, readiness_summary=summary, metrics=metrics,
            nav=_nav(store, instrument_id), market_price_fact=_fact(store, instrument_id, EtfMetric.MARKET_PRICE),
            trading_volume_fact=_fact(store, instrument_id, EtfMetric.TRADING_VOLUME),
            bid_ask_spread_fact=_fact(store, instrument_id, EtfMetric.BID_ASK_SPREAD),
            tracking_error_fact=_fact(store, instrument_id, EtfMetric.TRACKING_ERROR),
            tracking_difference_fact=_fact(store, instrument_id, EtfMetric.TRACKING_DIFFERENCE),
            expense_ratio_fact=_fact(store, instrument_id, EtfMetric.EXPENSE_RATIO),
            aum_fact=_fact(store, instrument_id, EtfMetric.AUM), holdings=_holdings(store, instrument_id), now=now,
        )
    except Exception as exc:
        return EtfCycleCandidateResult(instrument_id, decision.symbol, EtfCycleDisposition.TECHNICAL_FAILURE,
            f"EVIDENCE_LOOKUP_FAILED:{type(exc).__name__}:{str(exc)[:160]}")
    result = score_etf(rule_input)
    gates = evaluate_etf_risk_gates(rule_input, result)
    recommendation = derive_etf_recommendation(result, gates)
    subtype = _subtype_for(decision, profile_lookup)
    return EtfCycleCandidateResult(instrument_id, decision.symbol, EtfCycleDisposition.EVALUATED, "EVALUATED",
        subtype=subtype, rule_engine_result=result, risk_gates=tuple(gates), recommendation=str(recommendation))


def run_etf_radar_cycle(rows: list[dict], store, *, now: datetime | None = None, cycle_id: UUID | None = None,
                         correlation_id: str | None = None, top_n: int | None = 10,
                         profile_lookup: Callable[[UUID], object | None] | None = None) -> EtfCycleResult:
    """Deterministic, synchronous ETF Radar cycle over already-persisted
    evidence. Performs no provider acquisition of its own -- `rows` is the
    caller-supplied canonical universe (e.g. from CanonicalEtfUniverse), and
    every lookup against `store` reads existing evidence only.

    Every row in `rows` ends up in exactly one `candidates` entry; `ranked`
    is the EVALUATED subset ordered by score (ties broken by instrument id
    for reproducibility), truncated to `top_n` when given.
    """
    now = now or datetime.now(timezone.utc)
    cycle_id = cycle_id or uuid4()
    decisions = admit_etf_candidates(rows, store)
    candidates: list[EtfCycleCandidateResult] = []
    for decision in decisions:
        if decision.outcome == EtfAdmissionOutcome.ADMITTED:
            candidates.append(evaluate_admitted_etf(decision, store, now=now, profile_lookup=profile_lookup))
        else:
            disposition = _ADMISSION_TO_DISPOSITION[decision.outcome]
            candidates.append(EtfCycleCandidateResult(decision.global_instrument_id, decision.symbol,
                disposition, decision.reason))

    def _score_key(candidate: EtfCycleCandidateResult):
        score = candidate.rule_engine_result.overall_score if candidate.rule_engine_result else None
        # Highest score first; None (shouldn't occur for EVALUATED, but
        # handled defensively) sorts last; ties broken by instrument id so
        # ranking is byte-for-byte reproducible across runs.
        return (0 if score is not None else 1, -(score or 0), str(candidate.global_instrument_id))

    evaluated = [c for c in candidates if c.disposition == EtfCycleDisposition.EVALUATED]
    ordered = sorted(evaluated, key=_score_key)
    limited = ordered[:top_n] if top_n is not None else ordered
    ranked = tuple(
        EtfCycleCandidateResult(c.global_instrument_id, c.symbol, c.disposition, c.reason, c.subtype,
            c.rule_engine_result, c.risk_gates, c.recommendation, rank=i + 1)
        for i, c in enumerate(limited)
    )

    excluded_by_reason: dict[str, int] = {}
    for candidate in candidates:
        if candidate.disposition != EtfCycleDisposition.EVALUATED:
            excluded_by_reason[candidate.reason] = excluded_by_reason.get(candidate.reason, 0) + 1

    return EtfCycleResult(radar_version=ETF_RADAR_VERSION, cycle_id=cycle_id, correlation_id=correlation_id,
        as_of=now, candidates=tuple(candidates), ranked=ranked, excluded_by_reason=excluded_by_reason)


async def run_etf_radar_cycle_async(repository, portfolio_orchestrator, *, top_n: int | None = 10,
                                     candidate_ids: list | None = None, correlation_id: str | None = None,
                                     cycle_id: UUID | None = None, checkpoint=None,
                                     identity_headers: dict | None = None, **_ignored_resumable_extras):
    """Async entry point for the generic OpportunityCycleWorker (market='ETF').

    Mirrors the exact body the synchronous API endpoint used to run inline:
    universe/candidate rows -> run_etf_radar_cycle() -> persist. The only
    difference is that this now executes on the worker's queue instead of
    inside the HTTP request, so POST /api/v1/etf-radar/cycles returns as
    soon as the durable run row is created.

    `checkpoint` (a per-instrument CycleCheckpoint, passed by the worker
    whenever the persistence layer supports resumable runs) is deliberately
    accepted and ignored: a full ETF Radar pass is a single bounded,
    deterministic, DB-evidence-only computation with no per-candidate
    acquisition phases (no BASELINE/DEEP split like Equity's Stage2), so
    there is nothing in it that benefits from per-instrument resumability.
    If the worker process restarts mid-run, the whole (still-bounded) cycle
    simply re-executes under the same cycle_id, which is safe because this
    function performs no provider acquisition and is idempotent over
    already-persisted evidence.
    """
    if candidate_ids is not None:
        # Bounded diagnostic path: rows are built from the given ids
        # directly, with no portfolio-service call at all -- there is
        # nothing here for identity_headers to apply to, and this path is
        # deliberately left unchanged.
        rows = [{'globalInstrumentId': str(instrument_id), 'assetType': 'ETF', 'exchange': 'NSE', 'status': 'ACTIVE'}
                for instrument_id in candidate_ids]
    else:
        rows = await portfolio_orchestrator.active_global_etfs(correlation_id=correlation_id, identity_headers=identity_headers)
    with repository._persistence_worker_lock:
        result = run_etf_radar_cycle(rows, repository.persistence, cycle_id=cycle_id,
            correlation_id=correlation_id, top_n=top_n)
        payload = etf_cycle_result_to_dict(result)
        repository.persistence.save_etf_radar_cycle(result.cycle_id, result.radar_version, result.correlation_id,
            result.as_of, payload)
    return {'cycle_id': str(result.cycle_id), 'universe_count': len(rows)}
