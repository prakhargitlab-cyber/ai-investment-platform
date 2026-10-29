"""ETF Radar cycle: universe -> admission -> persisted-data baseline ->
ETF-specific readiness -> deterministic metrics -> ETF_RULE_ENGINE_V1 ->
risk gates -> recommendation -> ranking -> terminal accounting.

Deliberately separate from app.global_opportunity_cycle (which is
equity-specific: see its nse_equities() filter). This module never calls
into company-specific research acquisition -- no financial-PDF, quarterly
statement, shareholding, order-book, CAPEX, or governance evidence fetch at
any stage. Admission, readiness and the synchronous run_etf_radar_cycle()
core only ever read evidence that is already persisted (ETF-1/2/3); that
core function performs no provider acquisition of its own.

The one exception is the async entry point, run_etf_radar_cycle_async():
when invoked from the explicitly authorized, canonical-ID-verified bounded
manual cycle path (verify_canonical_candidates=True, the only path reached
by POST /api/v1/etf-radar/controlled-cycles), it may run
_ensure_etf_historical_coverage() first -- a bounded, resumable top-up of
missing daily bars for exactly the selected instruments, entirely before
the synchronous evaluate_and_save() closure acquires
repository._persistence_worker_lock, so network I/O never happens while a
DB transaction/lock is held. It reuses the existing NSE historical
pipeline verbatim (no second provider, no new persistence path, no
scheduler) and is a no-op unless the caller passes a `jobs` instance.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Callable
from uuid import UUID, uuid4

from app.etf_domain import classify_etf
from app.etf_evidence import EtfMetric
from app.etf_metrics import compute_etf_deterministic_metrics, usable_close_series, MOMENTUM_LONG_WINDOW
from app.etf_readiness import evaluate_etf_readiness, _DERIVED_METRIC_MAX_AGE_DAYS
from app.etf_rule_engine import (
    ETF_RULE_ENGINE_VERSION, EtfRecommendation, EtfRiskGate, EtfRuleEngineInput, EtfRuleEngineResult,
    derive_etf_recommendation, evaluate_etf_risk_gates, score_etf,
)
from app.etf_universe import EtfAdmissionDecision, EtfAdmissionOutcome, admit_etf_candidates

logger = logging.getLogger(__name__)
ETF_RADAR_VERSION = "ETF_RADAR_V1"

# Bounded, resumable historical-bar top-up (see _ensure_etf_historical_coverage).
# The largest window any ETF factor needs is momentum's 50/200-day MA (200
# sessions); satisfying it also satisfies volatility (63+1) and drawdown
# (<=252, min 2), so 200 is the single target this step acquires toward.
ETF_HISTORY_REQUIRED_SESSIONS = MOMENTUM_LONG_WINDOW
# At most this many provider requests per instrument per explicitly
# authorized cycle -- a finite request budget, never a retry loop. Each
# request is itself capped by the EXISTING settings.nse_historical_request_window_days.
ETF_HISTORY_MAX_WINDOWS_PER_CYCLE = 3
# Per-instrument wall-clock budget for the whole top-up attempt, guarding
# against a stalled provider holding up the cycle.
ETF_HISTORY_ACQUISITION_TIMEOUT_SECONDS = 60
# "Sufficient" coverage means both deep enough (>= REQUIRED_SESSIONS) and
# current -- a count-only check would treat 200 stale sessions as done
# forever and never fetch forward again. Reuses etf_readiness's own
# existing definition of "fresh" for these exact derived metrics
# (momentum/volatility/drawdown) rather than inventing a second threshold.
ETF_HISTORY_MAX_STALE_DAYS = _DERIVED_METRIC_MAX_AGE_DAYS


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
    payload = json.loads(json.dumps(dataclasses.asdict(result), default=_json_default))
    payload["universe_count"] = len(result.candidates)
    return payload


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
                           profile_lookup=None, historical_failure_reason: str | None = None) -> EtfCycleCandidateResult:
    """Score a single ADMITTED candidate. Never raises for an ordinary data
    gap -- those become EVALUATED-with-INSUFFICIENT_DATA-recommendation
    rather than aborting the candidate, except for a genuine exception from
    the persistence layer itself, which is reported as TECHNICAL_FAILURE.

    ``historical_failure_reason``, when set, means the caller's bounded
    historical-coverage top-up (_ensure_etf_historical_coverage) hit a
    genuine acquisition TECHNICAL_FAILURE (identity mismatch, HTTP/timeout,
    malformed response, or persistence failure) for THIS instrument this
    cycle -- never an ordinary "still not enough history" outcome, which is
    left unset and falls through to the normal readiness/scoring path
    below, reporting truthfully as INSUFFICIENT_DATA from freshly re-read
    bars. A technical failure must never be disguised as ordinary data
    absence, so this short-circuits straight to TECHNICAL_FAILURE without
    touching readiness or scoring for this one instrument -- other
    candidates in the same cycle are entirely unaffected."""
    instrument_id = decision.global_instrument_id
    if historical_failure_reason is not None:
        return EtfCycleCandidateResult(instrument_id, decision.symbol, EtfCycleDisposition.TECHNICAL_FAILURE,
            f"HISTORICAL_ACQUISITION_FAILED:{historical_failure_reason}")
    try:
        bars = _bars(store, instrument_id)
        statuses, summary = evaluate_etf_readiness(store, instrument_id, now=now)
        if not summary.ready_for_analysis:
            reason = "MANDATORY_EVIDENCE_NOT_READY:" + ",".join(
                f"{item.requirement}={item.status}" for item in statuses
                if item.role == "MANDATORY" and item.status != "READY_FRESH")
            return EtfCycleCandidateResult(instrument_id, decision.symbol,
                EtfCycleDisposition.INSUFFICIENT_DATA, reason)
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
                         profile_lookup: Callable[[UUID], object | None] | None = None,
                         historical_acquisition_failures: dict | None = None) -> EtfCycleResult:
    """Deterministic, synchronous ETF Radar cycle over already-persisted
    evidence. Performs no provider acquisition of its own -- `rows` is the
    caller-supplied canonical universe (e.g. from CanonicalEtfUniverse), and
    every lookup against `store` reads existing evidence only.

    ``historical_acquisition_failures``, when given, is the
    {instrument_id: failure_reason} map an earlier, separate async step
    (_ensure_etf_historical_coverage, called from run_etf_radar_cycle_async
    before this synchronous function is invoked) may have produced -- see
    evaluate_admitted_etf for how a listed instrument is short-circuited to
    TECHNICAL_FAILURE instead of being scored. This function itself still
    performs no acquisition; it only reads the outcome of one that already
    happened, same as every other lookup here.

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
            failure_reason = (historical_acquisition_failures or {}).get(decision.global_instrument_id)
            candidates.append(evaluate_admitted_etf(decision, store, now=now, profile_lookup=profile_lookup,
                historical_failure_reason=failure_reason))
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


async def _ensure_etf_historical_coverage(repository, rows, *, jobs, identity_headers, correlation_id) -> dict:
    """Bounded, resumable top-up of persisted daily bars for exactly the
    explicitly selected canonical ETF rows passed in -- never the full
    universe. Called from run_etf_radar_cycle_async BEFORE
    evaluate_and_save() acquires repository._persistence_worker_lock, so
    this function's network I/O never happens while that lock (or any DB
    transaction) is held.

    Reuses the existing NSE historical pipeline verbatim: jobs
    (IndiaMarketDataPopulationJobs) .populate_daily_bars() ->
    NseHistoricalDailyProvider -> the same canonical identity verification,
    date normalization and upsert_daily_market_bars_async persistence the
    manual POST /api/v1/etf-radar/evidence/history endpoint already uses.
    No second provider, no new persistence path, no scheduler.

    Bounds: at most ETF_HISTORY_MAX_WINDOWS_PER_CYCLE provider requests per
    instrument per cycle (a finite request budget, never a retry loop);
    each window is sized by the EXISTING
    settings.nse_historical_request_window_days cap. Requests are paced by
    populate_daily_bars' own existing inter-request sleep and serialized by
    IndiaMarketDataPopulationJobs' own existing _daily_bar_lock -- the same
    lock the manual history endpoint already uses on this same `jobs`
    instance, so concurrent acquisition for the same instrument (from a
    manual call or another cycle) is already prevented without a new lock.
    An ETF_HISTORY_ACQUISITION_TIMEOUT_SECONDS wall-clock budget per
    instrument guards against a stalled provider. One instrument's
    exception is caught and recorded, never raised -- it cannot abort
    another instrument's loop iteration or the cycle itself.

    Coverage is always re-read fresh from the store after each window and
    at the end -- never inferred from an attempt's own report -- so a
    candidate can never be treated as having coverage it does not actually
    have persisted (the subsequent evaluate_admitted_etf call re-reads
    bars itself too, independently).

    "Sufficient" means both deep enough (>= ETF_HISTORY_REQUIRED_SESSIONS)
    and current (newest usable bar within ETF_HISTORY_MAX_STALE_DAYS of
    today) -- a row count alone would let a one-time historical snapshot
    be treated as permanently done and silently go stale forever, since
    nothing else in this pipeline ever appends a bar going forward on its
    own. While still building depth, this walks backward from the oldest
    already-persisted bar (or from today, if none exist); once depth is
    satisfied but the recent end is stale, it walks forward from the
    newest persisted bar toward today instead -- either way, a later
    cycle only ever requests the still-missing range, never re-fetching
    history a prior cycle already obtained.

    Returns {instrument_id: failure_reason}, included only for instruments
    that hit a genuine acquisition TECHNICAL_FAILURE this cycle (identity
    mismatch, HTTP/timeout, malformed response, or persistence failure).
    An ordinary "provider had nothing for this window" (NO_VALID_HISTORY)
    is not an error and is left out -- the normal readiness/scoring path
    reports remaining insufficiency truthfully as INSUFFICIENT_DATA from
    the freshly re-read bars, never disguised as a technical failure.
    """
    failures: dict = {}

    async def load_usable(instrument_id):
        def read():
            return usable_close_series(repository.persistence.load_daily_market_bars({instrument_id}))
        run_blocking = getattr(repository, '_run_blocking_persistence', None)
        return await run_blocking(read) if run_blocking is not None else read()

    def is_covered(usable, today):
        """Deep enough AND current -- a count that stopped advancing weeks
        or months ago must never be treated as done. ``today`` is sampled
        once per instrument so this check and the acquisition it gates
        agree on what "current" means for this pass."""
        if len(usable) < ETF_HISTORY_REQUIRED_SESSIONS:
            return False
        return (today - usable[-1][0]).days <= ETF_HISTORY_MAX_STALE_DAYS

    for row in rows:
        if row.get('assetType') != 'ETF':
            continue
        try:
            instrument_id = UUID(str(row['globalInstrumentId']))
        except (KeyError, ValueError, TypeError):
            continue
        windows_used = 0
        technical_failure = None
        try:
            today = datetime.now(timezone.utc).date()
            usable = await load_usable(instrument_id)
            if is_covered(usable, today):
                continue  # already sufficient and current -- no acquisition needed

            async def run_windows():
                nonlocal windows_used, technical_failure, usable
                window_days = jobs.settings.nse_historical_request_window_days
                while windows_used < ETF_HISTORY_MAX_WINDOWS_PER_CYCLE:
                    if len(usable) >= ETF_HISTORY_REQUIRED_SESSIONS:
                        # Depth is already satisfied -- the remaining gap is
                        # staleness at the recent end, so walk FORWARD from
                        # the newest persisted bar toward today instead of
                        # re-requesting history that is already covered.
                        window_start = usable[-1][0] + timedelta(days=1)
                        window_end = min(today, window_start + timedelta(days=window_days - 1))
                    else:
                        # Still building depth -- walk BACKWARD from the
                        # oldest persisted bar (or today, if none exist),
                        # exactly as before.
                        window_end = (usable[0][0] - timedelta(days=1)) if usable else today
                        window_start = window_end - timedelta(days=window_days - 1)
                    if window_start > window_end:
                        break  # nothing left to request in this direction
                    result = await jobs.populate_daily_bars(instrument_id, start=window_start, end=window_end,
                        identity_headers=identity_headers, correlation_id=correlation_id)
                    windows_used += 1
                    if result.status != 'SUCCESS':
                        technical_failure = result.failure_reason
                        break
                    usable = await load_usable(instrument_id)
                    if is_covered(usable, today):
                        break

            try:
                await asyncio.wait_for(run_windows(), timeout=ETF_HISTORY_ACQUISITION_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                technical_failure = technical_failure or 'HISTORICAL_ACQUISITION_TIMEOUT'

            # Re-read once more regardless of outcome above: populate_daily_bars'
            # own existing finally-block pacing sleep (after persistence, inside
            # its own _daily_bar_lock) can be where a timeout cancellation lands,
            # even though that window's bars were already durably persisted. A
            # cancellation must never be reported as a failure for coverage that
            # is, truthfully, already sufficient and current once re-verified.
            usable = await load_usable(instrument_id)
            if is_covered(usable, today):
                technical_failure = None

            if technical_failure and technical_failure != 'NO_VALID_HISTORY':
                failures[instrument_id] = technical_failure
            logger.info(
                'etf_historical_coverage_ensure instrumentId=%s windowsUsed=%s usableSessions=%s failure=%s',
                instrument_id, windows_used, len(usable), technical_failure)
        except Exception as exc:
            failures[instrument_id] = f'COVERAGE_ENSURE_UNAVAILABLE:{type(exc).__name__}'
            logger.warning('etf_historical_coverage_ensure_failed instrumentId=%s exceptionClass=%s',
                instrument_id, type(exc).__name__)
    return failures


def _enrich_bounded_candidates_with_screening_evidence(payload, store, *, now):
    """Attach the SAME fund-quality/entry-evidence fields the full ETF
    universe cycle already publishes (app.etf_full_universe) to every
    bounded-cycle candidate that has a resolved canonical instrument id --
    screening_metrics (trading_volume, drawdown, volatility, concentration,
    expense_ratio, aum, tracking_error, tracking_difference, bid_ask_spread,
    fund_age), fund_house/domicile/distribution_type/replication_method,
    history_observations, holdings_coverage and entry_inputs.

    Deliberately reuses app.etf_screening.screening_evidence() and
    enrich_candidate() verbatim rather than recomputing anything: every
    value it produces was already trusted and persisted (EtfFact/EtfNav/
    DailyMarketBar/EtfHoldingsSnapshot reads only) -- this performs no
    acquisition, no network I/O, and changes no score, gate, ranking or
    full-universe behavior. A field with no persisted evidence still comes
    back unavailable/UNKNOWN from screening_evidence() itself, exactly as
    it already does for the full-universe cycle -- this never invents a
    value, only stops hiding ones that already exist.
    """
    from app.etf_screening import enrich_candidate, screening_evidence
    for candidate in payload.get('candidates', ()):
        raw_id = candidate.get('global_instrument_id')
        if not raw_id:
            continue  # no resolved canonical identity (e.g. NOT_ETF/INVALID_IDENTITY) -- nothing to read
        key = UUID(raw_id)
        selected = {}
        for fact in store.etf_facts(key):
            selected.setdefault(str(fact.metric), fact)
        navs = store.etf_navs(key)
        if navs:
            selected['NAV'] = navs[0]
        candidate.update(screening_evidence(store, key, selected, candidate, now=now))
        enrich_candidate(candidate, now=now)


async def run_etf_radar_cycle_async(repository, portfolio_orchestrator, *, top_n: int | None = 10,
                                     candidate_ids: list | None = None, correlation_id: str | None = None,
                                     cycle_id: UUID | None = None, checkpoint=None,
                                     identity_headers: dict | None = None, verify_canonical_candidates: bool = False,
                                     full_universe_limits: dict | None = None, jobs=None, **_ignored_resumable_extras):
    """Async entry point for the generic OpportunityCycleWorker (market='ETF').

    Mirrors the exact body the synchronous API endpoint used to run inline:
    universe/candidate rows -> run_etf_radar_cycle() -> persist. The only
    difference is that this now executes on the worker's queue instead of
    inside the HTTP request, so POST /api/v1/etf-radar/cycles returns as
    soon as the durable run row is created.

    The checkpoint fences publication and recovers an already published result.
    Evaluation reads persisted evidence only; an interrupted, unpublished pass
    can be repeated under its new lease without acquiring provider data.
    """
    run_blocking = getattr(repository, '_run_blocking_persistence', None)
    if checkpoint is not None:
        run = await checkpoint.run()
        if run and run.get('status') == 'PUBLISHED':
            loader = repository.persistence.etf_radar_cycle
            saved = await run_blocking(loader, cycle_id) if run_blocking else loader(cycle_id)
            if saved is None:
                raise RuntimeError('PUBLISHED_ETF_RESULT_MISSING')
            return {'cycle_id': str(cycle_id), 'universe_count': saved.get('universe_count', len(saved.get('candidates', [])))}
    if full_universe_limits is not None:
        from app.etf_full_universe import run_full_etf_cycle
        return await run_full_etf_cycle(repository, portfolio_orchestrator, checkpoint=checkpoint,
            cycle_id=cycle_id, identity_headers=identity_headers, correlation_id=correlation_id,
            limits=full_universe_limits)
    if verify_canonical_candidates:
        from app.etf_ingestion import canonical_etfs, etf_preflight
        rows = await canonical_etfs(portfolio_orchestrator, [UUID(str(key)) for key in (candidate_ids or [])],
            identity_headers=identity_headers, correlation_id=correlation_id)
    elif candidate_ids is not None:
        # Bounded diagnostic path: rows are built from the given ids
        # directly, with no portfolio-service call at all -- there is
        # nothing here for identity_headers to apply to, and this path is
        # deliberately left unchanged.
        rows = [{'globalInstrumentId': str(instrument_id), 'assetType': 'ETF', 'exchange': 'NSE', 'status': 'ACTIVE'}
                for instrument_id in candidate_ids]
    else:
        rows = await portfolio_orchestrator.active_global_etfs(correlation_id=correlation_id, identity_headers=identity_headers)
    if not rows:
        from app.global_opportunity_cycle import UniverseUnavailableError
        raise UniverseUnavailableError('ETF_UNIVERSE_UNAVAILABLE')

    historical_acquisition_failures: dict = {}
    if verify_canonical_candidates and jobs is not None:
        # Only the explicitly authorized, canonical-ID-verified bounded
        # manual path ever reaches here (full_universe_limits returns
        # early above; the unverified candidate_ids diagnostic path is
        # deliberately left unchanged) -- and only when the caller passed
        # a real jobs instance. All network I/O happens here, before the
        # lock below is ever acquired.
        historical_acquisition_failures = await _ensure_etf_historical_coverage(
            repository, rows, jobs=jobs, identity_headers=identity_headers, correlation_id=correlation_id)

    def evaluate_and_save():
        with repository._persistence_worker_lock:
            if verify_canonical_candidates and etf_preflight(repository.persistence, rows)['status'] != 'READY':
                raise ValueError('ETF_MANDATORY_EVIDENCE_NOT_READY')
            result = run_etf_radar_cycle(rows, repository.persistence, cycle_id=cycle_id,
                correlation_id=correlation_id, top_n=top_n,
                historical_acquisition_failures=historical_acquisition_failures)
            payload = etf_cycle_result_to_dict(result)
            _enrich_bounded_candidates_with_screening_evidence(payload, repository.persistence, now=result.as_of)
            repository.persistence.save_etf_radar_cycle(result.cycle_id, result.radar_version, result.correlation_id,
                result.as_of, payload, **({'fence': (checkpoint.cycle_id, checkpoint.owner_id)} if checkpoint else {}))
        return {'cycle_id': str(result.cycle_id), 'universe_count': len(rows)}

    run_blocking = getattr(repository, '_run_blocking_persistence', None)
    return await run_blocking(evaluate_and_save) if run_blocking is not None else evaluate_and_save()
