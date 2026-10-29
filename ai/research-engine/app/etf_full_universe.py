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
from app.etf_acquisition import NSE_ETF_LIST_HEADERS, NSE_ETF_LIST_URL, parse_nse_etf_list
from app.etf_evidence import EtfAuthority, EtfProvenance
from app.global_opportunity_cycle import UniverseUnavailableError
from app.etf_ingestion import ingest_quotes
from app.etf_history import acquire_etf_history
from app.etf_opportunity_cycle import run_etf_radar_cycle, etf_cycle_result_to_dict
from app.etf_screening import screening_evidence, enrich_candidate, research_overview
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
    from app.etf_domain import classify_etf, _CATEGORY_SUBTYPES, _normalized, _BROAD_ASSET_CLASS_TOKENS
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
            # The 'assets' NSE market-watch field is free-text fund
            # description on real records, not a classification signal; only
            # trust it as the broad asset class when it already reads as
            # one, else fall back to mainCatagory when THAT reads as one.
            asset_class = fields[0]
            if (_normalized(asset_class) not in _BROAD_ASSET_CLASS_TOKENS
                    and _normalized(fields[1]) in _BROAD_ASSET_CLASS_TOKENS):
                asset_class = fields[1]
            profile.asset_class = asset_class
            # Verbatim, already-identity-verified official metadata (never a
            # classification guess) -- lets peer grouping compare within the
            # same specific benchmark/category, not just the coarse subtype.
            profile.underlying_index = fields[2]
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
    from app.etf_domain import classify_etf, _normalized
    groups, shortlist = {}, []
    for candidate in candidates:
        key = candidate.get('global_instrument_id')
        profile = profiles.get(key)
        if not profile:
            peer = 'UNCLASSIFIED'
        else:
            subtype = str(classify_etf(profile))
            if subtype == 'OTHER':
                peer = 'UNCLASSIFIED'
            elif profile.asset_class and profile.underlying_index:
                # Finer-grained grouping within a subtype, by the specific
                # verified official benchmark/category name -- never a guess,
                # just the already-verified metadata read verbatim.
                peer = f"{profile.asset_class}:{_normalized(profile.underlying_index)}"
            else:
                peer = subtype
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

    async def reserve_request(self):
        if self.abort:
            raise RuntimeError(self.abort)
        if self.selection['request_reservations'] >= self.selection['limits']['max_requests']:
            self.abort = 'ETF_REQUEST_BUDGET_EXHAUSTED'
            raise RuntimeError(self.abort)
        if self.last is not None:
            await self.sleep(max(0, 1 - (self.monotonic() - self.last)))
        self.selection['request_reservations'] += 1
        await self.checkpoint.save_selection(self.selection)
        self.last = self.monotonic()

    async def get(self, url, **kwargs):
        from app.nse_etf_market import MARKET_URL, META_URL
        if url not in (NSE_ETF_LIST_URL, MARKET_URL, META_URL):
            raise ValueError('UNAPPROVED_ETF_PROVIDER_URL')
        key = (url, json.dumps(kwargs.get('params'), sort_keys=True))
        if key in self.cache:
            return self.cache[key]
        for attempt in range(3):
            await self.reserve_request()  # Lease + cancellation barrier before I/O.
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


def _classify_historical_acquisition(onboard_row):
    """Translate a persisted ETF_ONBOARD checkpoint row into a truthful,
    additive diagnostic for the published candidate -- never into a change
    to the candidate's recommendation or scoring (callers must not use this
    for anything but diagnostics/attribution).

    Distinguishes the five outcomes that were previously conflated by the
    final evaluation pass simply re-reading persisted market data without
    ever looking at *why* ETF_ONBOARD did or didn't produce it:
      * ACQUIRED -- historical acquisition succeeded (fresh fetch or
        sufficient pre-existing persisted history); scoring may use it.
      * IDENTITY_MISMATCH -- Yahoo's returned identity evidence did not
        verify against the canonical instrument (see
        verify_historical_price_identity); this is a correctness rejection,
        not a transient technical problem.
      * PROVIDER_UNAVAILABLE -- a technical failure unrelated to identity
        (timeout, malformed provider response, missing session calendar,
        etc.); retryable in a future cycle.
      * INSUFFICIENT_TRADING_HISTORY -- the provider/identity check
        succeeded but the fund genuinely does not have enough persisted
        trading sessions yet (new listing, thin trading).
      * NOT_ATTEMPTED -- ETF_ONBOARD never ran for this instrument in any
        completed or in-progress cycle (no checkpoint row at all).
    """
    if onboard_row is None:
        return {'status': 'NOT_ATTEMPTED', 'reason': None, 'observations': None}
    payload = onboard_row.get('payload') or {}
    disposition = onboard_row.get('disposition')
    reason = payload.get('reason') or onboard_row.get('failure_reason')
    observations = payload.get('observations')
    if disposition in ('SUCCESS_WITH_DATA', 'SUFFICIENT_EXISTING_HISTORY'):
        status = 'ACQUIRED'
    elif disposition == 'SUCCESS_EMPTY':
        status = 'INSUFFICIENT_TRADING_HISTORY'
    elif disposition == 'TECHNICAL_FAILURE' and str(reason or '').startswith('VERIFIED_HISTORICAL_IDENTITY_MISMATCH'):
        status = 'IDENTITY_MISMATCH'
    elif disposition == 'TECHNICAL_FAILURE':
        status = 'PROVIDER_UNAVAILABLE'
    else:
        status = 'NOT_ATTEMPTED'
    return {'status': status, 'reason': reason, 'observations': observations}


async def _ensure_etf_history(repository, row, *, now, checkpoint, provider=None, before_request=None):
    """Idempotent, incremental ETF history acquisition.

    Reuse completed sessions and fetch only missing ranges. The longest
    return window needs 253 closes, including its starting close.
    """
    from datetime import datetime, time as day_time, timezone, timedelta, date
    from app.market_sessions import completed_sessions
    from app.session_history import missing_history_ranges
    from app.etf_metrics import TRADING_SESSIONS_PER_YEAR
    key = UUID(str(row['globalInstrumentId']))

    def existing_history():
        store = repository.persistence
        return (store.load_daily_market_bars({key}), store.load_market_schedules({'NSE', 'XNSE'}),
                store.load_market_calendar_exceptions({'NSE', 'XNSE'}))

    bars, schedules, exceptions = await repository._run_blocking_persistence(existing_history)
    if not schedules:
        return {'status': 'TECHNICAL_FAILURE', 'provider': None,
                'reason': 'HISTORICAL_SESSION_CALENDAR_UNAVAILABLE', 'observations': 0}
    start_date = (now - timedelta(days=400)).date()
    listing_date = row.get('listingDate')
    if listing_date:
        start_date = max(start_date, date.fromisoformat(str(listing_date)[:10]))
    sessions = dict(completed_sessions('NSE', schedules, exceptions, start_date, now.date(), now))
    symbol = row['primarySymbol']
    present = {b.trading_date for b in bars
               if b.global_instrument_id == key and b.currency == 'INR' and b.source_mode == 'REAL'
               and b.provider in {'NSE', 'YAHOO_FINANCE', 'YAHOO_FINANCE_MCP'}
               and b.provider_symbol in {symbol, symbol + '.NS'} and b.source_url
               and b.close is not None and b.close.is_finite() and b.close > 0
               and b.trading_date in sessions and sessions[b.trading_date] <= b.retrieved_at <= now}
    if len(present) >= TRADING_SESSIONS_PER_YEAR + 1:
        start_date = min(present)
    ranges = missing_history_ranges(present, market='NSE', schedules=schedules,
        exceptions=exceptions, start=start_date, as_of=now)
    if not ranges:
        if len(present) < TRADING_SESSIONS_PER_YEAR + 1:
            return {'status': 'SUCCESS_EMPTY', 'provider': None, 'observations': 0,
                    'reason': 'HISTORICAL_COVERAGE_INSUFFICIENT'}
        return {'status': 'SUFFICIENT_EXISTING_HISTORY', 'provider': None, 'observations': 0}
    written = 0
    for start, end in ranges:
        # The same persisted request ceiling covers Yahoo as well as NSE.
        if before_request is not None:
            await before_request()
        result = await acquire_etf_history(repository, row, now=now, checkpoint=checkpoint, provider=provider,
            start=datetime.combine(start, day_time.min, timezone.utc),
            end=datetime.combine(end, day_time.min, timezone.utc))
        written += result['observations']
        if result['status'] == 'TECHNICAL_FAILURE':
            return {**result, 'observations': written}
    return {**result, 'observations': written,
            'status': 'SUCCESS_WITH_DATA' if written else 'SUCCESS_EMPTY'}


async def run_full_etf_cycle(repository, orchestrator, *, checkpoint, cycle_id, identity_headers,
                             correlation_id=None, limits=None, client=None, clock=None, sleep=asyncio.sleep,
                             history_acquirer=None):
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
    limits = selection['limits']
    if client is None:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as live_client:
            return await run_full_etf_cycle(repository, orchestrator, checkpoint=checkpoint, cycle_id=cycle_id,
                identity_headers=identity_headers, correlation_id=correlation_id, limits=limits,
                client=live_client, clock=clock, sleep=sleep, history_acquirer=history_acquirer)
    transport = BudgetClient(client, checkpoint, selection, sleep=sleep)

    async def body():
        if 'universe' not in selection:
            # Fetch the canonical universe FIRST: if it is empty, unavailable,
            # or malformed there is no point downloading the official NSE
            # security list (and the test harness asserts transport.get is
            # never called in those failure modes).
            try:
                canonical = await orchestrator.active_global_etfs(identity_headers=identity_headers, correlation_id=correlation_id)
            except Exception:
                raise UniverseUnavailableError("canonical ETF universe could not be resolved",
                                                  error_code='ETF_CANONICAL_UNIVERSE_UNAVAILABLE')
            if not canonical:
                raise UniverseUnavailableError("canonical ETF universe resolved no active equities",
                                                  error_code='ETF_CANONICAL_UNIVERSE_EMPTY')
            if not all(isinstance(row, dict) and row.get('globalInstrumentId') for row in canonical):
                raise UniverseUnavailableError("canonical ETF universe was malformed and unverifiable",
                                                  error_code='ETF_CANONICAL_UNIVERSE_UNAVAILABLE')
            # Same browser-identity headers as etf_acquisition.discover_nse --
            # NSE silently stalls (ReadTimeout) this exact URL otherwise; see
            # NSE_ETF_LIST_HEADERS for why.
            response = await transport.get(NSE_ETF_LIST_URL, headers=NSE_ETF_LIST_HEADERS)
            provenance = EtfProvenance(provider='NSE', source_type='NSE_OFFICIAL_ETF_SECURITY_LIST',
                source_identity=NSE_ETF_LIST_URL, source_url=NSE_ETF_LIST_URL, retrieved_at=now(),
                authority=EtfAuthority.OFFICIAL_EXCHANGE, reliability_level='LEVEL_A')
            listings = parse_nse_etf_list(response.text, provenance)
            if not listings or len(listings) > limits['max_universe']:
                raise ValueError('OFFICIAL_UNIVERSE_EMPTY_OR_RESOURCE_LIMIT_EXCEEDED')
            try:
                universe = reconcile(listings, canonical)
            except (TypeError, ValueError):
                raise UniverseUnavailableError("canonical ETF universe was malformed and unverifiable",
                                                  error_code='ETF_CANONICAL_UNIVERSE_UNAVAILABLE')
            if not any(item['canonical'] for item in universe):
                raise UniverseUnavailableError("canonical ETF universe resolved no identity-verified equities",
                                                  error_code='ETF_CANONICAL_UNIVERSE_EMPTY')
            selection.update(universe=universe, official_csv=response.text,
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
        # Incremental, idempotent history acquisition (Workstream 1), run as
        # its own pass over the whole universe -- decoupled from BASELINE's
        # per-batch resume/cancellation handling above. A crash here resumes
        # from wherever ETF_ONBOARD itself left off via its own checkpoint
        # phase, never re-running or interfering with BASELINE's.
        for item in selection['universe']:
            if not item['canonical'] or checkpoint.restorable('ETF_ONBOARD', item['isin']):
                continue
            owned = await checkpoint.run()
            if not owned or owned['status'] != 'RUNNING' or owned.get('owner_id') != checkpoint.owner_id:
                raise CycleOwnershipLost(checkpoint.cycle_id)
            await checkpoint.start('ETF_ONBOARD', item['isin'])
            if history_acquirer is None:
                history_result = await _ensure_etf_history(repository, item['canonical'], now=now(),
                    checkpoint=checkpoint, before_request=transport.reserve_request)
            else:
                history_result = await history_acquirer(repository, item['canonical'], now=now(), checkpoint=checkpoint)
            history_state = (CandidateState.COMPLETED if history_result['status'] in
                              ('SUCCESS_WITH_DATA', 'SUFFICIENT_EXISTING_HISTORY')
                              else CandidateState.EVIDENCE_UNAVAILABLE if history_result['status'] == 'SUCCESS_EMPTY'
                              else CandidateState.RETRYABLE_FAILURE)
            await checkpoint.finish('ETF_ONBOARD', item['isin'], history_state,
                disposition=history_result['status'], payload=history_result)
        # Re-evaluate from current persisted evidence at publication time; old checkpoint scores are not freshened.
        # A BASELINE technical failure (bad quote/market-watch acquisition for
        # THIS instrument) must never be fed into the rule engine as if its
        # evidence were trustworthy, and must never silently become a buy
        # recommendation or a silently-dropped instrument -- it is published
        # as an explicit technical-failure candidate instead (see the
        # publication gate below, which no longer fails the entire 355-ETF
        # cycle over one bad instrument).
        outcome_by_isin = {o['isin']: o.get('outcome') for o in outcomes}
        candidates, profiles = [], {}
        for item in selection['universe']:
            owned = await checkpoint.run()
            if not owned or owned['status'] != 'RUNNING' or owned.get('owner_id') != checkpoint.owner_id:
                raise CycleOwnershipLost(checkpoint.cycle_id)
            row = item['canonical']
            if row and outcome_by_isin.get(item['isin']) == 'FAILED':
                candidates.append({'global_instrument_id': str(row['globalInstrumentId']), 'symbol': item['symbol'],
                    'disposition': 'TECHNICAL_FAILURE', 'reason': 'BASELINE_ACQUISITION_TECHNICAL_FAILURE',
                    'recommendation': None, 'missing_data': [], 'limitations': [],
                    'historical_acquisition': _classify_historical_acquisition(
                        checkpoint.row('ETF_ONBOARD', item['isin']))})
                continue
            if row:
                def evaluate():
                    moment = now()
                    result = run_etf_radar_cycle([row], repository.persistence, now=moment, top_n=None)
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
                    # Wire the existing (previously dead) fund-quality/entry
                    # evidence builders into the actual published cycle so
                    # derived metrics, history and entry evidence reach the UI.
                    candidate.update(screening_evidence(repository.persistence, key, selected, candidate, now=moment))
                    enrich_candidate(candidate, now=moment)
                    return candidate, official_peer_profile(item, selected.get('MARKET_PRICE'))
                candidate, profile = await repository._run_blocking_persistence(evaluate)
                # Additive, backward-compatible diagnostics field: surfaces
                # the ETF_ONBOARD phase's own persisted checkpoint outcome
                # for THIS instrument (keyed by its canonical ISIN, exactly
                # as ETF_ONBOARD itself persists it) so a historical
                # identity-mismatch rejection, a provider/technical failure,
                # and genuinely insufficient trading history are no longer
                # indistinguishable to a reader of the published candidate.
                # checkpoint._progress is an in-memory dict updated
                # synchronously by checkpoint.finish() earlier in this same
                # run_full_etf_cycle() invocation -- no extra I/O, and never
                # consulted by the rule engine/scoring above, which already
                # ran inside evaluate().
                candidate['historical_acquisition'] = _classify_historical_acquisition(
                    checkpoint.row('ETF_ONBOARD', item['isin']))
                candidate['missing_data'] = [f['factor'] for f in (candidate.get('rule_engine_result') or {}).get('factor_results', []) if f['status'] == 'UNAVAILABLE']
                candidate['limitations'] = ['Price-history metrics are not verified total returns.',
                    'Corporate-action adjustment completeness and comparable benchmark returns are unavailable.']
                candidates.append(candidate)
                # No name-based category inference. Unknown classification cannot enter a buy shortlist.
                if profile is not None:
                    profiles[str(row['globalInstrumentId'])] = profile
            else:
                candidates.append({'global_instrument_id': None, 'symbol': item['symbol'],
                    'disposition': 'INELIGIBLE', 'reason': item['identity_status'], 'recommendation': None,
                    'historical_acquisition': _classify_historical_acquisition(
                        checkpoint.row('ETF_ONBOARD', item['isin']))})
        ranked = peer_results(candidates, profiles)
        top_opportunities = research_overview(ranked)
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
            'top_opportunities': top_opportunities,
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
        # A provider outage or wholly unavailable canonical universe cannot
        # replace authoritative results -- but a technical failure on SOME
        # subset of instruments is not a reason to discard every other
        # instrument's verified, ranked result. Those per-instrument failures
        # are already published explicitly above (disposition='TECHNICAL_FAILURE',
        # excluded from ranking, never silently dropped or fabricated as a buy).
        # The cycle only fails wholesale when EVERY reconciled instrument
        # technically failed (counts['failed'] can never exceed counts['reconciled']
        # by construction, since only a reconciled/canonical item can be marked
        # FAILED) -- a genuine universe-wide acquisition outage, not one bad ETF.
        if counts['failed'] >= counts['reconciled']:
            raise ValueError('ETF_UNIVERSE_OR_ACQUISITION_UNAVAILABLE')
        await repository._run_blocking_persistence(repository.persistence.save_etf_radar_cycle,
            cycle_id, 'ETF_RADAR_V1', correlation_id, now(), payload,
            fence=(checkpoint.cycle_id, checkpoint.owner_id))
        return {'cycle_id': str(cycle_id), 'universe_count': len(outcomes), 'coverage': counts}

    # Explicit per-invocation bound; request reservations are cumulative across recovery.
    return await asyncio.wait_for(body(), timeout=limits['max_seconds'])
