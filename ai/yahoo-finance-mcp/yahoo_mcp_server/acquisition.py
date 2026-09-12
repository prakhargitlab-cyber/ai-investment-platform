from __future__ import annotations

import asyncio
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

    async def close(self) -> None:
        await self.structured.client.aclose()

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
            async with self._concurrency:
                async with asyncio.timeout(self.settings.upstream_timeout_seconds):
                    result = await self.structured.collect_verified(resolution)
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
            async with self._concurrency:
                async with asyncio.timeout(self.settings.upstream_timeout_seconds):
                    values = await self.history.closes(
                        instrument,
                        start=now - timedelta(days=lookback_days),
                        end=now + timedelta(days=1),
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
