"""Compact, provider-free nominations. Selection is not investment eligibility."""
from dataclasses import dataclass
from datetime import timedelta
import logging
from uuid import UUID

from app.models import SourceMode, SourceClassification
from app.research_applicability import event_concept
from app.research_readiness import FreshnessPolicyRegistry

logger = logging.getLogger(__name__)
PATHS = ("MARKET_TECHNICAL", "FUNDAMENTAL_CHANGE", "EVENT_CATALYST", "ROTATION_EXPLORATION")
_CATALYST_WINDOW = FreshnessPolicyRegistry.default().get('ORDER_BOOK_CAPEX_GUIDANCE').maximum_age


@dataclass(frozen=True)
class CandidateNomination:
    instrument_id: UUID
    path: str
    reasons: tuple[str, ...]
    score: float | None = None
    evidence_timestamp: str | None = None
    source: str | None = None


def nominate_compact(candidate, *, market_ids, events=(), as_of):
    key = candidate.global_instrument_id
    if not candidate.eligible_for_acquisition:
        return ()
    output = []
    if key in market_ids:
        output.append(CandidateNomination(key, PATHS[0], ("EXISTING_PRELIMINARY_SIGNAL",), candidate.pre_score))
    growth = candidate.dimensions.get("GROWTH_QUALITY")
    if growth and growth.state in {"AVAILABLE", "PARTIAL"} and growth.score is not None:
        output.append(CandidateNomination(key, PATHS[1], ("DURABLE_REVENUE_OR_EARNINGS_CHANGE",), growth.score))
    # Durable authoritative, dated catalyst evidence only. A negative event is
    # also worth investigation; nomination must never imply a buy signal.
    latest = None
    for event in events:
        when = event.event_date or event.published_at
        if (event.source_mode != SourceMode.REAL or event.source_classification != SourceClassification.EXCHANGE
                or str(event.status) == "REJECTED" or not event_concept(str(event.event_type))
                or when is None or when.tzinfo is None or not timedelta(0) <= as_of - when <= _CATALYST_WINDOW):
            continue
        marker = (when, str(event.event_id))
        if latest is None or marker > latest[0]:
            latest = (marker, event)
    if latest:
        event = latest[1]
        output.append(CandidateNomination(key, PATHS[2], (str(event.event_type),),
            evidence_timestamp=latest[0][0].isoformat(), source=event.source_url))
    return tuple(output)


async def discover(candidates, market_pool, *, budget, as_of, repository, run_blocking,
                   rotation_after=None, exclude_ids=(), full_universe=False):
    eligible = {c.global_instrument_id: c for c in candidates
                if c.eligible_for_acquisition and c.global_instrument_id not in exclude_ids}
    market_ids = {c.global_instrument_id for c in market_pool}
    pools = {path: [] for path in PATHS[:-1]}
    # Only O(budget) nominations per path survive. Per-stock events are released
    # before the next stock; never retain raw evidence across the universe.
    def read_nominations(candidate):
        try:
            events = repository.events_for(candidate.global_instrument_id, source_mode=SourceMode.REAL)
        except Exception as exc:
            logger.info('candidate_nomination_unavailable instrument_id=%s path=EVENT_CATALYST reason=%s',
                        candidate.global_instrument_id, type(exc).__name__)
            events = ()
        return nominate_compact(candidate, market_ids=market_ids, events=events, as_of=as_of)
    for candidate in eligible.values():
        for nomination in await run_blocking(read_nominations, candidate):
            pool = pools[nomination.path]
            pool.append(nomination)
            pool.sort(key=lambda n: (-(n.score if n.score is not None else 0),
                                      -(int(n.evidence_timestamp[:10].replace('-', '')) if n.evidence_timestamp else 0),
                                      str(n.instrument_id)))
            del pool[budget:]
    selected = {}
    def add(nomination):
        selected.setdefault(nomination.instrument_id, {})[nomination.path] = nomination
    reserve = min(budget, max(1, budget // 4)) if eligible else 0
    for i in range(budget):
        for path in PATHS[:-1]:
            if i < len(pools[path]) and (len(selected) < budget - reserve or pools[path][i].instrument_id in selected):
                add(pools[path][i])
    ids = sorted(eligible, key=str)
    ordered = [key for key in ids if rotation_after is None or str(key) > rotation_after]
    ordered += [key for key in ids if rotation_after is not None and str(key) <= rotation_after]
    cursor = rotation_after
    explored = 0
    for key in ordered:
        if explored >= reserve and len(selected) >= budget:
            break
        cursor = str(key)
        if key in selected:
            continue  # already receives deep investigation this cycle
        add(CandidateNomination(key, PATHS[3], ("CANONICAL_ROTATION",)))
        explored += 1
        logger.info("rotation_nomination instrument_id=%s reason=CANONICAL_ROTATION", key)
    # The budget chooses priority, not coverage, for the production Radar.
    # Append every remaining eligible identity in deterministic scan order.
    # Keep only compact reasons; event payloads remain scoped to one read.
    if full_universe:
        for key in eligible:
            selected.setdefault(key, {})
    # Recompute nominations so top-K truncation cannot lose a stock's catalyst
    # applicability, including stocks outside the priority prefix.
    for key in selected:
        for nomination in await run_blocking(read_nominations, eligible[key]):
            add(nomination)
    nominations = {str(key): [dict(path=n.path, reasons=list(n.reasons), score=n.score,
        evidence_timestamp=n.evidence_timestamp, source=n.source) for n in values.values()]
        for key, values in selected.items()}
    for key, values in nominations.items():
        logger.info("candidate_nomination instrument_id=%s paths=%s", key, [n['path'] for n in values])
    logger.info("candidate_union selected=%d budget=%d", len(selected), budget)
    return [eligible[key] for key in selected], nominations, cursor
