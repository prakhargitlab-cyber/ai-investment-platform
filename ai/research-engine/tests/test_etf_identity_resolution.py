from dataclasses import replace
from pathlib import Path
from uuid import UUID
import pytest
from app.etf_amfi_navall import parse_amfi_navall
from app.etf_amfi_scheme_master import AmfiSchemeMasterError, parse_amfi_scheme_master
from app.etf_identity_resolution import (
    IdentityConfidence as C, IdentityRejection as R, build_amc_pairing, build_name_based_code_resolver,
    reconcile_isin, resolve_exact_name)

FX = Path(__file__).parent / "fixtures"
MASTER_TEXT = (FX / "amfi_scheme_master_excerpt.csv").read_text(encoding="utf-8")
NAV_TEXT = (FX / "amfi_navall_excerpt.txt").read_bytes().decode("utf-8") + (
    "\nOpen Ended Schemes(Other Scheme - Other  ETFs)\n\nSBI Mutual Fund\n\n"
    "135106;INF200KA1FS1;-;SBI Nifty 50 ETF;Regular Plan;Growth;250.0;05-Nov-2026\n"
    "152168;INF200KA13Z8;-;SBI NIFTY 1D Rate Liquid ETF - IDCW;Regular Plan;IDCW;1000.0;05-Nov-2026\n")
SBI = "SBI Funds Management Limited"
U = {i: UUID(int=n) for n, i in enumerate(["INF200K01VT2", "INF200KA1FS1", "INF204KB14I2", "INF200KA13Z8"], 1)}


@pytest.fixture
def idx():
    nav, master = parse_amfi_navall(NAV_TEXT), parse_amfi_scheme_master(MASTER_TEXT)
    return nav, master, build_amc_pairing(nav, master)


def test_master_parse_etf_only_split_isins_and_nfo_dates_not_listing(idx):
    _, m, _ = idx
    assert m.row_count == 8 and m.etf_row_count == 6 and "152733" not in m.by_code
    s = m.by_code["121109"]
    assert (s.nfo_open_date.isoformat(), s.nfo_close_date.isoformat()) == ("2013-02-09", "2013-03-08")
    assert not hasattr(s, "listing_date") and not hasattr(s, "inception_date")
    assert m.by_code["111954"].nfo_close_date is None


def test_master_header_is_strict():
    with pytest.raises(AmfiSchemeMasterError):
        parse_amfi_scheme_master("a,b\n1,2\n")


def test_amc_pairing_is_data_driven_and_one_to_one(idx):
    _, _, p = idx
    assert p == {"SBI Mutual Fund": SBI, "Nippon India Mutual Fund": "Nippon Life India Asset Management Limited"}


def test_isin_reconciled_across_both_sources(idx):
    n, m, p = idx
    d = reconcile_isin("INF204KB14I2", n, m, p)
    assert d.confidence is C.VERIFIED_ISIN_TWO_SOURCES and d.scheme_code == "140084"
    assert reconcile_isin("INF200KA16D8", n, m, p).rejection is R.ISIN_NOT_IN_NAVALL
    assert reconcile_isin("INF200KB1233", n, m, p).rejection is R.ISIN_NOT_IN_NAVALL   # FoF, not ETF
    assert reconcile_isin("INF000000000", n, m, p).rejection is R.ISIN_NOT_IN_NAVALL


def test_scheme_code_and_amc_conflicts_reject(idx):
    n, m, p = idx
    bad = replace(m.by_isin["INF204KB14I2"], scheme_code="999999")
    m2 = replace(m, by_isin={**m.by_isin, "INF204KB14I2": bad})
    assert reconcile_isin("INF204KB14I2", n, m2, p).rejection is R.SCHEME_CODE_CONFLICT
    assert reconcile_isin("INF204KB14I2", n, m, {}).rejection is R.AMC_UNVERIFIED
    assert reconcile_isin("INF204KB14I2", n, m, {**p, "Nippon India Mutual Fund": SBI}).rejection is R.AMC_CONFLICT
    other = replace(m.by_isin["INF204KB14I2"], category="Other Scheme - Gold ETF")
    m3 = replace(m, by_isin={**m.by_isin, "INF204KB14I2": other})
    assert reconcile_isin("INF204KB14I2", n, m3, p).rejection is R.CATEGORY_CONFLICT


def test_ambiguous_isin_rejected(idx):
    n, m, p = idx
    n2 = replace(n, ambiguous_isins={"INF204KB14I2"})
    assert reconcile_isin("INF204KB14I2", n2, m, p).rejection is R.ISIN_AMBIGUOUS


def kw(idx, **over):
    n, m, p = idx
    base = dict(amc=SBI, navall=n, master=m, pairing=p, canonical_by_isin=U)
    base.update(over)
    return base


def test_exact_name_accepts_only_with_all_conditions(idx):
    d = resolve_exact_name("  sbi  BSE sensex ETF ", **kw(idx))
    assert d.confidence is C.VERIFIED_EXACT_NAME and d.instrument_id == U["INF200K01VT2"] and d.scheme_code == "121109"


@pytest.mark.parametrize("name,reason", [
    ("SBI BSE SENSEX", R.NAME_NO_MATCH),                    # no fuzzy / prefix matching
    ("SBI Sensex ETF", R.NAME_NO_MATCH),
    ("SBI Silver ETF Fund of Fund", R.FUND_OF_FUND),
    ("SBI Silver ETF FOF", R.FUND_OF_FUND),
    ("SBI Gold ETF", R.ISIN_NOT_IN_NAVALL),                 # exists in master only: single-source -> rejected
])
def test_exact_name_rejections(idx, name, reason):
    assert resolve_exact_name(name, **kw(idx)).rejection is reason


def test_exact_name_requires_verified_amc_unique_canonical(idx):
    assert resolve_exact_name("SBI Nifty 50 ETF", **kw(idx, amc="Unknown AMC")).rejection is R.AMC_UNVERIFIED
    assert resolve_exact_name("SBI Nifty 50 ETF", **kw(idx, canonical_by_isin={})).rejection is R.NO_CANONICAL_ETF
    dup = frozenset({"INF200KA1FS1"})
    assert resolve_exact_name("SBI Nifty 50 ETF", **kw(idx, canonical_duplicate_isins=dup)).rejection is R.CANONICAL_AMBIGUOUS
    n, m, p = idx
    twin = replace(m.by_code["135106"], scheme_code="135107", isins=("INF200KA1FS9",))
    m2 = replace(m, by_code={**m.by_code, "135107": twin})
    assert resolve_exact_name("SBI Nifty 50 ETF", **kw(idx, master=m2)).rejection is R.NAME_AMBIGUOUS
    m3 = replace(m, ambiguous_names={(SBI, "sbi nifty 50 etf")})
    assert resolve_exact_name("SBI Nifty 50 ETF", **kw(idx, master=m3)).rejection is R.NAME_AMBIGUOUS


def test_code_resolver_rejects_two_codes_claiming_one_etf(idx):
    n, m, _ = idx
    names = {"1": "SBI Nifty 50 ETF", "2": "SBI Nifty 50 ETF", "3": "SBI BSE SENSEX ETF", "4": "SBI Gold ETF"}
    r = build_name_based_code_resolver(names, amc=SBI, navall=n, master=m, canonical_by_isin=U)
    assert (r("1"), r("2"), r("4"), r("999")) == (None, None, None, None)
    assert r("3") == U["INF200K01VT2"]
