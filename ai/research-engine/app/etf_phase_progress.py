"""ETF-only observed work counters in the existing fenced selection JSON."""
from copy import deepcopy

PHASES = ('UNIVERSE_DISCOVERY', 'IDENTITY_VALIDATION', 'HISTORY_ONBOARDING',
          'FUNDAMENTAL_ACQUISITION', 'EVIDENCE_VALIDATION', 'READINESS_EVALUATION',
          'SCORING_RANKING', 'PUBLICATION')
STATES = {'IN_PROGRESS', 'SUCCEEDED', 'FAILED', 'SKIPPED', 'REUSED'}


async def record_etf_phase(checkpoint, selection, phase, *, total=None, events=(),
                           current_instrument_id=None, reset=False):
    """Commit metadata before changing caller memory; ownership errors propagate.

    Events are (canonical ID or source unit ID, state). Retried starts increase
    attempt_count; terminal counters count units, not attempts. No evidence,
    credentials or candidate scores belong in this compact diagnostic state.
    """
    if phase not in PHASES or total is not None and (type(total) is not int or total < 0):
        raise ValueError('INVALID_ETF_PHASE')
    proposed = deepcopy(selection)
    progress = proposed.setdefault('etf_phase_progress', {'version': 'ETF_PHASE_PROGRESS_V1', 'phases': {}})
    row = progress['phases'].setdefault(phase, {'total': total, 'items': {}})
    if reset:
        row = progress['phases'][phase] = {'total': total, 'items': {}}
    if total is not None:
        row['total'] = total
    for key, state in events:
        if not isinstance(key, str) or not key or state not in STATES:
            raise ValueError('INVALID_ETF_PHASE_EVENT')
        item = row['items'].setdefault(key, {'attempts': 0, 'failed_attempts': 0})
        if state == 'IN_PROGRESS':
            item['attempts'] += 1
        elif state in {'SUCCEEDED', 'FAILED'} and item['attempts'] == 0:
            item['attempts'] = 1
        if state == 'FAILED' and item.get('state') != 'FAILED':
            item['failed_attempts'] += 1
        item['state'] = state
    if row['total'] is not None and len(row['items']) > row['total']:
        raise ValueError('ETF_PHASE_TOTAL_EXCEEDED')
    progress.update(current_phase=phase, current_instrument_id=current_instrument_id)
    await checkpoint.save_selection(proposed)
    # Preserve references held by the budget/document stages to other nested
    # selection dictionaries; only this metadata component changed.
    selection['etf_phase_progress'] = progress


def etf_phase_progress(run, baseline_rows=None, onboarding_rows=None):
    """Unknown legacy phases stay unknown; terminal status does not invent work."""
    recorded = (run.get('selection') or {}).get('etf_phase_progress') or {}
    phases = []
    for index, name in enumerate(PHASES, 1):
        row = deepcopy((recorded.get('phases') or {}).get(name))
        if row is not None and name == 'PUBLICATION' and run.get('status') in {'PUBLISHED', 'COMPLETED'}:
            # save_etf_radar_cycle atomically fences publication and changes
            # RUNNING -> PUBLISHED. Do not attempt a selection write afterward.
            if row.get('items', {}).get('result', {}).get('state') == 'IN_PROGRESS':
                row['items']['result']['state'] = 'SUCCEEDED'
        values = {'phase': name, 'number': index, 'observed': row is not None,
                  'unit': 'SOURCE_SNAPSHOTS' if name == 'UNIVERSE_DISCOVERY' else
                          'RESULT_PAYLOADS' if name == 'PUBLICATION' else 'INSTRUMENTS'}
        fields = ('total', 'pending', 'attempted', 'attempt_count', 'succeeded', 'failed',
                  'failed_attempts', 'skipped', 'reused', 'remaining', 'in_progress')
        if row is None:
            values.update({field: None for field in fields})
        else:
            items = list(row['items'].values())
            count = lambda state: sum(item['state'] == state for item in items)
            terminal = sum(count(state) for state in ('SUCCEEDED', 'FAILED', 'SKIPPED', 'REUSED'))
            total = row.get('total')
            values.update(total=total, attempted=sum(item['attempts'] > 0 for item in items),
                attempt_count=sum(item['attempts'] for item in items), succeeded=count('SUCCEEDED'),
                failed=count('FAILED'), failed_attempts=sum(item['failed_attempts'] for item in items),
                skipped=count('SKIPPED'), reused=count('REUSED'), in_progress=count('IN_PROGRESS'),
                remaining=None if total is None else total-terminal,
                pending=None if total is None else total-len(items))
        phases.append(values)
    return {'version': 'ETF_PHASE_PROGRESS_V1', 'current_phase': recorded.get('current_phase'),
            'current_instrument_id': recorded.get('current_instrument_id'), 'phases': phases,
            'overall_percentage': None}
