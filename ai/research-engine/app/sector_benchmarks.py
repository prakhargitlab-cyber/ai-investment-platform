"""Versioned platform benchmark keys resolved only through registered master rows."""
from hashlib import md5
from uuid import UUID
from datetime import datetime

from app.sector_relative_strength import BenchmarkReference, SectorContext

MAPPING_VERSION = 'SECTOR_BENCHMARK_MAPPING_V1'
CATALOG_VERSION = 'NSE_BENCHMARK_CATALOG_V1'
BENCHMARKS = {
    'INDIA_BROAD_PRICE': 'NIFTY 500',
    'INDIA_TECHNOLOGY_PRICE': 'NIFTY IT',
    'INDIA_FINANCIALS_PRICE': 'NIFTY FINANCIAL SERVICES',
    'INDIA_HEALTHCARE_PRICE': 'NIFTY HEALTHCARE INDEX',
}
SECTOR_KEYS = {'Technology': 'INDIA_TECHNOLOGY_PRICE', 'Financials': 'INDIA_FINANCIALS_PRICE',
               'Healthcare': 'INDIA_HEALTHCARE_PRICE'}
BROAD_KEY = 'INDIA_BROAD_PRICE'


def benchmark_id(key):
    """Same platform-key UUIDv3 as portfolio-service; never a provider-symbol UUID."""
    if key not in BENCHMARKS:
        raise ValueError('UNKNOWN_BENCHMARK_KEY')
    return UUID(bytes=md5(('aip:benchmark:' + key).encode(), usedforsecurity=False).digest(), version=3)


def benchmark_identity(metadata, key):
    if not isinstance(metadata, dict):
        raise ValueError('BENCHMARK_IDENTITY_UNAVAILABLE')
    mappings = metadata.get('providerMappings') or []
    nse = [m for m in mappings if isinstance(m, dict) and m.get('provider') == 'NSE']
    if (str(metadata.get('globalInstrumentId')) != str(benchmark_id(key))
            or metadata.get('assetType') != 'INDEX' or metadata.get('status') != 'ACTIVE'
            or metadata.get('country') != 'IN' or metadata.get('primaryExchange') != 'NSE'
            or metadata.get('currency') != 'INR' or len(nse) != 1):
        raise ValueError('BENCHMARK_IDENTITY_UNAVAILABLE')
    mapping = nse[0]
    if (mapping.get('status') != 'VERIFIED' or mapping.get('active') is False
            or mapping.get('resolutionSource') != CATALOG_VERSION or mapping.get('currency') != 'INR'
            or mapping.get('exchange') != 'NSE' or mapping.get('providerSymbol') != BENCHMARKS[key]):
        raise ValueError('BENCHMARK_IDENTITY_UNAVAILABLE')
    return BenchmarkReference(benchmark_id(key), 'INR', frozenset({'NSE'}))


def build_sector_contexts(classifications, registered, instrument_ids):
    """Pure adapter over canonical Nifty500 cache and registered benchmark metadata."""
    by_id = {}
    for row in registered:
        by_id.setdefault(str(row.get('globalInstrumentId')), []).append(row)
    def reference(key):
        matches = by_id.get(str(benchmark_id(key)), [])
        try:
            return benchmark_identity(matches[0], key) if len(matches) == 1 else None
        except ValueError:
            return None
    broad = reference(BROAD_KEY)
    grouped = {}
    for row in classifications:
        grouped.setdefault(str(row.get('globalInstrumentId')), []).append(row)
    output = {}
    for key in sorted(instrument_ids, key=str):
        matches = grouped.get(str(key), [])
        row = matches[0] if len(matches) == 1 else {}
        sector = row.get('canonicalSector')
        stamp = row.get('retrievedAt')
        try:
            stamp = datetime.fromisoformat(stamp.replace('Z', '+00:00')) if isinstance(stamp, str) else stamp
        except ValueError:
            stamp = None
        india_member = (row.get('source') == 'NSE_INDICES_NIFTY500' and row.get('status') == 'ACTIVE'
                and row.get('assetType') == 'EQUITY' and row.get('country') == 'IN'
                and row.get('exchange') in {'NSE', 'XNSE'})
        if not india_member:
            sector = None
        target = SECTOR_KEYS.get(sector)
        sector_ref = reference(target) if target else None
        classification_ok = bool(sector and stamp)
        status = ('NO_SECTOR_CLASSIFICATION' if not classification_ok else
            'UNMAPPED_SECTOR_BENCHMARK' if not target else
            'BENCHMARK_IDENTITY_UNAVAILABLE' if sector_ref is None else 'AVAILABLE')
        market_ref = broad if india_member else None
        output[key] = SectorContext(sector=sector, source=row.get('source'), as_of=stamp, region='INDIA' if india_member else None,
            sector_benchmark=sector_ref if classification_ok else None, market_benchmark=market_ref,
            mapping_version=MAPPING_VERSION, sector_mapping_status=status,
            market_mapping_status='AVAILABLE' if market_ref else 'BENCHMARK_IDENTITY_UNAVAILABLE')
    return output
