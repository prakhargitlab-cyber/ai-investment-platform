"""Explicit bounded news worker. Never imported/called by ranking or GET flows."""
import asyncio
from datetime import datetime, timezone, timedelta
from uuid import NAMESPACE_URL, uuid5
from urllib.parse import urlparse
from app.business_exposure import BusinessEvidence, SourceReference, extract_profile, query_plan
from app.news_intelligence import ProviderOutcome, aggregate_search, extract_impacts
from app.source_discovery import SearchDateWindow, CandidateSearchResult, classify_source, reliability_for_classification, source_type_for_classification
from app.normalization import canonicalize_url, content_hash, extract_text, extract_published_at
from app.models import ResearchDocument, SourceMode


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
    candidates={canonicalize_url(row.url):row for row in supplemental}
    for provider in sorted(providers,key=lambda p:p.provider_name):
        if provider.provider_name.lower() == 'disabled':
            outcomes.append(ProviderOutcome(provider=provider.provider_name,outcome='FAILED',candidate_count=0,
                queries_planned=len(plan),queries_completed=0,failure_code='SEARCH_PROVIDER_DISABLED'))
            continue
        completed=0; count=0; failed=False; degraded=False
        for _,query in plan:
            try:
                rows=await provider.discover(company,'CURRENT_NEWS',SearchDateWindow(query_limit=1,explicit_queries=(query,)))
                completed+=1; count+=len(rows)
                degraded |= bool(getattr(provider,'last_query_degraded',False))
                for row in rows: candidates.setdefault(canonicalize_url(row.url),row)
            except Exception:
                failed=True
            await sleep(repository.settings.market_data_population_request_interval_seconds)
        state=('PARTIAL' if failed or degraded else 'SUCCESS_WITH_RESULTS' if count else 'SUCCESS_EMPTY') if completed else 'FAILED'
        outcomes.append(ProviderOutcome(provider=provider.provider_name,outcome=state,candidate_count=count,
            queries_planned=len(plan),queries_completed=completed,failure_code='SEARCH_PROVIDER_UNAVAILABLE' if failed else None))
    features=[]; incomplete=len(candidates)>max_documents
    for url,candidate in sorted(candidates.items())[:max_documents]:
        try:
            existing=next((d for d in repository.documents.values() if d.canonical_url==url),None)
            if existing:
                document=existing
            else:
                fetched=await repository._fetcher.fetch(url)  # Existing redirects/SSRF/robots/size policy.
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
                await repository._run_blocking_persistence(repository._persistence.upsert_document,document)
                repository.documents[document.document_id]=document
            if not document.normalized_text: raise ValueError('DOCUMENT_BODY_UNAVAILABLE')
            extracted=extract_impacts(exposure,document,now=now or datetime.now(timezone.utc))
            for feature in extracted:
                features.append(await repository.append_news_record(feature))
        except Exception:
            incomplete=True
    if incomplete:
        outcomes=[p.model_copy(update={'outcome':'PARTIAL'}) if p.outcome.startswith('SUCCESS') else p for p in outcomes]
    run=aggregate_search(company.instrument_id,outcomes,started_at=started,completed_at=now or datetime.now(timezone.utc),
        qualifying_events=len({f.event_key for f in features}),query_plan=plan)
    await repository.append_news_record(run)
    return run,features
