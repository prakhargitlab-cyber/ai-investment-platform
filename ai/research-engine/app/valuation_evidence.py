"""Materialize price ratios without aging the earnings/book basis every day."""
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from app.models import ProvenancedValue

def materialize_valuation(records, prices, *, now):
    usable=[p for p in prices if p.observed_at<=now and p.retrieved_at<=now
        and p.price.is_finite() and p.price>0 and p.currency and p.source_url]
    if not usable: return {}
    stamp=max(p.observed_at for p in usable)
    latest=[p for p in usable if p.observed_at==stamp]
    if max(p.price for p in latest)!=min(p.price for p in latest): return {}
    price=min(latest,key=lambda p:(p.provider,p.source_url))
    output={}
    for source_name,target in (('trailingEps','trailingPE'),('bookValue','priceToBook')):
        bases=[]
        for record in records:
            if record.instrument_id != price.instrument_id or not record.currency or record.currency != price.currency:
                continue
            value=record.snapshot.facts.get(source_name)
            if value is None: continue
            # Provider metric names define per-share denominators. An explicit
            # incompatible unit or period cannot be silently treated as per-share.
            if value.unit and value.unit.upper() not in {record.currency.upper(), record.currency.upper()+"/SHARE", record.currency.upper()+" PER SHARE", "CURRENCY/SHARE"}: continue
            if source_name == 'trailingEps' and value.period and value.period.upper() not in {'TTM', 'TRAILING_TWELVE_MONTHS'}: continue
            anchor=value.as_of_date or value.published_at or value.retrieved_at
            if value.retrieved_at>now or anchor>now or now-anchor>timedelta(days=120): continue
            try: number=Decimal(str(value.value))
            except (InvalidOperation, ValueError, TypeError): continue
            if number.is_finite() and number>0: bases.append((anchor,value.source_url,number,value))
        if not bases: continue
        newest = max(b[0] for b in bases)
        current = [b for b in bases if b[0] == newest]
        if len({b[2] for b in current}) != 1: continue
        _,_,number,basis=max(current,key=lambda b:(b[0],b[1]))
        output[target]=ProvenancedValue(value=price.price/number,as_of_date=stamp,retrieved_at=max(price.retrieved_at,basis.retrieved_at),
            source_url=price.source_url,source_name='Persisted price / valid '+source_name,
            source_type='DERIVED_PERSISTED_VALUATION',confidence=basis.confidence,
            calculation_basis=f'price={price.source_url}; basis={basis.source_url}; basisAsOf={basis.as_of_date or basis.retrieved_at}')
    return output
