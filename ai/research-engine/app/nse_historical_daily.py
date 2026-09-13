"""Official NSE daily evidence; invoked only by explicit acquisition workers."""
from __future__ import annotations

import asyncio
import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

import httpx

from app.models import DailyMarketBar
from app.portfolio_orchestration import _trusted_provider_mapping
from app.research_fetching import _retry_after_seconds

ENDPOINT = "https://www.nseindia.com/api/historicalOR/generateSecurityWiseHistoricalData"
BOOTSTRAP = "https://www.nseindia.com/report-detail/eq_security"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/csv,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9", "Referer": BOOTSTRAP,
}
REQUIRED = {"date", "symbol", "series", "open", "high", "low", "close"}
ALIASES = {f"{name} price": name for name in ("open", "high", "low", "close")}
# Accept ungrouped, Western grouping, or Indian grouping; never strip arbitrary commas.
NUMBER = re.compile(r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+|[0-9]{1,2}(?:,[0-9]{2})*,[0-9]{3})(?:\.[0-9]+)?\Z")
MONTHS = {name: i for i, name in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}


@dataclass
class NseHistoricalResult:
    global_instrument_id: UUID
    request_from: date
    request_to: date
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    provider_symbol: str | None = None
    http_status: int | None = None
    status: str = "UNAVAILABLE"
    failure_reason: str | None = None
    source_url: str = ENDPOINT
    source_mode: str = "REAL"
    headers: list[str] = field(default_factory=list)
    rows_parsed: int = 0
    rows_rejected: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    bars: list[DailyMarketBar] = field(default_factory=list)
    persisted_rows: int = 0

    @property
    def rows_accepted(self):
        return len(self.bars)

    @property
    def first_trading_date(self):
        return self.bars[0].trading_date if self.bars else None

    @property
    def last_trading_date(self):
        return self.bars[-1].trading_date if self.bars else None


def verified_identity(metadata: dict, key: UUID) -> tuple[str, str]:
    if str(metadata.get("globalInstrumentId")) != str(key):
        raise ValueError("IDENTITY_MISMATCH")
    if (metadata.get("status") != "ACTIVE" or metadata.get("assetType") != "EQUITY"
            or metadata.get("country") not in {"IN", "IND", "INDIA"}
            or metadata.get("primaryExchange") not in {"NSE", "XNSE"}):
        raise ValueError("UNSUPPORTED_INSTRUMENT")
    mappings = [m for m in metadata.get("providerMappings", []) if isinstance(m, dict)
                and str(m.get("provider", "")).upper() == "NSE"]
    if len(mappings) != 1:
        raise ValueError("NO_UNAMBIGUOUS_NSE_MAPPING")
    mapping = mappings[0]
    if not _trusted_provider_mapping(mapping) or mapping.get("active") is False:
        raise ValueError("NO_TRUSTED_NSE_MAPPING")
    symbol = str(mapping.get("providerSymbol") or "").strip().upper()
    if not symbol or mapping.get("exchange") not in {None, "NSE", "XNSE"}:
        raise ValueError("INVALID_NSE_MAPPING")
    currencies = {str(c).strip().upper() for c in (metadata.get("currency"), mapping.get("currency")) if c and str(c).strip()}
    if len(currencies) != 1:
        raise ValueError("MISSING_OR_AMBIGUOUS_CURRENCY")
    return symbol, currencies.pop()


def number(value: str | None, *, integer=False, required=False):
    value = (value or "").strip()
    if value in {"", "-"} and not required:
        return None
    if not NUMBER.fullmatch(value) or (integer and "." in value):
        raise ValueError("MALFORMED_NUMBER")
    return int(value.replace(",", "")) if integer else Decimal(value.replace(",", ""))


def trading_date(value: str) -> date:
    day, month, year = value.strip().split("-")
    return date(int(year), MONTHS[month.title()] if month.isalpha() else int(month), int(day))


def parse_csv(content: bytes, result: NseHistoricalResult, currency: str) -> None:
    """Header aliases are explicit; symbol/series normalization is strip + uppercase.

    Turnover ₹ is rupees (live fixture 2026-09-13), with no scaling. Other
    turnover units, including lacs, remain unavailable until independently verified.
    Duplicate dates are all rejected, avoiding arbitrary correction selection.
    """
    text = content.decode("utf-8-sig")
    if not text.strip():
        raise ValueError("EMPTY_RESPONSE")
    if text.lstrip().startswith(("<", "{", "[")):
        raise ValueError("NON_CSV_RESPONSE")
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    result.headers = next(reader)
    headers = [" ".join(h.strip().casefold().split()) for h in result.headers]
    headers = [ALIASES.get(h, h) for h in headers]
    if len(set(headers)) != len(headers) or not REQUIRED.issubset(headers):
        raise ValueError("MISSING_OR_DUPLICATE_HEADERS")
    candidates: dict[date, list[DailyMarketBar]] = {}
    def reject(reason):
        result.rows_rejected += 1
        result.rejection_reasons[reason] = result.rejection_reasons.get(reason, 0) + 1
    for values in reader:
        if not any(v.strip() for v in values):
            continue
        result.rows_parsed += 1
        if len(values) != len(headers):
            reject("MALFORMED_ROW")
            continue
        row = dict(zip(headers, (v.strip() for v in values)))
        if row["symbol"].upper() != result.provider_symbol:
            reject("SYMBOL_MISMATCH")
            continue
        if row["series"].upper() != "EQ":
            reject("UNSUPPORTED_SERIES")
            continue
        try:
            day = trading_date(row["date"])
            if not result.request_from <= day <= result.request_to:
                reject("OUTSIDE_REQUEST_RANGE")
                continue
            bar = DailyMarketBar(global_instrument_id=result.global_instrument_id, trading_date=day,
                **{k: number(row[k], required=True) for k in ("open", "high", "low", "close")},
                previous_close=number(row.get("prev close")),
                volume=number(row.get("total traded quantity"), integer=True),
                turnover=number(row.get("turnover ₹")) if currency == "INR" else None,
                currency=currency, provider="NSE", provider_symbol=result.provider_symbol,
                source_mode="REAL", source_url=result.source_url, retrieved_at=result.retrieved_at)
            candidates.setdefault(day, []).append(bar)
        except (ValueError, KeyError, OverflowError):
            reject("INVALID_DATE_OR_VALUE")
    for day, bars in sorted(candidates.items()):
        if len(bars) != 1:
            for _ in bars:
                reject("DUPLICATE_DATE")
        else:
            result.bars.append(bars[0])


class NseHistoricalDailyProvider:
    provider_name = "NSE"

    def __init__(self, orchestrator, settings, *, client=None, sleep=asyncio.sleep):
        self.orchestrator, self.settings = orchestrator, settings
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(20, connect=4))
        self._owns_client = client is None
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._bootstrapped = False
        self._last_request = None

    async def aclose(self):
        if self._owns_client:
            await self.client.aclose()

    async def _get(self, url, result, **kwargs):
        for attempt in range(self.settings.nse_historical_max_retries + 1):
            now = asyncio.get_running_loop().time()
            if self._last_request is not None:
                await self._sleep(max(0, self.settings.market_data_population_request_interval_seconds - (now - self._last_request)))
            self._last_request = asyncio.get_running_loop().time()
            try:
                response = await self.client.get(url, headers=HEADERS, **kwargs)
                result.http_status = response.status_code
                if response.status_code < 400:
                    response.raise_for_status()
                    return response
                if response.status_code != 429 and response.status_code < 500:
                    response.raise_for_status()
                if attempt == self.settings.nse_historical_max_retries:
                    response.raise_for_status()
                retry_after = _retry_after_seconds(response.headers.get("retry-after"))
                # Do not retry earlier than a long server-requested cooldown.
                if retry_after is not None and retry_after > 30:
                    response.raise_for_status()
                await self._sleep(max(min(2 ** attempt, 8), retry_after or 0))
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == self.settings.nse_historical_max_retries:
                    raise
                await self._sleep(min(2 ** attempt, 8))

    async def fetch(self, global_instrument_id: UUID, *, start: date, end: date,
                    identity_headers=None, correlation_id=None) -> NseHistoricalResult:
        result = NseHistoricalResult(global_instrument_id, start, end)
        stage = "IDENTITY"
        try:
            if type(start) is not date or type(end) is not date or start > end:
                raise ValueError("INVALID_DATE_RANGE")
            if (end - start).days + 1 > self.settings.nse_historical_request_window_days:
                raise ValueError("REQUEST_WINDOW_EXCEEDED")
            metadata = await self.orchestrator.global_instrument_metadata(global_instrument_id,
                identity_headers=identity_headers, correlation_id=correlation_id)
            result.provider_symbol, currency = verified_identity(metadata, global_instrument_id)
            async with self._lock:
                if not self._bootstrapped:
                    stage = "BOOTSTRAP"
                    await self._get(BOOTSTRAP, result)
                    self._bootstrapped = True
                stage = "HISTORICAL"
                response = await self._get(ENDPOINT, result, params={
                    "from": start.strftime("%d-%m-%Y"), "to": end.strftime("%d-%m-%Y"),
                    "symbol": result.provider_symbol, "type": "priceVolumeDeliverable", "series": "EQ", "csv": "true"})
                result.retrieved_at = datetime.now(timezone.utc)
                result.source_url = str(response.url)
                if any(t in response.headers.get("content-type", "").lower() for t in ("html", "json")):
                    raise ValueError("NON_CSV_RESPONSE")
                parse_csv(response.content, result, currency)
            result.status = "SUCCESS" if result.bars else "UNAVAILABLE"
            result.failure_reason = None if result.bars else "NO_VALID_HISTORY"
        except httpx.HTTPStatusError as exc:
            result.failure_reason = f"{stage}_HTTP_{exc.response.status_code}"
            if exc.response.status_code == 403:
                self._bootstrapped = False
        except httpx.TimeoutException:
            result.failure_reason = f"{stage}_TIMEOUT"
        except httpx.HTTPError:
            result.failure_reason = f"{stage}_CONNECTION_FAILURE"
        except (ValueError, csv.Error, UnicodeError) as exc:
            result.failure_reason = str(exc) if type(exc) is ValueError else "MALFORMED_CSV"
        except Exception:
            result.failure_reason = f"{stage}_UNAVAILABLE"
        if result.failure_reason:
            result.bars.clear()
        return result


async def persist_daily_result(repository, result: NseHistoricalResult) -> NseHistoricalResult:
    if result.status == "SUCCESS":
        try:
            result.persisted_rows = await repository.upsert_daily_market_bars_async(result.bars)
        except Exception:
            result.status = "UNAVAILABLE"
            result.failure_reason = "DAILY_BAR_PERSISTENCE_UNAVAILABLE"
    return result
