"""Offline end-to-end integration of approved ETF facts into published rescoring."""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest

import app.etf_full_universe as full
import app.etf_official_document_sources as sources
from app.etf_evidence import EtfAuthority, EtfFact, EtfMetric, EtfProvenance
from app.etf_official_document_sources import (
    AccessTier, ApprovedDocumentSource, ApprovedDocumentType, ApprovedSourceRegistry,
)
from test_etf_full_universe import checkpoint, universe
from test_etf_ingestion import NOW, repo
from test_etf_preflight import compatible_dispatch  # autouse: checkpoint-compatible async dispatcher


@pytest.mark.asyncio
async def test_opt_in_approved_document_fact_reaches_final_published_score(repo, monkeypatch):
    rows, csv = universe(1)
    row = rows[0]
    doc_url = "https://fund.example.com/reviewed.pdf"
    approved = ApprovedSourceRegistry([ApprovedDocumentSource(
        instrument_id=UUID(int=999), isin=row['isin'], ticker=row['primarySymbol'],
        issuer_name="Fixture AMC", document_url=doc_url, document_host="fund.example.com",
        document_type=ApprovedDocumentType.FACTSHEET, approval_reference="offline-approved",
        access_tier=AccessTier.PUBLIC_UNAUTHENTICATED,
    )])
    monkeypatch.setattr(sources, 'default_bounded_registry', lambda: approved)
    repo.settings = SimpleNamespace(research_etf_official_document_fetch_enabled=True)

    async def fake_ingest(repository, orchestrator, selected, client, **kwargs):
        # The rule engine requires fresh MARKET_PRICE and TRADING_VOLUME.
        # This isolated fixture must persist them, not merely claim READY.
        for selected_row in selected:
            instrument_id = UUID(selected_row['globalInstrumentId'])
            for metric, value, unit in (
                (EtfMetric.MARKET_PRICE, '100', 'INR'),
                (EtfMetric.TRADING_VOLUME, '500000', 'shares'),
            ):
                repository.persistence.save_etf_evidence(EtfFact(
                    instrument_id=instrument_id, metric=metric,
                    value=Decimal(value), unit=unit, as_of_date=NOW.date(),
                    provenance=EtfProvenance(
                        provider='NSE', source_type='NSE_MARKET_WATCH',
                        source_identity=f'{selected_row["primarySymbol"]}:{metric}:fixture',
                        source_url='https://www.nseindia.com/',
                        authority=EtfAuthority.OFFICIAL_EXCHANGE,
                        reliability_level='LEVEL_A', retrieved_at=NOW,
                    ),
                ))
        return {'instruments': [{'instrument_id':r['globalInstrumentId'], 'status':'READY',
                                 'attempts':[]} for r in selected]}
    monkeypatch.setattr(full, 'ingest_quotes', fake_ingest)

    async def fake_history(repository, row, *, now, checkpoint):
        return {'status':'SUCCESS_EMPTY', 'reason':'HISTORICAL_COVERAGE_INSUFFICIENT',
                'observations':0}

    calls = []
    async def fake_document(store, profile, url):
        assert profile.instrument_id == UUID(row['globalInstrumentId'])
        assert profile.isin == row['isin'] and profile.ticker == row['primarySymbol']
        assert url == doc_url
        calls.append(url)
        store.save_etf_evidence(EtfFact(
            instrument_id=profile.instrument_id, metric=EtfMetric.EXPENSE_RATIO,
            value=Decimal('0.25'), unit='percent', as_of_date=NOW.date(),
            provenance=EtfProvenance(
                provider='fund.example.com', source_type='COMPANY_WEBSITE',
                source_identity=url+'#fixture', source_url=url,
                authority=EtfAuthority.OFFICIAL_FUND, reliability_level='LEVEL_B',
                retrieved_at=NOW)))
        return SimpleNamespace(status='INGESTED', reason='source-verified fixture',
                               facts={'expense_ratio': {'outcome':'SUCCESS_WITH_DATA',
                                                        'reason':'EXPLICIT_SOURCE_EVIDENCE'}})

    cp = await checkpoint(repo)
    orchestrator = SimpleNamespace(active_global_etfs=AsyncMock(return_value=rows))
    def handler(request):
        if request.url.path.endswith('.csv'):
            return httpx.Response(200, text=csv)
        return httpx.Response(200, json={'data': {'timestamp': NOW.strftime('%d-%b-%Y %H:%M:%S'),
            'navDate': NOW.strftime('%d-%b-%Y'),
            'data':[{'symbol':row['primarySymbol'],'series':'EQ',
                     'identifier':row['primarySymbol']+'EQN','lastPrice':100,
                     'totalTradedVolume':500000,'nav':100}]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await full.run_full_etf_cycle(repo, orchestrator, checkpoint=cp, cycle_id=cp.cycle_id,
            identity_headers={}, client=client, clock=lambda: NOW, sleep=AsyncMock(),
            history_acquirer=fake_history, document_acquirer=fake_document)

    assert calls == [doc_url]
    saved = repo.persistence.etf_radar_cycle(cp.cycle_id)
    candidate = saved['candidates'][0]
    assert saved['document_acquisition'][row['isin']]['status'] == 'INGESTED'
    assert candidate['document_acquisition']['status'] == 'INGESTED'
    assert any(ref['metric'] == 'EXPENSE_RATIO' and ref['value'] == '0.25'
               and ref['url'] == doc_url for ref in candidate['source_references'])
    cost = next(f for f in candidate['rule_engine_result']['factor_results'] if f['factor'] == 'COST')
    assert cost['status'] == 'SCORED'
    # This single supported metric must never produce a fabricated buy signal.
    assert candidate['recommendation'] not in ('OPPORTUNITY', 'STRONG_OPPORTUNITY')


@pytest.mark.asyncio
async def test_document_pipeline_disabled_never_calls_injected_acquirer(repo, monkeypatch):
    repo.settings = SimpleNamespace(research_etf_official_document_fetch_enabled=False)
    rows, csv = universe(1)
    cp = await checkpoint(repo)
    async def fake_history(repository, row, *, now, checkpoint):
        return {'status': 'SUCCESS_EMPTY', 'observations': 0}
    async def forbidden(*args, **kwargs):
        raise AssertionError('document acquisition ran without opt in')
    def handler(request):
        if request.url.path.endswith('.csv'):
            return httpx.Response(200, text=csv)
        if 'marketWatchApi' in request.url.path:
            return httpx.Response(200, json={'data': {
                'timestamp':NOW.strftime('%d-%b-%Y %H:%M:%S'),
                'navDate':NOW.strftime('%d-%b-%Y'),
                'data':[{'symbol':rows[0]['primarySymbol'], 'series':'EQ',
                         'identifier':rows[0]['primarySymbol']+'EQN',
                         'lastPrice':100,'totalTradedVolume':500000,'nav':100}]}})
        return httpx.Response(200, json={'symbol':rows[0]['primarySymbol'],
            'isin':rows[0]['isin'],'isETFSec':'true','isDelisted':'false',
            'isSuspended':'false','activeSeries':['EQ'],'marketType':'N'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await full.run_full_etf_cycle(repo, SimpleNamespace(active_global_etfs=AsyncMock(return_value=rows)),
            checkpoint=cp, cycle_id=cp.cycle_id, identity_headers={}, client=client, clock=lambda:NOW,
            sleep=AsyncMock(), history_acquirer=fake_history, document_acquirer=forbidden)
    assert repo.persistence.etf_radar_cycle(cp.cycle_id)['document_acquisition'] == {}
