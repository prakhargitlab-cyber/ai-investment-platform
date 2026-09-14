"""Typed search outcomes and deterministic company-specific event impact.

Search recency and event relevance are independent. Public availability is not
computation time. Historical online reads additionally require discovered and
computed timestamps <= the requested as-of, preventing backfill look-ahead.
"""
from datetime import timedelta
import re
from typing import Literal
from uuid import UUID, NAMESPACE_URL, uuid5, uuid4
from pydantic import AwareDatetime, Field, model_validator
from app.models import ResearchBaseModel
from app.business_exposure import SourceReference, ONTOLOGY, RELIABILITY, contains, fingerprint, reliability

FEATURE_VERSION='NEWS_IMPACT_V2'
QUERY_VERSION='NEWS_QUERY_V2'

class ProviderOutcome(ResearchBaseModel):
    provider: str
    outcome: Literal['SUCCESS_WITH_RESULTS','SUCCESS_EMPTY','PARTIAL','FAILED','DEGRADED']
    candidate_count: int = Field(default=0, ge=0)
    queries_planned: int = Field(ge=0)
    queries_completed: int = Field(ge=0)
    failure_code: str | None = None
    @model_validator(mode='after')
    def valid_counts(self):
        if self.queries_completed>self.queries_planned: raise ValueError('INVALID_SEARCH_COUNTS')
        if self.outcome=='SUCCESS_EMPTY' and self.candidate_count: raise ValueError('EMPTY_WITH_RESULTS')
        return self

class SearchRun(ResearchBaseModel):
    run_id: UUID = Field(default_factory=uuid4)
    instrument_id: UUID
    query_plan_version: str = QUERY_VERSION
    started_at: AwareDatetime
    completed_at: AwareDatetime
    outcome: Literal['SEARCH_COMPLETE_WITH_EVENTS','SEARCH_COMPLETE_NO_EVENTS','SEARCH_PARTIAL','SEARCH_FAILED']
    coverage: float = Field(ge=0,le=1)
    qualifying_events: int = Field(ge=0)
    providers: list[ProviderOutcome]
    query_plan: list[tuple[int,str]] = Field(default_factory=list)
    @model_validator(mode='after')
    def ordered(self):
        if self.completed_at<self.started_at: raise ValueError('INVALID_SEARCH_TIME')
        return self

def aggregate_search(instrument_id, providers, *, started_at, completed_at, qualifying_events, query_plan=()):
    if len({p.provider for p in providers}) != len(providers): raise ValueError('DUPLICATE_SEARCH_PROVIDER')
    total=sum(p.queries_planned for p in providers)
    completed=sum(p.queries_completed for p in providers if p.outcome not in {'FAILED','DEGRADED'})
    complete=bool(providers) and total>0 and all(p.outcome in {'SUCCESS_WITH_RESULTS','SUCCESS_EMPTY'}
        and p.queries_completed==p.queries_planned and p.queries_planned>0 for p in providers)
    outcome=('SEARCH_COMPLETE_WITH_EVENTS' if qualifying_events else 'SEARCH_COMPLETE_NO_EVENTS') if complete else (
        'SEARCH_PARTIAL' if any(p.queries_completed or p.candidate_count for p in providers if p.outcome not in {'FAILED','DEGRADED'}) else 'SEARCH_FAILED')
    return SearchRun(instrument_id=instrument_id,started_at=started_at,completed_at=completed_at,outcome=outcome,
        coverage=completed/total if total else 0, qualifying_events=qualifying_events,providers=providers,query_plan=list(query_plan))

def search_state(run, now):
    if run is None: return 'FAILED_SEARCH'
    if run.completed_at>now or now-run.completed_at>timedelta(days=1): return 'STALE_SEARCH'
    return {'SEARCH_COMPLETE_WITH_EVENTS':'READY_WITH_EVENTS','SEARCH_COMPLETE_NO_EVENTS':'READY_NO_EVENTS',
        'SEARCH_PARTIAL':'PARTIAL_SEARCH','SEARCH_FAILED':'FAILED_SEARCH'}[run.outcome]

class EventImpactFeature(ResearchBaseModel):
    feature_id: UUID
    instrument_id: UUID
    event_key: str
    feature_version: str = FEATURE_VERSION
    evidence_fingerprint: str
    profile_id: UUID
    source_document_id: UUID
    source_event_id: UUID | None = None
    event_type: str
    event_subject: str
    exposure_key: str | None = None
    direction: Literal[-1,0,1]
    magnitude: float = Field(ge=0,le=1)
    impact_score: float = Field(ge=-100,le=100)
    relevance: float = Field(ge=0,le=1)
    source_confidence: float = Field(ge=0,le=1)
    event_confidence: float = Field(ge=0,le=1)
    source_tier: str
    reliability_version: str = RELIABILITY['version']
    relevance_type: Literal['DIRECT_COMPANY','SECTOR_EXPOSURE']
    company_mentioned: bool
    novelty: float = Field(default=1,ge=0,le=1)
    publication_time: AwareDatetime | None
    public_available_at: AwareDatetime
    discovered_at: AwareDatetime
    computed_at: AwareDatetime
    valid_until: AwareDatetime | None
    short_term: bool
    medium_term: bool
    long_term: bool
    severe_validated: bool = False
    source_references: list[SourceReference]

    @model_validator(mode='after')
    def time_order(self):
        if self.discovered_at>self.computed_at or self.public_available_at>self.computed_at:
            raise ValueError('FUTURE_FEATURE_EVIDENCE')
        if self.publication_time is not None and self.publication_time>self.public_available_at:
            raise ValueError('PUBLICATION_AFTER_AVAILABILITY')
        if self.valid_until is not None and self.valid_until<self.public_available_at:
            raise ValueError('EXPIRED_BEFORE_AVAILABILITY')
        return self

# Explicit semantic patterns: no company-specific entries and no headline-only polarity.
EVENT_RULES=(
    ('GOVERNANCE_RISK',r'confirmed fraud|material auditor concern|accounting fraud confirmed|declared insolvent|confirmed debt default',-1,90,True,True,True),
    ('REGULATORY_NEGATIVE',r'regulatory ban|licen[cs]e revoked',-1,90,True,True,True),
    ('REGULATORY_POSITIVE',r'regulatory approval|licen[cs]e granted',1,30,False,True,True),
    ('GUIDANCE_CUT',r'guidance (?:cut|withdrawn|lowered)|cuts? (?:its )?guidance',-1,90,True,True,False),
    ('GUIDANCE_MAINTAINED',r'(?:margin )?guidance maintained|maintains? (?:margin )?guidance',1,90,True,True,False),
    ('GUIDANCE_RAISED',r'guidance raised|raises? (?:its )?guidance',1,90,True,True,False),
    ('ORDER_CANCELLATION',r'order cancell?ation|order cancelled',-1,30,False,True,False),
    ('LARGE_ORDER_WIN',r'large order win|wins? .*contract|secures? .*order',1,30,False,True,False),
    ('CAPACITY_DELAY',r'capacity delay|plant expansion delayed',-1,90,False,True,True),
    ('CAPACITY_EXPANSION',r'capacity expansion|expands? capacity|new manufacturing plant',1,90,False,True,True),
    ('DEMAND_INCREASE',r'demand (?:increases?|rises?|surges?|growth)',1,30,True,True,False),
    ('DEMAND_DECREASE',r'demand (?:falls?|declines?|slumps?)',-1,30,True,True,False),
    ('COMPANY_PRICE_INCREASE',r'raises? (?:its )?(?:selling )?prices|selling price increase',1,21,True,True,False),
    ('SUPPLY_CHAIN_DISRUPTION',r'supply chain disruption|supply shortage|large plant shutdown',-1,21,True,True,False),
    ('COMPETITIVE_PRESSURE',r'competitive pressure|loses? market share',-1,30,True,True,False),
    ('COMPETITIVE_IMPROVEMENT',r'gains? market share',1,30,False,True,True),
    ('GEOPOLITICAL_RISK',r'trade sanctions|shipping blockade',-1,30,True,True,False),
)

def extract_impacts(profile, document, *, now):
    if document.source_mode!='REAL' or not document.normalized_text: return []
    discovered=document.discovered_at or document.retrieved_at
    publication=document.published_at
    available=max(publication or discovered,profile.public_available_at)
    if max(available,discovered,document.retrieved_at,profile.computed_at)>now: return []
    text=(document.title or '')+'\n'+document.normalized_text
    direct=contains(text,profile.company_name) or (len(profile.ticker)>=4 and contains(text,profile.ticker))
    matched=[e for e in profile.exposures if e.confidence>=.6 and e.importance!='LOW'
        and any(contains(text,a) for a in ONTOLOGY['entries'].get(e.normalized_key,{}).get('aliases',[]))]
    if not direct and not matched: return []
    source=SourceReference(document_id=document.document_id,url=document.canonical_url,
        classification=document.source_classification,publication_time=publication,retrieved_at=document.retrieved_at,confidence=1)
    tier,source_confidence=reliability(source)
    findings=[]
    for exposure in matched:
        aliases=ONTOLOGY['entries'][exposure.normalized_key]['aliases']
        for sentence in re.split(r'[.\n;]',text):
            if not any(contains(sentence,a) for a in aliases): continue
            if re.search(r'\b(no|not|never|denies?)\b',sentence,re.I): continue
            rising=bool(re.search(r'record high|price\w* (?:rise|rises|rising|increase|surge)|higher prices',sentence,re.I))
            falling=bool(re.search(r'price\w* (?:fall|falls|falling|decrease|decline)|lower prices',sentence,re.I))
            if rising==falling or exposure.direction_when_price_rises is None: continue
            direction=exposure.direction_when_price_rises*(1 if rising else -1)
            event_type=('INPUT_COST_INCREASE' if rising else 'INPUT_COST_DECREASE') if exposure.direction_when_price_rises==-1 else (
                'COMMODITY_PRICE_INCREASE' if rising else 'COMMODITY_PRICE_DECREASE')
            findings.append((event_type,exposure.normalized_key,direction,21,True,True,False,exposure.confidence))
    # Company actions cannot be attributed through a commodity match alone.
    if direct:
        for event,pattern,direction,days,short,medium,long in EVENT_RULES:
            sentences=re.split(r'[.\n;]',text)
            # Do not attribute another company's action merely because the
            # requested company appears somewhere else in a long article.
            if any(re.search(pattern,s,re.I) and
                (contains(s,profile.company_name) or (len(profile.ticker)>=4 and contains(s,profile.ticker))) and
                not re.search(r'\b(no|not|never|denies?)\b',s,re.I) for s in sentences):
                findings.append((event,None,direction,days,short,medium,long,1))
    output={}
    for kind,exposure,direction,days,short,medium,long,exposure_confidence in findings:
        relevance=1 if direct else min(.8,exposure_confidence)
        event_confidence=.9 if direct else .75
        # A record commodity price is not evidence of severe earnings damage.
        magnitude=.5
        severe=direct and tier in {'OFFICIAL','REGULATORY','EXCHANGE','COMPANY'} and (
            kind in {'GOVERNANCE_RISK','REGULATORY_NEGATIVE'} or
            (kind=='GUIDANCE_CUT' and bool(re.search(r'guidance withdrawn',text,re.I))) or
            (kind=='SUPPLY_CHAIN_DISRUPTION' and bool(re.search(r'large plant shutdown',text,re.I))))
        expiry=None if severe else (publication or discovered)+timedelta(days=days)
        if expiry is not None and expiry<available: continue
        event_key=fingerprint([document.canonical_url,kind,exposure,profile.instrument_id])
        signature=fingerprint([event_key,document.content_hash,str(profile.profile_id),publication,available,discovered,
            FEATURE_VERSION,RELIABILITY['version']])
        output[event_key]=EventImpactFeature(feature_id=uuid5(NAMESPACE_URL,signature),instrument_id=profile.instrument_id,
            event_key=event_key,evidence_fingerprint=signature,profile_id=profile.profile_id,source_document_id=document.document_id,
            event_type=kind,event_subject=exposure or profile.company_name,exposure_key=exposure,direction=direction,magnitude=magnitude,
            impact_score=round(100*direction*magnitude*relevance*source_confidence*event_confidence,8),
            relevance=relevance,source_confidence=source_confidence,event_confidence=event_confidence,source_tier=tier,
            relevance_type='DIRECT_COMPANY' if direct else 'SECTOR_EXPOSURE',company_mentioned=direct,
            publication_time=publication,public_available_at=available,discovered_at=discovered,computed_at=now,valid_until=expiry,
            short_term=short,medium_term=medium,long_term=long,severe_validated=severe,source_references=[source])
    return [output[k] for k in sorted(output)]

def impact_at(feature, now):
    if max(feature.public_available_at,feature.discovered_at,feature.computed_at)>now: return None
    if feature.valid_until is None: return feature.impact_score
    if now>=feature.valid_until: return None
    start=feature.publication_time or feature.discovered_at
    decay=max(0,min(1,(feature.valid_until-now).total_seconds()/(feature.valid_until-start).total_seconds()))
    return round(feature.impact_score*decay,8)

def latest_known_features(features, now):
    # Latest known revision per logical event, never whichever DB row arrives last.
    latest={}
    for f in features:
        if max(f.computed_at,f.discovered_at,f.public_available_at)>now: continue
        old=latest.get(f.event_key)
        if old is None or (f.computed_at,str(f.feature_id))>(old.computed_at,str(old.feature_id)): latest[f.event_key]=f
    return [latest[key] for key in sorted(latest)]

def aggregate_impact(features, now):
    values=[v for f in latest_known_features(features,now) if (v:=impact_at(f,now)) is not None]
    return round(sum(values)/len(values),8) if values else None
