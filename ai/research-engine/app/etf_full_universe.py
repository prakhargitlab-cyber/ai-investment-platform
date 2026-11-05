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
from app.etf_screening import screening_evidence, enrich_candidate, research_overview, recommendation_evidence_gaps
from app.nse_historical_daily import verified_identity
from app.portfolio_orchestration import _trusted_nse_provider_mapping
from app.etf_phase_progress import record_etf_phase


def reconcile(listings, canonical):
    """One auditable result per official identity; reject ambiguity, never guess."""
    # Enumeration uses ticker/exchange; point metadata uses primarySymbol/primaryExchange.
    canonical = [{**row,
                  'isin': ''.join(str(row.get('isin') or '').split()).upper(),
                  'primarySymbol': str(row.get('primarySymbol') or row.get('ticker') or '').strip().upper(),
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
                    mappings = [mapping for mapping in candidate.get('providerMappings', [])
                                if isinstance(mapping, dict) and str(mapping.get('provider', '')).upper() == 'NSE']
                    if (len(mappings) != 1 or not _trusted_nse_provider_mapping(mappings[0])
                            or mappings[0].get('active') is False):
                        raise ValueError('NO_TRUSTED_NSE_MAPPING')
                    symbol, currency = verified_identity(candidate, key)
                    if (candidate['isin'] == item.isin and candidate['primarySymbol'] == symbol == item.symbol
                            and currency == 'INR' and candidate.get('currency') == 'INR'
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
            if (not isinstance(record.get('symbol'), str)
                    or record['symbol'].strip().upper() != row['primarySymbol']):
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
                peer = f"{_normalized(profile.asset_class).upper()}:{_normalized(profile.underlying_index)}"
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
    elif disposition == 'TECHNICAL_FAILURE' and str(reason or '').startswith(
            'VERIFIED_HISTORICAL_IDENTITY_MISMATCH:PROVIDER_IDENTITY_UNAVAILABLE'):
        # Yahoo metadata unavailable / Equity Radar retries root cause: this
        # specific reason means the provider returned NO identity evidence
        # at all (a stale/invalid session, not a comparison that actually
        # disagreed) -- the same kind of technical, retryable gap as any
        # other PROVIDER_UNAVAILABLE case, not a proven wrong-company
        # correctness rejection. See verify_identity_by_isin_or_name's
        # docstring in app/structured_market.py.
        status = 'PROVIDER_UNAVAILABLE'
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
    from app.etf_history import ETF_HISTORY_TARGET_DAYS, ETF_HISTORY_WINDOW_DAYS
    key = UUID(str(row['globalInstrumentId']))

    def existing_history():
        store = repository.persistence
        return (store.load_daily_market_bars({key}), store.load_market_schedules({'NSE', 'XNSE'}),
                store.load_market_calendar_exceptions({'NSE', 'XNSE'}))

    bars, schedules, exceptions = await repository._run_blocking_persistence(existing_history)
    if not schedules:
        return {'status': 'TECHNICAL_FAILURE', 'provider': None,
                'reason': 'HISTORICAL_SESSION_CALENDAR_UNAVAILABLE', 'observations': 0}
    start_date = (now - timedelta(days=ETF_HISTORY_TARGET_DAYS)).date()
    listing_date = row.get('listingDate')
    listed = date.fromisoformat(str(listing_date)[:10]) if listing_date else None
    if listed:
        start_date = max(start_date, listed)  # never request sessions before the ETF was listed
    sessions = dict(completed_sessions('NSE', schedules, exceptions, start_date, now.date(), now))
    symbol = row['primarySymbol']
    present = {b.trading_date for b in bars
               if b.global_instrument_id == key and b.currency == 'INR' and b.source_mode == 'REAL'
               and b.provider in {'NSE', 'YAHOO_FINANCE', 'YAHOO_FINANCE_MCP'}
               and b.provider_symbol in {symbol, symbol + '.NS'} and b.source_url
               and b.close is not None and b.close.is_finite() and b.close > 0
               and b.trading_date in sessions and sessions[b.trading_date] <= b.retrieved_at <= now}
    # A leading gap is not re-requested every cycle: without a known listing date the first stored session is the
    # provider's earliest data; with one, only a gap longer than two weeks after listing is still worth a request.
    if len(present) >= TRADING_SESSIONS_PER_YEAR + 1 and (listed is None or min(present) - max(start_date, listed or start_date) <= timedelta(days=14)):
        start_date = max(start_date, min(present))
    ranges = missing_history_ranges(present, market='NSE', schedules=schedules,
        exceptions=exceptions, start=start_date, as_of=now)
    if not ranges:
        if len(present) < TRADING_SESSIONS_PER_YEAR + 1:
            return {'status': 'SUCCESS_EMPTY', 'provider': None, 'observations': 0,
                    'reason': 'HISTORICAL_COVERAGE_INSUFFICIENT'}
        return {'status': 'SUFFICIENT_EXISTING_HISTORY', 'provider': None, 'observations': 0}
    written = 0
    windows = []
    for gap_start, gap_end in ranges:  # bounded provider windows, oldest first, so a failure never hides a leading gap
        while gap_start < gap_end:
            window_end = min(gap_end, gap_start + timedelta(days=ETF_HISTORY_WINDOW_DAYS))
            windows.append((gap_start, window_end))
            gap_start = window_end
    for start, end in windows:
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


async def _acquire_fund_identity(repository, selection, checkpoint, client, now, fetcher):
    """Persist AMC_FUND_HOUSE evidence by exact ISIN from AMFI NAVAll; one request, per-instrument outcomes."""
    import httpx
    from app.etf_amfi_navall import AMFI_NAVALL_URL, AmfiNavAllError, fund_identity_for, parse_amfi_navall
    from app.etf_evidence import EtfAcquisitionAttempt, EtfAcquisitionOutcome, EtfMetric
    items = [i for i in selection['universe'] if i['canonical']]
    retrieved = now()
    try:
        if fetcher is not None:
            text, published = await fetcher()
        else:
            owned = client or httpx.AsyncClient(timeout=60.0, follow_redirects=False)
            try:
                response = await owned.get(AMFI_NAVALL_URL, headers={'Accept': 'text/plain'}, timeout=60.0)
                response.raise_for_status()
                text, published = response.content.decode('utf-8'), None
                modified = response.headers.get('last-modified')
                if modified:
                    from email.utils import parsedate_to_datetime
                    published = parsedate_to_datetime(modified)
            finally:
                if client is None:
                    await owned.aclose()
        index = parse_amfi_navall(text)
        failure = None
    except (CycleOwnershipLost, asyncio.CancelledError):
        raise  # lease loss / cancellation is never a provider failure
    except Exception as error:  # any AMFI-side problem is bounded to this source and never blocks the cycle
        index, published, failure = None, None, f'AMFI_NAVALL_{type(error).__name__}'
    fence = (checkpoint.cycle_id, checkpoint.owner_id)
    for item in items:
        row = item['canonical']
        instrument = UUID(str(row['globalInstrumentId']))
        if index is None:
            evidence, attempt = [], EtfAcquisitionAttempt(
                instrument_id=instrument, metric=EtfMetric.AMC_FUND_HOUSE.value, provider='AMFI', attempted_at=retrieved,
                outcome=EtfAcquisitionOutcome.TECHNICAL_FAILURE, reason=failure)
        else:
            evidence, attempt = fund_identity_for(index, isin=row['isin'], instrument_id=instrument,
                                                  retrieved_at=retrieved, published_at=published)
        await repository._run_blocking_persistence(repository.persistence.save_etf_acquisition, evidence, attempt, fence=fence)
        await record_etf_phase(checkpoint, selection, 'FUNDAMENTAL_ACQUISITION', events=[(item['isin'],
            'SUCCEEDED' if evidence else 'FAILED' if index is None else 'SKIPPED')])
    return index


async def run_full_etf_cycle(repository, orchestrator, *, checkpoint, cycle_id, identity_headers,
                             correlation_id=None, limits=None, client=None, clock=None, sleep=asyncio.sleep,
                             history_acquirer=None, document_acquirer=None, fund_identity_fetcher=None,
                             sbi_ter_fetchers=None):
    if checkpoint is None:
        raise ValueError('DURABLE_ETF_CHECKPOINT_REQUIRED')
    now = clock or (lambda: datetime.now(timezone.utc))
    limits = limits or {'max_universe': 2000, 'max_requests': 3000, 'max_seconds': 7200}
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
                client=live_client, clock=clock, sleep=sleep, history_acquirer=history_acquirer,
                document_acquirer=document_acquirer)
    transport = BudgetClient(client, checkpoint, selection, sleep=sleep)

    async def body():
        frozen_universe = 'universe' in selection
        await record_etf_phase(checkpoint, selection, 'UNIVERSE_DISCOVERY', total=1,
            events=[('official_snapshot', 'REUSED' if frozen_universe else 'IN_PROGRESS')])
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
        if not frozen_universe:
            await record_etf_phase(checkpoint, selection, 'UNIVERSE_DISCOVERY', total=1,
                events=[('official_snapshot', 'SUCCEEDED')])
        await record_etf_phase(checkpoint, selection, 'IDENTITY_VALIDATION', total=len(selection['universe']),
            events=[(i['isin'], 'REUSED' if frozen_universe else 'SUCCEEDED' if i['canonical'] else
                     'FAILED' if i['identity_status'] == 'CANONICAL_IDENTITY_CONFLICT' else 'SKIPPED')
                    for i in selection['universe']])
        await record_etf_phase(checkpoint, selection, 'EVIDENCE_VALIDATION', total=len(selection['universe']))
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
            if pending:
                await record_etf_phase(checkpoint, selection, 'EVIDENCE_VALIDATION',
                    events=[(item['isin'], 'IN_PROGRESS') for item in pending],
                    current_instrument_id=str(rows[0]['globalInstrumentId']) if len(rows) == 1 else None)
            acquired = {}
            if rows:
                data = await ingest_quotes(repository, orchestrator, rows, transport, clock=now,
                    fence=(checkpoint.cycle_id, checkpoint.owner_id))
                acquired = {r['instrument_id']: r for r in data['instruments']}
            for item in batch:
                previous = checkpoint.restorable('BASELINE', item['isin'])
                if previous:
                    outcomes.append(previous['payload'])
                    await record_etf_phase(checkpoint, selection, 'EVIDENCE_VALIDATION',
                        events=[(item['isin'], 'REUSED')])
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
                await record_etf_phase(checkpoint, selection, 'EVIDENCE_VALIDATION',
                    events=[(item['isin'], 'SUCCEEDED' if record['outcome'] == 'VERIFIED' else
                             'FAILED' if record['outcome'] == 'FAILED' else 'SKIPPED')],
                    current_instrument_id=key)
            if transport.abort:
                raise RuntimeError(transport.abort)
        if len(outcomes) != len(selection['universe']):
            raise RuntimeError('INCOMPLETE_OFFICIAL_COVERAGE')
        # Incremental, idempotent history acquisition (Workstream 1), run as
        # its own pass over the whole universe -- decoupled from BASELINE's
        # per-batch resume/cancellation handling above. A crash here resumes
        # from wherever ETF_ONBOARD itself left off via its own checkpoint
        # phase, never re-running or interfering with BASELINE's.
        await record_etf_phase(checkpoint, selection, 'HISTORY_ONBOARDING', total=len(selection['universe']))
        for item in selection['universe']:
            previous = checkpoint.restorable('ETF_ONBOARD', item['isin'])
            if not item['canonical'] or previous:
                await record_etf_phase(checkpoint, selection, 'HISTORY_ONBOARDING',
                    events=[(item['isin'], 'REUSED' if previous else 'SKIPPED')])
                continue
            owned = await checkpoint.run()
            if not owned or owned['status'] != 'RUNNING' or owned.get('owner_id') != checkpoint.owner_id:
                raise CycleOwnershipLost(checkpoint.cycle_id)
            await checkpoint.start('ETF_ONBOARD', item['isin'])
            await record_etf_phase(checkpoint, selection, 'HISTORY_ONBOARDING',
                events=[(item['isin'], 'IN_PROGRESS')], current_instrument_id=str(item['canonical']['globalInstrumentId']))
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
            await record_etf_phase(checkpoint, selection, 'HISTORY_ONBOARDING', events=[(item['isin'],
                {'SUCCESS_WITH_DATA': 'SUCCEEDED', 'SUFFICIENT_EXISTING_HISTORY': 'REUSED',
                 'SUCCESS_EMPTY': 'SKIPPED'}.get(history_result['status'], 'FAILED'))])
        # Optional, strictly approved document acquisition before final scoring.
        # This is deliberately not a discovery crawler or an expanded source
        # allowlist. Resumed cycles skip completed document outcomes stored in
        # the fenced selection, avoiding repeat HTTP requests on recovery.
        # The default reviewed sources require authentication; no network
        # request is made for them, even when the feature flag is enabled.
        document_results = selection.setdefault('document_acquisition', {})
        await record_etf_phase(checkpoint, selection, 'FUNDAMENTAL_ACQUISITION',
            total=len(selection['universe']), events=[(i['isin'], 'REUSED' if i['isin'] in document_results else 'SKIPPED')
                                                     for i in selection['universe']])
        if getattr(getattr(repository, 'settings', None),
                   'research_etf_official_document_fetch_enabled', False):
            from app.etf_official_document_pipeline import EtfOfficialDocumentAcquisitionPipeline
            from app.etf_official_document_sources import (
                AccessTier, default_bounded_registry, bind_approved_sources_to_canonical_universe,
            )
            approved = bind_approved_sources_to_canonical_universe(
                default_bounded_registry(),
                [item['canonical'] for item in selection['universe'] if item['canonical']],
            )
            pipeline = (None if document_acquirer is not None else
                        EtfOfficialDocumentAcquisitionPipeline(approved, repository.settings, clock=now))
            # Only the exact reviewed sources (max two instruments) are attempted.
            for source in approved.all_sources():
                isin = source.isin
                if isin in document_results:
                    continue
                owned = await checkpoint.run()
                if not owned or owned['status'] != 'RUNNING' or owned.get('owner_id') != checkpoint.owner_id:
                    raise CycleOwnershipLost(checkpoint.cycle_id)
                if source.access_tier is not AccessTier.PUBLIC_UNAUTHENTICATED:
                    diagnostic = {'status': 'AUTHENTICATION_REQUIRED_NOT_SUITABLE',
                                  'reason': 'APPROVED_SOURCE_NOT_PUBLIC', 'url': source.document_url}
                else:
                    item = next((item for item in selection['universe']
                                 if item['isin'] == isin and item['canonical']), None)
                    if item is None:
                        continue
                    await record_etf_phase(checkpoint, selection, 'FUNDAMENTAL_ACQUISITION',
                        events=[(isin, 'IN_PROGRESS')], current_instrument_id=str(item['canonical']['globalInstrumentId']))
                    from app.models import EtfResearchProfile
                    row = item['canonical']
                    profile = EtfResearchProfile(
                        instrument_id=UUID(str(row['globalInstrumentId'])),
                        fund_id=UUID(str(row['globalInstrumentId'])),
                        fund_name=row['canonicalName'], ticker=row['primarySymbol'],
                        exchange='NSE', mic='XNSE', isin=row['isin'], currency='INR',
                        known_domains=[source.document_host],
                    )
                    if document_acquirer is None:
                        result = await pipeline.acquire(repository.persistence, profile, source.document_url)
                    else:
                        result = await document_acquirer(repository.persistence, profile, source.document_url)
                    diagnostic = {'status': result.status, 'reason': result.reason, 'url': source.document_url,
                                  'facts': {key: {'outcome': value.get('outcome'),
                                                   'reason': value.get('reason')}
                                            for key, value in (result.facts or {}).items()}}
                document_results[isin] = diagnostic
                await checkpoint.save_selection(selection)
                await record_etf_phase(checkpoint, selection, 'FUNDAMENTAL_ACQUISITION', events=[(isin,
                    'SKIPPED' if source.access_tier is not AccessTier.PUBLIC_UNAUTHENTICATED else
                    'SUCCEEDED' if diagnostic['status'] == 'INGESTED' and any(
                        f.get('outcome') == 'SUCCESS_WITH_DATA' for f in diagnostic.get('facts', {}).values()) else
                    'SKIPPED' if diagnostic['status'] in {'INGESTED', 'DISABLED', 'NOT_APPROVED'} else 'FAILED')])

        # AMFI NAVAll (official industry register): exact-ISIN fund-house evidence and the AMFI scheme code.
        # One bounded GET for the whole universe; any failure is recorded per instrument and never fatal.
        settings_ = getattr(repository, 'settings', None)
        ter_enabled = sbi_ter_fetchers is not None or getattr(settings_, 'research_etf_sbi_ter_enabled', False)
        navall_index = None
        if fund_identity_fetcher is not None or ter_enabled or getattr(settings_, 'research_etf_amfi_navall_enabled', False):
            navall_index = await _acquire_fund_identity(repository, selection, checkpoint, client, now, fund_identity_fetcher)
        # SBI Mutual Fund TER (official disclosure workbook): only for ETFs whose identity is verified in both AMFI
        # files and by an exact AMC-scoped name; every other AMC's TER and all AUM stay unavailable.
        if ter_enabled:
            from app.etf_sbi_ter_acquisition import acquire_sbi_ter
            workbook_fetcher, master_fetcher = sbi_ter_fetchers or (None, None)
            await acquire_sbi_ter(repository, selection, checkpoint, client, now, navall_index,
                                  workbook_fetcher=workbook_fetcher, master_fetcher=master_fetcher)
            await checkpoint.save_selection(selection)

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
        # Scores are re-evaluated from current evidence after resume, so these
        # diagnostics restart with this pass rather than reusing stale scores.
        await record_etf_phase(checkpoint, selection, 'READINESS_EVALUATION', total=len(selection['universe']), reset=True)
        await record_etf_phase(checkpoint, selection, 'SCORING_RANKING', total=len(selection['universe']), reset=True)
        for item in selection['universe']:
            owned = await checkpoint.run()
            if not owned or owned['status'] != 'RUNNING' or owned.get('owner_id') != checkpoint.owner_id:
                raise CycleOwnershipLost(checkpoint.cycle_id)
            row = item['canonical']
            if row:
                await record_etf_phase(checkpoint, selection, 'READINESS_EVALUATION',
                    events=[(item['isin'], 'IN_PROGRESS')], current_instrument_id=str(row['globalInstrumentId']))
            if row and outcome_by_isin.get(item['isin']) == 'FAILED':
                candidates.append({'global_instrument_id': str(row['globalInstrumentId']), 'symbol': item['symbol'],
                    'fund_name': row.get('canonicalName'),
                    'disposition': 'TECHNICAL_FAILURE', 'reason': 'BASELINE_ACQUISITION_TECHNICAL_FAILURE',
                    'recommendation': None,
                    'historical_acquisition': _classify_historical_acquisition(
                        checkpoint.row('ETF_ONBOARD', item['isin']))})
                await record_etf_phase(checkpoint, selection, 'READINESS_EVALUATION', events=[(item['isin'], 'SKIPPED')])
                await record_etf_phase(checkpoint, selection, 'SCORING_RANKING', events=[(item['isin'], 'FAILED')])
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
                await record_etf_phase(checkpoint, selection, 'READINESS_EVALUATION', events=[(item['isin'],
                    'SUCCEEDED' if (candidate.get('research_readiness') or {}).get('status') == 'READY' else 'FAILED')])
                await record_etf_phase(checkpoint, selection, 'SCORING_RANKING', events=[(item['isin'],
                    'SUCCEEDED' if candidate['disposition'] == 'EVALUATED' else
                    'FAILED' if candidate['disposition'] == 'TECHNICAL_FAILURE' else 'SKIPPED')])
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
                candidate['document_acquisition'] = document_results.get(item['isin'])
                candidate['historical_acquisition'] = _classify_historical_acquisition(
                    checkpoint.row('ETF_ONBOARD', item['isin']))
                candidate['missing_data'] = list(dict.fromkeys([*(candidate.get('missing_data') or []),
                    *(f['factor'] for f in (candidate.get('rule_engine_result') or {}).get('factor_results', []) if f['status'] == 'UNAVAILABLE')]))
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
                await record_etf_phase(checkpoint, selection, 'READINESS_EVALUATION', events=[(item['isin'], 'SKIPPED')])
                await record_etf_phase(checkpoint, selection, 'SCORING_RANKING', events=[(item['isin'], 'SKIPPED')])
        ranked = peer_results(candidates, profiles)
        # Peer verification can downgrade a research opportunity; refresh only
        # the presentation after that decision, never the authoritative score.
        for candidate in candidates:
            enrich_candidate(candidate, now=now())
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
            'recommendation_evidence': recommendation_evidence_gaps(candidates),
            'document_acquisition': {isin: result for isin, result in sorted(document_results.items())},
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
        await record_etf_phase(checkpoint, selection, 'PUBLICATION', total=1,
            events=[('result', 'IN_PROGRESS')])
        await repository._run_blocking_persistence(repository.persistence.save_etf_radar_cycle,
            cycle_id, 'ETF_RADAR_V1', correlation_id, now(), payload,
            fence=(checkpoint.cycle_id, checkpoint.owner_id))
        return {'cycle_id': str(cycle_id), 'universe_count': len(outcomes), 'coverage': counts}

    # Explicit per-invocation bound; request reservations are cumulative across recovery.
    try:
        return await asyncio.wait_for(body(), timeout=limits['max_seconds'])
    except (CycleOwnershipLost, asyncio.CancelledError):
        raise
    except Exception:
        # Record only an operation this invocation actually started. A fence
        # loss or cancellation must never become permission to advance it.
        progress = selection.get('etf_phase_progress') or {}
        active_phase = progress.get('current_phase')
        active_items = ((progress.get('phases') or {}).get(active_phase) or {}).get('items') or {}
        failed = [(key, 'FAILED') for key, value in active_items.items() if value.get('state') == 'IN_PROGRESS']
        if failed:
            try:
                await record_etf_phase(checkpoint, selection, active_phase, events=failed)
            except Exception:
                pass  # Diagnostic failure cannot replace the original error.
        raise
