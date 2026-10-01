"""Explicit bounded news worker. Never imported/called by ranking or GET flows."""
import asyncio
from datetime import datetime, timezone, timedelta
from uuid import NAMESPACE_URL, uuid5
from urllib.parse import urlparse
from app.business_exposure import BusinessEvidence, SourceReference, extract_profile, query_plan
from app.news_intelligence import ProviderOutcome, aggregate_search, extract_impacts
from app.source_discovery import SearchDateWindow, CandidateSearchResult, _candidate_rank, classify_source, reliability_for_classification, source_type_for_classification
from app.normalization import canonicalize_url, content_hash, extract_text, extract_published_at
from app.models import ResearchDocument, SourceMode
from app.source_discovery import SearchProviderError


_WRAPPED_PREFIX='SEARCH_PROVIDER_UNAVAILABLE:'
# Guardian Review Issue 5 -- see the bounded-degraded-provider comment
# in acquire_news() below.
_MAX_CONSECUTIVE_DEGRADED_QUERIES = 3


def _query_failure_code(exc):
    """Stable, secret-free technical code for one failed query.

    Provider errors already carry fixed codes built by the adapter (never raw
    response text or credentials); other exceptions contribute only their
    class name. The redundant SearXNG wrapper prefix is removed so specific
    codes (RATE_LIMITED, TIMEOUT, DEGRADED) stay classifiable."""
    if isinstance(exc, SearchProviderError):
        code=str(exc).strip() or 'SEARCH_PROVIDER_UNAVAILABLE'
        if code.startswith(_WRAPPED_PREFIX) and code[len(_WRAPPED_PREFIX):].startswith('SEARCH_PROVIDER_'):
            code=code[len(_WRAPPED_PREFIX):]
        return code
    return f'SEARCH_QUERY_FAILED:{type(exc).__name__}'


def _join_codes(*codes):
    values=sorted({part for code in codes if code for part in str(code).split('|') if part})
    return '|'.join(values) or None


def persisted_yahoo_discovery(repository, company, now):
    """Reuse an explicitly recorded Yahoo news outcome; never infer search
    success from an empty general-purpose quote snapshot. One company-news
    check is supplemental to, and cannot replace, the web query plan.
    """
    loader=getattr(repository,'acquisition_observations_for',None)
    checks=[]
    for row in loader(company.instrument_id) if callable(loader) else []:
        if row.get('requirement_id')!='CURRENT_NEWS' or row.get('provider')!='YAHOO_FINANCE_MCP': continue
        stamp=row.get('observed_at')
        if isinstance(stamp,str): stamp=datetime.fromisoformat(stamp.replace('Z','+00:00'))
        if stamp and stamp.tzinfo and timedelta(0)<=now-stamp<=timedelta(days=1): checks.append((stamp,row))
    if not checks: return [],[]
    stamp,row=max(checks,key=lambda item:item[0])
    candidates=[]
    success=row.get('outcome') in {'SUCCESS','SUCCESS_EMPTY'}
    if row.get('outcome')=='SUCCESS':
        for doc in repository.documents_for(company.instrument_id,source_mode=SourceMode.REAL):
            if doc.discovery_provider=='YAHOO_FINANCE_MCP' and doc.retrieved_at<=stamp:
                candidates.append(CandidateSearchResult(doc.title,doc.canonical_url,'',doc.discovered_at or doc.retrieved_at,
                    'YAHOO_FINANCE_MCP','persisted-company-news','persisted company news','CURRENT_NEWS'))
    state=('SUCCESS_WITH_RESULTS' if candidates else 'SUCCESS_EMPTY') if success else 'FAILED'
    if row.get('outcome')=='SUCCESS' and not candidates: state='PARTIAL'
    return [ProviderOutcome(provider='YAHOO_FINANCE_MCP',outcome=state,candidate_count=len(candidates),
        queries_planned=1,queries_completed=int(success),failure_code=None if success else 'PERSISTED_YAHOO_SEARCH_FAILED')],candidates

# Only literal exchange (NSE) corporate-announcement documents are reused as
# CURRENT_NEWS candidates here -- not every OFFICIAL_COMPANY document another
# requirement persisted, which may exist for unrelated purposes (e.g. a
# financial-facts PDF) and would otherwise be double-purposed as "news"
# without ever having been selected as a news candidate.
_OFFICIAL_NEWS_CLASSIFICATIONS = {'EXCHANGE'}


def persisted_official_discovery(repository, company):
    """Reuse already-persisted NSE corporate-announcement documents as
    CURRENT_NEWS candidates before any generic search runs.

    These documents were already fetched and persisted by
    OfficialFilingDiscovery for other mandatory requirements (financial
    results, shareholding, order book/capex/guidance, governance). No new
    network call is made here: this only looks at what is already on file
    for this instrument, so an authoritative, CAPTCHA-free exchange
    disclosure can satisfy company-news evidence -- and, because the
    downstream fetch loop reuses a persisted document by canonical URL
    instead of re-fetching it, the same announcement is never downloaded
    twice for the sake of CURRENT_NEWS.

    Returns ([], []) when no such document exists, mirroring
    persisted_yahoo_discovery: a provider outcome with zero planned queries
    would corrupt aggregate_search's completeness classification, so nothing
    is contributed rather than a hollow SUCCESS_EMPTY outcome.
    """
    candidates = []
    for doc in repository.documents_for(company.instrument_id, source_mode=SourceMode.REAL):
        if doc.source_classification not in _OFFICIAL_NEWS_CLASSIFICATIONS:
            continue
        if not doc.normalized_text:
            continue
        candidates.append(CandidateSearchResult(
            doc.title or '', doc.canonical_url, '', doc.discovered_at or doc.retrieved_at,
            doc.discovery_provider or 'NSE_OFFICIAL_API', 'persisted-official-filing',
            'persisted official filing', 'CURRENT_NEWS'))
    if not candidates:
        return [], []
    outcome = ProviderOutcome(provider='OFFICIAL_FILING_REUSE', outcome='SUCCESS_WITH_RESULTS',
        candidate_count=len(candidates), queries_planned=1, queries_completed=1, failure_code=None)
    return [outcome], candidates


def persisted_business_evidence(repository, company):
    evidence=[]
    for doc in repository.documents_for(company.instrument_id,source_mode=SourceMode.REAL):
        if doc.normalized_text and doc.source_classification in {'OFFICIAL_COMPANY','EXCHANGE','REGULATORY','COMPANY_FILING'}:
            evidence.append(BusinessEvidence(instrument_id=company.instrument_id,text=doc.normalized_text,
                source=SourceReference(document_id=doc.document_id,url=doc.canonical_url,classification=doc.source_classification,
                    publication_time=doc.published_at,retrieved_at=doc.retrieved_at,confidence=.95)))
    snapshots=repository.structured_market_snapshots_for({company.instrument_id}).get(company.instrument_id,[])
    for record in snapshots:
        description=record.snapshot.facts.get('businessSummary')
        if description and description.value and record.provider in {'YAHOO_FINANCE','NSE','BSE'}:
            evidence.append(BusinessEvidence(instrument_id=company.instrument_id,text=str(description.value),
                source=SourceReference(url=description.source_url,classification='STRUCTURED_MARKET_PROVIDER',
                    publication_time=description.published_at,retrieved_at=description.retrieved_at,confidence=description.confidence or 0)))
    return evidence

async def acquire_news(repository, company, *, providers, industry=None, now=None, max_queries=14, max_documents=6, sleep=asyncio.sleep):
    """One company; injected existing search adapters and existing safe fetcher.

    A document budget exhaustion or failed fetch makes the run PARTIAL; it can
    never certify no-events. Empty results certify only the configured plan.
    """
    if not 1<=len(providers)<=3 or not 1<=max_queries<=20 or not 1<=max_documents<=20: raise ValueError('INVALID_NEWS_BUDGET')
    started=now or datetime.now(timezone.utc)
    if industry is None:
        records=repository.structured_market_snapshots_for({company.instrument_id}).get(company.instrument_id,[])
        for record in sorted(records,key=lambda r:r.retrieved_at,reverse=True):
            fact=record.snapshot.facts.get('industry')
            if fact and fact.value and fact.retrieved_at<=started:
                industry=str(fact.value); break
    exposure=extract_profile(company.instrument_id,company.company_name,company.ticker,
        persisted_business_evidence(repository,company),now=started,industry=industry)
    exposure=await repository.append_news_record(exposure)
    plan=query_plan(exposure)
    # Fair tier allocation under the platform's smaller operational budget.
    buckets=[[q for q in plan if q[0]==tier] for tier in (1,2,3,4)]
    plan=[b[i] for i in range(max(map(len,buckets),default=0)) for b in buckets if i<len(b)][:max_queries]
    outcomes,supplemental=persisted_yahoo_discovery(repository,company,started)
    official_outcomes,official_supplemental=persisted_official_discovery(repository,company)
    outcomes=outcomes+official_outcomes
    candidates={canonicalize_url(row.url):row for row in supplemental}
    for row in official_supplemental: candidates.setdefault(canonicalize_url(row.url),row)
    for provider in sorted(providers,key=lambda p:p.provider_name):
        if provider.provider_name.lower() == 'disabled':
            outcomes.append(ProviderOutcome(provider=provider.provider_name,outcome='FAILED',candidate_count=0,
                queries_planned=len(plan),queries_completed=0,failure_code='SEARCH_PROVIDER_DISABLED'))
            continue
        # Provider outcome is decided by query-plan completion: a query that
        # returned usable results is complete even if some underlying
        # meta-search engines were unresponsive (kept as a diagnostic). A query
        # that raised, or returned nothing while engines were unresponsive
        # (CAPTCHA / 429 / outage), is a failed query -- never a valid empty.
        #
        # Guardian Review (STAGE2_LIVE_RUN_DEFECTS_20260930.md, Issue 5):
        # bounded degraded-provider work. Each explicit_queries call bypasses
        # SearxngSearchDiscoveryProvider's own cross-call backoff on purpose
        # (the CURRENT_NEWS completion-semantics fix), which is correct for a
        # provider that is merely slow on ONE query -- but it also means
        # nothing here stopped issuing all `max_queries` (up to 14) queries
        # to a provider that has been degraded for every single one of them
        # THIS call. _MAX_CONSECUTIVE_DEGRADED_QUERIES is a per-call-only
        # circuit breaker: after that many CONSECUTIVE degraded/empty
        # responses from the SAME provider in the SAME acquire_news() call,
        # remaining queries to it are skipped for this call. This never
        # touches candidate ranking or which queries get issued -- tier
        # fairness (buckets/plan above) is unchanged for every query that DID
        # run -- and it resets on the very next call (no persisted state), so
        # a future freshness retry is unaffected. The run is still marked
        # PARTIAL/retryable (not SUCCESS), so this never fabricates success.
        completed=0; count=0; degraded_queries=0; codes=[]; consecutive_degraded=0
        for query_index,(_,query) in enumerate(plan):
            try:
                rows=await provider.discover(company,'CURRENT_NEWS',SearchDateWindow(query_limit=1,explicit_queries=(query,)))
            except Exception as exc:
                codes.append(_query_failure_code(exc))
                consecutive_degraded=0
            else:
                degraded=bool(getattr(provider,'last_query_degraded',False))
                if degraded and not rows:
                    codes.append('SEARCH_PROVIDER_DEGRADED')
                    consecutive_degraded+=1
                else:
                    completed+=1; count+=len(rows); degraded_queries+=int(degraded)
                    consecutive_degraded=0
                    for row in rows: candidates.setdefault(canonicalize_url(row.url),row)
            if consecutive_degraded>=_MAX_CONSECUTIVE_DEGRADED_QUERIES and query_index+1<len(plan):
                # Bounded degraded-provider work: stop issuing further queries
                # to a provider that has been degraded/empty for this many
                # CONSECUTIVE queries in THIS call. Deliberately does not
                # append a distinct marker code here -- 'SEARCH_PROVIDER_DEGRADED'
                # is already recorded per degraded query above and _join_codes
                # dedupes identical codes, so the provider's failure
                # classification (FAILED/PARTIAL + SEARCH_PROVIDER_DEGRADED) is
                # unchanged by whether the run stopped here or continued
                # through every remaining query -- only the amount of wasted
                # work against an already-proven-unresponsive provider changes.
                break
            await sleep(repository.settings.market_data_population_request_interval_seconds)
        failed=bool(codes)
        state=('PARTIAL' if failed else 'SUCCESS_WITH_RESULTS' if count else 'SUCCESS_EMPTY') if completed else 'FAILED'
        outcomes.append(ProviderOutcome(provider=provider.provider_name,outcome=state,candidate_count=count,
            queries_planned=len(plan),queries_completed=completed,failure_code=_join_codes(*codes),
            degraded_queries=degraded_queries))
    features=[]; document_codes=[]
    documents_fetched = 0          # real network fetch attempts (excludes persisted-document reuse)
    usable_documents = 0           # documents that yielded company-relevant CURRENT_NEWS evidence
    # Rank candidates by relevance + recency (exchange/regulatory authority,
    # filing/ownership relevance, freshness, HTML-over-PDF) rather than by URL,
    # so the most usable CURRENT_NEWS candidate is fetched first. `max_documents`
    # bounds *usable* evidence, while a separate, strictly-bounded attempt
    # allowance (`max_attempts`) lets a failing / oversized / unextractable
    # candidate fall through to the next ranked candidate without permanently
    # consuming a usable slot. Canonical-equivalent URLs are already collapsed in
    # `candidates`, so each URL is attempted at most once. The per-item try/except
    # continues the run instead of aborting the batch.
    ranked_candidates = sorted(candidates.items(), key=lambda kv: _candidate_rank(company, kv[1]), reverse=True)
    max_attempts = min(len(ranked_candidates), max_documents * 2)
    for url,candidate in ranked_candidates:
        if usable_documents >= max_documents:
            break
        if documents_fetched >= max_attempts:
            break
        stage='DOCUMENT_FETCH_FAILED'
        try:
            existing=next((d for d in repository.documents_for(company.instrument_id) if d.canonical_url==url),None)
            if existing:
                document=existing
            else:
                documents_fetched += 1
                fetched=await repository._fetcher.fetch(url)  # Existing redirects/SSRF/robots/size policy.
                stage='EXTRACTION_FAILED'
                title,body=extract_text(fetched.text,fetched.content_type)
                if not body: raise ValueError('EMPTY_DOCUMENT')
                classification=classify_source(urlparse(fetched.final_url).hostname or '',company,candidate)
                stamp=now or datetime.now(timezone.utc)
                document=ResearchDocument(document_id=uuid5(NAMESPACE_URL,url),canonical_url=url,original_url=url,
                    title=title or candidate.title,source_type=source_type_for_classification(classification),source_classification=classification,
                    source_name=candidate.provider,content_type=fetched.content_type,document_type='HTML',
                    normalized_text=body,content_hash=content_hash(body),instrument_id=company.instrument_id,company_id=company.company_id,
                    reliability_level=reliability_for_classification(classification),source_mode='REAL',status='PARSED',
                    retrieved_at=stamp,discovered_at=candidate.discovered_at,
                    published_at=extract_published_at(fetched.text),discovery_provider=candidate.provider)
                stage='DOCUMENT_PERSIST_FAILED'
                await repository._run_blocking_persistence(repository._persistence.upsert_document,document)
                repository.remember_persisted_document(document)
            stage='EXTRACTION_FAILED'
            if not document.normalized_text: raise ValueError('DOCUMENT_BODY_UNAVAILABLE')
            extracted=extract_impacts(exposure,document,now=now or datetime.now(timezone.utc))
            if not extracted:
                # Usable fetch but no company-relevant event: does not consume a
                # usable slot; continue to the next ranked candidate.
                continue
            usable_documents += 1
            for feature in extracted:
                features.append(await repository.append_news_record(feature))
        except Exception:
            # Attempted but failed: consumes an attempt, not a usable slot;
            # recorded as a diagnostic so a genuinely empty run is retryable,
            # but never used to certify no-events when usable evidence was obtained.
            document_codes.append(stage)
    # Incompleteness is "technical document failures OR candidate-pool exhaustion
    # occurred AND zero usable CURRENT_NEWS evidence was obtained" -- i.e. a real
    # attempt was made but produced no usable evidence. Crucially, a run that DID
    # obtain usable evidence is never forced partial, even if some candidates
    # failed or the candidate pool exceeded max_documents. Two things are left
    # untouched: (a) a completed search with zero candidates (SUCCESS_EMPTY)
    # certifies explicit zero-result coverage (COMPLETE_NO_EVENTS); and (b) a
    # completed search whose fetched documents simply found no company news (no
    # technical failure, candidate pool within max_documents) is also
    # COMPLETE_NO_EVENTS. Only a technical failure (fetch/extraction/persistence)
    # that consumed a bounded attempt without producing usable evidence -- or a
    # candidate pool that exhausted max_documents with zero usable evidence --
    # becomes PARTIAL and carries the specific retryable reason plus
    # DOCUMENT_BUDGET_EXHAUSTED. This never certifies no-events when usable
    # evidence was in fact obtained, and never discards successful results merely
    # because the candidate pool exceeded max_documents (no SUCCESS_EMPTY
    # conversion).
    incomplete = (bool(document_codes) or len(ranked_candidates) > max_documents) and usable_documents == 0
    if incomplete:
        document_codes.append('DOCUMENT_BUDGET_EXHAUSTED')
        outcomes=[p.model_copy(update={'outcome':'PARTIAL','failure_code':_join_codes(p.failure_code,*document_codes)})
                  if p.outcome.startswith('SUCCESS') else p for p in outcomes]
    run=aggregate_search(company.instrument_id,outcomes,started_at=started,completed_at=now or datetime.now(timezone.utc),
        qualifying_events=len({f.event_key for f in features}),query_plan=plan)
    await repository.append_news_record(run)
    # FIX B: bridge real CURRENT_NEWS activity into the deep-investigation
    # RequirementAcquisitionBudget. Without this, an attempted news search used a
    # separate internal budget and left discovery_attempted/queries_reserved/
    # documents_attempted at zero, so deep_investigation mis-classified a real
    # attempt as ACQUISITION_NOT_DUE. Only the CURRENT_NEWS requirement owns a
    # per-requirement budget; ordinary (no-budget) callers are untouched.
    from app.deep_investigation import acquisition_budget
    _news_budget = acquisition_budget(company.instrument_id)
    if _news_budget is not None and _news_budget.requirement_id == "CURRENT_NEWS":
        _news_budget.discovery_attempted = True
        _completed_queries = sum(getattr(p, "queries_completed", 0) for p in outcomes)
        if _completed_queries:
            _news_budget.reserve_queries(_completed_queries)
        _news_budget.documents_attempted = min(
            _news_budget.documents_attempted + documents_fetched,
            _news_budget.max_documents,
        )
        if run.outcome in ("SEARCH_PARTIAL", "SEARCH_FAILED"):
            _code = _join_codes(*(p.failure_code for p in outcomes if p.failure_code))
            if _code and _code not in _news_budget.failures:
                _news_budget.failures.append(_code)
    return run,features
