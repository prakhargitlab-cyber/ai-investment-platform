"""Versioned, evidence-only business exposures. No sector-to-company guesses."""
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Literal
from uuid import UUID, NAMESPACE_URL, uuid5
from pydantic import AwareDatetime, Field, model_validator
from app.models import ResearchBaseModel

CONFIG = Path(__file__).parent / 'config'
ONTOLOGY = json.loads((CONFIG/'exposure_ontology_v1.json').read_text())
RELIABILITY = json.loads((CONFIG/'source_reliability_v1.json').read_text())
PROFILE_VERSION = 'BUSINESS_EXPOSURE_V1'

def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, default=str, separators=(',', ':')).encode()).hexdigest()

def contains(text, phrase):
    return re.search(r'(?<!\w)'+re.escape(phrase)+r'(?!\w)', text, re.I) is not None

class SourceReference(ResearchBaseModel):
    document_id: UUID | None = None
    url: str
    classification: str
    publication_time: AwareDatetime | None = None
    retrieved_at: AwareDatetime
    confidence: float = Field(ge=0, le=1)

class BusinessEvidence(ResearchBaseModel):
    instrument_id: UUID
    text: str
    source: SourceReference

class Exposure(ResearchBaseModel):
    name: str
    normalized_key: str
    exposure_type: Literal['RAW_MATERIAL','COMMODITY','ENERGY','CURRENCY','INTEREST_RATE','DEMAND_DRIVER','REGULATION','GEOGRAPHY','COMPETITIVE','SUPPLY_CHAIN']
    direction_when_price_rises: Literal[-1,0,1] | None = None
    importance: Literal['LOW','MEDIUM','HIGH']
    confidence: float = Field(ge=0,le=1)
    source_references: list[SourceReference] = Field(min_length=1)

class CompanyBusinessExposureProfile(ResearchBaseModel):
    profile_id: UUID
    instrument_id: UUID
    company_name: str
    ticker: str
    business_description: str | None = None
    industries: list[str] = Field(default_factory=list)
    business_segments: list[str] = Field(default_factory=list)
    products_services: list[str] = Field(default_factory=list)
    exposures: list[Exposure] = Field(default_factory=list)
    demand_drivers: list[str] = Field(default_factory=list)
    regulatory_drivers: list[str] = Field(default_factory=list)
    geographic_exposures: list[str] = Field(default_factory=list)
    customer_segments: list[str] = Field(default_factory=list)
    competitor_names: list[str] = Field(default_factory=list)
    business_risks: list[str] = Field(default_factory=list)
    source_references: list[SourceReference]
    public_available_at: AwareDatetime
    retrieved_at: AwareDatetime
    computed_at: AwareDatetime
    confidence: float = Field(ge=0,le=1)
    profile_version: str = PROFILE_VERSION
    evidence_fingerprint: str

    @model_validator(mode='after')
    def validate_availability(self):
        if max(self.public_available_at,self.retrieved_at)>self.computed_at:
            raise ValueError('FUTURE_PROFILE_EVIDENCE')
        if any(max(s.publication_time or s.retrieved_at,s.retrieved_at)>self.computed_at for s in self.source_references):
            raise ValueError('FUTURE_PROFILE_SOURCE')
        return self

def reliability(source):
    from urllib.parse import urlparse
    host = (urlparse(source.url).hostname or '').lower()
    category = RELIABILITY['domains'].get(host, source.classification)
    row = RELIABILITY['classifications'].get(category, RELIABILITY['classifications']['OTHER'])
    return row['tier'], min(source.confidence, row['confidence'])

def extract_profile(instrument_id, company_name, ticker, evidence, *, now, industry=None):
    selected = [e for e in evidence if e.instrument_id == instrument_id and e.source.retrieved_at <= now
        and (e.source.publication_time is None or e.source.publication_time <= now)]
    selected.sort(key=lambda e: (-reliability(e.source)[1], e.source.url, fingerprint(e.text)))
    exposures = {}
    for item in selected:
        _, confidence = reliability(item.source)
        for sentence in re.split(r'[.\n;]', item.text):
            if re.search(r'\b(does not|do not|not exposed|no exposure|never uses)\b',sentence,re.I):
                continue
            # An explicit economic relationship is required, not commodity presence alone.
            input_use = bool(re.search(r'\b(uses?|consum\w*|raw materials?|inputs?|input costs?|depends? on|purchases?)\b', sentence, re.I))
            produces = bool(re.search(r'\b(produces?|mines?|sells?)\b', sentence, re.I))
            if not (input_use or produces):
                continue
            for key, entry in ONTOLOGY['entries'].items():
                if not any(contains(sentence, alias) for alias in entry['aliases']):
                    continue
                direction = -1 if input_use and not produces else 1 if produces and not input_use else None
                importance = 'HIGH' if re.search(r'\b(major|key|principal|significant|primary)\b', sentence,re.I) else 'MEDIUM'
                exposure = Exposure(name=entry['aliases'][0], normalized_key=key, exposure_type=entry['type'],
                    direction_when_price_rises=direction, importance=importance, confidence=confidence, source_references=[item.source])
                old = exposures.get(key)
                if old is None or confidence > old.confidence:
                    exposures[key] = exposure
                elif confidence == old.confidence:
                    if old.direction_when_price_rises != direction:
                        old.direction_when_price_rises = None
                    if item.source not in old.source_references: old.source_references.append(item.source)
    sources = [e.source for e in selected]
    payload = [e.model_dump(mode='json') for e in selected]
    signature = fingerprint([PROFILE_VERSION, ONTOLOGY['version'], RELIABILITY['version'],str(instrument_id),company_name,ticker,industry,payload,
        now.isoformat() if not selected else None])
    # Only explicitly labelled lists are extracted; absent relationships remain
    # empty. These fields inherit the snapshot's retained source references.
    labels = {'business_segments':'business segments', 'products_services':'products(?: and services)?',
        'demand_drivers':'demand drivers', 'regulatory_drivers':'regulatory drivers',
        'geographic_exposures':'geographic exposures', 'customer_segments':'customer segments',
        'competitor_names':'competitors', 'business_risks':'business risks'}
    structured = {}
    for field, label in labels.items():
        values = set()
        for item in selected:
            for match in re.finditer(r'(?:^|[\n.;])\s*'+label+r'\s*:\s*([^\n.;]+)',item.text,re.I):
                values.update(v.strip() for v in match[1].split(',') if v.strip())
        structured[field] = sorted(values,key=lambda v:(v.casefold(),v))
    return CompanyBusinessExposureProfile(profile_id=uuid5(NAMESPACE_URL, signature),instrument_id=instrument_id,
        company_name=company_name,ticker=ticker,business_description=selected[0].text if selected else None,
        industries=[industry] if industry else [],exposures=[exposures[k] for k in sorted(exposures)],**structured,
        source_references=sources, public_available_at=max((s.publication_time or s.retrieved_at for s in sources),default=now),
        retrieved_at=max((s.retrieved_at for s in sources),default=now), computed_at=now,
        confidence=max((reliability(s)[1] for s in sources),default=0),evidence_fingerprint=signature)

def query_plan(profile, *, max_exposures=3, tier_limits=(3,4,3,4)):
    if not 0 <= max_exposures <= 5 or len(tier_limits)!=4 or any(not 0<=x<=10 for x in tier_limits):
        raise ValueError('INVALID_QUERY_LIMIT')
    exposures = sorted((e for e in profile.exposures if e.confidence>=.6 and e.importance!='LOW'),
        key=lambda e: (e.importance!='HIGH',-e.confidence,e.normalized_key))[:max_exposures]
    company=profile.company_name
    industry=profile.industries[0] if profile.industries else None
    tiers = [[company,profile.ticker,company+' news',company+' latest'],
        [company+' '+term for term in ('order','guidance','expansion','capacity','regulation','management','earnings','acquisition','litigation')],
        [company+' '+e.name for e in exposures],
        ([e.name+' '+industry for e in exposures]+[industry+' '+t for t in ('demand','regulation','pricing','capacity')]) if industry else []]
    output, seen = [],set()
    for tier,(queries,limit) in enumerate(zip(tiers,tier_limits),1):
        accepted=0
        for query in queries:
            normalized=' '.join(query.lower().split())
            if normalized and normalized not in seen and accepted<limit:
                output.append((tier,query)); seen.add(normalized); accepted+=1
    return output
