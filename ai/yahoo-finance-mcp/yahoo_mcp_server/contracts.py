from __future__ import annotations

import math
import re
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


TOOL_SCHEMA_VERSION = "YAHOO_FINANCE_MCP_TOOL_V1"
ADAPTER_VERSION = "FIRST_PARTY_YAHOO_MCP_V1"
_SYMBOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.^=_-]{0,39}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Region(StrEnum):
    INDIA = "INDIA"
    USA = "USA"
    EUROPE = "EUROPE"


class IdentityInput(StrictModel):
    global_instrument_id: UUID = Field(alias="globalInstrumentId")
    verified_yahoo_symbol: str = Field(alias="verifiedYahooSymbol", min_length=1, max_length=40)
    region: Region
    exchange: str | None = Field(default=None, max_length=100)
    currency: str | None = Field(default=None, min_length=3, max_length=20)

    @field_validator("verified_yahoo_symbol")
    @classmethod
    def valid_symbol(cls, value: str) -> str:
        result = value.strip().upper()
        if not _SYMBOL.fullmatch(result):
            raise ValueError("verifiedYahooSymbol is invalid")
        return result

    @field_validator("exchange")
    @classmethod
    def clean_exchange(cls, value: str | None) -> str | None:
        if value is None:
            return None
        result = value.strip().upper()
        return result or None

    @field_validator("currency")
    @classmethod
    def clean_currency(cls, value: str | None) -> str | None:
        return value.strip().upper() if value else None


class HistoryInput(IdentityInput):
    lookback_days: int = Field(default=400, alias="lookbackDays", ge=2, le=3650)


class NewsInput(IdentityInput):
    days: int = Field(default=30, ge=1, le=30)


class RawFact(StrictModel):
    metric: str = Field(min_length=1, max_length=100)
    value: Any
    unit: str | None = Field(default=None, max_length=30)
    period_end: str | None = Field(default=None, alias="periodEnd")
    period_type: str | None = Field(default=None, alias="periodType")
    reporting_basis: str = Field(default="UNKNOWN", alias="reportingBasis")
    as_of: datetime | None = Field(default=None, alias="asOf")
    published_at: datetime | None = Field(default=None, alias="publishedAt")
    confidence: float = Field(default=0.78, ge=0, le=1)
    raw_field_origin: str | None = Field(default=None, alias="rawFieldOrigin", max_length=200)

    @field_validator("value")
    @classmethod
    def finite_scalar(cls, value: Any) -> Any:
        if isinstance(value, (dict, list, tuple)):
            raise ValueError("fact value must be scalar")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("fact value must be finite")
        if isinstance(value, Decimal) and not value.is_finite():
            raise ValueError("fact value must be finite")
        return value

    @field_validator("period_end")
    @classmethod
    def valid_period_end(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("periodEnd must be an ISO date") from exc
        return value

    @field_validator("period_type")
    @classmethod
    def valid_period_type(cls, value: str | None) -> str | None:
        if value is None:
            return None
        result = value.strip().upper()
        if result not in {"ANNUAL", "QUARTERLY", "AS_AT"}:
            raise ValueError("periodType is unsupported")
        return result


class RawPrice(StrictModel):
    observed_at: datetime = Field(alias="observedAt")
    close: Decimal
    currency: str | None = None

    @field_validator("close")
    @classmethod
    def positive_finite(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("close must be positive and finite")
        return value


class RawProfile(StrictModel):
    company_name: str | None = Field(default=None, alias="companyName", max_length=300)
    sector: str | None = Field(default=None, max_length=200)
    industry: str | None = Field(default=None, max_length=200)


class RawArticle(StrictModel):
    headline: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=1, max_length=2048)
    published_at: datetime = Field(alias="publishedAt")
    publisher: str = Field(default="Yahoo Finance", max_length=200)
    issuer_symbol: str = Field(alias="issuerSymbol", min_length=1, max_length=40)
    summary: str | None = Field(default=None, max_length=2000)

    @field_validator("url")
    @classmethod
    def http_url(cls, value: str) -> str:
        if not value.startswith(("https://", "http://")):
            raise ValueError("news URL must use HTTP(S)")
        return value


class ToolPayload(StrictModel):
    schema_version: str = Field(default=TOOL_SCHEMA_VERSION, alias="schemaVersion")
    adapter_version: str = Field(default=ADAPTER_VERSION, alias="adapterVersion")
    source: Literal["YAHOO_FINANCE"] = "YAHOO_FINANCE"
    global_instrument_id: UUID = Field(alias="globalInstrumentId")
    symbol: str = Field(min_length=1, max_length=40)
    exchange: str | None = Field(default=None, max_length=100)
    currency: str | None = Field(default=None, max_length=20)
    as_of: datetime | None = Field(default=None, alias="asOf")
    retrieved_at: datetime = Field(alias="retrievedAt")
    source_url: str = Field(alias="sourceUrl", min_length=1, max_length=2048)
    price: Decimal | None = None
    facts: tuple[RawFact, ...] = ()
    prices: tuple[RawPrice, ...] = ()
    profile: RawProfile | None = None
    news: tuple[RawArticle, ...] = ()
    news_query_succeeded: bool = Field(default=False, alias="newsQuerySucceeded")

    @model_validator(mode="after")
    def valid_payload(self):
        if self.schema_version != TOOL_SCHEMA_VERSION:
            raise ValueError("unsupported schema version")
        if not self.source_url.startswith(("https://", "http://")):
            raise ValueError("source URL must use HTTP(S)")
        if self.price is not None and (not self.price.is_finite() or self.price <= 0):
            raise ValueError("price must be positive and finite")
        return self

    def wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)
