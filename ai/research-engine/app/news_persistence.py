"""Typed append-only V12 storage through the existing persistence connection."""
import json
from app.business_exposure import CompanyBusinessExposureProfile
from app.news_intelligence import SearchRun, EventImpactFeature

TABLES={
    CompanyBusinessExposureProfile:('company_business_exposure_profiles','profile_id',
        'profile_id instrument_id profile_version evidence_fingerprint public_available_at retrieved_at computed_at confidence'),
    SearchRun:('research_news_search_runs','run_id',
        'run_id instrument_id query_plan_version started_at completed_at outcome coverage qualifying_events'),
    EventImpactFeature:('research_event_impact_features','feature_id',
        'feature_id instrument_id event_key feature_version evidence_fingerprint profile_id source_document_id source_event_id event_type exposure_key direction magnitude impact_score relevance source_confidence event_confidence source_tier relevance_type publication_time public_available_at discovered_at computed_at valid_until short_term medium_term long_term'),
}

def sqlite_schema(connection):
    for model,(table,pk,names) in TABLES.items():
        columns=[]
        for name in names.split():
            kind='REAL' if name in {'confidence','coverage','magnitude','impact_score','relevance','source_confidence','event_confidence'} else 'INTEGER' if name in {'qualifying_events','direction','short_term','medium_term','long_term'} else 'TEXT'
            nullable=name in {'source_event_id','exposure_key','publication_time','valid_until'}
            columns.append(f'{name} {kind}'+(' PRIMARY KEY' if name==pk else '' if nullable else ' NOT NULL'))
        columns.append('payload TEXT NOT NULL')
        if model is EventImpactFeature:
            columns.extend(['FOREIGN KEY(profile_id,instrument_id) REFERENCES company_business_exposure_profiles(profile_id,instrument_id)',
                'FOREIGN KEY(source_document_id) REFERENCES research_documents(document_id)',
                'FOREIGN KEY(source_event_id) REFERENCES research_events(event_id)',
                'UNIQUE(instrument_id,event_key,feature_version,evidence_fingerprint)'])
        if model is CompanyBusinessExposureProfile:
            columns.extend(['UNIQUE(profile_id,instrument_id)','UNIQUE(instrument_id,profile_version,evidence_fingerprint)'])
        connection.execute(f"CREATE TABLE IF NOT EXISTS {table} ({','.join(columns)})")
        time='completed_at' if model is SearchRun else 'public_available_at'
        connection.execute(f'CREATE INDEX IF NOT EXISTS ix_{table}_time ON {table}(instrument_id,{time})')
        for action in ('UPDATE','DELETE'):
            connection.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'NEWS_HISTORY_IS_IMMUTABLE'); END")
    connection.commit()

class NewsPersistenceMixin:
    def append_news_record(self, record):
        record=type(record).model_validate(record.model_dump())
        table,pk,names=TABLES[type(record)]
        payload=record.model_dump(mode='json')
        key=payload[pk]
        existing=self._connection.execute(f'SELECT payload FROM {table} WHERE {pk}=?',(key,)).fetchone()
        if existing:
            old=existing['payload']
            old=old if isinstance(old,dict) else json.loads(old)
            compare=dict(payload)
            if 'computed_at' in old: compare['computed_at']=old['computed_at']
            if compare!=old: raise ValueError('IMMUTABLE_NEWS_REVISION_CONFLICT')
            return type(record).model_validate(old)
        if isinstance(record,EventImpactFeature):
            parent=self._connection.execute('SELECT instrument_id,public_available_at,computed_at FROM company_business_exposure_profiles WHERE profile_id=?',(str(record.profile_id),)).fetchone()
            if not parent or str(parent['instrument_id'])!=str(record.instrument_id): raise ValueError('EXPOSURE_PROVENANCE_UNAVAILABLE')
            from datetime import datetime
            for field in ('public_available_at','computed_at'):
                value=parent[field]
                stamp=datetime.fromisoformat(value) if isinstance(value,str) else value
                if stamp>getattr(record,field): raise ValueError('EXPOSURE_LOOKAHEAD')
        cols=names.split()
        with self._connection:
            self._connection.execute(f"INSERT INTO {table} ({','.join(cols)},payload) VALUES ({','.join('?' for _ in range(len(cols)+1))})",
                tuple(getattr(record,c).isoformat() if hasattr(getattr(record,c),'isoformat') else payload[c] for c in cols)+(json.dumps(payload,sort_keys=True),))
        return record

    def load_news_records(self, model, instrument_id, *, as_of):
        table,pk,_=TABLES[model]
        cutoff='completed_at' if model is SearchRun else 'computed_at'
        rows=self._connection.execute(f'SELECT payload FROM {table} WHERE instrument_id=? AND {cutoff}<=? ORDER BY {cutoff},{pk}',
            (str(instrument_id),as_of.isoformat())).fetchall()
        values=[model.model_validate(row['payload'] if isinstance(row['payload'],dict) else json.loads(row['payload'])) for row in rows]
        return [v for v in values if not hasattr(v,'public_available_at') or v.public_available_at<=as_of]
