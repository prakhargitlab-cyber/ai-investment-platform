from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import yfinance as yf

from app.historical_market_data import YahooHistoricalPriceProvider
from app.models import StructuredInstrumentResolution, StructuredMarketSnapshot
from app.settings import Settings as ResearchSettings
from app.structured_market import StructuredProviderError, YahooFinanceProvider

from yahoo_mcp_server.contracts import IdentityInput
from yahoo_mcp_server.settings import YahooMcpSettings


class YahooMcpServiceError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class YahooAcquisitionService:
    """Reuses the proven research-engine Yahoo client and normalizers."""

    def __init__(
        self,
        settings: YahooMcpSettings,
        *,
        ticker_factory: Callable[[str], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.ticker_factory = ticker_factory or yf.Ticker
        shared_settings = ResearchSettings(
            structured_provider_timeout_seconds=settings.upstream_timeout_seconds,
            research_user_agent=settings.user_agent,
        )
        self.structured = YahooFinanceProvider(
            shared_settings, ticker_factory=self.ticker_factory
        )
        self.history = YahooHistoricalPriceProvider(self.ticker_factory)
        self._concurrency = asyncio.Semaphore(settings.max_concurrency)
        self._workers = ThreadPoolExecutor(
            max_workers=settings.max_concurrency, thread_name_prefix="yahoo-mcp"
        )
        self._closed = False

    async def close(self) -> None:
        self._closed = True
        # Running synchronous calls cannot be cancelled. Do not block the HTTP
        # loop during shutdown; no further work may be admitted to this pool.
        self._workers.shutdown(wait=False, cancel_futures=True)
        await self.structured.client.aclose()

    async def _run_blocking(self, operation: Callable[..., Any], *args: Any) -> Any:
        # Include capacity waiting in the existing acquisition budget. Submit
        # only after admission, so the executor queue cannot grow with retries.
        async with asyncio.timeout(self.settings.upstream_timeout_seconds):
            if self._closed:
                raise YahooMcpServiceError("YAHOO_MCP_UPSTREAM_UNAVAILABLE")
            await self._concurrency.acquire()
            try:
                if self._closed:
                    raise YahooMcpServiceError("YAHOO_MCP_UPSTREAM_UNAVAILABLE")
                future = asyncio.get_running_loop().run_in_executor(
                    self._workers, copy_context().run, operation, *args
                )
            except BaseException:
                self._concurrency.release()
                raise
            future.add_done_callback(self._blocking_finished)
            # A request timeout/cancellation must not cancel this future or
            # release its slot while the underlying Yahoo thread is still busy.
            return await asyncio.shield(future)

    def _blocking_finished(self, future: asyncio.Future) -> None:
        self._concurrency.release()
        if not future.cancelled():
            # Observe late failures even when their request has timed out.
            future.exception()

    async def snapshot(self, identity: IdentityInput) -> StructuredMarketSnapshot:
        resolution = StructuredInstrumentResolution(
            instrument_id=identity.global_instrument_id,
            provider="YAHOO_FINANCE",
            provider_ticker=identity.verified_yahoo_symbol,
            company_name=identity.verified_yahoo_symbol,
            exchange=identity.exchange,
            currency=identity.currency,
            quote_type="EQUITY",
            confidence=1.0,
            resolved_at=self.clock(),
            status="VERIFIED_APPLICATION_MAPPING",
        )
        try:
            # Reuse the same synchronous acquisition/normalization underneath
            # collect_verified, with this service owning worker lifetime.
            result = await self._run_blocking(
                self.structured._collect_resolved_yfinance, resolution
            )
        except TimeoutError as exc:
            raise YahooMcpServiceError("YAHOO_MCP_UPSTREAM_TIMEOUT") from exc
        except StructuredProviderError as exc:
            raise _map_shared_error(exc) from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise YahooMcpServiceError("YAHOO_MCP_UPSTREAM_UNAVAILABLE") from exc
        self._validate_resolution(identity, result)
        return result

    async def closes(self, identity: IdentityInput, *, lookback_days: int):
        now = self.clock().astimezone(timezone.utc)
        instrument = {
            "globalInstrumentId": str(identity.global_instrument_id),
            "structuredProviderTicker": identity.verified_yahoo_symbol,
            "currency": identity.currency,
        }
        try:
            values = await self._run_blocking(
                self.history._closes,
                instrument,
                now - timedelta(days=lookback_days),
                now + timedelta(days=1),
            )
        except TimeoutError as exc:
            raise YahooMcpServiceError("YAHOO_MCP_UPSTREAM_TIMEOUT") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise YahooMcpServiceError("YAHOO_MCP_UPSTREAM_UNAVAILABLE") from exc
        if not values:
            raise YahooMcpServiceError("YAHOO_MCP_INCOMPLETE")
        return values[: self.settings.max_response_items]

    @staticmethod
    def _validate_resolution(identity: IdentityInput, snapshot: StructuredMarketSnapshot) -> None:
        resolved = snapshot.resolution
        if resolved.provider_ticker.strip().upper() != identity.verified_yahoo_symbol:
            raise YahooMcpServiceError("YAHOO_MCP_IDENTITY_MISMATCH")
        if identity.currency and (
            not resolved.currency or resolved.currency.upper() != identity.currency
        ):
            raise YahooMcpServiceError("YAHOO_MCP_IDENTITY_MISMATCH")


def _map_shared_error(error: StructuredProviderError) -> YahooMcpServiceError:
    code = str(error)
    if code.startswith("PERSISTED_MAPPING_CONFLICT") or code == "COMPANY_NOT_RESOLVED":
        return YahooMcpServiceError("YAHOO_MCP_IDENTITY_MISMATCH")
    if "PRICE_UNAVAILABLE" in code or "NO_ACCEPTED_FIELDS" in code:
        return YahooMcpServiceError("YAHOO_MCP_INCOMPLETE")
    return YahooMcpServiceError("YAHOO_MCP_UPSTREAM_UNAVAILABLE")
