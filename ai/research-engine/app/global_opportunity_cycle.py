"""Explicit global job with bounded, injected acquisition before strict ranking."""
from datetime import datetime, timezone
# Bound at import: tests substitute the module-level `datetime` clock.
_parse_datetime = datetime.fromisoformat
from uuid import uuid4, UUID
from copy import deepcopy
from app.global_opportunity_orchestration import GlobalOpportunityOrchestrator
from app.recommendation_engine import RecommendationEngineV1, lifecycle, area_scores, digest


class UniverseUnavailableError(RuntimeError):
    """Raised when a production (candidate_ids=None) opportunity cycle cannot
    assemble a non-empty ranking -- i.e. the canonical NSE universe resolved to
    no eligible equities, or no candidates reached deep analysis and there are
    no prior active recommendations to review.

    The cycle is rejected BEFORE publish_opportunity_cycle so an empty result
    can never overwrite the persisted Global Opportunity Radar. The owning
    OpportunityCycleWorker maps this to error_code='UNIVERSE_UNAVAILABLE'
    (matching the existing UNIVERSE_UNAVAILABLE convention).
    """


# Internal lifecycle actions that map to the four public radar actions.
# Exit signals (EXIT / EXIT_REVIEW) take priority over buy signals so that an
# instrument with a thesis break or invalidation is never surfaced as a buy.
_PUBLIC_BUY_SHORT = frozenset({'BUY'})
_PUBLIC_BUY_LONG = frozenset({'ACCUMULATE', 'TOP_UP'})
_PUBLIC_EXIT_SHORT = frozenset({'EXIT'})
_PUBLIC_EXIT_LONG = frozenset({'EXIT_REVIEW'})
_PUBLIC_PARTIAL_EXIT_SHORT = frozenset({'PARTIAL_EXIT'})
_PUBLIC_PARTIAL_EXIT_LONG = frozenset({'REDUCE'})


def public_action(card):
    """Map internal lifecycle actions to public radar actions.

    Only STRONG_BUY, BUY, PARTIAL_EXIT, SELL are surfaced. HOLD/WAIT/ACCUMULATE/
    ENTRY_APPROACHING/IN_ENTRY_ZONE/etc. are excluded from the public radar.
    Exit signals always take priority over buy signals so that a weakening
    thesis is never surfaced alongside a buy recommendation.
    """
    short = card.get('current_short_action')
    long = card.get('current_long_action')
    new_action = card.get('new_investor_action')
    if short in _PUBLIC_EXIT_SHORT or long in _PUBLIC_EXIT_LONG:
        return 'SELL'
    if short in _PUBLIC_PARTIAL_EXIT_SHORT or long in _PUBLIC_PARTIAL_EXIT_LONG:
        return 'PARTIAL_EXIT'
    if short in _PUBLIC_BUY_SHORT or long in _PUBLIC_BUY_LONG:
        if new_action == 'STRONG_BUY_CANDIDATE':
            return 'STRONG_BUY'
        return 'BUY'
    return None


def public_horizon(card):
    """Determine the primary horizon for a card's public action."""
    short = card.get('current_short_action')
    long = card.get('current_long_action')
    if short in _PUBLIC_EXIT_SHORT or short in _PUBLIC_PARTIAL_EXIT_SHORT or short in _PUBLIC_BUY_SHORT:
        return 'SHORT_TERM'
    if long in _PUBLIC_EXIT_LONG or long in _PUBLIC_PARTIAL_EXIT_LONG or long in _PUBLIC_BUY_LONG:
        return 'LONG_TERM'
    return None


def active_recommendation(state):
    return (state.get('current_short_action') != 'EXIT'
            or state.get('long_term_state') != 'INVALIDATED' and state.get('current_long_action') != 'EXIT')


def nse_equities(rows):
    return [r for r in rows if (r.get('exchange') or r.get('primaryExchange')) == 'NSE'
            and r.get('status') == 'ACTIVE' and r.get('assetType') == 'EQUITY']


def snapshot_from_entry(entry, cycle_id, now):
    s = entry.model_dump(mode='json')
    technical = s['evidence_state'].get('technical', {})
    s.update(snapshot_id=str(uuid4()), cycle_id=cycle_id, market='NSE', generated_at=now,
             scanner_version='GLOBAL_PRE_SCORE_V1', rank_position=s.pop('rank'),
             technical_version=s['technical_feature_version'], sector_version=s['sector_feature_version'],
             ranker_version=s['opportunity_ranker_version'], current_price=technical.get('latest_price'),
             price_as_of=technical.get('history_end'))
    areas = area_scores(s['evidence_state'].get('rule', {}))
    for area in ('valuation', 'quality', 'growth', 'balance_sheet', 'quarterly', 'catalyst', 'news', 'shareholding', 'governance'):
        s[area + '_score'] = areas.get(area.upper())
    s['evidence_state']['as_of'] = now
    return s


def prepare_cycle(persistence, snapshots, *, cycle_id, now, top_n, diagnostics=None, correlation_id=None):
    engine = RecommendationEngineV1()
    old_states = {s['global_instrument_id']: s for s in persistence.recommendation_states()}
    histories = {r['recommendation_id']: r for r in persistence.recommendation_history()}
    new_history, states, cards = [], [], []
    for s in snapshots:
        key = s['global_instrument_id']
        old = old_states.get(key)
        previous = histories.get(old['latest_recommendation_id']) if old else None
        anchor = histories.get(old.get('anchor_recommendation_id')) if old else None
        recommendation = engine.evaluate(s, anchor or previous)
        anchor = anchor or previous or recommendation
        state = lifecycle(anchor, recommendation, s['current_price'], old)
        recommendation['short_term_action'] = state['current_short_action']
        recommendation['long_term_action'] = state['current_long_action']
        recommendation['fingerprint'] = digest({'policy': 'LIFECYCLE_REVIEW_V2',
            'base': recommendation['fingerprint'], 'short': state['current_short_action'], 'long': state['current_long_action']})
        if previous and previous['fingerprint'] == recommendation['fingerprint']:
            latest = previous
        else:
            recommendation.update(recommendation_id=str(uuid4()), snapshot_id=s['snapshot_id'])
            latest = recommendation
            new_history.append(latest)
        state.update(global_instrument_id=key, latest_recommendation_id=latest['recommendation_id'],
            anchor_recommendation_id=(old.get('anchor_recommendation_id') or old['latest_recommendation_id']) if old else latest['recommendation_id'],
            updated_at=now, symbol=s.get('symbol'), company_name=s.get('company_name'))
        states.append(state)
        evidence = s['evidence_state']
        stale = sorted(set(evidence.get('stale_inputs', []) + evidence.get('technical', {}).get('stale_inputs', [])))
        missing = sorted(set(evidence.get('missing_inputs', []) + evidence.get('rule', {}).get('missing_inputs', []) + evidence.get('technical', {}).get('missing_inputs', [])))
        card = {**s, **latest, **state, 'opportunity_score': s['opportunity_score'],
            'confidence': s['opportunity_confidence'], 'coverage': s['score_coverage'],
            'missing_areas': missing, 'stale_areas': stale,
            'data_state': 'STALE' if stale else 'PARTIAL' if missing or s['score_coverage'] < 100 else 'FRESH',
            'short_horizon': '1 week–3 months', 'long_horizon': '6–12 months'}
        card['public_action'] = public_action(card)
        card['horizon'] = public_horizon(card)
        cards.append(card)
    eligible = [c for c in cards if c['rank_eligible']]
    def order(c):
        return (-c['opportunity_score'], -c['opportunity_confidence'], -c['score_coverage'],
                -(c['rule_engine_score'] if c['rule_engine_score'] is not None else -1), c['global_instrument_id'])
    # BUY sections: only actionable STRONG_BUY / BUY public actions surface.
    # Exit signals on either horizon exclude a card from the buy sections.
    # RADAR V2 UI-1: persist ALL qualifying recommendations. The UI display
    # count (first 4) is a presentation concern handled client-side; the
    # backend must not discard otherwise-qualifying results.
    short = sorted([c for c in eligible if c['current_short_action'] == 'BUY' and c['public_action'] in ('STRONG_BUY', 'BUY')], key=order)
    long = sorted([c for c in eligible if c['current_long_action'] in {'ACCUMULATE', 'TOP_UP'} and c['public_action'] in ('STRONG_BUY', 'BUY')], key=order)
    buys = sorted({c['global_instrument_id']: c for c in short + long}.values(), key=order)
    # Exit / profit-booking alerts: PARTIAL_EXIT / SELL from both new discoveries
    # and historical review. Historical review never consumes the new-buy shortlist.
    def exit_urgency(c):
        action = c['public_action']
        score = c.get('opportunity_score') or 0
        # SELL (thesis broken / invalidation) is more urgent than PARTIAL_EXIT (target reached).
        # Within SELL: lower opportunity score = more deterioration = more urgent.
        # Within PARTIAL_EXIT: higher opportunity score = more unrealised gain = more urgent.
        if action == 'SELL':
            return (0, score, c.get('global_instrument_id', ''))
        return (1, -score, c.get('global_instrument_id', ''))
    top_exit = sorted([c for c in cards if c['public_action'] in ('SELL', 'PARTIAL_EXIT')], key=exit_urgency)
    # Unseen prior recommendations remain visible, explicitly marked as not evaluated this cycle.
    unseen = [{**s, 'evaluation_status': 'REVIEW_UNAVAILABLE'} for key, s in old_states.items()
              if key not in {r['global_instrument_id'] for r in states}]
    selection = dict(cycle_id=cycle_id, generated_at=now, market='NSE', top_n=top_n,
        best_buy_today=buys[0] if buys else None, top_short_term=short, top_long_term=long,
        top_exit=top_exit,
        previous_recommendations=[c for c in cards if c['global_instrument_id'] in old_states] + unseen,
        diagnostics=diagnostics or [],
        # Correlation carries the worker job correlation id through the persisted
        # radar payload (global_opportunity_top_selection.payload JSON) so the
        # DI-16 stage logs, the job record, and the published radar share one id
        # with no schema change. None when absent for backward compatibility.
        correlation_id=correlation_id)
    return new_history, states, selection


def _suggestion_cards(selection):
    cards = list(selection.get('top_short_term', [])) + list(selection.get('top_long_term', [])) + list(selection.get('top_exit', []))
    if selection.get('best_buy_today'):
        cards.append(selection['best_buy_today'])
    return cards


async def _complete_published_cycle(repository, checkpoint):
    """Resume a cycle that crashed after its atomic publication: re-run only
    the idempotent suggestion-lifecycle step from the durable selection."""
    selection = await _blocking(repository, repository.persistence.cycle_published_selection, checkpoint.cycle_id)
    if selection is None:
        raise RuntimeError('PUBLISHED_CYCLE_SELECTION_MISSING')
    scan = selection.get('suggestion_scan')
    if scan is not None:
        with repository._persistence_worker_lock:
            repository.persistence.persist_global_suggestion_lifecycle(scan, _suggestion_cards(selection))
    return selection


async def _blocking(repository, operation, *args, **kwargs):
    run_blocking = getattr(repository, '_run_blocking_persistence', None)
    if run_blocking is not None:
        return await run_blocking(operation, *args, **kwargs)
    return operation(*args, **kwargs)


async def run_global_opportunity_cycle(repository, canonical_source, *, top_n=4, shortlist_limit=25,
                                       candidate_ids=None, identity_headers=None, readiness_runtime=None,
                                       correlation_id=None, cycle_id=None, checkpoint=None):
    # cycle_id/checkpoint: a resumable production cycle keeps ONE stable
    # cycle_id across worker restarts (app/cycle_checkpoint.py). Controlled
    # (candidate_ids) cycles never carry a checkpoint.
    # top_n is legacy display metadata (the UI initial page size). It is validated
    # against its previous compatible range and is NEVER used to truncate the
    # qualifying persisted recommendation set: production passes top_n=None to
    # the orchestrator (unbounded ranking) and publication iterates
    # ranking.evaluated_entries (all rank-eligible analyzed candidates).
    if type(top_n) is not int or not 2 <= top_n <= 4:
        raise ValueError('TOP_N_MUST_BE_2_TO_4')
    if checkpoint is not None and candidate_ids is not None:
        raise ValueError('CONTROLLED_CYCLE_NOT_RESUMABLE')
    now = datetime.now(timezone.utc)
    if checkpoint is not None:
        await checkpoint.load()
        run = await checkpoint.run() or {}
        if run.get('status') == 'PUBLISHED':
            return await _complete_published_cycle(repository, checkpoint)
        if run.get('as_of'):
            # A resumed cycle keeps its original evaluation instant.
            now = _parse_datetime(run['as_of'])
        else:
            await _blocking(repository, repository.persistence.update_cycle_run, checkpoint.cycle_id,
                            checkpoint.owner_id, as_of=now.isoformat())
    rows = nse_equities(await canonical_source.active_global_equities(identity_headers=identity_headers))
    if candidate_ids is not None:
        allowed = {str(k) for k in candidate_ids}
        rows = [r for r in rows if str(r['globalInstrumentId']) in allowed]
    ids = {UUID(str(r['globalInstrumentId'])) for r in rows}
    contexts = await canonical_source.sector_benchmark_contexts(ids, identity_headers=identity_headers)
    # Event-loop starvation fix: recommendation_states() is a synchronous
    # persistence read; route it through the repository's existing blocking-
    # persistence offload (_blocking -> repository._run_blocking_persistence)
    # instead of calling it directly on the asyncio event loop.
    previous_states = sorted(await _blocking(repository, repository.persistence.recommendation_states),
                             key=lambda s: (s['updated_at'], s['global_instrument_id']))
    review_ids = [UUID(s['global_instrument_id']) for s in previous_states
                  if UUID(s['global_instrument_id']) in ids and
                  active_recommendation(s)]
    orchestrator = GlobalOpportunityOrchestrator(repository, repository.persistence,
        profile_hydrator=canonical_source.register_global_profile_metadata, readiness_runtime=readiness_runtime)
    ranking = await orchestrator.run(rows, as_of=now, sector_contexts=contexts,
                                     shortlist_limit=shortlist_limit, top_n=None, review_ids=review_ids,
                                     identity_headers=identity_headers, correlation_id=correlation_id,
                                     discovery_v2=readiness_runtime is not None,
                                     rotation_after=(await orchestrator._run_blocking(repository.persistence.opportunity_rotation_after)
                                                     if readiness_runtime is not None else None),
                                     checkpoint=checkpoint)
    cycle_id, timestamp = (checkpoint.cycle_id if checkpoint is not None else str(cycle_id or uuid4())), datetime.now(timezone.utc).isoformat()
    snapshots = [snapshot_from_entry(e, cycle_id, timestamp) for e in ranking.evaluated_entries]
    # A strict analysis failure must not skip an existing position's price lifecycle.
    # Use newly computed persisted technical evidence plus explicitly last-known thesis
    # evidence; suppress all discovery eligibility. No unavailable price is invented.
    if candidate_ids is None:
        done = {s['global_instrument_id'] for s in snapshots}
        histories = {r['recommendation_id']: r for r in repository.persistence.recommendation_history()}
        for state in previous_states:
            key = state['global_instrument_id']
            if key in done or not active_recommendation(state):
                continue
            previous = histories.get(state['latest_recommendation_id'])
            if previous is None:
                continue
            evidence = deepcopy(previous['evidence_snapshot'])
            evidence.update(ranking.review_evidence.get(key, {'technical': {}, 'sector': {}}))
            evidence['stale_inputs'] = sorted(set(evidence.get('stale_inputs', []) + ['THESIS_NOT_REVALIDATED']))
            technical = evidence['technical']
            price = technical.get('latest_price')
            if technical.get('stale_inputs') or technical.get('conflicting_dates'):
                price = None
            snapshots.append(dict(snapshot_id=str(uuid4()), cycle_id=cycle_id, global_instrument_id=key,
                market='NSE', generated_at=timestamp, symbol=state.get('symbol'), company_name=state.get('company_name'),
                current_price=price, price_as_of=technical.get('history_end'), rank_eligible=False,
                suppression_reasons=['REVIEW_EVIDENCE_INSUFFICIENT'], opportunity_score=None,
                opportunity_confidence=0, score_coverage=0, rule_engine_score=None,
                rule_engine_version=previous['rule_engine_version'], ranker_version=previous['ranker_version'],
                top_positive_reasons=[], top_negative_reasons=['REVIEW_EVIDENCE_INSUFFICIENT'], evidence_state=evidence))
    # Empty-universe invariant: a production (candidate_ids=None) cycle is refused
    # BEFORE publish_opportunity_cycle only when the canonical NSE universe itself
    # resolved to no active equities. A valid non-empty universe that produces zero
    # rankable opportunities (no candidates reached deep analysis, or all were
    # suppressed, and there are no prior active recommendations to review) is NOT an
    # unavailable universe -- it falls through below and publishes a real, valid
    # empty COMPLETED radar (generated_at/source_scan_id set, universe_count > 0,
    # best_buy_today=None, top_short_term/top_long_term/top_exit=[]). Controlled/
    # diagnostic cycles (candidate_ids given) are exempt from this check entirely:
    # they never replace the shared radar.
    if candidate_ids is None and ranking.universe_count == 0:
        raise UniverseUnavailableError(
            "empty NSE universe: canonical source resolved no active equities "
            "(candidate_ids=None universe_count=0)")
    def build(persisted):
        history, states, selection = prepare_cycle(repository.persistence, persisted, cycle_id=cycle_id,
            now=timestamp, top_n=top_n, diagnostics=[d.model_dump(mode='json') for d in ranking.diagnostics],
            correlation_id=correlation_id)
        selection['universe_count'] = ranking.universe_count
        selection['status'] = 'COMPLETED'
        selection['controlled_candidate_set'] = candidate_ids is not None
        if readiness_runtime is not None:
            selection['rotation_after'] = ranking.rotation_after
            selection['candidate_nominations'] = ranking.candidate_nominations
            selection['investigation_matrix'] = ranking.investigation_matrix
        selection['suggestion_scan'] = scan
        return history, states, selection
    # V14: the durable global auto-suggestion lifecycle scan record. Built
    # before publication and embedded in the published selection so a cycle
    # that crashes between publication and suggestions can finish idempotently.
    scan = {
        'scan_id': cycle_id,
        'started_at': now.isoformat(),
        'completed_at': timestamp,
        'status': 'COMPLETED',
        'market': 'NSE',
        'exchange': 'NSE',
        'universe_count': int(ranking.universe_count),
        'shortlist_count': int(ranking.shortlist_count),
        'evaluated_count': int(ranking.deep_evaluated_count),
        'rank_eligible_count': int(ranking.rank_eligible_count),
        'suppressed_count': int(len([d for d in ranking.diagnostics if d.status != 'RANK_ELIGIBLE'])),
        'engine_version': ranking.top_n[0].rule_engine_version if ranking.top_n else 'STOCK_RULE_ENGINE_V1',
        'controlled': candidate_ids is not None,
        'failure_reason_code': None,
        'created_at': timestamp,
    }
    # Other research jobs use this lock for the same shared connection. Prevent their
    # commits from publishing a half-built cycle while its projections are being assembled.
    fence = (checkpoint.cycle_id, checkpoint.owner_id) if checkpoint is not None else None
    # Event-loop starvation fix: the complete synchronous publish operation --
    # publish_opportunity_cycle() (which itself synchronously runs build(),
    # i.e. prepare_cycle() and RecommendationEngineV1().evaluate(...), plus its
    # own SQL persistence) and persist_global_suggestion_lifecycle() -- used to
    # execute directly on the asyncio event loop. For a full ~2,580-stock
    # production cycle this synchronous pass could run long enough to starve
    # the event loop (e.g. delaying /health dispatch past the liveness probe's
    # timeoutSeconds). It is now offloaded, as one unit under the same lock and
    # in the same order, through the existing blocking-persistence offload
    # (_blocking -> repository._run_blocking_persistence: a worker thread for
    # the production Postgres adapter, same-thread for the single-thread-affine
    # SQLite adapter used in tests/controlled runs) -- exact same arguments,
    # transaction/lock boundaries, ordering and return value; still awaited
    # here, so the cycle does not return before publication completes, and any
    # exception raised inside still propagates to this awaiter unchanged.
    def _publish():
        with repository._persistence_worker_lock:
            selection = repository.persistence.publish_opportunity_cycle(
                snapshots, [], [], {'cycle_id': cycle_id}, build=build,
                **({'fence': fence} if fence is not None else {}))
            repository.persistence.persist_global_suggestion_lifecycle(scan, _suggestion_cards(selection))
            return selection
    return await _blocking(repository, _publish)
