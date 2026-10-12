"""Explicit full official NSE coverage using the existing ETF worker and evidence writer.

No scheduler or universe mutation. Five is a batch size, never a coverage limit.
"""
import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone
from uuid import UUID

import httpx

from app.cycle_checkpoint import CandidateState, CycleOwnershipLost
from app.etf_acquisition import NSE_ETF_LIST_URL, parse_nse_etf_list
from app.etf_evidence import EtfAuthority, EtfProvenance
from app.etf_ingestion import ingest_quotes
from app.etf_opportunity_cycle import run_etf_radar_cycle, etf_cycle_result_to_dict
from app.nse_historical_daily import verified_identity


def reconcile(listings, canonical):
    """One auditable result per official identity; reject ambiguity, never guess."""
    # Enumeration uses ticker/exchange; point metadata uses primarySymbol/primaryExchange.
    canonical = [{**row, 'primarySymbol': row.get('primarySymbol') or row.get('ticker'),
                  'primaryExchange': row.get('primaryExchange') or row.get('exchange')} for row in canonical]
    symbols = [item.symbol for item in listings]
    if len(set(symbols)) != len(symbols):
        raise ValueError('DUPLICATE_OFFICIAL_SYMBOL')
    ids = [str(row.get('globalInstrumentId')) for row in canonical]
    if len(ids) != len(set(ids)):
        raise ValueError('DUPLICATE_CANONICAL_ID')
    results = []
    for item in sorted(listings, key=lambda x: (x.isin, x.symbol)):
        matches = [r for r in canonical if r.get('isin') == item.isin or r.get('primarySymbol') == item.symbol]
        reason, row = 'CANONICAL_ETF_UNAVAILABLE', None
        if matches:
            reason = 'CANONICAL_IDENTITY_CONFLICT'
            if len(matches) == 1:
                candidate = matches[0]
                try:
                    key = UUID(candidate['globalInstrumentId'])
                    symbol, _ = verified_identity(candidate, key)
                    if (candidate['isin'] == item.isin and symbol == item.symbol
                            and candidate['assetType'] == 'ETF'):
                        row, reason = candidate, 'VERIFIED'
                except (ValueError, KeyError, TypeError):
                    pass
        results.append({'isin': item.isin, 'symbol': item.symbol, 'identity_status': reason, 'canonical': row, 'underlying_reference': item.underlying_reference})
    return results


def official_peer_profile(item, market_fact=None):
    """Classify explicit, identity-verified NSE metadata; never fund names/returns."""
    from app.models import EtfResearchProfile, EtfSubtype
    from app.etf_domain import classify_etf, _CATEGORY_SUBTYPES, _normalized
    from app.nse_etf_market import MARKET_URL, verify_metadata
    row = item.get('canonical')
    if row is None:
        return None
    key = UUID(row['globalInstrumentId'])
    subtype = {'Gold': EtfSubtype.GOLD, 'Silver': EtfSubtype.SILVER}.get(item.get('underlying_reference'), EtfSubtype.OTHER)
    profile = EtfResearchProfile(instrument_id=key, fund_id=key, fund_name=row['canonicalName'],
        ticker=row['primarySymbol'], exchange='NSE', mic='XNSE', isin=row['isin'],
        provider='NSE', currency='INR', subtype=subtype)
    if market_fact is not None:
        p = market_fact.provenance
        if (market_fact.instrument_id != key or p.provider != 'NSE'
                or p.authority != EtfAuthority.OFFICIAL_EXCHANGE or p.source_url != MARKET_URL
                or p.source_type != 'NSE_ETF_MARKET_WATCH'):
            return None
        details = p.source_metadata
        try:
            verify_metadata(details['metadata'], row)
            record = details['record']
            if record['symbol'] != row['primarySymbol']:
                return None
            fields = [record.get(k) for k in ('assets', 'mainCatagory', 'subCatagory')]
            if any(v is not None and not isinstance(v, str) for v in fields):
                return None
            profile.asset_class = fields[0]
            # The existing classifier accepts exact normalized category/strategy
            # vocabulary and rejects conflicting asset families. Unknown values
            # remain OTHER; there is no substring or benchmark-name heuristic.
            categories = {value for field in fields[1:]
                          if (value := _CATEGORY_SUBTYPES.get(_normalized(field))) not in (None, EtfSubtype.OTHER)}
            if len(categories) > 1:
                return None
            category = next(iter(categories)).value if categories else None
            profile.subtype = classify_etf(profile, category=category, investment_strategy=fields[2])
            if profile.subtype == EtfSubtype.OTHER:
                return None
        except (KeyError, TypeError, ValueError):
            return None
    return profile if profile.subtype != EtfSubtype.OTHER else None


def peer_results(candidates, profiles):
    """Rank only within verified profile subtypes. No cross-asset league table."""
    from app.etf_domain import classify_etf
    groups, shortlist = {}, []
    for candidate in candidates:
        key = candidate.get('global_instrument_id')
        profile = profiles.get(key)
        peer = str(classify_etf(profile)) if profile else 'UNCLASSIFIED'
        if peer == 'OTHER':
            peer = 'UNCLASSIFIED'
        candidate['peer_group'] = peer
        candidate['rank'] = None
        if peer == 'UNCLASSIFIED':
            candidate.setdefault('missing_data', []).append('VERIFIED_PEER_CLASSIFICATION')
            if candidate.get('recommendation') in ('OPPORTUNITY', 'STRONG_OPPORTUNITY'):
                candidate['recommendation'] = 'INSUFFICIENT_DATA'
            continue
        if candidate.get('recommendation') in ('OPPORTUNITY', 'STRONG_OPPORTUNITY'):
            groups.setdefault(peer, []).append(candidate)
    for peer in sorted(groups):
        ordered = sorted(groups[peer], key=lambda c: (-(c['rule_engine_result']['overall_score'] or 0), c['global_instrument_id']))
        for rank, candidate in enumerate(ordered, 1):
            candidate['rank'] = rank
            shortlist.append(candidate)
    return shortlist


class BudgetClient:
    """One concurrent fixed-host request, one-second spacing, bounded 5xx retry.

    403/429 stop this run; no cookie workaround, alternate host or Yahoo fallback.
    Budget reservations survive restart; an interrupted reservation is not refunded.
    """
    def __init__(self, client, checkpoint, selection, *, sleep=asyncio.sleep, monotonic=time.monotonic):
        self.client, self.checkpoint, self.selection = client, checkpoint, selection
        self.sleep, self.monotonic = sleep, monotonic
        self.last = None
        self.cache = {}
        self.abort = None

    async def get(self, url, **kwargs):
        from app.nse_etf_market import MARKET_URL, META_URL
        if url not in (NSE_ETF_LIST_URL, MARKET_URL, META_URL):
            raise ValueError('UNAPPROVED_ETF_PROVIDER_URL')
        key = (url, json.dumps(kwargs.get('params'), sort_keys=True))
        if key in self.cache:
            return self.cache[key]
        for attempt in range(3):
            if self.abort:
                raise RuntimeError(self.abort)
            if self.selection['request_reservations'] >= self.selection['limits']['max_requests']:
                self.abort = 'ETF_REQUEST_BUDGET_EXHAUSTED'
                raise RuntimeError(self.abort)
            if self.last is not None:
                await self.sleep(max(0, 1 - (self.monotonic() - self.last)))
            self.selection['request_reservations'] += 1
            await self.checkpoint.save_selection(self.selection)  # Lease + cancellation barrier before I/O.
            self.last = self.monotonic()
            kwargs.update(timeout=15, follow_redirects=False)
            response = await self.client.get(url, **kwargs)
            if response.status_code in (403, 429):
                self.abort = 'NSE_ACCESS_RESTRICTED'
                self.selection['failure_reason'] = self.abort
                await self.checkpoint.save_selection(self.selection)
                response.raise_for_status()
            if response.status_code >= 500 and attempt < 2:
                await self.sleep(2 ** (attempt + 1))
                continue
            response.raise_for_status()
            if len(response.content) > 3_000_000:
                self.abort = 'NSE_RESPONSE_TOO_LARGE'
                raise ValueError(self.abort)
            self.cache[key] = response
            return response


async def run_full_etf_cycle(repository, orchestrator, *, checkpoint, cycle_id, identity_headers,
                             correlation_id=None, limits=None, client=None, clock=None, sleep=asyncio.sleep):
    if checkpoint is None:
        raise ValueError('DURABLE_ETF_CHECKPOINT_REQUIRED')
    now = clock or (lambda: datetime.now(timezone.utc))
    limits = limits or {'max_universe': 2000, 'max_requests': 1500, 'max_seconds': 3600}
    selection = await checkpoint.selection()
    if selection is None:
        selection = {'scope': 'FULL_OFFICIAL_NSE_ETF', 'limits': limits, 'request_reservations': 0,
                     'created_at': now().isoformat()}
        await checkpoint.save_selection(selection)
    if selection.get('failure_reason') == 'NSE_ACCESS_RESTRICTED':
        raise RuntimeError('NSE_ACCESS_RESTRICTED')
    if selection.get('scope') != 'FULL_OFFICIAL_NSE_ETF':
        raise ValueError('ETF_SCOPE_MISMATCH')
    if client is None:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as live_client:
            return await run_full_etf_cycle(repository, orchestrator, checkpoint=checkpoint, cycle_id=cycle_id,
                identity_headers=identity_headers, correlation_id=correlation_id, limits=limits,
                client=live_client, clock=clock, sleep=sleep)
    transport = BudgetClient(client, checkpoint, selection, sleep=sleep)

    async def body():
        if 'universe' not in selection:
            response = await transport.get(NSE_ETF_LIST_URL)
            provenance = EtfProvenance(provider='NSE', source_type='NSE_OFFICIAL_ETF_SECURITY_LIST',
                source_identity=NSE_ETF_LIST_URL, source_url=NSE_ETF_LIST_URL, retrieved_at=now(),
                authority=EtfAuthority.OFFICIAL_EXCHANGE, reliability_level='LEVEL_A')
            listings = parse_nse_etf_list(response.text, provenance)
            if not listings or len(listings) > limits['max_universe']:
                raise ValueError('OFFICIAL_UNIVERSE_EMPTY_OR_RESOURCE_LIMIT_EXCEEDED')
            canonical = await orchestrator.active_global_etfs(identity_headers=identity_headers, correlation_id=correlation_id)
            selection.update(universe=reconcile(listings, canonical), official_csv=response.text,
                official_sha256=hashlib.sha256(response.content).hexdigest(), official_as_of=now().isoformat())
            await checkpoint.save_selection(selection)
        # Frozen official membership on recovery; next cycle discovers a new snapshot.
        transport.cache[(NSE_ETF_LIST_URL, 'null')] = httpx.Response(200, text=selection['official_csv'],
            request=httpx.Request('GET', NSE_ETF_LIST_URL))
        outcomes = []
        for offset in range(0, len(selection['universe']), 5):
            batch = selection['universe'][offset:offset+5]
            pending = [item for item in batch if not checkpoint.restorable('BASELINE', item['isin'])]
            rows = [item['canonical'] for item in pending if item['canonical']]
            for item in pending:
                await checkpoint.start('BASELINE', item['isin'])
            acquired = {}
            if rows:
                data = await ingest_quotes(repository, orchestrator, rows, transport, clock=now,
                    fence=(checkpoint.cycle_id, checkpoint.owner_id))
                acquired = {r['instrument_id']: r for r in data['instruments']}
            for item in batch:
                previous = checkpoint.restorable('BASELINE', item['isin'])
                if previous:
                    outcomes.append(previous['payload'])
                    continue
                row = item['canonical']
                key = str(row['globalInstrumentId']) if row else None
                record = {'isin': item['isin'], 'symbol': item['symbol'], 'global_instrument_id': key,
                          'identity_status': item['identity_status'], 'attempted': bool(row),
                          'acquisition': acquired.get(key), 'outcome': 'REJECTED' if item['identity_status'] == 'CANONICAL_IDENTITY_CONFLICT' else 'UNAVAILABLE'}
                if row:
                    def evaluate():
                        result = run_etf_radar_cycle([row], repository.persistence, now=now(), top_n=None)
                        return etf_cycle_result_to_dict(result)['candidates'][0]
                    record['analysis'] = await repository._run_blocking_persistence(evaluate)
                    record['outcome'] = ('VERIFIED' if acquired.get(key, {}).get('status') == 'READY' else
                        'FAILED' if not acquired.get(key) or any(a['outcome'] == 'TECHNICAL_FAILURE' for a in acquired[key]['attempts']) else 'INCOMPLETE')
                record = json.loads(json.dumps(record, default=str))
                await checkpoint.finish('BASELINE', item['isin'], CandidateState.TERMINAL_OUTCOME,
                    disposition=record['outcome'], payload=record)
                outcomes.append(record)
            if transport.abort:
                raise RuntimeError(transport.abort)
        if len(outcomes) != len(selection['universe']):
            raise RuntimeError('INCOMPLETE_OFFICIAL_COVERAGE')
        # Re-evaluate from current persisted evidence at publication time; old checkpoint scores are not freshened.
        candidates, profiles = [], {}
        for item in selection['universe']:
            owned = await checkpoint.run()
            if not owned or owned['status'] != 'RUNNING' or owned.get('owner_id') != checkpoint.owner_id:
                raise CycleOwnershipLost(checkpoint.cycle_id)
            row = item['canonical']
            if row:
                def evaluate():
                    result = run_etf_radar_cycle([row], repository.persistence, now=now(), top_n=None)
                    candidate = etf_cycle_result_to_dict(result)['candidates'][0]
                    key = UUID(row['globalInstrumentId'])
                    selected = {}
                    for fact in repository.persistence.etf_facts(key):
                        selected.setdefault(str(fact.metric), fact)
                    navs = repository.persistence.etf_navs(key)
                    if navs:
                        selected['NAV'] = navs[0]
                    candidate['source_references'] = [{'metric': metric,
                        'value': str(getattr(fact, 'value', getattr(fact, 'nav', ''))),
                        'as_of': str(getattr(fact, 'as_of_date', getattr(fact, 'nav_date', None))),
                        'provider': fact.provenance.provider, 'url': fact.provenance.source_url}
                        for metric, fact in sorted(selected.items())]
                    return candidate, official_peer_profile(item, selected.get('MARKET_PRICE'))
                candidate, profile = await repository._run_blocking_persistence(evaluate)
                candidate['missing_data'] = [f['factor'] for f in (candidate.get('rule_engine_result') or {}).get('factor_results', []) if f['status'] == 'UNAVAILABLE']
                candidate['limitations'] = ['Price-history metrics are not verified total returns.',
                    'Corporate-action adjustment completeness and comparable benchmark returns are unavailable.']
                candidates.append(candidate)
                # No name-based category inference. Unknown classification cannot enter a buy shortlist.
                if profile is not None:
                    profiles[str(row['globalInstrumentId'])] = profile
            else:
                candidates.append({'global_instrument_id': None, 'symbol': item['symbol'],
                    'disposition': 'INELIGIBLE', 'reason': item['identity_status'], 'recommendation': None})
        ranked = peer_results(candidates, profiles)
        counts = {'official': len(outcomes), 'reconciled': sum(bool(i['canonical']) for i in selection['universe']),
            'activated': sum(bool(i['canonical']) for i in selection['universe']),
            'attempted': sum(o['attempted'] for o in outcomes),
            'acquired': sum(o['outcome'] == 'VERIFIED' for o in outcomes),
            'evaluated': sum(c['disposition'] == 'EVALUATED' for c in candidates),
            'eligible': sum(c.get('recommendation') in ('OPPORTUNITY', 'STRONG_OPPORTUNITY') for c in candidates),
            'rejected': sum(o['outcome'] == 'REJECTED' or o['identity_status'] == 'CANONICAL_IDENTITY_CONFLICT' for o in outcomes),
            'failed': sum(o['outcome'] == 'FAILED' for o in outcomes),
            'acquisition_incomplete': sum(o['outcome'] == 'INCOMPLETE' for o in outcomes),
            'analysis_incomplete': sum(bool(c.get('missing_data')) or c.get('recommendation') == 'INSUFFICIENT_DATA' for c in candidates),
            'unclassified': sum(c.get('peer_group') == 'UNCLASSIFIED' for c in candidates),
            'unavailable': sum(o['outcome'] == 'UNAVAILABLE' for o in outcomes),
            'ranked': len(ranked), 'shortlisted': len(ranked)}
        payload = {'cycle_id': str(cycle_id), 'radar_version': 'ETF_RADAR_V1', 'scope': selection['scope'],
            'as_of': now().isoformat(), 'correlation_id': correlation_id, 'universe_count': len(outcomes),
            'coverage': counts, 'outcomes': outcomes, 'candidates': candidates, 'ranked': ranked,
            'comparison_scope': 'WITHIN_VERIFIED_PEER_ONLY', 'official_sha256': selection['official_sha256'],
            'official_as_of': selection['official_as_of'], 'official_source_url': NSE_ETF_LIST_URL, 'excluded_by_reason': {}}
        for c in candidates:
            reason = None
            if c['disposition'] != 'EVALUATED':
                reason = c['reason']
            elif c.get('peer_group') == 'UNCLASSIFIED':
                reason = 'UNVERIFIED_PEER_CLASSIFICATION'
            elif c.get('recommendation') == 'INSUFFICIENT_DATA':
                reason = 'INSUFFICIENT_ANALYTICAL_DATA'
            elif c.get('recommendation') not in ('OPPORTUNITY', 'STRONG_OPPORTUNITY'):
                reason = 'RISK_GATE_OR_SCORE_EXCLUSION'
            if reason:
                payload['excluded_by_reason'][reason] = payload['excluded_by_reason'].get(reason, 0) + 1
        # A provider outage or wholly unavailable canonical universe cannot replace authoritative results.
        if not counts['reconciled'] or counts['failed']:
            raise ValueError('ETF_UNIVERSE_OR_ACQUISITION_UNAVAILABLE')
        await repository._run_blocking_persistence(repository.persistence.save_etf_radar_cycle,
            cycle_id, 'ETF_RADAR_V1', correlation_id, now(), payload,
            fence=(checkpoint.cycle_id, checkpoint.owner_id))
        return {'cycle_id': str(cycle_id), 'universe_count': len(outcomes), 'coverage': counts}

    # Explicit per-invocation bound; request reservations are cumulative across recovery.
    return await asyncio.wait_for(body(), timeout=limits['max_seconds'])
