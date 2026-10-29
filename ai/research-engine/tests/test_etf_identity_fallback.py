"""Regression tests for the ETF-specific historical-identity fallback in
app.structured_market.verify_historical_price_identity (Full NSE ETF Radar
historical identity validation correction).

Yahoo frequently omits ISIN for Indian-listed ETFs and returns either the
raw ticker, a truncated AMC-prefix name, or the underlying benchmark index
name as longName/shortName instead of the fund's own registered name --
fuzzy name similarity (_name_similarity, threshold 0.55) then rejects a
genuinely correct match. These tests prove the new ETF-specific fallback
(exact bare canonical NSE symbol + Yahoo's own returned quoteType=="ETF" +
already-established exchange/currency agreement) accepts only the narrow,
safe cases it was designed for, while leaving every other path -- ISIN
conflicts, wrong exchange/symbol/type, and the existing equity path --
completely unchanged. The shared name-similarity threshold itself
(_name_similarity, verify_identity_by_isin_or_name) is never modified or
bypassed by this fallback; it is consulted ONLY when that shared gate has
already specifically returned COMPANY_NAME_MISMATCH.
"""
from app.structured_market import verify_historical_price_identity


ETF_BASE = dict(
    expected_isin=None,
    expected_name="Nippon India ETF Nifty 50 BeES",
    expected_exchange_family="XNSE",
    expected_currency="INR",
    returned_isin=None,
    returned_name="NIFTY 50",  # benchmark-index name, not the fund's own name
    returned_exchange="NSI",
    returned_currency="INR",
    expected_symbol="NIFTYBEES",
    expected_asset_type="ETF",
    returned_symbol="NIFTYBEES.NS",
    returned_quote_type="ETF",
)


def test_scenario1_correct_verified_nse_etf_with_compatible_yahoo_identity_evidence():
    """A verified NSE ETF where Yahoo DOES return a matching ISIN is accepted
    by the existing, untouched ISIN-exact path -- never needs the new
    fallback at all."""
    kwargs = {**ETF_BASE, "expected_isin": "INF204KB17I5", "returned_isin": "INF204KB17I5",
              "returned_name": "Nippon India ETF Nifty 50 BeES"}
    assert verify_historical_price_identity(**kwargs) is None


def test_scenario2_yahoo_missing_isin_with_independently_verifiable_identity():
    """Yahoo omits ISIN (both sides empty) and returns only the benchmark
    name ("NIFTY 50") -- the shared name-similarity gate alone would reject
    this (COMPANY_NAME_MISMATCH), but the exact canonical symbol plus
    Yahoo's own confirmed quoteType=="ETF" plus already-agreeing exchange/
    currency together establish identity, so this is accepted."""
    assert verify_historical_price_identity(**ETF_BASE) is None


def test_scenario3_conflicting_yahoo_isin_fails_closed():
    """A genuinely conflicting, non-empty Yahoo ISIN must still fail closed
    -- the ETF fallback must NEVER be reachable once the shared gate has
    returned ISIN_MISMATCH, even when symbol/quoteType/exchange/currency
    would otherwise all agree."""
    kwargs = {**ETF_BASE, "expected_isin": "INF204KB17I5", "returned_isin": "INF000000000"}
    assert verify_historical_price_identity(**kwargs) == "ISIN_MISMATCH"


def test_scenario4_wrong_exchange_rejected():
    kwargs = {**ETF_BASE, "returned_exchange": "NYQ"}
    assert verify_historical_price_identity(**kwargs) == "EXCHANGE_MISMATCH"


def test_scenario4_wrong_quote_type_rejected():
    """Yahoo's ticker silently resolving to a non-ETF instrument (quoteType
    no longer "ETF") must not be treated as a verified match even though
    the symbol still matches and the name is benchmark-only."""
    kwargs = {**ETF_BASE, "returned_quote_type": "EQUITY"}
    assert verify_historical_price_identity(**kwargs) == "COMPANY_NAME_MISMATCH"


def test_scenario4_wrong_symbol_rejected():
    """A genuinely different ticker (symbol mismatch) must not be treated as
    a verified match even though quoteType/exchange/currency all agree."""
    kwargs = {**ETF_BASE, "returned_symbol": "JUNIORBEES.NS"}
    assert verify_historical_price_identity(**kwargs) == "COMPANY_NAME_MISMATCH"


def test_scenario5_benchmark_name_only_evidence_is_not_automatically_verified():
    """Symbol matching ALONE (no caller-asserted ETF classification, and no
    confirmed Yahoo quoteType) must never be treated as sufficient identity
    proof -- this is the literal requirement 'do not assume symbol matching
    alone is sufficient'. Each of the three independent signals is
    required; dropping any one must fall back to the original rejection."""
    # Missing expected_asset_type (caller never asserted this is an ETF).
    kwargs = {**ETF_BASE, "expected_asset_type": None}
    assert verify_historical_price_identity(**kwargs) == "COMPANY_NAME_MISMATCH"
    # Caller asserts ETF, but Yahoo's own quoteType is absent/unconfirmed.
    kwargs = {**ETF_BASE, "returned_quote_type": None}
    assert verify_historical_price_identity(**kwargs) == "COMPANY_NAME_MISMATCH"
    # expected_asset_type is something other than "ETF".
    kwargs = {**ETF_BASE, "expected_asset_type": "EQUITY"}
    assert verify_historical_price_identity(**kwargs) == "COMPANY_NAME_MISMATCH"


def test_trust_boundary_missing_exchange_and_currency_cannot_pass_on_symbol_alone():
    """Trust-boundary regression (final review): when Yahoo's response is
    maximally ambiguous -- no ISIN, no exchange, and no currency returned
    at all, only a bare symbol and quoteType -- the fallback must NOT pass.
    The general EXCHANGE_MISMATCH/CURRENCY_MISMATCH checks correctly do not
    raise when the PROVIDER side is simply absent (absence is not a
    conflict), but that must never be read by this fallback as "exchange/
    currency agreement" -- agreement requires both sides to actually be
    present and matching, not merely non-conflicting via omission."""
    kwargs = {**ETF_BASE, "returned_exchange": None, "returned_currency": None}
    assert verify_historical_price_identity(**kwargs) == "COMPANY_NAME_MISMATCH"
    # Only exchange missing (currency still present and agreeing) is also insufficient.
    kwargs = {**ETF_BASE, "returned_exchange": None}
    assert verify_historical_price_identity(**kwargs) == "COMPANY_NAME_MISMATCH"
    # Only currency missing (exchange still present and agreeing) is also insufficient.
    kwargs = {**ETF_BASE, "returned_currency": None}
    assert verify_historical_price_identity(**kwargs) == "COMPANY_NAME_MISMATCH"


def test_scenario6_existing_equity_validation_unchanged():
    """An equity caller passes no ETF-specific kwargs at all (as every
    existing call site for equities does) -- it must take the exact same
    code path and produce the exact same result as before this change: a
    name-similarity rejection stays rejected, a name-similarity pass stays
    passed, with the new kwargs having zero effect."""
    equity_kwargs = dict(
        expected_isin=None, expected_name="Infosys Limited",
        expected_exchange_family="XNSE", expected_currency="INR",
        returned_isin=None, returned_name="HCL Technologies Limited",
        returned_exchange="NSI", returned_currency="INR",
    )
    # Genuinely wrong company name -- still rejected, no ETF kwargs supplied.
    assert verify_historical_price_identity(**equity_kwargs) == "COMPANY_NAME_MISMATCH"
    # Even if an equity caller's instrument dict happened to produce a
    # matching bare symbol and someone mislabels quoteType "ETF", the
    # asset-type gate (expected_asset_type) was never asserted as "ETF" by
    # this caller, so the fallback still cannot fire.
    assert verify_historical_price_identity(
        **equity_kwargs, expected_symbol="INFY", returned_symbol="INFY.NS",
        returned_quote_type="ETF",
    ) == "COMPANY_NAME_MISMATCH"
    # A genuinely matching equity name is accepted exactly as before.
    equity_match = {**equity_kwargs, "returned_name": "Infosys Limited"}
    assert verify_historical_price_identity(**equity_match) is None
