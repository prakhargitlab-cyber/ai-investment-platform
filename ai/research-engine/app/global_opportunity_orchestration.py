"""Explicit engineering ranking over already-read canonical and persisted evidence.

With an injected readiness runtime (acquisition_enabled=True), ALL eligible stocks
receive provider-free evaluation of persisted evidence. Deterministic discovery
then admits at most shortlist_limit candidates for live baseline acquisition and
deep research; unselected candidates are explicitly deferred.

Without an injected readiness runtime this remains a persisted-only diagnostic for
dashboard reads.

Pass PortfolioResearchOrchestrator.register_global_profile_metadata as the
profile_hydrator: this existing synchronous method consumes supplied metadata
only. The repository must already have hydrated its persisted public evidence
(ResearchRepository does this at initialization). Existing cache writes are
preserved; this operation does not persist ranking snapshots.

Use a fixed timezone-aware as_of to reproduce evidence selection/fingerprints.
generated_at is the completion clock; cache_hit diagnostics may change on a warm
run, but ranking content does not. shortlist_limit bounds deep investigation
and is never a recommendation display cap. When run() is
invoked with top_n=None -- the production Radar V2 path -- EVERY rank-eligible
investigated entry is exposed in ranking.top_n, in global rank order, with no
numeric cap. The legacy integer top_n (a display/legacy metadata knob at the
OpportunityCycleRequest layer) only truncates ranking.top_n to its first N
rank-eligible entries when an explicit bounded value is requested; it never
truncates publication, which iterates ranking.evaluated_entries.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping
from uuid import UUID
from pydantic import Field

from decimal import Decimal, InvalidOperation

from app import cycle_timing
from app.global_scanner import GlobalScanCandidate, GlobalScanner, StageBCandidate
from app.cycle_checkpoint import (CandidateState, CycleOwnershipLost, PHASE_BASELINE, PHASE_DEEP,
                                  state_for_disposition)
from app.failure_taxonomy import TECHNICAL_RETRYABLE, blocking_failures, classify_requirement_failures
from app.global_opportunity_ranker import GlobalOpportunityRanker
from app.models import ResearchBaseModel
from app.portfolio_orchestration import _public_analyst_from_record
from app.research_readiness_runtime import (
    BASELINE_REQUIREMENT_IDS,
    RepositoryResearchReadinessAdapter,
    ResearchReadinessRuntime,
    jurisdiction_for_profile,
)
from app.sector_relative_strength import SectorContext
from app.stock_rule_engine import StockRuleEngineResult, StockRuleEngineService
from app.news_intelligence import EventImpactFeature, latest_known_features

logger = logging.getLogger(__name__)
DEFAULT_DEEP_LIMIT = 25
ACQUISITION_SELECTION_VERSION = "BOUNDED_ACQUISITION_V1"


@dataclass(frozen=True)
class _BaselineOutcome:
    # The caller needs only acquisition disposition, never the readiness graph.
    planned_requirement_ids: tuple[str, ...]
    ready: bool = False
    attempted: bool = False
    timed_out: bool = False
    failed: bool = False

    @classmethod
    def from_result(cls, result):
        failures = getattr(result, "failures", {}) or {}
        readiness = getattr(result, "readiness", None)
        ready = callable(getattr(readiness, "for_requirement", None)) and all(
            (row := readiness.for_requirement(key)) is not None
            and row.status in {"READY_FRESH", "NOT_APPLICABLE"}
            for key in BASELINE_REQUIREMENT_IDS)
        return cls(tuple(result.planned_requirement_ids), bool(ready and not failures),
                   bool(getattr(result, "executed_capabilities", ())),
                   any("TIMEOUT" in str(reason).upper() for reason in failures.values()), bool(failures))


def _baseline_jurisdiction(profile) -> str:
    """jurisdiction_for_profile(), widened to accept an EtfResearchProfile.

    EtfResearchProfile has no ``country`` field (unlike CompanyResearchProfile),
    so an ETF profile's jurisdiction is derived from ``exchange`` alone; this is
    the exact same exchange-based branch jurisdiction_for_profile already uses,
    just reached via getattr instead of a required attribute access. Behaviour
    for a CompanyResearchProfile is unchanged -- getattr returns its real
    ``country`` value.
    """
    country = (getattr(profile, "country", "") or "").strip().upper()
    exchange = profile.exchange.strip().upper()
    if country in {"IN", "IND", "INDIA"} or exchange in {"NSE", "XNSE", "BSE", "XBOM"}:
        return "INDIA"
    if country in {"US", "USA", "UNITED STATES"}:
        return "USA"
    if country in {
        "AT", "BE", "CH", "CZ", "DE", "DK", "ES", "EU", "FI", "FR", "GB", "IE",
        "IT", "LU", "NL", "NO", "PL", "PT", "SE", "UK",
    }:
        return "EUROPE"
    return "GLOBAL"


class OpportunityEntry(ResearchBaseModel):
    rank: int = 0
    global_instrument_id: UUID = None
    symbol: str | None = None
    company_name: str | None = None
    sector: str | None = None
    market: str | None = None
    pre_score: float | None = None
    stage_b_score: float | None = None
    technical_score: float | None = None
    technical_state: str = ""
    sector_score: float | None = None
    sector_state: str = ""
    rule_engine_score: float | None = None
    rule_engine_confidence: float = 0.0
    opportunity_score: float | None = None
    opportunity_confidence: float = 0.0
    score_coverage: float = 0.0
    top_positive_reasons: list[str] = Field(default_factory=list)
    top_negative_reasons: list[str] = Field(default_factory=list)
    rule_engine_version: str = ""
    technical_feature_version: str = ""
    sector_feature_version: str = ""
    opportunity_ranker_version: str = ""
    evidence_state: dict = Field(default_factory=dict)
    rank_eligible: bool = True
    suppression_reasons: list[str] = Field(default_factory=list)


class CandidateDiagnostic(ResearchBaseModel):
    global_instrument_id: UUID
    status: str
    failure_reason: str | None = None
    cache_hit: bool | None = None
    rank_eligible: bool = False
    suppression_reasons: list[str] = Field(default_factory=list)
    # Explicit, deterministic Stage-2 disposition (DI-10B). Every shortlisted
    # candidate ends Stage 2 with exactly one of: ANALYZED, RANK_FILTERED,
    # DEEP_READINESS_NOT_MET, DEEP_ACQUISITION_TIMEOUT, DEEP_SOURCE_UNAVAILABLE,
    # RULE_ENGINE_EXCEPTION, STAGE2_INTERNAL_ERROR, or a known pre-ensure
    # failure reason (e.g. PROFILE_HYDRATION_FAILED). None for diagnostics
    # produced by code paths this task did not touch (baseline-only, non-
    # acquisition Stage-2, prior-recommendation review). Additive-only: the
    # existing `status`/`failure_reason` vocabulary is unchanged for backward
    # compatibility.
    disposition: str | None = None
    # Slice 4 (additive): TECHNICAL_RETRYABLE | EVIDENCE_UNAVAILABLE for a
    # readiness shortfall, from the DI-20H.4 per-requirement reasons of the
    # BLOCKING requirements (preserved verbatim in acquisition_failures). A
    # technical failure is repaired within a bounded budget and is never a
    # business/investment rejection.
    failure_class: str | None = None
    acquisition_failures: dict[str, str] = Field(default_factory=dict)


class OpportunityRanking(ResearchBaseModel):
    candidate_nominations: dict[str, list[dict]] = Field(default_factory=dict)
    investigation_matrix: dict[str, dict] = Field(default_factory=dict)
    rotation_after: str | None = None
    generated_at: datetime
    as_of: datetime
    universe_count: int
    phase1_eligible_count: int
    stage_b_count: int
    shortlist_count: int
    deep_evaluated_count: int
    rank_eligible_count: int
    baseline_ready_count: int = 0
    baseline_acquisition_needed_count: int = 0
    baseline_incomplete_count: int = 0
    baseline_evaluated_count: int = 0
    acquisition_admitted_count: int = 0
    acquisition_deferred_count: int = 0
    baseline_acquisition_attempted_count: int = 0
    baseline_acquisition_timeout_count: int = 0
    baseline_acquisition_failed_count: int = 0
    preliminary_eligible_count: int = 0
    deep_pool_eligible_count: int = 0
    deep_candidate_count: int = 0
    # DI-10B: accurate Stage-2 (acquisition-enabled shortlist loop) counters.
    # deep_evaluated_count above is PRESERVED with its existing (pre-DI-10B)
    # externally-consumed meaning -- see deep_evaluated_count's own comment at
    # its assignment site in run(). These new counters are additive and give
    # the true breakdown of what happened to each shortlisted candidate.
    deep_attempted_count: int = 0
    deep_ready_count: int = 0
    deep_readiness_failed_count: int = 0
    deep_technical_failure_count: int = 0
    deep_repair_attempted_count: int = 0
    deep_repair_recovered_count: int = 0
    stage2_concurrency: int = 1
    stage2_peak_inflight: int = 0
    deep_acquisition_timeout_count: int = 0
    deep_source_unavailable_count: int = 0
    rule_analyzed_count: int = 0
    rule_exception_count: int = 0
    stage2_internal_error_count: int = 0
    evaluated_count: int = 0
    suppressed_count: int = 0
    # RADAR V2: in production run() is called with top_n=None (unbounded), so this
    # list holds ALL rank-eligible investigated entries in global rank order -- no
    # 4/25/100 cap. A bounded integer top_n is a legacy/test knob only; when
    # provided, ranking.top_n is truncated to that many rank-eligible entries.
    # Publication never reads this truncated tail -- it iterates
    # ranking.evaluated_entries (all analyzed candidates) -- so this field exists
    # for diagnostics/ordering and engine_version selection, not for truncation.
    top_n: list[OpportunityEntry]
    diagnostics: list[CandidateDiagnostic]
    evaluated_entries: list[OpportunityEntry] = Field(default_factory=list)
    review_evidence: dict[str, dict] = Field(default_factory=dict)


class _PersistedUniverse:
    """The scanner's existing universe protocol, backed solely by supplied rows."""
    def __init__(self, rows):
        self.rows = rows

    async def active_global_equities(self, **unused):
        return self.rows


class GlobalOpportunityOrchestrator:
    _DEEP_POOL_PERCENTILE: float = 0.10
    _DEEP_POOL_SCORE_MARGIN: float = 15.0
    _DEEP_POOL_MIN_SCORE: float = 50.0
    _PROMISING_CONFIDENCE_THRESHOLD: float = 50.0
    _BASELINE_CONCURRENCY: int = 8

    # Fields already computed by TechnicalFeatureEngine.compute() (see
    # app/technical_features.py) that RecommendationEngineV1.ranges() and other
    # evidence_state consumers need but were previously dropped: only
    # latest_price/history_end/feature_version ever reached evidence_state,
    # so ranges() could never see support_level/resistance_level/atr14 even
    # though it already reads them via technical.get(...). No field here is
    # recomputed -- every value is forwarded verbatim from the canonical
    # TechnicalFeatureSnapshot the scanner already built for this candidate.
    # DI-10B: known, deliberately-raised pre-ensure Stage-2 failure reasons
    # (identity/hydration problems, not unexpected internal errors). Their
    # existing failure_reason string IS the disposition -- never reclassified
    # as STAGE2_INTERNAL_ERROR.
    _KNOWN_STAGE2_FAILURE_REASONS = frozenset({
        "PROFILE_HYDRATION_FAILED",
        "PROFILE_IDENTITY_MISMATCH",
        "PROFILE_MISSING_AFTER_HYDRATION",
    })

    _TECHNICAL_EVIDENCE_FIELDS: tuple = (
        'support_level', 'resistance_level', 'distance_to_support_pct', 'distance_to_resistance_pct',
        'atr14', 'atr_pct', 'rsi14', 'macd', 'macd_signal', 'macd_histogram', 'adx14',
        'technical_state', 'breakout_state',
        'current_volume', 'volume_average20', 'volume_ratio20', 'volume_state',
        'volume_expansion', 'volume_contraction', 'breakout_volume_confirmed', 'reversal_volume_confirmed',
        'dma20', 'dma50', 'dma100', 'dma200',
        'distance_to_dma20_pct', 'distance_to_dma50_pct', 'distance_to_dma100_pct', 'distance_to_dma200_pct',
        'return1_w', 'return1_m', 'return3_m', 'return6_m', 'return1_y',
        'trend_slope20', 'trend_slope50',
        'distance_from52_week_high_pct', 'distance_from52_week_low_pct',
        'higher_highs_higher_lows', 'lower_highs_lower_lows',
        'latest_swing_high', 'previous_swing_high', 'latest_swing_low', 'previous_swing_low',
        'swing_higher_high', 'swing_lower_high', 'swing_higher_low', 'swing_lower_low',
        'market_structure_state',
        'technical_score', 'confidence', 'history_readiness',
        'missing_inputs', 'stale_inputs',
    )

    def __init__(self, repository, persistence, *, profile_hydrator: Callable[[UUID, dict], bool],
                 readiness_runtime=None, clock=None):
        self.repository = repository
        self.persistence = persistence
        self.profile_hydrator = profile_hydrator
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.acquisition_enabled = readiness_runtime is not None
        self.readiness_adapter = readiness_runtime.data_source if readiness_runtime is not None else RepositoryResearchReadinessAdapter(repository)
        self.readiness = readiness_runtime if readiness_runtime is not None else ResearchReadinessRuntime(repository, self.readiness_adapter, executor=None)
        self.rule_engine = StockRuleEngineService(repository, self.readiness_adapter)
        self.ranker = GlobalOpportunityRanker()

    async def _run_blocking(self, operation, *args, **kwargs):
        """Offload a synchronous, potentially slow scanner/persistence call off
        the request event loop, reusing ResearchRepository's existing bounded
        executor boundary (_run_blocking_persistence) rather than adding new
        threading/locking logic. self.persistence is repository.persistence --
        the same object repository._persistence_worker_lock serializes access
        to -- so this is safe for both the SQLite (kept inline) and Postgres
        (offloaded to a worker thread) backends. Falls back to inline execution
        if repository does not expose that boundary (e.g. a minimal test double).
        """
        run_blocking = getattr(self.repository, "_run_blocking_persistence", None)
        if run_blocking is not None:
            return await run_blocking(operation, *args, **kwargs)
        return operation(*args, **kwargs)

    async def _acquire_baseline_requirements(self, candidates, metadata, as_of, identity_headers, checkpoint=None):
        results = {}
        failures = {}
        if not candidates:
            return results, failures
        max_workers = self._BASELINE_CONCURRENCY
        total = len(candidates)
        completed = 0
        failed = 0
        logger.info(
            "baseline_acquisition_start: eligible=%d concurrency=%d",
            total, max_workers,
        )

        async def _acquire_one(candidate):
            # Durable per-instrument checkpoint (resumable production cycles):
            # a baseline outcome recorded by a previous attempt of this SAME
            # cycle is restored without any acquisition.
            key = candidate.global_instrument_id
            if checkpoint is None:
                return await _acquire_one_body(candidate)
            if await self._run_blocking(checkpoint.persistence.cycle_cancellation_requested, checkpoint.cycle_id):
                raise asyncio.CancelledError("CYCLE_CANCELLED")
            restored = checkpoint.restorable(PHASE_BASELINE, key)
            if restored is not None:
                stored = restored.get("payload") or {}
                if "ready" in stored or restored["state"] != CandidateState.COMPLETED:
                    checkpoint.note_restored(PHASE_BASELINE)
                    if stored.get("has_outcome"):
                        return key, _BaselineOutcome(
                            tuple(stored.get("planned_requirement_ids") or ()),
                            stored.get("ready", False), stored.get("attempted", False),
                            stored.get("timed_out", False), stored.get("failed", False)), None
                    return key, None, restored.get("failure_reason") or "BASELINE_ACQUISITION_FAILED"
                # Legacy COMPLETED meant only that ensure returned. Reassess
                # admitted candidates instead of restoring fabricated readiness.
            await checkpoint.start(PHASE_BASELINE, key)
            outcome = await _acquire_one_body(candidate)
            _, result, reason = outcome
            if result is not None:
                await checkpoint.finish(PHASE_BASELINE, key,
                    CandidateState.COMPLETED if result.ready else CandidateState.RETRYABLE_FAILURE,
                    disposition="BASELINE_READY" if result.ready else "BASELINE_INCOMPLETE",
                    payload={"has_outcome": True, "planned_requirement_ids": list(result.planned_requirement_ids),
                             "ready": result.ready, "attempted": result.attempted,
                             "timed_out": result.timed_out, "failed": result.failed})
            else:
                await checkpoint.finish(PHASE_BASELINE, key, state_for_disposition(reason),
                                        disposition=reason, failure_reason=reason)
            return outcome

        async def _acquire_one_body(candidate):
            key = candidate.global_instrument_id
            # Canonical pre-ensure sequence (mirrors Phase 3 enrichment at
            # lines ~469-486): hydrate a process-local profile from canonical
            # metadata, verify identity, remember the metadata, then derive the
            # real jurisdiction.  ResearchReadinessRuntime.ensure REQUIRES a
            # non-None jurisdiction string -- passing jurisdiction=None is the
            # live failure that produced shortlist_count=0 across the 2585-wide
            # universe.  Phase 1 acquisition is bounded to BASELINE_REQUIREMENT_IDS
            # only; deep acquisition (requirement_ids=None) is deferred to Phase 3.
            # Concurrency is bounded by the enclosing worker-pool below (see run
            # loop), so no per-candidate semaphore is materialized and we never
            # create one coroutine object per candidate up front -- this is what
            # kept full-market cycles OOM-free under tight memory limits while
            # NSE PDF bytes accumulated concurrently.
            try:
                payload = dict(metadata.get(str(key), {}))
                payload.setdefault("primaryExchange", payload.get("exchange"))
                payload.setdefault("primarySymbol", payload.get("ticker"))
                if not self.profile_hydrator(key, payload):
                    return key, None, "PROFILE_HYDRATION_FAILED"
                # register_global_profile_metadata hydrates either a
                # CompanyResearchProfile (repository.profiles) or an
                # EtfResearchProfile (repository.etf_profiles) -- ETFs are
                # routed exclusively into the latter store. Resolve whichever
                # store actually holds this instrument instead of assuming
                # CompanyResearchProfile storage. A hydration success that
                # lands in neither store is a genuine, retryable anomaly --
                # not an identity mismatch and not swallowed.
                try:
                    profile = self.repository.profile(key)
                except StopIteration:
                    try:
                        profile = self.repository.etf_profile(key)
                    except StopIteration:
                        return key, None, "PROFILE_MISSING_AFTER_HYDRATION"
                if profile.instrument_id != key:
                    return key, None, "PROFILE_IDENTITY_MISMATCH"
                self.readiness_adapter.remember_canonical_metadata(key, payload)
                result = await self.readiness.ensure(
                    key,
                    jurisdiction=_baseline_jurisdiction(profile),
                    requirement_ids=BASELINE_REQUIREMENT_IDS,
                    identity_headers=identity_headers,
                )
                return key, (_BaselineOutcome.from_result(result)
                             if result is not None else None), None
            except Exception as exc:
                # readiness/ensure exception (and any other unexpected failure)
                # is the generic baseline-acquisition failure.  Provider/private
                # exception text is never exposed: only the label is retained.
                logger.warning("baseline_acquisition_exception instrument_id=%s exception=%s",
                               key, type(exc).__name__)
                return key, None, "BASELINE_ACQUISITION_FAILED"

        # Bounded worker-pool: seed at most max_workers candidate coroutines, and
        # only create the next one once a slot frees. Peak in-flight acquisition
        # state is therefore O(max_workers) -- not O(N) -- while every candidate is
        # still processed and per-candidate failures stay isolated. Candidate
        # ordering is deterministic (seeded in scan order); results are keyed by
        # global_instrument_id so completion order cannot change their contracts.
        pending: set = set()
        candidates_iter = iter(candidates)
        for _ in range(min(max_workers, total)):
            pending.add(asyncio.ensure_future(_acquire_one(next(candidates_iter))))
        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    completed += 1
                    try:
                        outcome = task.result()
                    except CycleOwnershipLost:
                        raise
                    except Exception:
                        # Unexpected exception escaping _acquire_one's own guard;
                        # isolate per candidate (matches the historical gather
                        # with return_exceptions=True, which skipped such
                        # outcomes) so one candidate never aborts the pass.
                        failed += 1
                        logger.warning(
                            "baseline_acquisition_unexpected: completed=%d/%d failed=%d",
                            completed, total, failed,
                        )
                        continue
                    key, result, reason = outcome
                    results[key] = result
                    if reason is not None:
                        failures[key] = reason
                        failed += 1
                        logger.warning("baseline_acquisition_failed instrument_id=%s reason=%s", key, reason)
                # Refill only the slots that just freed, never exceeding max_workers.
                for _ in range(min(len(done), max_workers - len(pending))):
                    try:
                        pending.add(asyncio.ensure_future(_acquire_one(next(candidates_iter))))
                    except StopIteration:
                        break
                if completed % 50 == 0 or completed >= total:
                    logger.info(
                        "baseline_acquisition_progress: completed=%d/%d failed=%d active=%d/%d",
                        completed, total, failed, len(pending), max_workers,
                    )
                    if callable(getattr(type(self.readiness), 'retention_stats', None)):
                        logger.info("baseline_acquisition_retention completed=%d compactOutcomes=%d activeCandidates=%d runtime=%s",
                                    completed, len(results), len(pending), self.readiness.retention_stats())
        except BaseException:
            # On shutdown/cancellation (or lost cycle ownership), cancel in-flight
            # workers and drain them so no acquisition task is orphaned and no
            # provider call is left hanging.
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            raise
        logger.info(
            "baseline_acquisition_complete: eligible=%d completed=%d failed=%d",
            total, completed, failed,
        )
        return results, failures

    _RESTORE_INCREMENTS = {
        "ANALYZED": {"deep_ready": 1, "rule_analyzed": 1},
        "RANK_FILTERED": {"deep_ready": 1, "rule_analyzed": 1},
        "DEEP_READINESS_NOT_MET": {"deep_readiness_failed": 1},
        "DEEP_ACQUISITION_TIMEOUT": {"deep_acquisition_timeout": 1},
        "DEEP_SOURCE_UNAVAILABLE": {"deep_source_unavailable": 1},
        "DEEP_TECHNICAL_FAILURE": {"deep_readiness_failed": 1},
        "RULE_ENGINE_EXCEPTION": {"deep_ready": 1, "rule_exception": 1},
        "STAGE2_INTERNAL_ERROR": {"stage2_internal_error": 1},
    }

    @staticmethod
    def _incomplete_analysis(result, readiness):
        """Return the blocking requirement ids when a Rule Engine result is not
        a fully analyzed result, else None. Only a full, non-partial result
        with every applicable area scorable may reach ranking (RANK_FILTERED is
        reserved for fully analyzed candidates that fail a real ranking gate)."""
        # CURRENT_NEWS is optional contextual research: NEWS_GEOPOLITICAL_EVENTS
        # is unconditionally exempt here, in every state (not only a technical
        # /provider search failure), so it can never by itself keep a
        # fundamental full result out of ranking. Any other applicable
        # UNSCORABLE area still completes the result incompletely.
        unscorable = {str(area.area) for area in result.area_scores
                      if area.applicable
                      and str(area.status) == "UNSCORABLE"
                      and str(area.area) != "NEWS_GEOPOLITICAL_EVENTS"}
        if result.eligibility.full_analysis_allowed and not result.partial and not unscorable:
            return None
        by_area = {str(item.rule_engine_area): item.requirement_id
                   for item in getattr(readiness, "requirements", ()) or ()}
        blocking = set(result.eligibility.blocking_requirements)
        blocking |= {by_area.get(area, area) for area in unscorable}
        return sorted(blocking) or ["RULE_ENGINE_RESULT_NOT_FULL"]

    def _unscorable_failures(self, key, blocking, cycle_failures):
        """Classify why readiness was full but the rules could not score.

        Ready evidence with no supported score is a contract/input gap, not
        proof of authoritative absence. Preserve acquisition diagnostics, but
        keep the disagreement retryable even when a prior check was empty or
        a refresh was not due. The mandatory scoring gate remains closed."""
        from app.deep_investigation import _prior_failure
        failures = blocking_failures(cycle_failures, blocking)
        for requirement_id in blocking:
            prior = failures.get(requirement_id)
            if not prior and self.readiness is not None:
                prior = _prior_failure(self.readiness, key, requirement_id)
            failures[requirement_id] = "|".join(dict.fromkeys(
                [part for part in str(prior or "").split("|") if part] + ["RULE_AREA_UNSCORABLE"]
            ))
        return failures

    async def _checkpoint_deep_outcome(self, checkpoint, key, diagnostics, rules, by_id, investigation_matrix):
        diagnostic = next((d for d in reversed(diagnostics) if d.global_instrument_id == key), None)
        disposition = diagnostic.disposition if diagnostic is not None else "STAGE2_INTERNAL_ERROR"
        enriched = by_id.get(key)
        payload = {
            "diagnostic": diagnostic.model_dump(mode="json") if diagnostic is not None else None,
            "rule": rules[key].model_dump(mode="json") if key in rules else None,
            "enriched": enriched.model_dump(mode="json") if enriched is not None else None,
            "enriched_type": type(enriched).__name__ if enriched is not None else None,
            "matrix": investigation_matrix.get(str(key)),
        }
        state = state_for_disposition(disposition)
        if disposition == "DEEP_READINESS_NOT_MET" and diagnostic is not None and diagnostic.failure_class == TECHNICAL_RETRYABLE:
            # A technical failure is never recorded as unavailable evidence.
            state = CandidateState.RETRYABLE_FAILURE
        await checkpoint.finish(PHASE_DEEP, key, state, disposition=disposition,
                                failure_reason=diagnostic.failure_reason if diagnostic is not None else None,
                                payload=payload)

    def _restore_deep_candidate(self, row, key, diagnostics, rules, by_id, stage_b, successful, investigation_matrix):
        """Rebuild a candidate's outcome from its durable checkpoint without
        acquisition or re-evaluation. Returns counter increments, or None when
        the payload cannot be restored (the candidate is then re-run)."""
        payload = row.get("payload") or {}
        try:
            diagnostic = CandidateDiagnostic.model_validate(payload["diagnostic"])
            rule = StockRuleEngineResult.model_validate(payload["rule"]) if payload.get("rule") else None
            enriched = None
            if payload.get("enriched"):
                model = StageBCandidate if payload.get("enriched_type") == "StageBCandidate" else GlobalScanCandidate
                enriched = model.model_validate(payload["enriched"])
        except Exception:
            logger.warning("cycle_checkpoint_restore_failed instrument_id=%s phase=DEEP", key)
            return None
        diagnostics.append(diagnostic)
        if rule is not None and enriched is not None:
            rules[key] = rule
            by_id[key] = enriched
            stage_b.append(enriched)
            if diagnostic.rank_eligible:
                successful.append(enriched)
        if payload.get("matrix") is not None:
            investigation_matrix[str(key)] = payload["matrix"]
        logger.info("stage2_candidate_restored instrument_id=%s disposition=%s", key, diagnostic.disposition)
        increments = dict(self._RESTORE_INCREMENTS.get(diagnostic.disposition or "", {}))
        if diagnostic.disposition == "DEEP_READINESS_NOT_MET" and diagnostic.failure_class == TECHNICAL_RETRYABLE:
            increments["deep_technical_failure"] = 1
        return increments

    def _form_dynamic_deep_pool(self, candidates, exclude_ids=None):
        if not candidates:
            return []
        exclude_ids = set(exclude_ids or [])
        best_score = candidates[0].pre_score or 0.0
        selected_ids: set = set()
        for c in candidates[:max(1, math.ceil(len(candidates) * self._DEEP_POOL_PERCENTILE))]:
            selected_ids.add(c.global_instrument_id)
        for c in candidates:
            if c.pre_score is not None and best_score - c.pre_score <= self._DEEP_POOL_SCORE_MARGIN:
                selected_ids.add(c.global_instrument_id)
        for c in candidates:
            if c.pre_score is not None and c.pre_score >= self._DEEP_POOL_MIN_SCORE:
                selected_ids.add(c.global_instrument_id)
        for c in candidates:
            if not c.eligible_for_deep_analysis and c.confidence >= self._PROMISING_CONFIDENCE_THRESHOLD:
                selected_ids.add(c.global_instrument_id)
        filtered = [c for c in candidates if c.global_instrument_id in selected_ids and c.global_instrument_id not in exclude_ids]
        return sorted(
            filtered,
            key=lambda c: (
                -(c.pre_score if c.pre_score is not None else -1),
                -c.confidence,
                -c.critical_completeness,
                str(c.global_instrument_id),
            ),
        )

    def _rule_evidence(self, rule):
        """Serialize a rule-engine result into the evidence_state['rule'] contract
        consumed by snapshot_from_entry and the recommendation engine."""
        if rule is None:
            return {}
        def _scalar(v):
            return v.value if hasattr(v, "value") else v
        return {
            'rule_engine_version': getattr(rule, 'rule_engine_version', ''),
            'overall_score': getattr(rule, 'overall_score', None),
            'quality_score': getattr(rule, 'quality_score', None),
            'opportunity_score': getattr(rule, 'opportunity_score', None),
            'risk_score': getattr(rule, 'risk_score', None),
            'confidence_score': getattr(rule, 'confidence_score', None),
            'decision_signal': _scalar(getattr(rule, 'decision_signal', None)),
            'partial': getattr(rule, 'partial', None),
            'area_scores': [
                {'area': _scalar(a.area), 'raw_score': a.raw_score,
                 'weighted_contribution': getattr(a, 'weighted_contribution', None),
                 'status': _scalar(getattr(a, 'status', None)),
                 'applicable': getattr(a, 'applicable', None)}
                for a in getattr(rule, 'area_scores', []) or []
            ],
            'missing_inputs': list(getattr(rule, 'missing_inputs', []) or []),
            'stale_inputs': list(getattr(rule, 'stale_inputs', []) or []),
        }

    def _analyst_valuation_evidence(self, public_analyst, instrument_currency):
        """Map real, already-persisted analyst target-price consensus onto
        evidence_state.valuation -- the only source RecommendationEngineV1.ranges()
        reads for the long-term fair_value/bull_target price levels. No new
        valuation formula and no score-to-price conversion: an ineligible or
        absent analyst record simply leaves valuation empty, exactly as before
        this wiring existed.

        Eligibility (all required, the same standard materialize_valuation
        applies to its own price-ratio bases): a real persisted PublicAnalyst
        record; a finite, positive target price; the analyst record's currency
        matches the instrument's actual trading currency; freshness == 'FRESH'
        per the existing structured-market analyst freshness window
        (Settings.structured_analyst_freshness_seconds, via
        _public_analyst_from_record). analyst_count is carried in provenance
        only -- it never gates eligibility, and target_low_price is deliberately
        never mapped to `invalidation`: the existing range contract does not
        define that meaning, and inventing one here would be fabrication.
        """
        if public_analyst is None or not instrument_currency:
            return {}
        if public_analyst.currency != instrument_currency:
            return {}
        if public_analyst.freshness != 'FRESH':
            return {}

        def _eligible(value):
            if value is None:
                return None
            try:
                number = Decimal(str(value))
            except (InvalidOperation, TypeError):
                return None
            return float(number) if number.is_finite() and number > 0 else None

        fair_value = _eligible(public_analyst.target_median_price)
        bull_target = _eligible(public_analyst.target_high_price)
        if fair_value is None and bull_target is None:
            return {}

        result = {}
        fields_used = []
        if fair_value is not None:
            result['fair_value'] = fair_value
            fields_used.append('target_median_price')
        if bull_target is not None:
            result['bull_target'] = bull_target
            fields_used.append('target_high_price')
        result['provenance'] = {
            'provider': public_analyst.provider,
            'source_name': public_analyst.source_name,
            'source_url': public_analyst.source_url,
            'as_of': public_analyst.as_of.isoformat() if public_analyst.as_of else None,
            'retrieved_at': public_analyst.retrieved_at.isoformat() if public_analyst.retrieved_at else None,
            'freshness': public_analyst.freshness,
            'analyst_count': public_analyst.analyst_count,
            'fields_used': fields_used,
        }
        return result

    def _technical_evidence(self, snapshot):
        """Forward the canonical TechnicalFeatureSnapshot verbatim -- no
        recomputation. A value already None on the snapshot (missing, stale,
        or insufficient history per TechnicalFeatureEngine's own readiness
        gating) stays None here; missing_inputs/stale_inputs/history_readiness
        are carried through so a consumer can tell why, not just that.

        history_end is a raw datetime on TechnicalFeatureSnapshot. Every
        normal (evaluated-entry) snapshot reaches persistence via
        OpportunityRankingEntry.model_dump(mode="json"), which converts it to
        an ISO-8601 string -- but the "previous_states review" / price-lifecycle
        path in global_opportunity_cycle.py reads this dict (via
        OpportunityRanking.review_evidence) by plain attribute access and never
        calls model_dump, so a raw datetime here reaches
        json.dumps(..., allow_nan=False) at final persistence unconverted and
        raises "TypeError: Object of type datetime is not JSON serializable".
        Isoformat it at the source, same as the analyst provenance block
        below, so every consumer of this dict gets the same canonical
        JSON-safe representation regardless of which path it took.
        """
        history_end = getattr(snapshot, 'history_end', None)
        technical = {
            'latest_price': snapshot.latest_price,
            'history_end': history_end.isoformat() if history_end is not None else None,
            'feature_version': getattr(snapshot, 'feature_version', ''),
            'currency': getattr(snapshot, 'currency', None),
        }
        for field in self._TECHNICAL_EVIDENCE_FIELDS:
            value = getattr(snapshot, field, None)
            # current_volume is int | Decimal | None on the snapshot; ranges()'s
            # number() helper only accepts int/float, matching the same
            # Decimal-to-float normalization already applied to analyst evidence.
            technical[field] = float(value) if isinstance(value, Decimal) else value
        return technical

    def _evidence_state(self, enriched, rule, public_analyst=None):
        try:
            return {
                'technical': self._technical_evidence(enriched.technical_feature_snapshot),
                'sector': {
                    'sector_score': enriched.sector_relative_strength_snapshot.relative_strength_score,
                    'sector_state': enriched.sector_relative_strength_snapshot.sector_state,
                },
                'valuation': self._analyst_valuation_evidence(
                    public_analyst, getattr(enriched.technical_feature_snapshot, 'currency', None)),
                'rule': self._rule_evidence(rule),
            }
        except Exception:
            return {'technical': {}, 'sector': {}, 'valuation': {}, 'rule': self._rule_evidence(rule)}

    async def run(self, canonical_instruments: Iterable[dict], *, as_of: datetime,
                  sector_contexts: Mapping[UUID, SectorContext] | None = None,
                  shortlist_limit: int | None = DEFAULT_DEEP_LIMIT, top_n: int | None = 10,
                  review_ids: Iterable[UUID] = (), identity_headers=None,
                  correlation_id=None, discovery_v2=False, rotation_after=None,
                  checkpoint=None) -> OpportunityRanking:
        # Production supplies the requested deep limit independently of top_n.
        # Explicit diagnostic callers may still opt into an unbounded pool.
        if (not (shortlist_limit is None
                 or (type(shortlist_limit) is int and 1 <= shortlist_limit <= 100))
                or (top_n is not None and (type(top_n) is not int or not 0 <= top_n <= 100))):
            raise ValueError("INVALID_OPPORTUNITY_LIMIT")
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("AWARE_AS_OF_REQUIRED")
        as_of = as_of.astimezone(timezone.utc)
        rows = deepcopy(list(canonical_instruments))
        for row in rows:
            row.setdefault("exchange", row.get("primaryExchange"))
            row.setdefault("ticker", row.get("primarySymbol"))
        # Each offloaded scan loads one instrument's complete evidence at a time.
        # This bounds history/model hydration without truncating evidence or scores.
        scanner = GlobalScanner(_PersistedUniverse(rows), self.persistence, batch_size=1,
                                run_blocking=self._run_blocking)
        scan = await scanner.scan(as_of=as_of, top_n=0)
        phase1 = {c.global_instrument_id:c for c in scan.candidates if c.eligible_for_deep_analysis}
        metadata = {str(row.get("globalInstrumentId")): row for row in rows}

        # Diagnostics counters
        baseline_ready_count = 0
        baseline_acquisition_needed_count = 0
        baseline_incomplete_count = 0
        baseline_evaluated_count = sum(c.eligible_for_acquisition for c in scan.candidates)
        baseline_acquisition_attempted_count = 0
        baseline_acquisition_timeout_count = 0
        baseline_acquisition_failed_count = 0
        acquisition_admitted_count = 0
        acquisition_deferred_count = 0
        preliminary_eligible_count = 0
        deep_pool_eligible_count = 0
        deep_candidate_count = 0
        deep_attempted_count = 0
        deep_durable_completed_count = 0
        deep_ready_count = 0
        deep_readiness_failed_count = 0
        deep_acquisition_timeout_count = 0
        deep_source_unavailable_count = 0
        rule_analyzed_count = 0
        rule_exception_count = 0
        stage2_internal_error_count = 0
        deep_technical_failure_count = 0
        stage2_peak_inflight = 0
        stage2_concurrency = 1
        deep_repair_attempted_count = 0
        deep_repair_recovered_count = 0
        shortlist = []
        nominations, investigation_matrix = {}, {}
        stage_b = []
        baseline_eligible_ids = set()
        deep_ids = set()
        ensure_results = {}
        baseline_failures = {}
        diagnostics = []
        rules = {}
        successful = []
        temporal_evidence = {}
        evaluation_at = as_of
        evaluated = 0
        # initial_by_id keeps the original GlobalScanCandidate (has missing_inputs, stale_inputs, etc.)
        initial_by_id = {c.global_instrument_id: c for c in scan.candidates}
        by_id = {}
        review_evidence = {}

        # Real analyst target-price consensus, already persisted by the existing
        # Yahoo-routed structured-market acquisition path (yahoo_mcp_acquisition.py's
        # VALUATION_INPUTS -> NSE_OR_APPROVED_STRUCTURED routing) -- no new provider
        # call and no new formula are introduced here. Projected one instrument
        # at a time for the candidate + review universe; only compact analyst
        # values survive, so every _evidence_state() call this cycle
        # (both the review-fallback path and the main deep-analysis path) sees
        # identical, already-loaded data. Reuses PortfolioResearchOrchestrator's own
        # provider-preference + freshness + provenance logic
        # (_public_analyst_from_record) so both pipelines agree on eligibility.
        analyst_candidate_ids = set(phase1.keys()) | set(review_ids)
        public_analyst_by_id: dict = {}
        try:
            analyst_now = self.clock()
            for instrument_id in sorted(analyst_candidate_ids, key=str):
                structured_records = await self.repository.structured_market_snapshots_for_instruments({instrument_id})
                choices = structured_records.get(instrument_id, [])
                record = (
                    next((item for item in choices if item.provider == "NSE_STRUCTURED"), None)
                    or next((item for item in choices if item.provider == "YAHOO_FINANCE"), None)
                    or (choices[0] if choices else None)
                )
                if record is not None:
                    public_analyst_by_id[instrument_id] = _public_analyst_from_record(
                        record, analyst_now, self.repository.settings)
                # Keep only the compact analyst projection across the universe.
                del structured_records, choices, record
        except Exception:
            public_analyst_by_id = {}

        if self.acquisition_enabled:
            # Stage A is the provider-free scan already completed above. Missing
            # evidence is a nomination input, never a reason to acquire the whole
            # universe. Rotation can admit sparse candidates independently of the
            # market pool; only admitted identities proceed to live acquisition.
            baseline_scan = scan
            deep_ready_candidates = [c for c in baseline_scan.candidates if c.eligible_for_deep_analysis]
            baseline_ready_candidates = deep_ready_candidates
            preliminary_eligible_count = len(deep_ready_candidates)

            # Reviews share the same admission boundary as discovery. Unselected
            # prior recommendations retain a persisted-only lifecycle review.
            # review_id_set is also the discovery/rotation exclusion set below:
            # an already-reviewed candidate must never consume a second, fresh
            # admission slot (and its re-acquisition budget) on top of its
            # existing persisted-only review -- that would silently shrink how
            # many genuinely new candidates this cycle's shortlist_limit admits.
            review_id_set = set(review_ids) if review_ids else set()
            pre_review_pool = self._form_dynamic_deep_pool(baseline_ready_candidates, exclude_ids=review_id_set)
            deep_pool_eligible_count = len(pre_review_pool)
            if shortlist_limit and len(pre_review_pool) > shortlist_limit:
                shortlist = pre_review_pool[:shortlist_limit]
            else:
                shortlist = pre_review_pool
            deep_candidates = shortlist
            # Resumable cycle: the deep-candidate selection is made once per
            # cycle and persisted, so a resumed cycle investigates exactly the
            # same ordered set (discovery rotation is not re-run).
            restored_selection = (await checkpoint.selection()) if checkpoint is not None else None
            selected_missing: list = []
            if restored_selection is not None:
                restored_selection = dict(restored_selection)
                restored_selection["deep_ids"] = list(dict.fromkeys(restored_selection.get("deep_ids", [])))[:shortlist_limit]
                by_scan = {c.global_instrument_id: c for c in baseline_scan.candidates if c.eligible_for_acquisition}
                deep_candidates = [by_scan[UUID(i)] for i in restored_selection.get("deep_ids", []) if UUID(i) in by_scan]
                selected_missing = [UUID(i) for i in restored_selection.get("deep_ids", []) if UUID(i) not in by_scan]
                shortlist = [by_scan[UUID(i)] for i in restored_selection.get("shortlist_ids", []) if UUID(i) in by_scan]
                nominations = restored_selection.get("nominations") or {}
                rotation_after = restored_selection.get("rotation_after", rotation_after)
            else:
                from app.opportunity_discovery import discover
                discover_budget = shortlist_limit if shortlist_limit else max(1, len(pre_review_pool))
                deep_candidates, nominations, rotation_after = await discover(
                    baseline_scan.candidates, pre_review_pool, budget=discover_budget,
                    as_of=evaluation_at, repository=self.repository, run_blocking=self._run_blocking,
                    rotation_after=rotation_after, exclude_ids=review_id_set,
                    full_universe=shortlist_limit is None)
                shortlist = deep_candidates[:shortlist_limit] if shortlist_limit else deep_candidates
            # Also bound checkpoints produced by the former unbounded policy.
            # Preserve their order; do not re-nominate on resume.
            if shortlist_limit is not None:
                if restored_selection is not None:
                    selected_ids = restored_selection.get("deep_ids", [])[:shortlist_limit]
                    selected_set = {UUID(key) for key in selected_ids}
                    deep_candidates = [c for c in deep_candidates if c.global_instrument_id in selected_set]
                    selected_missing = [key for key in selected_missing if key in selected_set]
                else:
                    deep_candidates = deep_candidates[:shortlist_limit]
                shortlist = list(deep_candidates)
            if checkpoint is not None:
                await checkpoint.save_selection({
                    "acquisition_selection_version": ACQUISITION_SELECTION_VERSION,
                    "deep_ids": (restored_selection["deep_ids"] if restored_selection is not None
                                 else [str(c.global_instrument_id) for c in deep_candidates]),
                    "shortlist_ids": [str(c.global_instrument_id) for c in shortlist],
                    "nominations": nominations, "rotation_after": rotation_after})
            for missing_key in selected_missing:
                stage2_internal_error_count += 1
                diagnostics.append(CandidateDiagnostic(
                    global_instrument_id=missing_key, status="SUPPRESSED",
                    failure_reason="SELECTED_CANDIDATE_NOT_IN_RESCAN", disposition="STAGE2_INTERNAL_ERROR"))
            if discovery_v2:
                for candidate in baseline_scan.candidates:
                    if not candidate.eligible_for_acquisition and candidate.global_instrument_id not in review_id_set:
                        diagnostics.append(CandidateDiagnostic(
                            global_instrument_id=candidate.global_instrument_id, status="SUPPRESSED",
                            rank_eligible=False, suppression_reasons=candidate.exclusion_reasons,
                            disposition="CANONICAL_INELIGIBLE"))
                        investigation_matrix[str(candidate.global_instrument_id)] = dict(
                            rule_evaluated=False, disposition="CANONICAL_INELIGIBLE")
            deep_candidate_count = len(deep_candidates)
            from app.deep_investigation import deferred_investigation
            deep_ids = {c.global_instrument_id for c in deep_candidates}
            for candidate in baseline_scan.candidates:
                if candidate.eligible_for_acquisition and candidate.global_instrument_id not in deep_ids:
                    diagnostics.append(CandidateDiagnostic(
                        global_instrument_id=candidate.global_instrument_id,
                        status="DEFERRED", disposition="DEFERRED"))
                    investigation_matrix[str(candidate.global_instrument_id)] = deferred_investigation()
            acquisition_admitted_count = len(deep_ids)
            acquisition_deferred_count = baseline_evaluated_count - acquisition_admitted_count
            logger.info("radar_acquisition_count operation=baseline_candidates_evaluated count=%d", baseline_evaluated_count)
            logger.info("radar_acquisition_count operation=acquisition_candidates_admitted count=%d", acquisition_admitted_count)
            logger.info("radar_acquisition_count operation=acquisition_candidates_deferred count=%d", acquisition_deferred_count)
            logger.info("radar_acquisition_count operation=deep_candidates_selected count=%d", deep_candidate_count)
            logger.info("stage2_selected correlationId=%s preliminary=%d pool=%d shortlist=%d",
                        correlation_id, preliminary_eligible_count, deep_pool_eligible_count, len(shortlist))

            # Stage B shares exactly the deep admission set; there is no second
            # limit or post-enrichment replacement queue that could fan out.
            baseline_eligible_ids = deep_ids
            ensure_results, baseline_failures = await self._acquire_baseline_requirements(
                deep_candidates, metadata, as_of, identity_headers, checkpoint=checkpoint)
            for key in deep_ids:
                outcome = ensure_results.get(key)
                baseline_ready_count += int(outcome is not None and outcome.ready)
                baseline_acquisition_needed_count += int(outcome is not None and bool(outcome.planned_requirement_ids))
                baseline_acquisition_attempted_count += int(outcome is not None and outcome.attempted)
                baseline_acquisition_timeout_count += int(outcome is not None and outcome.timed_out)
                baseline_acquisition_failed_count += int(outcome is None or outcome.failed)
            baseline_incomplete_count = acquisition_admitted_count - baseline_ready_count
            logger.info("baseline_acquisition_outcomes evaluated=%d admitted=%d deferred=%d needed=%d attempted=%d ready=%d incomplete=%d timed_out=%d failed=%d",
                        baseline_evaluated_count, acquisition_admitted_count, acquisition_deferred_count,
                        baseline_acquisition_needed_count, baseline_acquisition_attempted_count, baseline_ready_count,
                        baseline_incomplete_count, baseline_acquisition_timeout_count, baseline_acquisition_failed_count)

            # DI-16: Stage 1 -> Stage 2 transition must be observable so a
            # silent post-Stage-1 stall (stage2_start logged with no matching
            # stage2_complete) is diagnosable. Emitted immediately before the
            # Phase-2 re-scan, the first thing Stage 2 actually does.
            logger.info("stage2_start correlationId=%s admitted=%d", correlation_id, acquisition_admitted_count)

            # Refresh only admitted scanner observations. Preserve admission order
            # and membership even if fresh evidence changes their pre-scores.
            evaluation_at = max(as_of, self.clock())
            admitted_id_strings = {str(key) for key in deep_ids}
            admitted_scanner = GlobalScanner(
                _PersistedUniverse([row for row in rows if str(row.get("globalInstrumentId")) in admitted_id_strings]),
                self.persistence, batch_size=1, run_blocking=self._run_blocking)
            admitted_scan = await admitted_scanner.scan(as_of=evaluation_at, top_n=0, progress_label="admitted")
            refreshed_by_id = {c.global_instrument_id: c for c in admitted_scan.candidates}
            deep_candidates = [refreshed_by_id[c.global_instrument_id] for c in deep_candidates]
            shortlist = list(deep_candidates)

            # V2 enriches only the current ready stock below; never pre-enrich
            # the full investigation universe. Preserve the legacy diagnostic path.
            enriched_candidates = [] if discovery_v2 else await self._run_blocking(scanner.enrich_candidates,
                type('ScanResult', (), {'candidates': shortlist, 'as_of': evaluation_at})(),
                sector_contexts=dict(sector_contexts or {}))
            enriched_by_id = {c.global_instrument_id: c for c in enriched_candidates}
            logger.info("stage2_enriched correlationId=%s shortlist=%d", correlation_id, len(shortlist))

            repair_passes = max(0, int(getattr(self.repository.settings, "research_opportunity_repair_passes", 1)
                                       if getattr(self.repository, "settings", None) is not None else 1))
            max_attempts = checkpoint.max_attempts if checkpoint is not None else 1 + repair_passes
            local_attempts: dict = {}
            repairing: set = set()

            def _last_diagnostic(candidate_key):
                return next((d for d in reversed(diagnostics) if d.global_instrument_id == candidate_key), None)

            def _retryable(diagnostic):
                if diagnostic is None:
                    return False
                if diagnostic.disposition == "DEEP_READINESS_NOT_MET":
                    return diagnostic.failure_class == TECHNICAL_RETRYABLE
                return state_for_disposition(diagnostic.disposition) == CandidateState.RETRYABLE_FAILURE

            def _deep_schedule():
                # Main pass, then bounded repair passes over candidates whose
                # outcome is a technical (retryable) failure. The previous
                # outcome is undone first so accounting stays exact: one
                # diagnostic and one disposition count per candidate.
                nonlocal deep_attempted_count, deep_readiness_failed_count, deep_acquisition_timeout_count
                nonlocal deep_source_unavailable_count, rule_exception_count, stage2_internal_error_count
                nonlocal deep_ready_count, deep_technical_failure_count, deep_repair_attempted_count
                nonlocal phase_items, phase_index
                phase_items = list(deep_candidates)
                for phase_index, item in enumerate(phase_items):
                    yield item
                for _ in range(repair_passes):
                    retry = []
                    for item in deep_candidates:
                        item_key = item.global_instrument_id
                        attempts = ((checkpoint.row(PHASE_DEEP, item_key) or {}).get("attempts", 0)
                                    if checkpoint is not None else local_attempts.get(item_key, 0))
                        if attempts < max_attempts and _retryable(_last_diagnostic(item_key)):
                            retry.append(item)
                    if not retry:
                        return
                    phase_items = retry
                    for phase_index, item in enumerate(retry):
                        item_key = item.global_instrument_id
                        previous = _last_diagnostic(item_key)
                        undo = dict(self._RESTORE_INCREMENTS.get(previous.disposition or "", {}))
                        if previous.disposition == "DEEP_READINESS_NOT_MET" and previous.failure_class == TECHNICAL_RETRYABLE:
                            undo["deep_technical_failure"] = 1
                        deep_ready_count -= undo.get("deep_ready", 0)
                        deep_readiness_failed_count -= undo.get("deep_readiness_failed", 0)
                        deep_acquisition_timeout_count -= undo.get("deep_acquisition_timeout", 0)
                        deep_source_unavailable_count -= undo.get("deep_source_unavailable", 0)
                        rule_exception_count -= undo.get("rule_exception", 0)
                        stage2_internal_error_count -= undo.get("stage2_internal_error", 0)
                        deep_technical_failure_count -= undo.get("deep_technical_failure", 0)
                        diagnostics[:] = [d for d in diagnostics if d.global_instrument_id != item_key]
                        investigation_matrix.pop(str(item_key), None)
                        deep_attempted_count -= 1
                        deep_repair_attempted_count += 1
                        repairing.add(item_key)
                        logger.info("stage2_candidate_repair instrument_id=%s previous=%s failures=%s",
                                    item_key, previous.disposition, previous.acquisition_failures)
                        yield item

            stage2_concurrency = max(1, min(8, int(getattr(getattr(self.repository, "settings", None),
                                                             "research_stage2_concurrency", 1) or 1)))
            phase_items: list = []
            phase_index = -1
            inflight: dict = {}
            started_attempts: set = set()
            # Live references to CURRENT_NEWS background acquisitions started
            # by investigate() (see app/deep_investigation.py) are tracked in
            # the process-lifetime `current_news_background_tasks` registry
            # (app/background_task_registry.py), not a phase-local set. A
            # phase-local set drained by *this* phase's own `finally:` block
            # would make mandatory Stage-2/cycle completion wait for the
            # slowest optional CURRENT_NEWS search -- exactly the wait
            # CURRENT_NEWS's optional/non-blocking contract forbids (Section H,
            # consolidated fix pass). The registry still guarantees nothing is
            # orphaned and every exception is observed (see its own
            # done-callback); only *when* that's guaranteed moves from "before
            # this phase ends" to "the registry's own done-callback, plus
            # process shutdown" (app/main.py's `research_lifespan`).
            from app.background_task_registry import current_news_background_tasks
            pending_news_tasks = current_news_background_tasks
            acquired_deep_ids = set()

            def note_deep_acquisition(key, result):
                if getattr(result, "planned_requirement_ids", ()) and key not in acquired_deep_ids:
                    acquired_deep_ids.add(key)
                    logger.info("radar_acquisition_count operation=deep_candidates_acquired count=1")

            async def _acquire_deep(item):
                item_key = item.global_instrument_id
                item_payload = dict(metadata.get(str(item_key), {}))
                item_payload.setdefault("primaryExchange", item_payload.get("exchange"))
                item_payload.setdefault("primarySymbol", item_payload.get("ticker"))
                if not self.profile_hydrator(item_key, item_payload):
                    raise ValueError("PROFILE_HYDRATION_FAILED")
                # Same ETF-aware resolution as baseline acquisition above:
                # hydration may have landed this instrument in
                # repository.etf_profiles rather than repository.profiles.
                try:
                    item_profile = self.repository.profile(item_key)
                except StopIteration:
                    try:
                        item_profile = self.repository.etf_profile(item_key)
                    except StopIteration:
                        raise ValueError("PROFILE_MISSING_AFTER_HYDRATION")
                if item_profile.instrument_id != item_key:
                    raise ValueError("PROFILE_HYDRATION_FAILED")
                self.readiness_adapter.remember_canonical_metadata(item_key, item_payload)
                if discovery_v2:
                    from app.deep_investigation import investigate
                    item_result, item_plan, item_matrix = await investigate(
                        self.readiness, item_key, jurisdiction=_baseline_jurisdiction(item_profile),
                        nomination_paths=tuple(n['path'] for n in nominations.get(str(item_key), ())),
                        company_context={k: item_payload.get(k) for k in ('sector', 'industry', 'assetType')},
                        identity_headers=identity_headers, correlation_id=correlation_id,
                        background_tasks=pending_news_tasks)
                    note_deep_acquisition(item_key, item_result)
                    return item_profile, item_payload, item_result, item_plan, item_matrix
                item_result = await self.readiness.ensure(
                    item_key, jurisdiction=_baseline_jurisdiction(item_profile), requirement_ids=None,
                    identity_headers=identity_headers, wait_for_completion=True)
                note_deep_acquisition(item_key, item_result)
                return item_profile, item_payload, item_result, None, None

            lookahead_depth = 4 * stage2_concurrency

            def _running():
                return sum(1 for task in inflight.values() if not task.done())

            async def _prefetch_ahead():
                # Bounded look-ahead within the current phase (backpressure):
                # at most stage2_concurrency acquisitions RUNNING and at most
                # lookahead_depth started-but-unconsumed (never one task per
                # candidate). Results are CONSUMED in candidate order, so
                # diagnostics/accounting stay deterministic, while a slow
                # candidate at the head no longer stops independent work.
                nonlocal stage2_peak_inflight
                repair_phase = phase_items is not deep_candidates and bool(repairing)
                for upcoming in phase_items[phase_index + 1:]:
                    if _running() >= stage2_concurrency or len(inflight) >= lookahead_depth:
                        break
                    upcoming_key = upcoming.global_instrument_id
                    if upcoming_key in inflight:
                        continue
                    if (checkpoint is not None and not repair_phase
                            and checkpoint.restorable(PHASE_DEEP, upcoming_key) is not None):
                        continue
                    if checkpoint is not None:
                        await checkpoint.start(PHASE_DEEP, upcoming_key)
                        started_attempts.add(upcoming_key)
                    inflight[upcoming_key] = asyncio.ensure_future(_acquire_deep(upcoming))
                    stage2_peak_inflight = max(stage2_peak_inflight, _running())

            async def _take_acquisition(item):
                nonlocal stage2_peak_inflight
                item_key = item.global_instrument_id
                if stage2_concurrency <= 1:
                    task = inflight.pop(item_key, None)
                    return await (task if task is not None else _acquire_deep(item))
                if item_key not in inflight:
                    inflight[item_key] = asyncio.ensure_future(_acquire_deep(item))
                    stage2_peak_inflight = max(stage2_peak_inflight, _running())
                head = inflight[item_key]
                await _prefetch_ahead()
                while not head.done():
                    await asyncio.wait([task for task in inflight.values() if not task.done()],
                                       return_when=asyncio.FIRST_COMPLETED)
                    await _prefetch_ahead()
                inflight.pop(item_key, None)
                return head.result()

            if checkpoint is not None:
                # Durable truthful resume state: count candidates that are
                # ALREADY final/restorable per the persisted checkpoint, before
                # this process touches any of them. This is the K in the
                # K/N recovery invariant -- it must never be re-derived from
                # iteration order alone, or a restart would appear to regress
                # progress even though no completed work is re-executed.
                deep_durable_completed_count = sum(
                    1 for c in deep_candidates
                    if checkpoint.restorable(PHASE_DEEP, c.global_instrument_id) is not None)
                if deep_durable_completed_count:
                    logger.info("stage2_deep_resume_state cycleId=%s correlationId=%s durableCompleted=%d/%d",
                                checkpoint.cycle_id, correlation_id, deep_durable_completed_count, len(deep_candidates))
            _cycle_timing_recorder = cycle_timing.CycleTimingRecorder(
                cycle_id=str(correlation_id) if correlation_id else None)
            _cycle_timing_token = cycle_timing.bind_recorder(_cycle_timing_recorder)
            _cycle_timing_recorder.mark_stage2_start()
            try:
                for candidate in _deep_schedule():
                    key = candidate.global_instrument_id
                    # Stop admitting NEW candidates promptly if this cycle's
                    # cancellation has been requested (either via the durable
                    # two-phase request or a terminal CANCELLED). In-flight
                    # acquisition tasks (inflight) are still drained by the
                    # existing _prefetch_ahead / _take_acquisition backpressure,
                    # and completed evidence rows are preserved.
                    if checkpoint is not None and (key not in repairing) and await self._run_blocking(
                            checkpoint.persistence.cycle_cancellation_requested, checkpoint.cycle_id):
                        logger.info("opportunity_cycle_cancel_during_stage2 cycleId=%s instrument=%s",
                                    checkpoint.cycle_id, key)
                        break
                    deep_attempted_count += 1
                    local_attempts[key] = local_attempts.get(key, 0) + 1
                    if checkpoint is not None:
                        if key not in repairing:
                            restored = checkpoint.restorable(PHASE_DEEP, key)
                            increments = (self._restore_deep_candidate(restored, key, diagnostics, rules, by_id, stage_b,
                                                                       successful, investigation_matrix)
                                          if restored is not None else None)
                            if increments is not None:
                                checkpoint.note_restored(PHASE_DEEP)
                                deep_ready_count += increments.get("deep_ready", 0)
                                deep_readiness_failed_count += increments.get("deep_readiness_failed", 0)
                                deep_acquisition_timeout_count += increments.get("deep_acquisition_timeout", 0)
                                deep_source_unavailable_count += increments.get("deep_source_unavailable", 0)
                                rule_analyzed_count += increments.get("rule_analyzed", 0)
                                rule_exception_count += increments.get("rule_exception", 0)
                                stage2_internal_error_count += increments.get("stage2_internal_error", 0)
                                deep_technical_failure_count += increments.get("deep_technical_failure", 0)
                                evaluated += increments.get("rule_analyzed", 0)
                                continue
                        if key in started_attempts:
                            started_attempts.discard(key)
                        else:
                            await checkpoint.start(PHASE_DEEP, key)
                    body_completed = False
                    try:
                        enriched = enriched_by_id.get(key, candidate)
                        if enriched is None:
                            enriched = candidate

                        # Acquisition (hydrate + investigate/ensure) may already be
                        # running in the bounded look-ahead window (Slice 7).
                        profile, payload, ensure_result, plan, matrix = await _take_acquisition(candidate)

                        evaluation_at = max(as_of, self.clock())
                        if discovery_v2:
                            investigation_matrix[str(key)] = dict(requirements=matrix,
                                rule_families=list(plan.applicable_rule_families), rule_evaluated=False,
                                company_context=dict(plan.company_context), required=list(plan.required_evidence),
                                optional=list(plan.optional_evidence), not_applicable=list(plan.not_applicable_evidence))

                        # DI-10B: the deep readiness snapshot for the eligibility
                        # decision below. Falls back to an explicit read (using its
                        # real return value, and the same jurisdiction the ensure
                        # call used) only if ensure() itself returned no readiness at
                        # all -- the real production ResearchReadinessRuntime.ensure()
                        # never does this (it always returns a readiness snapshot,
                        # even after a bounded-acquisition timeout), so this remains a
                        # defensive fallback, not the primary path.
                        readiness_result = ensure_result.readiness if ensure_result is not None else None
                        if ensure_result is not None and readiness_result is None:
                            readiness_result = await self.readiness.read(
                                key, jurisdiction=_baseline_jurisdiction(profile), now=evaluation_at)

                        if ensure_result is None or readiness_result is None:
                            logger.info("stage2_candidate_readiness instrument_id=%s state=SOURCE_UNAVAILABLE", key)
                            # No usable readiness snapshot was ever produced for this
                            # candidate: the acquisition source itself is unavailable.
                            # The candidate stays NON-RANKABLE -- it is never forced
                            # through rule_engine.analyze() with nothing to evaluate.
                            deep_source_unavailable_count += 1
                            diagnostics.append(CandidateDiagnostic(
                                global_instrument_id=key,
                                status="SUPPRESSED",
                                failure_reason="DEEP_SOURCE_UNAVAILABLE",
                                rank_eligible=False,
                                suppression_reasons=["DEEP_SOURCE_UNAVAILABLE"],
                                disposition="DEEP_SOURCE_UNAVAILABLE"
                            ))
                        else:
                            # The exact same eligibility gate rule_engine.analyze()
                            # applies internally (StockRuleEngineV1.evaluate() calls
                            # this identical policy with allow_partial=False, so
                            # permitted == full_analysis_allowed exactly as here).
                            # Reusing it -- rather than the non-existent attribute the
                            # old code accessed -- is what makes this an observability
                            # fix, not a readiness-weakening one.
                            eligibility = self.rule_engine.eligibility_policy.evaluate(readiness_result)
                            if discovery_v2:
                                investigation_matrix[str(key)].update(
                                    ready=eligibility.full_analysis_allowed,
                                    blocking_requirements=eligibility.blocking_requirements)
                                logger.info("%s instrument_id=%s family=STOCK_RULE_ENGINE_V1 reasons=%s",
                                    'deep_rule_family_ready' if eligibility.full_analysis_allowed else 'deep_rule_family_suppressed',
                                    key, eligibility.blocking_requirements)
                            logger.info("stage2_candidate_readiness instrument_id=%s state=%s",
                                        key, "READY" if eligibility.full_analysis_allowed else "INCOMPLETE")
                            if not eligibility.full_analysis_allowed:
                                # Mandatory deep evidence is genuinely missing.
                                # rule_engine.analyze() is deliberately NOT called.
                                # Distinguish a bounded acquisition timeout (using the
                                # ensure() result's own failures info, set by
                                # ResearchReadinessRuntime._execute_plan_bounded() on
                                # TimeoutError) from any other readiness shortfall.
                                timed_out = any(
                                    "ACQUISITION_TIMEOUT" in str(reason)
                                    for reason in (ensure_result.failures or {}).values())
                                acquisition_failures = blocking_failures(
                                    ensure_result.failures, eligibility.blocking_requirements)
                                failure_class = (TECHNICAL_RETRYABLE if timed_out else classify_requirement_failures(
                                    ensure_result.failures, eligibility.blocking_requirements))
                                if timed_out:
                                    logger.info("stage2_candidate_readiness instrument_id=%s state=ACQUISITION_TIMEOUT", key)
                                    deep_acquisition_timeout_count += 1
                                    disposition = "DEEP_ACQUISITION_TIMEOUT"
                                else:
                                    deep_readiness_failed_count += 1
                                    disposition = "DEEP_READINESS_NOT_MET"
                                    if failure_class == TECHNICAL_RETRYABLE:
                                        deep_technical_failure_count += 1
                                        logger.info("stage2_candidate_readiness instrument_id=%s state=TECHNICAL_FAILURE failures=%s",
                                                    key, acquisition_failures)
                                diagnostics.append(CandidateDiagnostic(
                                    global_instrument_id=key,
                                    status="SUPPRESSED",
                                    failure_reason=eligibility.reason,
                                    rank_eligible=False,
                                    suppression_reasons=list(eligibility.blocking_requirements) or [eligibility.reason],
                                    disposition=disposition,
                                    failure_class=failure_class,
                                    acquisition_failures=acquisition_failures,
                                ))
                            else:
                                deep_ready_count += 1
                                try:
                                    if discovery_v2:
                                        evaluation_at = max(as_of, self.clock())
                                        refreshed = await self._run_blocking(scanner.enrich_candidates,
                                            type('ScanResult', (), {'candidates': [candidate], 'as_of': evaluation_at})(),
                                            sector_contexts=dict(sector_contexts or {}), include_unready=True)
                                        if refreshed:
                                            enriched = refreshed[0]
                                    result = await self.rule_engine.analyze(
                                        profile, readiness_result, allow_partial=False, now=evaluation_at)
                                except Exception:
                                    # An unexpected exception from the rule engine
                                    # itself (NOT a readiness rejection -- readiness
                                    # was already confirmed sufficient above) is
                                    # classified explicitly and never exposes its raw
                                    # message in the diagnostic.
                                    rule_exception_count += 1
                                    diagnostics.append(CandidateDiagnostic(
                                        global_instrument_id=key,
                                        status="SUPPRESSED",
                                        failure_reason="RULE_ENGINE_EXCEPTION",
                                        rank_eligible=False,
                                        suppression_reasons=["RULE_ENGINE_EXCEPTION"],
                                        disposition="RULE_ENGINE_EXCEPTION"
                                    ))
                                else:
                                  incomplete = self._incomplete_analysis(result, readiness_result)
                                  if incomplete is not None:
                                    # Full-research state contract: the Rule Engine
                                    # could not produce a fully analyzed result
                                    # (applicable required area UNSCORABLE / not
                                    # full). This is never an ordinary ranking
                                    # rejection or proof of evidence absence:
                                    # keep the gap technical/retryable and out
                                    # of Stage B.
                                    blocking = incomplete
                                    acquisition_failures = self._unscorable_failures(
                                        key, blocking, ensure_result.failures)
                                    failure_class = classify_requirement_failures(acquisition_failures, blocking)
                                    deep_ready_count -= 1
                                    deep_readiness_failed_count += 1
                                    if failure_class == TECHNICAL_RETRYABLE:
                                        deep_technical_failure_count += 1
                                    if discovery_v2:
                                        investigation_matrix[str(key)].update(
                                            ready=False, blocking_requirements=blocking)
                                    logger.info("stage2_candidate_readiness instrument_id=%s state=RULE_AREA_UNSCORABLE "
                                                "blocking=%s failure_class=%s failures=%s",
                                                key, blocking, failure_class, acquisition_failures)
                                    diagnostics.append(CandidateDiagnostic(
                                        global_instrument_id=key,
                                        status="SUPPRESSED",
                                        failure_reason=result.eligibility.reason
                                        if not result.eligibility.full_analysis_allowed
                                        else "APPLICABLE_RULE_ENGINE_AREA_UNSCORABLE",
                                        rank_eligible=False,
                                        suppression_reasons=list(blocking),
                                        disposition="DEEP_READINESS_NOT_MET",
                                        failure_class=failure_class,
                                        acquisition_failures=acquisition_failures,
                                    ))
                                  else:
                                    rule_analyzed_count += 1
                                    evaluated += 1
                                    rules[key] = result
                                    ranked = self.ranker.score(enriched, result)
                                    by_id[key] = enriched
                                    stage_b.append(enriched)
                                    if ranked.rank_eligible:
                                        diagnostics.append(CandidateDiagnostic(
                                            global_instrument_id=key,
                                            status="RANK_ELIGIBLE",
                                            cache_hit=getattr(result, "cache_hit", None),
                                            rank_eligible=True,
                                            disposition="ANALYZED"
                                        ))
                                        successful.append(enriched)
                                    else:
                                        diagnostics.append(CandidateDiagnostic(
                                            global_instrument_id=key,
                                            status="SUPPRESSED",
                                            rank_eligible=False,
                                            suppression_reasons=ranked.eligibility_reasons,
                                            disposition="RANK_FILTERED"
                                        ))
                        body_completed = True
                    except Exception as e:
                        # Final defensive boundary: one bad candidate (e.g. a
                        # hydration/identity failure raised above, or any other
                        # unexpected error) can never crash the whole cycle. A known,
                        # deliberately-raised reason keeps its own literal disposition
                        # (unchanged, existing failure_reason vocabulary); anything
                        # else is classified explicitly rather than left
                        # indistinguishable from a readiness rejection.
                        reason = str(e)
                        disposition = reason if reason in self._KNOWN_STAGE2_FAILURE_REASONS else "STAGE2_INTERNAL_ERROR"
                        reason = disposition
                        if disposition == "STAGE2_INTERNAL_ERROR":
                            stage2_internal_error_count += 1
                        diagnostics.append(CandidateDiagnostic(
                            global_instrument_id=key,
                            status="SUPPRESSED",
                            failure_reason=reason,
                            disposition=disposition
                        ))
                        body_completed = True
                    finally:
                        if discovery_v2:
                            investigation_matrix.setdefault(str(key), {}).update(
                                rule_evaluated=key in rules,
                                disposition=diagnostics[-1].disposition if diagnostics and diagnostics[-1].global_instrument_id == key else None)
                            logger.info("stage2_candidate_complete instrument_id=%s disposition=%s", key,
                                        investigation_matrix[str(key)]['disposition'])
                        # Only a candidate whose body finished (normally or via its
                        # own defensive boundary) gets a durable outcome; a crash /
                        # cancellation leaves it IN_PROGRESS for resume.
                        if checkpoint is not None and body_completed:
                            await self._checkpoint_deep_outcome(checkpoint, key, diagnostics, rules, by_id,
                                                                investigation_matrix)
                            deep_durable_completed_count += 1
                        if key in repairing and body_completed:
                            repairing.discard(key)
                            if key in rules:
                                deep_repair_recovered_count += 1
                        # Readiness is needed only for this candidate's strict gate.
                        ensure_result = readiness_result = None
                        enriched = refreshed = plan = matrix = None
                        if deep_attempted_count % 10 == 0 or deep_attempted_count == len(deep_candidates):
                            _deep_progress_completed = (deep_durable_completed_count if checkpoint is not None
                                                        else deep_attempted_count)
                            logger.info("stage2_deep_progress: completed=%d/%d active=%d/%d",
                                        _deep_progress_completed, len(deep_candidates), _running(), stage2_concurrency)
            finally:
                # Crash/cancellation/ownership loss: never orphan look-ahead
                # acquisitions (provider calls) or leave them un-awaited.
                for pending_task in inflight.values():
                    pending_task.cancel()
                if inflight:
                    await asyncio.gather(*inflight.values(), return_exceptions=True)
                inflight.clear()
                # CURRENT_NEWS background acquisitions are deliberately NOT
                # drained here. They live in the process-lifetime
                # `current_news_background_tasks` registry (added to it, not
                # to a phase-local set -- see the declaration above), so
                # this phase -- and therefore this cycle -- can complete
                # without waiting for the slowest optional per-candidate
                # news search. Nothing is orphaned: the registry retains a
                # live reference to every task until it finishes and
                # observes/logs its exception via its own done-callback
                # regardless of whether anything ever awaits it explicitly.
                # True process shutdown drains whatever is still
                # outstanding -- see app/main.py's `research_lifespan`.
                _cycle_timing_recorder.mark_stage2_complete()
                _cycle_timing_recorder.note_concurrency(stage2_peak_inflight)
                cycle_timing.unbind_recorder(_cycle_timing_token)
                _timing_report = _cycle_timing_recorder.report()
                logger.info(
                    # NOTE: this recorder's report()['cycle_id'] field holds the
                    # CORRELATION id (CycleTimingRecorder is constructed with
                    # cycle_id=str(correlation_id)), never the durable API
                    # cycle/job id. Logged explicitly as correlationId here, with
                    # the real durable cycleId alongside it, so this line cannot
                    # be mistaken for a queryable /status cycle id again.
                    "stage2_timing_report cycleId=%s correlationId=%s stage2WallClockMs=%s maxConcurrentMandatoryInvestigations=%s "
                    "trackedOperations=%s droppedOperations=%s aggregatePdfQueueWaitMs=%s aggregateProviderWaitMs=%s "
                    "aggregatePersistenceWaitMs=%s aggregateSingleFlightWaitMs=%s aggregateSearchProviderMs=%s aggregateCurrentNewsThrottleMs=%s "
                    "aggregateNewsWorkerLockWaitMs=%s "
                    "slowestOperationsJson=%s "
                    "persistenceOperationTimingJson=%s",
                    checkpoint.cycle_id if checkpoint is not None else None, _timing_report["cycle_id"],
                    _timing_report["stage2_wall_clock_ms"],
                    _timing_report["max_concurrent_mandatory_investigations"], _timing_report["tracked_operation_count"],
                    _timing_report["dropped_operation_count"], _timing_report["aggregate_pdf_queue_wait_ms"],
                    _timing_report["aggregate_provider_wait_ms"], _timing_report["aggregate_persistence_wait_ms"],
                    # Area 2 production-validation gap: this value was already
                    # computed by cycle_timing (record_single_flight_wait_elapsed
                    # -> _cycle_single_flight_wait_ms -> report()'s
                    # aggregate_single_flight_wait_ms) but never included in this
                    # log line, so the overlap-aware runtime_ensure fix had no
                    # production-visible counter to confirm its impact by. Purely
                    # additive: no new instrumentation, no runtime_ensure change.
                    _timing_report["aggregate_single_flight_wait_ms"],
                    # Issue 2 (CURRENT_NEWS 84-133s runtime_ensure latency):
                    # search-provider HTTP time was previously invisible
                    # (see _safe_search_get / record_search_provider_elapsed
                    # in app.source_discovery) and folded entirely into
                    # other_unattributed_ms. Purely additive: no behavior
                    # change, no new provider calls, no retry/concurrency
                    # change.
                    _timing_report["aggregate_search_provider_ms"],
                    _timing_report["aggregate_current_news_throttle_ms"],
                    # Turn-7 diagnostics-only pass: time spent awaiting
                    # this instrument's keyed entry in
                    # Repository._news_worker_locks before acquire_news()
                    # starts (see record_news_worker_lock_wait_elapsed in
                    # app.cycle_timing and refresh_news_intelligence in
                    # app.repository). Now isolates same-instrument
                    # queueing only -- the lock was changed from
                    # process-wide to per-instrument-keyed (Issue 1 fix),
                    # so unrelated candidates no longer serialize here.
                    _timing_report["aggregate_news_worker_lock_wait_ms"],
                    json.dumps(_timing_report["slowest_operations"]),
                    json.dumps(_timing_report["persistence_operation_timing"]),
                )


            for candidate_id in baseline_eligible_ids:
                if ensure_results.get(candidate_id) is None:
                    reason = baseline_failures.get(candidate_id, "BASELINE_ACQUISITION_FAILED")
                    diagnostics.append(CandidateDiagnostic(
                        global_instrument_id=candidate_id,
                        status="SUPPRESSED",
                        failure_reason=reason,
                        suppression_reasons=[reason]))

        else:
            stage_b = await self._run_blocking(scanner.enrich_candidates, scan, sector_contexts=dict(sector_contexts or {}))
            by_id = {c.global_instrument_id: c for c in stage_b}
            deep_pool_eligible_count = len([c for c in stage_b if c.global_instrument_id in phase1])
            def desc(value): return -value if value is not None else float('inf')
            shortlist = sorted((c for c in stage_b if c.global_instrument_id in phase1), key=lambda c: (
                desc(c.stage_b_score), -c.confidence, desc(phase1[c.global_instrument_id].pre_score),
                -phase1[c.global_instrument_id].confidence, str(c.global_instrument_id)))[:shortlist_limit]

            for candidate in shortlist:
                key = candidate.global_instrument_id
                step = 'PUBLIC_EVIDENCE_UNAVAILABLE'
                cache_hit = None
                try:
                    payload = dict(metadata[str(key)])
                    payload.setdefault('primaryExchange', payload.get('exchange'))
                    payload.setdefault('primarySymbol', payload.get('ticker'))
                    if not self.profile_hydrator(key, payload):
                        raise ValueError('UNRESOLVED_PROFILE')
                    profile = self.repository.profile(key)
                    if profile.instrument_id != key:
                        raise ValueError('PROFILE_IDENTITY_MISMATCH')
                    self.readiness_adapter.remember_canonical_metadata(key, payload)
                    step = 'READINESS_UNAVAILABLE'
                    readiness = await self.readiness.read(key, jurisdiction=jurisdiction_for_profile(profile), now=as_of)
                    step = 'RULE_ENGINE_UNAVAILABLE'
                    result = await self.rule_engine.analyze(profile, readiness, allow_partial=False, now=as_of)
                    evaluated += 1
                    cache_hit = result.cache_hit
                    ranked = self.ranker.score(candidate, result)
                    diagnostics.append(CandidateDiagnostic(global_instrument_id=key,
                        status='RANK_ELIGIBLE' if ranked.rank_eligible else 'SUPPRESSED', cache_hit=cache_hit,
                        rank_eligible=ranked.rank_eligible, suppression_reasons=ranked.eligibility_reasons))
                    # Every analyzed candidate carries its rule result so that prior
                    # recommendation reviews and rank-ineligible evaluations still
                    # round-trip into ranking.evaluated_entries / cycle snapshots.
                    rules[key] = result
                    if ranked.rank_eligible:
                        successful.append(candidate)
                except Exception:
                    # Never return exception messages, headers, raw evidence or recommendations.
                    diagnostics.append(CandidateDiagnostic(global_instrument_id=key, status='FAILED',
                        failure_reason=step, cache_hit=cache_hit))

        # Prior recommendations review (read-only)
        review_ids = list(dict.fromkeys(review_ids))
        stage_by_id = {c.global_instrument_id: c for c in stage_b}
        screening_by_id = {c.global_instrument_id: c for c in scan.candidates}

        for key in review_ids:
            if key not in screening_by_id:
                continue

            candidate = stage_by_id.get(key, screening_by_id[key])

            if self.acquisition_enabled:
                if key in rules and key in by_id:
                    review_evidence[str(key)] = self._evidence_state(by_id[key], rules[key], public_analyst_by_id.get(key))
                    continue
                # A deferred prior recommendation may still need price lifecycle
                # handling. Supply durable technical observations only; never run
                # rules, restore a previous score, or put it into ranked entries.
                review_scan = type("ScanResult", (), {"candidates": [candidate], "as_of": evaluation_at})()
                try:
                    enriched_reviews = await self._run_blocking(scanner.enrich_candidates,
                        review_scan, sector_contexts=dict(sector_contexts or {}), include_unready=True)
                except Exception:
                    # Unavailable persisted observations cannot become a price.
                    enriched_reviews = []
                if enriched_reviews:
                    enriched = enriched_reviews[0]
                    review_evidence[str(key)] = {
                        "technical": self._technical_evidence(enriched.technical_feature_snapshot),
                        "sector": {"sector_score": enriched.sector_relative_strength_snapshot.relative_strength_score,
                                   "sector_state": enriched.sector_relative_strength_snapshot.sector_state},
                    }
                continue

            if key in rules:
                result = rules[key]
                ranked = self.ranker.score(candidate, result)
                if ranked.rank_eligible:
                    diagnostics.append(CandidateDiagnostic(
                        global_instrument_id=key,
                        status="RANK_ELIGIBLE",
                        cache_hit=getattr(result, "cache_hit", None),
                        rank_eligible=True
                    ))
                    by_id[key] = candidate
                    successful.append(candidate)
                else:
                    diagnostics.append(CandidateDiagnostic(
                        global_instrument_id=key,
                        status="SUPPRESSED",
                        cache_hit=getattr(result, "cache_hit", None),
                        rank_eligible=False,
                        suppression_reasons=ranked.eligibility_reasons
                    ))
                continue

            try:
                payload = dict(metadata.get(str(key), {}))
                payload.setdefault("primaryExchange", payload.get("exchange"))
                payload.setdefault("primarySymbol", payload.get("ticker"))

                if not self.profile_hydrator(key, payload):
                    raise ValueError("PROFILE_HYDRATION_FAILED")
                profile = self.repository.profile(key)
                if profile.instrument_id != key:
                    raise ValueError("PROFILE_HYDRATION_FAILED")
                self.readiness_adapter.remember_canonical_metadata(key, payload)

                # A prior recommendation is re-scored with the same strict
                # full-analysis gate as discovery (allow_partial=False).  The
                # candidate is enriched into a Stage-B candidate so the ranker can
                # read its technical/sector snapshots — a raw GlobalScanCandidate
                # from screening carries none of those.  A review never performs
                # additional acquisition; only the already-acquired baseline is
                # reused (acquisition-enabled mode) or the persisted evidence
                # (non-acquisition mode).
                enriched = candidate if hasattr(candidate, "technical_feature_snapshot") else None
                if enriched is None:
                    review_scan = type("ScanResult", (), {"candidates": [candidate], "as_of": evaluation_at})()
                    enriched_candidates = await self._run_blocking(scanner.enrich_candidates,
                        review_scan, sector_contexts=dict(sector_contexts or {}))
                    enriched = next(iter(enriched_candidates), None)
                if enriched is None:
                    raise ValueError("REVIEW_EVIDENCE_INSUFFICIENT")

                result = await self.rule_engine.analyze(
                    profile, readiness=None, allow_partial=False, now=evaluation_at)
                rules[key] = result
                ranked = self.ranker.score(enriched, result)
                by_id[key] = enriched
                stage_b.append(enriched)
                review_evidence[str(key)] = self._evidence_state(enriched, result, public_analyst_by_id.get(key))
                if ranked.rank_eligible:
                    diagnostics.append(CandidateDiagnostic(
                        global_instrument_id=key,
                        status="RANK_ELIGIBLE",
                        cache_hit=getattr(result, "cache_hit", None),
                        rank_eligible=True
                    ))
                    successful.append(enriched)
                else:
                    diagnostics.append(CandidateDiagnostic(
                        global_instrument_id=key,
                        status="SUPPRESSED",
                        cache_hit=getattr(result, "cache_hit", None),
                        rank_eligible=False,
                        suppression_reasons=ranked.eligibility_reasons
                    ))
            except Exception as e:
                diagnostics.append(CandidateDiagnostic(
                    global_instrument_id=key,
                    status="SUPPRESSED",
                    failure_reason=str(e)
                ))

        # Build final ranking over every analyzed candidate (shortlist deep
        # evaluations + prior-recommendation reviews). shortlist_limit bounds
        # acquisition, not publication. top_n is None in production: ALL rank-eligible entries
        # round-trip into ranking.top_n in global rank order. A bounded integer
        # top_n is a legacy/test knob only that truncates ranking.top_n to its
        # first N rank-eligible entries. Every analyzed candidate also
        # round-trips into ranking.evaluated_entries so cycle snapshots and
        # recommendation history remain complete and uncapped.
        analyzed_keys = list(rules.keys())
        analyzed_enriched = [by_id[k] for k in analyzed_keys if k in by_id]
        ranked_results = self.ranker.rank(analyzed_enriched, rules)
        eligible_ids = {r.global_instrument_id for r in ranked_results if r.rank_eligible}

        entries = []
        for position, result in enumerate(ranked_results, 1):
            key = result.global_instrument_id
            initial = initial_by_id.get(key)
            enriched = by_id.get(key)
            rule = rules.get(key)
            if enriched is None or initial is None or rule is None:
                continue
            entries.append(OpportunityEntry(
                rank=position, global_instrument_id=key,
                symbol=initial.symbol, company_name=initial.company_name,
                sector=enriched.sector_relative_strength_snapshot.sector, market=initial.market,
                pre_score=initial.pre_score, stage_b_score=enriched.stage_b_score,
                technical_score=result.technical_score, technical_state=result.technical_state,
                sector_score=result.sector_score, sector_state=result.sector_state,
                rule_engine_score=result.rule_engine_score, rule_engine_confidence=rule.confidence_score,
                opportunity_score=result.opportunity_score, opportunity_confidence=result.opportunity_confidence,
                score_coverage=result.score_coverage, top_positive_reasons=result.top_positive_reasons,
                top_negative_reasons=result.top_negative_reasons, rule_engine_version=rule.rule_engine_version,
                technical_feature_version=getattr(enriched.technical_feature_snapshot, 'feature_version', ''),
                sector_feature_version=getattr(enriched.sector_relative_strength_snapshot, 'feature_version', ''),
                opportunity_ranker_version=result.ranker_version,
                evidence_state=self._evidence_state(enriched, rule, public_analyst_by_id.get(key)),
                rank_eligible=result.rank_eligible,
                suppression_reasons=result.eligibility_reasons))

        evaluated_entries = entries
        # RADAR V2 production contract: top_n=None -> expose ALL rank-eligible
        # investigated entries (no 4/25/100 cap). A bounded integer top_n is a
        # legacy/test knob only and truncates ranking.top_n to the first N.
        # Publication iterates ranking.evaluated_entries (above), never this tail.
        eligible_entries = [e for e in entries if e.rank_eligible]
        top_n_entries = eligible_entries if top_n is None else eligible_entries[:top_n]

        logger.info(
            "stage2_complete correlationId=%s deep_attempted=%d deep_ready=%d deep_readiness_failed=%d "
            "deep_acquisition_timeout=%d deep_source_unavailable=%d rule_analyzed=%d rule_exception=%d "
            "stage2_internal_error=%d evaluated=%d suppressed=%d eligible=%d technical_failure=%d "
            "repair_attempted=%d repair_recovered=%d",
            correlation_id, deep_attempted_count, deep_ready_count, deep_readiness_failed_count,
            deep_acquisition_timeout_count, deep_source_unavailable_count, rule_analyzed_count,
            rule_exception_count, stage2_internal_error_count, rule_analyzed_count,
            sum(1 for d in diagnostics if d.status == "SUPPRESSED"), len(eligible_ids),
            deep_technical_failure_count, deep_repair_attempted_count, deep_repair_recovered_count,
        )
        logger.info("stage2_disposition_counts correlationId=%s shortlist=%d baseline_incomplete=%d suppressed=%d diagnostics=%d",
                    correlation_id, len(shortlist), baseline_incomplete_count,
                    sum(1 for d in diagnostics if d.status == "SUPPRESSED"), len(diagnostics))

        return OpportunityRanking(generated_at=self.clock(), as_of=as_of,
            candidate_nominations=nominations, investigation_matrix=investigation_matrix, rotation_after=rotation_after,
            universe_count=scan.total_canonical_active_equities, phase1_eligible_count=len(phase1),
            stage_b_count=len(stage_b), shortlist_count=len(shortlist),
            # Preserved exactly as before DI-10B: len(shortlist) in acquisition-
            # enabled production is a pre-existing, externally-consumed meaning
            # ("how many candidates were considered for deep evaluation"), not
            # "how many were actually analyzed". rule_analyzed_count /
            # evaluated_count below carry the accurate meaning.
            deep_evaluated_count=len(evaluated_entries) if not self.acquisition_enabled else len(shortlist),
            rank_eligible_count=len(eligible_ids), top_n=top_n_entries, diagnostics=diagnostics,
            baseline_ready_count=baseline_ready_count,
            baseline_acquisition_needed_count=baseline_acquisition_needed_count,
            baseline_incomplete_count=baseline_incomplete_count,
            baseline_evaluated_count=baseline_evaluated_count,
            acquisition_admitted_count=acquisition_admitted_count,
            acquisition_deferred_count=acquisition_deferred_count,
            baseline_acquisition_attempted_count=baseline_acquisition_attempted_count,
            baseline_acquisition_timeout_count=baseline_acquisition_timeout_count,
            baseline_acquisition_failed_count=baseline_acquisition_failed_count,
            preliminary_eligible_count=preliminary_eligible_count,
            deep_pool_eligible_count=deep_pool_eligible_count,
            deep_candidate_count=deep_candidate_count,
            deep_attempted_count=deep_attempted_count,
            deep_ready_count=deep_ready_count,
            deep_readiness_failed_count=deep_readiness_failed_count,
            deep_technical_failure_count=deep_technical_failure_count,
            deep_repair_attempted_count=deep_repair_attempted_count,
            deep_repair_recovered_count=deep_repair_recovered_count,
            stage2_concurrency=stage2_concurrency,
            stage2_peak_inflight=stage2_peak_inflight,
            deep_acquisition_timeout_count=deep_acquisition_timeout_count,
            deep_source_unavailable_count=deep_source_unavailable_count,
            rule_analyzed_count=rule_analyzed_count,
            rule_exception_count=rule_exception_count,
            stage2_internal_error_count=stage2_internal_error_count,
            evaluated_count=rule_analyzed_count,
            suppressed_count=sum(1 for d in diagnostics if d.status == "SUPPRESSED"),
            evaluated_entries=evaluated_entries,
            review_evidence=review_evidence)
