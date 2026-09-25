"""Exact-next-engineering-action fix: `_trusted_nse_candidate_reason()` in
app/structured_market.py must treat an exact official ISIN match as
authoritative identity proof and never subsequently reject that candidate
for COMPANY_NAME_MISMATCH -- mirroring the Java
NseMappingReconciliationService's EXACT_ISIN priority (see the Data
Accuracy & Search Correctness Audit, Section D/remediation P1-1).

Ground rules under test:
  - Symbol / quote-type / exchange / currency validation is unchanged and
    still gates the match regardless of ISIN (cases H, I).
  - Exact ISIN match (both present, equal) => accepted unconditionally,
    company name is never consulted (cases A, B).
  - ISIN mismatch (both present, differ) => still rejected as
    ISIN_MISMATCH, before the name check ever runs (cases C, D).
  - Either side missing an ISIN => today's name-similarity behavior is
    unchanged (cases E, F, G).
  - Adversarial: without an exact ISIN match, two real, differently-named
    group companies sharing a corporate prefix must still be rejected by
    the existing name-similarity protection -- the objective is not to
    raise the match rate, only to make exact ISIN authoritative.
"""
from app.structured_market import _trusted_nse_candidate_reason


IDENTITY = "Zen Technologies Limited"
ISIN = "INE251B01027"
OTHER_ISIN = "INE999Z99999"
SYMBOL = "ZENTEC.NS"


def instrument(**overrides):
    base = {"isin": ISIN, "tradingCurrency": "INR"}
    base.update(overrides)
    return base


def candidate(**overrides):
    base = {"symbol": SYMBOL, "quoteType": "EQUITY", "exchange": "NSE",
            "currency": "INR", "isin": ISIN, "longname": IDENTITY}
    base.update(overrides)
    return base


def reason(instrument_overrides=None, candidate_overrides=None, identity=IDENTITY):
    return _trusted_nse_candidate_reason(
        instrument(**(instrument_overrides or {})), identity,
        candidate(**(candidate_overrides or {})), SYMBOL,
    )


def test_A_exact_isin_match_with_very_different_company_names_is_accepted():
    assert reason(
        candidate_overrides={"longname": "Totally Unrelated Industries Private Limited"},
    ) is None


def test_B_exact_isin_match_with_similar_company_names_is_accepted():
    assert reason(candidate_overrides={"longname": "Zen Technologies Ltd"}) is None


def test_C_mismatched_isin_with_identical_company_names_is_isin_mismatch():
    assert reason(candidate_overrides={"isin": OTHER_ISIN}) == "ISIN_MISMATCH"


def test_D_mismatched_isin_with_different_company_names_is_isin_mismatch():
    assert reason(candidate_overrides={
        "isin": OTHER_ISIN, "longname": "Totally Unrelated Industries Private Limited",
    }) == "ISIN_MISMATCH"
    # Proves ISIN is checked before the name check: a different-ISIN,
    # different-name candidate must not be reported as COMPANY_NAME_MISMATCH.


def test_E_expected_isin_present_candidate_isin_absent_preserves_name_behavior():
    assert reason(candidate_overrides={"isin": ""}) is None
    assert reason(candidate_overrides={
        "isin": "", "longname": "Totally Unrelated Industries Private Limited",
    }) == "COMPANY_NAME_MISMATCH"


def test_F_candidate_isin_present_expected_isin_absent_preserves_name_behavior():
    assert reason(instrument_overrides={"isin": ""}) is None
    assert reason(instrument_overrides={"isin": ""}, candidate_overrides={
        "longname": "Totally Unrelated Industries Private Limited",
    }) == "COMPANY_NAME_MISMATCH"


def test_G_both_isins_absent_preserves_existing_name_similarity_behavior():
    assert reason(instrument_overrides={"isin": ""}, candidate_overrides={"isin": ""}) is None
    assert reason(instrument_overrides={"isin": ""}, candidate_overrides={
        "isin": "", "longname": "Totally Unrelated Industries Private Limited",
    }) == "COMPANY_NAME_MISMATCH"


def test_H_symbol_mismatch_still_rejects_even_with_matching_isin():
    assert reason(candidate_overrides={"symbol": "OTHER.NS"}) == "SYMBOL_MISMATCH"


def test_I_quote_type_exchange_currency_mismatches_still_reject_with_matching_isin():
    assert reason(candidate_overrides={"quoteType": "ETF"}) == "QUOTE_TYPE_MISMATCH"
    assert reason(candidate_overrides={"exchange": "BSE"}) == "EXCHANGE_MISMATCH"
    assert reason(candidate_overrides={"currency": "USD"}) == "CURRENCY_MISMATCH"
    # Exact ISIN must not bypass any of these -- only the name check is skipped.


def test_adversarial_similarly_named_group_companies_without_isin_match_stay_rejected():
    # Real, different NSE-listed companies sharing the "Tata ... Limited"
    # shape. _name_similarity scores this pair well under the 0.55
    # threshold (~0.41), so without an exact ISIN match the existing
    # protection must still reject the substitution.
    result = reason(
        instrument_overrides={"isin": ""},
        candidate_overrides={"isin": "", "longname": "Tata Power Company Limited"},
        identity="Tata Motors Limited",
    )
    assert result == "COMPANY_NAME_MISMATCH"
