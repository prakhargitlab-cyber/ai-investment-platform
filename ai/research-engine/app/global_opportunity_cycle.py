"""Explicit global job. No provider acquisition; canonical catalog reads precede ranking."""
from datetime import datetime, timezone
from uuid import uuid4, UUID
from app.global_opportunity_orchestration import GlobalOpportunityOrchestrator
from app.recommendation_engine import RecommendationEngineV1, lifecycle, area_scores


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


def prepare_cycle(persistence, snapshots, *, cycle_id, now, top_n, diagnostics=None):
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
        cards.append({**s, **latest, **state, 'opportunity_score': s['opportunity_score'],
            'confidence': s['opportunity_confidence'], 'coverage': s['score_coverage'],
            'missing_areas': missing, 'stale_areas': stale,
            'data_state': 'STALE' if stale else 'PARTIAL' if missing or s['score_coverage'] < 100 else 'FRESH',
            'short_horizon': '1 week–3 months', 'long_horizon': '6–12 months'})
    eligible = [c for c in cards if c['rank_eligible']]
    def order(c):
        return (-c['opportunity_score'], -c['opportunity_confidence'], -c['score_coverage'],
                -(c['rule_engine_score'] if c['rule_engine_score'] is not None else -1), c['global_instrument_id'])
    short = sorted([c for c in eligible if c['current_short_action'] == 'BUY'], key=order)[:top_n]
    long = sorted([c for c in eligible if c['current_long_action'] in {'ACCUMULATE', 'TOP_UP'}], key=order)[:top_n]
    buys = sorted({c['global_instrument_id']: c for c in short + long}.values(), key=order)
    # Unseen prior recommendations remain visible, explicitly marked as not evaluated this cycle.
    unseen = [{**s, 'evaluation_status': 'NOT_EVALUATED_THIS_CYCLE'} for key, s in old_states.items()
              if key not in {r['global_instrument_id'] for r in states}]
    selection = dict(cycle_id=cycle_id, generated_at=now, market='NSE', top_n=top_n,
        best_buy_today=buys[0] if buys else None, top_short_term=short, top_long_term=long,
        previous_recommendations=[c for c in cards if c['global_instrument_id'] in old_states] + unseen,
        diagnostics=diagnostics or [])
    return new_history, states, selection


async def run_global_opportunity_cycle(repository, canonical_source, *, top_n=4, shortlist_limit=25,
                                       candidate_ids=None, identity_headers=None):
    if type(top_n) is not int or not 2 <= top_n <= 4:
        raise ValueError('TOP_N_MUST_BE_2_TO_4')
    now = datetime.now(timezone.utc)
    rows = nse_equities(await canonical_source.active_global_equities(identity_headers=identity_headers))
    if candidate_ids is not None:
        allowed = {str(k) for k in candidate_ids}
        rows = [r for r in rows if str(r['globalInstrumentId']) in allowed]
    ids = {UUID(str(r['globalInstrumentId'])) for r in rows}
    contexts = await canonical_source.sector_benchmark_contexts(ids, identity_headers=identity_headers)
    previous_states = sorted(repository.persistence.recommendation_states(),
                             key=lambda s: (s['updated_at'], s['global_instrument_id']))
    review_ids = [UUID(s['global_instrument_id']) for s in previous_states if UUID(s['global_instrument_id']) in ids][:25]
    orchestrator = GlobalOpportunityOrchestrator(repository, repository.persistence,
        profile_hydrator=canonical_source.register_global_profile_metadata)
    ranking = await orchestrator.run(rows, as_of=now, sector_contexts=contexts,
                                     shortlist_limit=shortlist_limit, top_n=100, review_ids=review_ids)
    cycle_id, timestamp = str(uuid4()), datetime.now(timezone.utc).isoformat()
    snapshots = [snapshot_from_entry(e, cycle_id, timestamp) for e in ranking.evaluated_entries]
    def build(persisted):
        history, states, selection = prepare_cycle(repository.persistence, persisted, cycle_id=cycle_id,
            now=timestamp, top_n=top_n, diagnostics=[d.model_dump(mode='json') for d in ranking.diagnostics])
        selection['universe_count'] = ranking.universe_count
        selection['controlled_candidate_set'] = candidate_ids is not None
        return history, states, selection
    # Other research jobs use this lock for the same shared connection. Prevent their
    # commits from publishing a half-built cycle while its projections are being assembled.
    with repository._persistence_worker_lock:
        return repository.persistence.publish_opportunity_cycle(snapshots, [], [], {'cycle_id': cycle_id}, build=build)
