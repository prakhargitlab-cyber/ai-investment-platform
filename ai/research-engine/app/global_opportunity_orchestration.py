"""Explicit engineering ranking over already-read canonical and persisted evidence.

No HTTP universe adapter, readiness ensure, provider refresh, or membership reads.
Pass PortfolioResearchOrchestrator.register_global_profile_metadata as the
profile_hydrator: this existing synchronous method consumes supplied metadata
only. The repository must already have hydrated its persisted public evidence
(ResearchRepository does this at initialization). Existing V1 cache writes are
preserved; this operation does not persist ranking snapshots.

Use a fixed timezone-aware as_of to reproduce evidence selection/fingerprints.
generated_at is the completion clock; cache_hit diagnostics may change on a warm
run, but ranking content does not. All limits are explicit and at most 100.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping
from uuid import UUID
from pydantic import Field

from app.global_scanner import GlobalScanner
from app.global_opportunity_ranker import GlobalOpportunityRanker
from app.models import ResearchBaseModel
from app.research_readiness_runtime import RepositoryResearchReadinessAdapter, ResearchReadinessRuntime, jurisdiction_for_profile
from app.sector_relative_strength import SectorContext
from app.stock_rule_engine import StockRuleEngineService


class OpportunityEntry(ResearchBaseModel):
    rank: int
    global_instrument_id: UUID
    symbol: str | None
    company_name: str | None
    sector: str | None
    market: str | None
    pre_score: float | None
    stage_b_score: float | None
    technical_score: float | None
    technical_state: str
    sector_score: float | None
    sector_state: str
    rule_engine_score: float | None
    rule_engine_confidence: float
    opportunity_score: float | None
    opportunity_confidence: float
    score_coverage: float
    top_positive_reasons: list[str]
    top_negative_reasons: list[str]
    rule_engine_version: str
    technical_feature_version: str
    sector_feature_version: str
    opportunity_ranker_version: str


class CandidateDiagnostic(ResearchBaseModel):
    global_instrument_id: UUID
    status: str
    failure_reason: str | None = None
    cache_hit: bool | None = None
    rank_eligible: bool = False
    suppression_reasons: list[str] = Field(default_factory=list)


class OpportunityRanking(ResearchBaseModel):
    generated_at: datetime
    as_of: datetime
    universe_count: int
    phase1_eligible_count: int
    stage_b_count: int
    shortlist_count: int
    deep_evaluated_count: int
    rank_eligible_count: int
    top_n: list[OpportunityEntry]
    diagnostics: list[CandidateDiagnostic]


class _PersistedUniverse:
    """The scanner's existing universe protocol, backed solely by supplied rows."""
    def __init__(self, rows):
        self.rows = rows

    async def active_global_equities(self, **unused):
        return self.rows


class GlobalOpportunityOrchestrator:
    def __init__(self, repository, persistence, *, profile_hydrator: Callable[[UUID, dict], bool], clock=None):
        self.repository, self.persistence = repository, persistence
        self.profile_hydrator = profile_hydrator
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.readiness_adapter = RepositoryResearchReadinessAdapter(repository)
        self.readiness = ResearchReadinessRuntime(repository, self.readiness_adapter, executor=None)
        self.rule_engine = StockRuleEngineService(repository, self.readiness_adapter)
        self.ranker = GlobalOpportunityRanker()

    async def run(self, canonical_instruments: Iterable[dict], *, as_of: datetime,
                  sector_contexts: Mapping[UUID, SectorContext] | None = None,
                  shortlist_limit: int = 25, top_n: int = 10) -> OpportunityRanking:
        if (type(shortlist_limit) is not int or not 1 <= shortlist_limit <= 100
                or type(top_n) is not int or not 0 <= top_n <= 100):
            raise ValueError('INVALID_OPPORTUNITY_LIMIT')
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError('AWARE_AS_OF_REQUIRED')
        as_of = as_of.astimezone(timezone.utc)
        rows = deepcopy(list(canonical_instruments))
        # Support both existing canonical enumeration and master-detail shapes.
        for row in rows:
            row.setdefault('exchange', row.get('primaryExchange'))
            row.setdefault('ticker', row.get('primarySymbol'))
        scanner = GlobalScanner(_PersistedUniverse(rows), self.persistence)
        scan = await scanner.scan(as_of=as_of, top_n=0)
        phase1 = {c.global_instrument_id:c for c in scan.candidates if c.eligible_for_deep_analysis}
        stage_b = scanner.enrich_candidates(scan, sector_contexts=dict(sector_contexts or {}))
        def desc(value): return -value if value is not None else float('inf')
        shortlist = sorted((c for c in stage_b if c.global_instrument_id in phase1), key=lambda c: (
            desc(c.stage_b_score), -c.confidence, desc(phase1[c.global_instrument_id].pre_score),
            -phase1[c.global_instrument_id].confidence, str(c.global_instrument_id)))[:shortlist_limit]
        metadata = {str(row.get('globalInstrumentId')):row for row in rows}
        diagnostics, rules, successful = [], {}, []
        evaluated = 0
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
                step = 'RANKER_INPUT_UNAVAILABLE'
                ranked = self.ranker.score(candidate, result)
                diagnostics.append(CandidateDiagnostic(global_instrument_id=key,
                    status='RANK_ELIGIBLE' if ranked.rank_eligible else 'SUPPRESSED', cache_hit=cache_hit,
                    rank_eligible=ranked.rank_eligible, suppression_reasons=ranked.eligibility_reasons))
                if ranked.rank_eligible:
                    rules[key] = result
                    successful.append(candidate)
            except Exception:
                # Never return exception messages, headers, raw evidence or recommendations.
                diagnostics.append(CandidateDiagnostic(global_instrument_id=key, status='FAILED',
                    failure_reason=step, cache_hit=cache_hit))
        ranked = self.ranker.rank(successful, rules)
        by_id = {c.global_instrument_id:c for c in successful}
        entries = []
        for position, result in enumerate(ranked[:top_n], 1):
            key = result.global_instrument_id
            initial, enriched, rule = phase1[key], by_id[key], rules[key]
            entries.append(OpportunityEntry(rank=position, global_instrument_id=key,
                symbol=initial.symbol, company_name=initial.company_name,
                sector=enriched.sector_relative_strength_snapshot.sector, market=initial.market,
                pre_score=initial.pre_score, stage_b_score=enriched.stage_b_score,
                technical_score=result.technical_score, technical_state=result.technical_state,
                sector_score=result.sector_score, sector_state=result.sector_state,
                rule_engine_score=result.rule_engine_score, rule_engine_confidence=rule.confidence_score,
                opportunity_score=result.opportunity_score, opportunity_confidence=result.opportunity_confidence,
                score_coverage=result.score_coverage, top_positive_reasons=result.top_positive_reasons,
                top_negative_reasons=result.top_negative_reasons, rule_engine_version=rule.rule_engine_version,
                technical_feature_version=enriched.technical_feature_snapshot.feature_version,
                sector_feature_version=enriched.sector_relative_strength_snapshot.feature_version,
                opportunity_ranker_version=result.ranker_version))
        return OpportunityRanking(generated_at=self.clock(), as_of=as_of,
            universe_count=scan.total_canonical_active_equities, phase1_eligible_count=len(phase1),
            stage_b_count=len(stage_b), shortlist_count=len(shortlist), deep_evaluated_count=evaluated,
            rank_eligible_count=len(ranked), top_n=entries, diagnostics=diagnostics)
