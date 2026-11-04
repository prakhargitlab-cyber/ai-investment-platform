"""Fail-closed BSE boundary and an explicitly synthetic, offline contract.

There is no verified live response schema or live parser in this module.
The normalized dataclasses below are application contracts for unit tests;
their field names are not claimed to be BSE response fields. They cannot
produce REAL evidence, and this adapter performs no network or database I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Iterable
from urllib.parse import urlsplit
from uuid import UUID

from app.etf_evidence import ETF_MAX_AGE, EtfAuthority, EtfFact, EtfListing, EtfMetric, EtfProvenance
from app.models import SourceMode
from app.portfolio_orchestration import _trusted_mapping_for_provider


LIVE_SOURCE_CONTRACT = None
SUPPORTED_NSE_SYMBOLS = frozenset({"ABSL10BANK", "ABSLMSCIN", "MSCIADD", "LIQUIDBETA"})
IST = timezone(timedelta(hours=5, minutes=30))
_SECURITY_CODE = re.compile(r"[0-9]{6}\Z")
_ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]\Z")
_OFFICIAL_HOSTS = {
    "NSE": frozenset({"www.nseindia.com", "nseindia.com", "nsearchives.nseindia.com"}),
    "BSE": frozenset({"www.bseindia.com", "bseindia.com"}),
}


class BseContractError(ValueError):
    """Static validation reason, safe to retain in an acquisition attempt."""


@dataclass(frozen=True)
class BseIdentityBinding:
    instrument_id: UUID
    nse_symbol: str
    isin: str
    security_code: str
    listing_source_mode: str = SourceMode.REAL
    currency: str = "INR"
    exchange: str = "BSE"


@dataclass(frozen=True)
class BseOfflineIdentity:
    """Synthetic normalized independent identity, never an actual feed row."""

    security_code: str
    isin: str
    instrument_type: str
    currency: str
    exchange: str
    provenance: EtfProvenance


@dataclass(frozen=True)
class BseOfflineSession:
    """Synthetic executed-session observation; no NAV/close aliases exist."""

    security_code: str
    isin: str
    instrument_type: str
    currency: str
    exchange: str
    session_date: date | None
    session_timestamp: datetime | None
    last_trade_timestamp: datetime | None
    executed_price: Decimal | str | int | None
    executed_quantity: Decimal | str | int | None
    price_type: str
    volume_type: str
    provenance: EtfProvenance


def _official_provenance(provenance: EtfProvenance, provider: str, *, synthetic=False) -> None:
    if not isinstance(provenance, EtfProvenance):
        raise BseContractError(f"{provider}_OFFICIAL_PROVENANCE_REQUIRED")
    try:
        parsed = urlsplit(provenance.source_url)
        valid_url = (parsed.scheme == "https" and parsed.hostname in _OFFICIAL_HOSTS[provider]
                     and parsed.port in {None, 443} and not parsed.username and not parsed.password)
    except ValueError:
        valid_url = False
    if (provenance.provider != provider or provenance.authority != EtfAuthority.OFFICIAL_EXCHANGE
            or not valid_url):
        raise BseContractError(f"{provider}_OFFICIAL_PROVENANCE_REQUIRED")
    if synthetic and (provenance.source_mode != SourceMode.DEMO
                      or (provenance.source_metadata or {}).get("synthetic") is not True
                      or (provenance.source_metadata or {}).get("fixture_kind") != "SYNTHETIC"):
        raise BseContractError("BSE_SYNTHETIC_INPUT_REQUIRED")


def _uuid(value, reason: str) -> UUID:
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise BseContractError(reason) from None


def _mapping(row: dict, provider: str, instrument_id: UUID, isin: str) -> dict:
    mappings = row.get("providerMappings")
    if not isinstance(mappings, list):
        raise BseContractError(f"NO_UNAMBIGUOUS_{provider}_MAPPING")
    selected = [item for item in mappings if isinstance(item, dict)
                and str(item.get("provider") or "").upper() == provider]
    if len(selected) != 1:
        raise BseContractError(f"NO_UNAMBIGUOUS_{provider}_MAPPING")
    mapping = selected[0]
    # BSE's general helper permits RESOLVED. Alternate-exchange recovery
    # deliberately adds the VERIFIED-only bar and broker-import exclusion.
    source = str(mapping.get("resolutionSource") or mapping.get("resolution_source") or "").upper()
    if (not _trusted_mapping_for_provider(mapping, provider) or mapping.get("status") != "VERIFIED"
            or ("active" in mapping and mapping["active"] is not True)
            or not source or source == "BROKER_IMPORT_IDENTITY"
            or mapping.get("failureReason")):
        raise BseContractError(f"NO_TRUSTED_{provider}_MAPPING")
    exchanges = {"NSE", "XNSE"} if provider == "NSE" else {"BSE", "XBOM"}
    if mapping.get("exchange") not in exchanges or mapping.get("currency") not in {None, "INR"}:
        raise BseContractError(f"INVALID_{provider}_MAPPING")
    for field in ("instrumentId", "globalInstrumentId"):
        if field in mapping and _uuid(mapping[field], "BSE_MAPPING_UUID_MISMATCH") != instrument_id:
            raise BseContractError("BSE_MAPPING_UUID_MISMATCH")
    if "isin" in mapping and mapping["isin"] != isin:
        raise BseContractError("BSE_MAPPING_ISIN_MISMATCH")
    return mapping


def verified_bse_identity(row: dict, listings: Iterable[EtfListing]) -> BseIdentityBinding:
    """Bind existing trusted mappings to an unchanged canonical NSE listing.

    Candidate codes and ticker-only lookup tables are intentionally absent.
    A structural DEMO binding remains DEMO; it is not live identity evidence.
    The public mapping API omits ``active``: VERIFIED is its active trust
    state, and an explicitly supplied inactive flag is additionally rejected.
    """
    if not isinstance(row, dict):
        raise BseContractError("BSE_CANONICAL_IDENTITY_REQUIRED")
    instrument_id = _uuid(row.get("globalInstrumentId") or row.get("instrumentId"),
                          "BSE_CANONICAL_UUID_REQUIRED")
    for field in ("instrumentId", "globalInstrumentId"):
        if field in row and _uuid(row[field], "BSE_CANONICAL_UUID_MISMATCH") != instrument_id:
            raise BseContractError("BSE_CANONICAL_UUID_MISMATCH")
    symbol = row.get("primarySymbol")
    isin = row.get("isin")
    if (row.get("status") != "ACTIVE" or row.get("assetType") != "ETF"
            or row.get("primaryExchange") not in {"NSE", "XNSE"}
            or row.get("country") not in {"IN", "IND", "INDIA"}
            or row.get("currency") != "INR"
            or row.get("tradingCurrency") not in {None, "INR"}
            or symbol not in SUPPORTED_NSE_SYMBOLS or not isinstance(isin, str)
            or not _ISIN.fullmatch(isin)):
        raise BseContractError("BSE_UNSUPPORTED_CANONICAL_ETF")
    relevant = [item for item in listings if isinstance(item, EtfListing)
                and (item.instrument_id == instrument_id or item.isin == isin or item.symbol == symbol)]
    if len(relevant) != 1:
        raise BseContractError("BSE_AMBIGUOUS_OR_MISSING_CANONICAL_LISTING")
    listing = relevant[0]
    if (listing.instrument_id != instrument_id or listing.isin != isin or listing.symbol != symbol
            or listing.exchange != "NSE" or listing.asset_type != "ETF"):
        raise BseContractError("BSE_CANONICAL_LISTING_MISMATCH")
    _official_provenance(listing.provenance, "NSE")
    nse = _mapping(row, "NSE", instrument_id, isin)
    if nse.get("providerSymbol") != symbol or nse.get("providerInstrumentId") not in {None, isin}:
        raise BseContractError("BSE_CANONICAL_NSE_MAPPING_MISMATCH")
    bse = _mapping(row, "BSE", instrument_id, isin)
    identifiers = [str(bse[field]).strip() for field in ("providerSymbol", "providerInstrumentId")
                   if bse.get(field) is not None and str(bse[field]).strip()]
    # No unverified symbolic alias is silently interpreted as a code.
    if not identifiers or any(not _SECURITY_CODE.fullmatch(value) for value in identifiers):
        raise BseContractError("BSE_SECURITY_CODE_REQUIRED")
    if len(set(identifiers)) != 1:
        raise BseContractError("BSE_MAPPING_SECURITY_CODE_CONFLICT")
    return BseIdentityBinding(instrument_id=instrument_id, nse_symbol=symbol, isin=isin,
                              security_code=identifiers[0], listing_source_mode=listing.provenance.source_mode)


def _one_matching(rows, binding: BseIdentityBinding, expected_type, kind: str):
    items = list(rows)
    if any(not isinstance(item, expected_type) for item in items):
        raise BseContractError(f"BSE_INVALID_{kind}_CONTRACT")
    matches = [item for item in items
               if item.security_code == binding.security_code or item.isin == binding.isin]
    if len(matches) > 1:
        raise BseContractError(f"BSE_AMBIGUOUS_{kind}")
    if not matches:
        raise BseContractError(f"BSE_{kind}_NOT_FOUND")
    record = matches[0]
    if record.security_code != binding.security_code:
        raise BseContractError("BSE_SECURITY_CODE_MISMATCH")
    if record.isin != binding.isin:
        raise BseContractError("BSE_ISIN_MISMATCH")
    if record.exchange != "BSE" or record.currency != "INR" or record.instrument_type != "ETF":
        raise BseContractError("BSE_ETF_EXCHANGE_CURRENCY_MISMATCH")
    _official_provenance(record.provenance, "BSE", synthetic=True)
    return record


def _aware(value, reason: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BseContractError(reason)
    return value.astimezone(IST)


def _positive_number(value, *, integer=False) -> Decimal:
    reason = "BSE_POSITIVE_EXECUTED_QUANTITY_REQUIRED" if integer else "BSE_EXECUTED_PRICE_REQUIRED"
    if value is None or isinstance(value, bool):
        raise BseContractError(reason)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise BseContractError(reason) from None
    if (not result.is_finite() or result <= 0
            or (integer and result != result.to_integral_value())):
        raise BseContractError(reason)
    return result


def validate_synthetic_session(binding: BseIdentityBinding, identities: Iterable[BseOfflineIdentity],
                               sessions: Iterable[BseOfflineSession], now: datetime) -> tuple[EtfFact, EtfFact]:
    """Validate a synthetic pair for offline review; always emits DEMO facts.

    The same security, exchange, currency and actual session bind both values.
    A previous close, NAV, iNAV or carried-forward last price cannot satisfy
    this contract, even when the snapshot itself has a fresh timestamp.
    """
    if (not isinstance(binding, BseIdentityBinding) or binding.nse_symbol not in SUPPORTED_NSE_SYMBOLS
            or not _SECURITY_CODE.fullmatch(binding.security_code) or not _ISIN.fullmatch(binding.isin)
            or binding.currency != "INR" or binding.exchange != "BSE"):
        raise BseContractError("BSE_INVALID_IDENTITY_BINDING")
    identity = _one_matching(identities, binding, BseOfflineIdentity, "IDENTITY")
    session = _one_matching(sessions, binding, BseOfflineSession, "SESSION")
    current = _aware(now, "BSE_AWARE_NOW_REQUIRED")
    stamp = _aware(session.session_timestamp, "BSE_VALID_SESSION_TIMESTAMP_REQUIRED")
    trade = _aware(session.last_trade_timestamp, "BSE_VALID_LAST_TRADE_TIMESTAMP_REQUIRED")
    if type(session.session_date) is not date or session.session_date != stamp.date():
        raise BseContractError("BSE_SESSION_DATE_MISMATCH")
    if stamp > current or trade > current:
        raise BseContractError("BSE_FUTURE_SESSION")
    if trade.date() != stamp.date() or trade > stamp:
        raise BseContractError("BSE_LAST_TRADE_SESSION_MISMATCH")
    if (current - stamp > ETF_MAX_AGE[EtfMetric.MARKET_PRICE]
            or current - trade > ETF_MAX_AGE[EtfMetric.MARKET_PRICE]):
        raise BseContractError("BSE_STALE_SESSION")
    # A reference identity may have been fetched before this trading
    # session. Its retrieval time is not the market observation time.
    if (identity.provenance.retrieved_at > current or session.provenance.retrieved_at > current
            or session.provenance.retrieved_at < stamp):
        raise BseContractError("BSE_PROVENANCE_TIMESTAMP_MISMATCH")
    if session.price_type != "EXECUTED_TRADE" or session.volume_type != "EXECUTED_QUANTITY":
        raise BseContractError("BSE_EXECUTED_TRADE_CONTRACT_REQUIRED")
    price = _positive_number(session.executed_price)
    volume = _positive_number(session.executed_quantity, integer=True)
    metadata = {
        "exchange": "BSE", "security_code": binding.security_code, "isin": binding.isin,
        "session_date": stamp.date().isoformat(), "session_timestamp": stamp.isoformat(),
        "last_trade_timestamp": trade.isoformat(), "price_type": "EXECUTED_TRADE",
        "volume_type": "EXECUTED_QUANTITY", "identity_verified": True,
        "currency": "INR", "instrument_type": "ETF", "synthetic": True, "fixture_kind": "SYNTHETIC",
        "identity_source": identity.provenance.model_dump(mode="json"),
    }
    provenance = EtfProvenance(provider="BSE", source_type="BSE_VERIFIED_ETF_SESSION",
        source_identity=f"SYNTHETIC:BSE:{binding.security_code}:{binding.isin}:{stamp.isoformat()}",
        source_url=session.provenance.source_url, source_metadata=metadata,
        source_locator="SYNTHETIC_NORMALIZED_SESSION", retrieved_at=now,
        authority=EtfAuthority.OFFICIAL_EXCHANGE, source_mode=SourceMode.DEMO)
    return tuple(EtfFact(instrument_id=binding.instrument_id, metric=metric, value=value,
                        unit=unit, as_of_date=stamp.date(), provenance=provenance)
                 for metric, value, unit in ((EtfMetric.MARKET_PRICE, price, "INR"),
                                             (EtfMetric.TRADING_VOLUME, volume, "shares")))


def require_verified_live_pair(binding: BseIdentityBinding, evidence, *, now: datetime):
    """The production admission gate stays closed until its source is verified."""
    raise BseContractError("BSE_LIVE_SOURCE_CONTRACT_UNVERIFIED")


class BseEtfMarketProvider:
    provider_name = "BSE"

    async def collect(self, binding: BseIdentityBinding):
        """No invented endpoint, guessed parser, or network attempt."""
        raise BseContractError("BSE_LIVE_SOURCE_CONTRACT_UNVERIFIED")
