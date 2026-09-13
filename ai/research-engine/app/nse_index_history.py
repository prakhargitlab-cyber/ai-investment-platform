"""Official index JSON acquisition; reuses NSE transport, not equity CSV parsing."""
from datetime import date, datetime, timezone
from decimal import Decimal
import json

import httpx
from app.models import DailyMarketBar
from app.nse_historical_daily import NseHistoricalDailyProvider, NseHistoricalResult, BOOTSTRAP, number, trading_date
from app.sector_benchmarks import BENCHMARKS, benchmark_id, benchmark_identity

ENDPOINT = 'https://www.nseindia.com/api/historicalOR/indicesHistory'
HISTORY_VERSION = 'NSE_INDEX_HISTORY_V1'
# Exact response names observed in the checked-in official response fixtures.
# These are contract aliases, never canonical identities or fuzzy matches.
RESPONSE_NAMES = {'NIFTY 500': 'NIFTY 500', 'NIFTY IT': 'NIFTY IT',
                  'NIFTY FINANCIAL SERVICES': 'NIFTY FIN SERVICE',
                  'NIFTY HEALTHCARE INDEX': 'NIFTY HEALTHCARE'}
REQUIRED = {'EOD_INDEX_NAME', 'EOD_TIMESTAMP', 'EOD_OPEN_INDEX_VAL', 'EOD_HIGH_INDEX_VAL',
            'EOD_LOW_INDEX_VAL', 'EOD_CLOSE_INDEX_VAL'}


def parse_index_history(content, result, currency):
    try:
        payload = json.loads(content.decode('utf-8-sig'), parse_float=Decimal)
    except (ValueError, UnicodeError):
        raise ValueError('NON_JSON_INDEX_RESPONSE') from None
    data = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ValueError('INVALID_INDEX_RESPONSE_CONTRACT')
    if not data:
        raise ValueError('EMPTY_RESPONSE')
    accepted = {}
    for row in data:
        result.rows_parsed += 1
        if not isinstance(row, dict) or not REQUIRED.issubset(row):
            raise ValueError('MISSING_INDEX_FIELDS')
        result.headers = sorted(row)
        if ' '.join(str(row['EOD_INDEX_NAME']).upper().split()) != RESPONSE_NAMES.get(result.provider_symbol):
            raise ValueError('INDEX_SYMBOL_MISMATCH')
        try:
            day = trading_date(row['EOD_TIMESTAMP'])
            if not result.request_from <= day <= result.request_to or day in accepted:
                raise ValueError('INVALID_OR_DUPLICATE_INDEX_DATE')
            bar = DailyMarketBar(global_instrument_id=result.global_instrument_id, trading_date=day,
                **{name:number(str(row[field]), required=True) for name,field in (
                    ('open','EOD_OPEN_INDEX_VAL'),('high','EOD_HIGH_INDEX_VAL'),
                    ('low','EOD_LOW_INDEX_VAL'),('close','EOD_CLOSE_INDEX_VAL'))},
                currency=currency, provider='NSE', provider_symbol=result.provider_symbol,
                source_mode='REAL', source_url=result.source_url, retrieved_at=result.retrieved_at,
                volume=None, turnover=None, previous_close=None)
        except (ValueError, TypeError, KeyError):
            raise ValueError('INVALID_INDEX_ROW') from None
        accepted[day] = bar
    result.bars = [accepted[day] for day in sorted(accepted)]


class NseIndexHistoryProvider:
    def __init__(self, orchestrator, settings, *, client=None, sleep=None):
        kwargs = {'client':client}
        if sleep is not None:
            kwargs['sleep'] = sleep
        self.transport = NseHistoricalDailyProvider(orchestrator, settings, **kwargs)
        self.orchestrator, self.settings = orchestrator, settings

    async def aclose(self):
        await self.transport.aclose()

    async def fetch(self, global_instrument_id, *, start, end, identity_headers=None, correlation_id=None):
        result = NseHistoricalResult(global_instrument_id, start, end, source_url=ENDPOINT)
        stage = 'IDENTITY'
        try:
            keys = [key for key in BENCHMARKS if benchmark_id(key) == global_instrument_id]
            if len(keys) != 1:
                raise ValueError('BENCHMARK_IDENTITY_UNAVAILABLE')
            if type(start) is not date or type(end) is not date or start > end or (end-start).days+1 > self.settings.nse_historical_request_window_days:
                raise ValueError('INVALID_REQUEST_WINDOW')
            key = keys[0]
            metadata = await self.orchestrator.global_instrument_metadata(global_instrument_id,
                identity_headers=identity_headers, correlation_id=correlation_id)
            ref = benchmark_identity(metadata, key)
            result.provider_symbol = BENCHMARKS[key]
            async with self.transport._lock:
                if not self.transport._bootstrapped:
                    stage = 'BOOTSTRAP'
                    await self.transport._get(BOOTSTRAP, result)
                    self.transport._bootstrapped = True
                stage = 'HISTORICAL'
                response = await self.transport._get(ENDPOINT, result, params={'indexType':result.provider_symbol,
                    'from':start.strftime('%d-%m-%Y'),'to':end.strftime('%d-%m-%Y')})
                result.retrieved_at = datetime.now(timezone.utc)
                result.source_url = str(response.url)
                parse_index_history(response.content, result, ref.currency)
            result.status = 'SUCCESS'
        except httpx.HTTPStatusError as exc:
            result.failure_reason = f'{stage}_HTTP_{exc.response.status_code}'
            if exc.response.status_code == 403:
                self.transport._bootstrapped = False
        except httpx.HTTPError:
            result.failure_reason = f'{stage}_PROVIDER_UNAVAILABLE'
        except ValueError as exc:
            safe = {'BENCHMARK_IDENTITY_UNAVAILABLE','INVALID_REQUEST_WINDOW','NON_JSON_INDEX_RESPONSE',
                'INVALID_INDEX_RESPONSE_CONTRACT','EMPTY_RESPONSE','MISSING_INDEX_FIELDS','INDEX_SYMBOL_MISMATCH','INVALID_INDEX_ROW'}
            result.failure_reason = str(exc) if str(exc) in safe else f'{stage}_UNAVAILABLE'
        except Exception:
            result.failure_reason = f'{stage}_UNAVAILABLE'
        if result.failure_reason:
            result.rows_rejected = result.rows_parsed
            result.bars.clear()
        return result
