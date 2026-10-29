"""Numerical progress derived exclusively from durable stage checkpoints."""


def stages_for_run(run, selection, baseline_completed, universe_total, deep_completed, deep_total):
    terminal = run.get('status') in {'COMPLETED', 'FAILED', 'CANCELLED'}
    current = selection.get('phase', 'DEEP' if 'deep_ids' in selection else 'BASELINE')
    stages = []

    def add(name, completed, total, *, active=False, failures=None):
        valid = total is not None and completed is not None and 0 <= completed <= total
        done = valid and completed == total
        stages.append(dict(name=name, status='COMPLETED' if done else
                           run.get('status') if terminal and active else 'RUNNING' if active else 'PENDING',
                           completed=completed, total=total,
                           remaining=total-completed if valid else None,
                           percentage=round(100*completed/total, 2) if valid and total > 0 else None,
                           updated_at=run.get('updated_at'), failures=failures))

    onboarding = selection.get('onboarding')
    if onboarding:
        total = onboarding.get('examined')
        remaining = onboarding.get('remaining')
        add('ONBOARDING', total-len(remaining) if total is not None and remaining is not None else None,
            total, active='universe' not in selection, failures=onboarding.get('failed'))
    if run.get('market') == 'ETF':
        # Each terminal baseline row checkpoints acquisition and screening
        # together. Pending rows cannot claim either operation completed.
        add('ACQUISITION', baseline_completed, universe_total, active=current in {'BASELINE', 'ACQUISITION'})
        add('SCREENING', baseline_completed, universe_total, active=current == 'SCREENING')
    else:
        add('BASELINE', baseline_completed, universe_total, active=current == 'BASELINE')
    if run.get('market') != 'ETF':
        add('DEEP_ANALYSIS', deep_completed if 'deep_ids' in selection else None,
            deep_total if 'deep_ids' in selection else None, active=current == 'DEEP')
    for name in ('SCORING', 'RANKING', 'PUBLICATION'):
        checkpoint = selection.get('stage_checkpoints', {}).get(name, {})
        if name == 'PUBLICATION' and run.get('status') == 'COMPLETED':
            checkpoint = {'completed': 1, 'total': 1}
        add(name, checkpoint.get('completed'), checkpoint.get('total'), active=current == name)
    return stages
