"""Entirely SYNTHETIC unit fixtures, not captured or actual BSE evidence.

599001 and the fixture ISIN are arbitrary. Official host URLs below exercise
provenance validation only; no request or actual BSE listing is claimed.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import httpx
import pytest

from app.bse_etf_market import (
    BseContractError, BseEtfMarketProvider, BseOfflineIdentity, BseOfflineSession,
    LIVE_SOURCE_CONTRACT, require_verified_live_pair, validate_synthetic_session, verified_bse_identity,
)
from app.etf_evidence import EtfAuthority, EtfListing, EtfMetric, EtfProvenance
from app.models import SourceMode


NOW = datetime(2026, 11, 4, 11, 0, tzinfo=timezone.utc)
STAMP = NOW - timedelta(minutes=20)
KEY = UUID("10000000-0000-0000-0000-000000000001")
ISIN = "INF000000001"
CODE = "599001"


def provenance(provider="BSE", **changes):
    """DEMO source identity and markers make the fake provenance explicit."""
    return EtfProvenance(provider=provider, source_type="SYNTHETIC_UNIT_TEST",
        source_identity=f"SYNTHETIC:{provider}:UNIT_TEST",
        source_url=f"https://www.{provider.lower()}india.com/",
        retrieved_at=NOW, authority=EtfAuthority.OFFICIAL_EXCHANGE,
        source_mode=SourceMode.DEMO,
        source_metadata={"synthetic": True, "fixture_kind": "SYNTHETIC"},
        **changes)


def canonical():
    return {"globalInstrumentId": str(KEY), "isin": ISIN, "assetType": "ETF", "status": "ACTIVE",
        "primaryExchange": "NSE", "primarySymbol": "ABSL10BANK", "country": "IN", "currency": "INR",
        "providerMappings": [
            {"provider": "NSE", "providerSymbol": "ABSL10BANK", "providerInstrumentId": ISIN,
             "status": "VERIFIED", "exchange": "NSE", "currency": "INR",
             "resolutionSource": "SYNTHETIC_UNIT_TEST"},
            {"provider": "BSE", "providerSymbol": CODE, "providerInstrumentId": CODE,
             "status": "VERIFIED", "exchange": "BSE", "currency": "INR",
             "resolutionSource": "SYNTHETIC_UNIT_TEST"},
        ]}


def listings():
    return [EtfListing(instrument_id=KEY, symbol="ABSL10BANK", isin=ISIN,
                       name="Synthetic ETF identity fixture", provenance=provenance("NSE"))]


def binding():
    return verified_bse_identity(canonical(), listings())


def identity(**changes):
    values = dict(security_code=CODE, isin=ISIN, instrument_type="ETF", currency="INR",
                  exchange="BSE", provenance=provenance())
    return BseOfflineIdentity(**(values | changes))


def session(**changes):
    values = dict(security_code=CODE, isin=ISIN, instrument_type="ETF", currency="INR", exchange="BSE",
        session_date=STAMP.date(), session_timestamp=STAMP, last_trade_timestamp=STAMP - timedelta(minutes=2),
        executed_price="123.45", executed_quantity="100", price_type="EXECUTED_TRADE",
        volume_type="EXECUTED_QUANTITY", provenance=provenance())
    return BseOfflineSession(**(values | changes))


def validate(*, identities=None, sessions=None, current=NOW):
    return validate_synthetic_session(binding(), identities if identities is not None else [identity()],
                                      sessions if sessions is not None else [session()], current)


def test_verified_offline_security_and_isin_binding_preserves_canonical_nse_identity():
    row, official = canonical(), listings()
    before = deepcopy(row), official[0].model_dump()
    result = verified_bse_identity(row, official)
    assert (result.instrument_id, result.nse_symbol, result.isin, result.security_code) == (
        KEY, "ABSL10BANK", ISIN, CODE)
    assert result.listing_source_mode == SourceMode.DEMO
    assert row == before[0] and official[0].model_dump() == before[1]


def test_candidate_table_is_never_used_as_mapping_authority():
    row = canonical()
    row["providerMappings"] = row["providerMappings"][:1]
    with pytest.raises(BseContractError, match="NO_UNAMBIGUOUS_BSE_MAPPING"):
        verified_bse_identity(row, listings())
    # A synthetic, already trusted mapping can use a different fixture code;
    # no hardcoded candidate code is imposed or assumed to prove identity.
    assert binding().security_code == CODE


@pytest.mark.parametrize("change", [
    {"status": "INACTIVE"}, {"assetType": "EQUITY"}, {"primaryExchange": "BSE"},
    {"primarySymbol": "UNSCOPED_ETF"}, {"currency": "USD"}, {"tradingCurrency": "USD"}, {"country": "US"},
    {"isin": "invalid"}, {"primarySymbol": "absl10bank"},
])
def test_canonical_contract_rejects_unsupported_or_mutated_identity(change):
    with pytest.raises(BseContractError, match="BSE_UNSUPPORTED_CANONICAL_ETF"):
        verified_bse_identity(canonical() | change, listings())


@pytest.mark.parametrize("value", [None, "not-a-uuid", ""])
def test_missing_or_malformed_canonical_uuid_rejected(value):
    with pytest.raises(BseContractError, match="BSE_CANONICAL_UUID_REQUIRED"):
        verified_bse_identity(canonical() | {"globalInstrumentId": value}, listings())


def test_conflicting_canonical_uuid_fields_rejected():
    with pytest.raises(BseContractError, match="BSE_CANONICAL_UUID_MISMATCH"):
        verified_bse_identity(canonical() | {"instrumentId": str(UUID(int=2))}, listings())


@pytest.mark.parametrize("change", [
    {"isin": "INF000000002"}, {"symbol": "ABSLMSCIN"}, {"instrument_id": UUID(int=2)},
])
def test_official_listing_must_match_uuid_symbol_and_isin(change):
    official = listings()[0].model_copy(update=change)
    with pytest.raises(BseContractError, match="BSE_CANONICAL_LISTING_MISMATCH"):
        verified_bse_identity(canonical(), [official])


@pytest.mark.parametrize("count", [0, 2])
def test_missing_or_duplicate_canonical_listing_rejected(count):
    with pytest.raises(BseContractError, match="BSE_AMBIGUOUS_OR_MISSING_CANONICAL_LISTING"):
        verified_bse_identity(canonical(), listings() * count)


@pytest.mark.parametrize("provider", ["NSE", "BSE"])
@pytest.mark.parametrize("change", [
    {"status": "RESOLVED"}, {"status": "PENDING"}, {"status": None}, {"active": False},
    {"active": "true"}, {"resolutionSource": "BROKER_IMPORT_IDENTITY"},
    {"resolutionSource": ""}, {"failureReason": "INACTIVE_MAPPING"},
])
def test_only_verified_active_nonbroker_mappings_are_trusted(provider, change):
    row = canonical()
    index = 0 if provider == "NSE" else 1
    row["providerMappings"][index].update(change)
    with pytest.raises(BseContractError, match=f"NO_TRUSTED_{provider}_MAPPING"):
        verified_bse_identity(row, listings())


@pytest.mark.parametrize("provider", ["NSE", "BSE"])
def test_duplicate_provider_mapping_is_ambiguous_even_if_identical(provider):
    row = canonical()
    row["providerMappings"].append(dict(row["providerMappings"][0 if provider == "NSE" else 1]))
    with pytest.raises(BseContractError, match=f"NO_UNAMBIGUOUS_{provider}_MAPPING"):
        verified_bse_identity(row, listings())


@pytest.mark.parametrize("change,reason", [
    ({"providerInstrumentId": "599002"}, "BSE_MAPPING_SECURITY_CODE_CONFLICT"),
    ({"providerSymbol": "UNVERIFIED_ALIAS"}, "BSE_SECURITY_CODE_REQUIRED"),
    ({"providerSymbol": None, "providerInstrumentId": None}, "BSE_SECURITY_CODE_REQUIRED"),
    ({"providerInstrumentId": "599001.0"}, "BSE_SECURITY_CODE_REQUIRED"),
    ({"isin": "INF000000002"}, "BSE_MAPPING_ISIN_MISMATCH"),
    ({"instrumentId": str(UUID(int=2))}, "BSE_MAPPING_UUID_MISMATCH"),
    ({"exchange": "NSE"}, "INVALID_BSE_MAPPING"),
    ({"currency": "USD"}, "INVALID_BSE_MAPPING"),
])
def test_mapping_identifiers_and_security_code_must_be_unambiguous(change, reason):
    row = canonical()
    row["providerMappings"][1].update(change)
    with pytest.raises(BseContractError, match=reason):
        verified_bse_identity(row, listings())


def test_canonical_nse_mapping_must_still_match_primary_symbol_and_isin():
    row = canonical()
    row["providerMappings"][0]["providerInstrumentId"] = "INF000000002"
    with pytest.raises(BseContractError, match="BSE_CANONICAL_NSE_MAPPING_MISMATCH"):
        verified_bse_identity(row, listings())


def test_synthetic_executed_pair_is_bound_to_same_security_venue_and_session():
    price, volume = validate()
    assert (price.metric, volume.metric) == (EtfMetric.MARKET_PRICE, EtfMetric.TRADING_VOLUME)
    assert (price.value, volume.value) == (Decimal("123.45"), Decimal("100"))
    assert (price.unit, volume.unit) == ("INR", "shares")
    assert price.instrument_id == volume.instrument_id == KEY
    assert price.as_of_date == volume.as_of_date == STAMP.date()
    assert price.provenance.source_identity == volume.provenance.source_identity
    assert price.provenance.provider == volume.provenance.provider == "BSE"
    assert price.provenance.source_type == "BSE_VERIFIED_ETF_SESSION"
    assert price.provenance.source_mode == volume.provenance.source_mode == SourceMode.DEMO
    metadata = price.provenance.source_metadata
    assert metadata["synthetic"] is True and metadata["fixture_kind"] == "SYNTHETIC"
    assert metadata["security_code"] == CODE and metadata["isin"] == ISIN
    assert metadata["identity_verified"] is True and metadata["exchange"] == "BSE"
    assert datetime.fromisoformat(metadata["last_trade_timestamp"]).date() == price.as_of_date


@pytest.mark.parametrize("factory,kind", [(identity, "IDENTITY"), (session, "SESSION")])
@pytest.mark.parametrize("change,reason", [
    ({"security_code": "599002"}, "BSE_SECURITY_CODE_MISMATCH"),
    ({"isin": "INF000000002"}, "BSE_ISIN_MISMATCH"),
    ({"instrument_type": "EQUITY"}, "BSE_ETF_EXCHANGE_CURRENCY_MISMATCH"),
    ({"currency": "USD"}, "BSE_ETF_EXCHANGE_CURRENCY_MISMATCH"),
    ({"exchange": "NSE"}, "BSE_ETF_EXCHANGE_CURRENCY_MISMATCH"),
])
def test_independent_identity_and_session_reject_wrong_code_isin_or_venue(factory, kind, change, reason):
    kwargs = {"identities" if kind == "IDENTITY" else "sessions": [factory(**change)]}
    with pytest.raises(BseContractError, match=reason):
        validate(**kwargs)


@pytest.mark.parametrize("kind", ["IDENTITY", "SESSION"])
@pytest.mark.parametrize("count", [0, 2])
def test_absent_or_duplicate_bse_identity_and_session_fail_closed(kind, count):
    kwargs = {"identities" if kind == "IDENTITY" else "sessions":
              [identity() if kind == "IDENTITY" else session()] * count}
    reason = f"BSE_{kind}_NOT_FOUND" if count == 0 else f"BSE_AMBIGUOUS_{kind}"
    with pytest.raises(BseContractError, match=reason):
        validate(**kwargs)


def test_conflicting_identity_under_same_isin_is_ambiguous():
    with pytest.raises(BseContractError, match="BSE_AMBIGUOUS_IDENTITY"):
        validate(identities=[identity(), identity(security_code="599002")])


def test_two_sessions_cannot_be_selected_or_combined():
    earlier = STAMP - timedelta(hours=1)
    with pytest.raises(BseContractError, match="BSE_AMBIGUOUS_SESSION"):
        validate(sessions=[session(), session(session_timestamp=earlier, last_trade_timestamp=earlier)])


@pytest.mark.parametrize("field", ["session_timestamp", "last_trade_timestamp"])
@pytest.mark.parametrize("value", [None, "2026-11-04T10:40:00+00:00", datetime(2026, 11, 4, 10, 40)])
def test_missing_invalid_and_naive_session_timestamps_rejected(field, value):
    reason = "BSE_VALID_SESSION_TIMESTAMP_REQUIRED" if field == "session_timestamp" else "BSE_VALID_LAST_TRADE_TIMESTAMP_REQUIRED"
    with pytest.raises(BseContractError, match=reason):
        validate(sessions=[session(**{field: value})])


@pytest.mark.parametrize("value", [None, "2026-11-04", STAMP.date() - timedelta(days=1)])
def test_missing_invalid_or_incompatible_session_date_rejected(value):
    with pytest.raises(BseContractError, match="BSE_SESSION_DATE_MISMATCH"):
        validate(sessions=[session(session_date=value)])


def test_future_session_rejected():
    future = NOW + timedelta(minutes=1)
    with pytest.raises(BseContractError, match="BSE_FUTURE_SESSION"):
        validate(sessions=[session(session_timestamp=future, last_trade_timestamp=future)])


def test_stale_session_rejected():
    stale = NOW - timedelta(days=2)
    with pytest.raises(BseContractError, match="BSE_STALE_SESSION"):
        validate(sessions=[session(session_date=stale.date(), session_timestamp=stale, last_trade_timestamp=stale)])


def test_fresh_snapshot_cannot_relabel_previous_session_last_trade():
    with pytest.raises(BseContractError, match="BSE_LAST_TRADE_SESSION_MISMATCH"):
        validate(sessions=[session(last_trade_timestamp=STAMP - timedelta(days=1))])


def test_trade_timestamp_cannot_follow_its_snapshot():
    with pytest.raises(BseContractError, match="BSE_LAST_TRADE_SESSION_MISMATCH"):
        validate(sessions=[session(last_trade_timestamp=STAMP + timedelta(minutes=1))])


@pytest.mark.parametrize("value", [None, "", "-", 0, -1, True, "NaN", "Infinity", "garbage"])
def test_absent_or_invalid_executed_price_rejected(value):
    with pytest.raises(BseContractError, match="BSE_EXECUTED_PRICE_REQUIRED"):
        validate(sessions=[session(executed_price=value)])


@pytest.mark.parametrize("value", [None, "", 0, "0", -1, True, "0.5", "NaN", "Infinity"])
def test_zero_absent_or_nonintegral_executed_volume_rejected(value):
    with pytest.raises(BseContractError, match="BSE_POSITIVE_EXECUTED_QUANTITY_REQUIRED"):
        validate(sessions=[session(executed_quantity=value)])


@pytest.mark.parametrize("change", [
    {"price_type": "NAV"}, {"price_type": "INAV"}, {"price_type": "PREVIOUS_CLOSE"},
    {"price_type": "LAST_TRADED_PRICE"}, {"volume_type": "TURNOVER"},
])
def test_nav_inav_previous_close_and_undefined_last_price_cannot_replace_trades(change):
    with pytest.raises(BseContractError, match="BSE_EXECUTED_TRADE_CONTRACT_REQUIRED"):
        validate(sessions=[session(**change)])


@pytest.mark.parametrize("change", [
    {"provider": "NSE"}, {"authority": EtfAuthority.SECONDARY},
    {"source_url": "http://www.bseindia.com/"},
    {"source_url": "https://www.bseindia.com.evil.invalid/"},
    {"source_url": "https://www.bseindia.com@evil.invalid/"},
    {"source_url": "https://www.bseindia.com:8443/"},
    {"source_url": "https://example.invalid/"},
])
def test_official_exchange_provenance_is_required_for_both_independent_sources(change):
    bad = provenance().model_copy(update=change)
    for kwargs in ({"identities": [identity(provenance=bad)]}, {"sessions": [session(provenance=bad)]}):
        with pytest.raises(BseContractError, match="BSE_OFFICIAL_PROVENANCE_REQUIRED"):
            validate(**kwargs)


@pytest.mark.parametrize("change", [
    {"source_mode": SourceMode.REAL}, {"source_metadata": None},
    {"source_metadata": {"synthetic": True}},
    {"source_metadata": {"synthetic": False, "fixture_kind": "SYNTHETIC"}},
])
def test_offline_inputs_must_explicitly_be_synthetic_demo(change):
    with pytest.raises(BseContractError, match="BSE_SYNTHETIC_INPUT_REQUIRED"):
        validate(sessions=[session(provenance=provenance().model_copy(update=change))])


@pytest.mark.parametrize("retrieved", [NOW + timedelta(seconds=1), STAMP - timedelta(seconds=1)])
def test_retrieval_timestamp_cannot_precede_observation_or_be_future(retrieved):
    with pytest.raises(BseContractError, match="BSE_PROVENANCE_TIMESTAMP_MISMATCH"):
        validate(sessions=[session(provenance=provenance().model_copy(update={"retrieved_at": retrieved}))])


def test_independent_reference_identity_can_be_retrieved_before_the_market_session():
    earlier = provenance().model_copy(update={"retrieved_at": NOW - timedelta(days=1)})
    price, volume = validate(identities=[identity(provenance=earlier)])
    assert price.provenance.source_mode == volume.provenance.source_mode == SourceMode.DEMO
    captured = price.provenance.source_metadata["identity_source"]["retrieved_at"]
    assert datetime.fromisoformat(captured.replace("Z", "+00:00")) == earlier.retrieved_at


@pytest.mark.asyncio
async def test_live_provider_fails_without_network_or_fabricated_success(monkeypatch):
    calls = []

    async def forbidden_request(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Live adapter must not make a request")

    monkeypatch.setattr(httpx.AsyncClient, "get", forbidden_request)
    assert LIVE_SOURCE_CONTRACT is None
    provider = BseEtfMarketProvider()
    assert provider.provider_name == "BSE"
    with pytest.raises(BseContractError, match="^BSE_LIVE_SOURCE_CONTRACT_UNVERIFIED$"):
        await provider.collect(binding())
    assert calls == []


def test_synthetic_success_never_passes_the_production_admission_gate():
    with pytest.raises(BseContractError, match="^BSE_LIVE_SOURCE_CONTRACT_UNVERIFIED$"):
        require_verified_live_pair(binding(), validate(), now=NOW)


@pytest.mark.asyncio
async def test_liquidbeta_remains_blocked_for_live_acquisition():
    row = canonical() | {"primarySymbol": "LIQUIDBETA"}
    row["providerMappings"][0]["providerSymbol"] = "LIQUIDBETA"
    listing = listings()[0].model_copy(update={"symbol": "LIQUIDBETA"})
    linked = verified_bse_identity(row, [listing])
    with pytest.raises(BseContractError, match="BSE_LIVE_SOURCE_CONTRACT_UNVERIFIED"):
        await BseEtfMarketProvider().collect(linked)
