"""SBI TER parser + strict identity validated against REAL values.

Fixtures (captured on the Windows host 2026-11-05): the SBI TER workbook's latest row per NSDL code (raw cell values,
workbook SHA-256 in the JSON), plus the AMFI NAVAll / scheme-master ETF rows for SBI and one representative per other AMC.
"""
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID
import openpyxl
import pytest
from app.etf_amfi_navall import parse_amfi_navall
from app.etf_amfi_scheme_master import parse_amfi_scheme_master
from app.etf_evidence import EtfAuthority, EtfMetric
from app.etf_identity_resolution import (IdentityConfidence, IdentityRejection, build_amc_pairing,
                                         build_name_based_code_resolver, reconcile_isin)
from app.sbi_ter_workbook_adapter import (EXPECTED_HEADERS, AdapterOutcome, BoundedSbiTerAdapter,
                                          SchemeClassification, build_staged_observations)

FX = Path(__file__).parent / "fixtures"
REAL = json.loads((FX / "sbi_ter_real_2026-11-04.json").read_text())
NAV = parse_amfi_navall((FX / "amfi_navall_sbi_subset.txt").read_bytes().decode("utf-8"))
MASTER = parse_amfi_scheme_master((FX / "amfi_scheme_master_sbi_subset.csv").read_text(encoding="utf-8"))
SBI = "SBI Funds Management Limited"
NOW = datetime(2026, 11, 5, 23, 10, tzinfo=timezone.utc)
SERIAL_TO_DATE = {46330: datetime(2026, 11, 4), 46329: datetime(2026, 11, 3)}
SBI_ISINS = sorted(i for i, s in MASTER.by_isin.items() if s.amc == SBI)
CANONICAL = {isin: UUID(int=n) for n, isin in enumerate(SBI_ISINS, 1)}


def build_workbook(tmp_path, *, with_older=True):
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Sheet1"; ws.append(EXPECTED_HEADERS)
    for code, names, ber, brok, trans, stat, total in REAL["etf"]:
        ws.append([code, names[0], SERIAL_TO_DATE[46330], float(ber), float(brok), float(trans), float(stat), float(total),
                   0, 0, 0, 0, 0])
        if with_older:   # an older observation (under the trailing-space name variant) with another value must never win
            ws.append([code, names[-1], SERIAL_TO_DATE[46329], 9.0, 0, 0, 0, 9.0, 0, 0, 0, 0, 0])
    for code, name in REAL["fof"]:
        ws.append([code, name, SERIAL_TO_DATE[46330], 0.2, 0, 0, 0, 0.2, 0, 0, 0, 0, 0])
    path = tmp_path / "current-year-ter.xlsx"; wb.save(path); return path


class Store:
    def __init__(self): self.saved = []
    def etf_facts(self, *_): return []
    def save_etf_acquisition(self, evidence, attempt): self.saved.append((list(evidence), attempt))


def test_amc_pairing_and_every_sbi_etf_isin_reconciles_across_both_amfi_files():
    pairing = build_amc_pairing(NAV, MASTER)
    assert pairing["SBI Mutual Fund"] == SBI and len(SBI_ISINS) == 22
    for isin in SBI_ISINS:
        decision = reconcile_isin(isin, NAV, MASTER, pairing)
        assert decision.confidence is IdentityConfidence.VERIFIED_ISIN_TWO_SOURCES, (isin, decision)
    # the 1D IDCW scheme publishes its ISIN in the second ISIN column of NAVAll
    assert NAV.by_isin["INF200KA13Z8"].scheme_code == "152168"


def test_parser_values_dates_units_and_classification_from_real_rows(tmp_path):
    staged = build_staged_observations(build_workbook(tmp_path))
    assert len(staged) == 22                                    # three FoF schemes excluded, 9.0 older rows ignored
    assert all(o.classification is SchemeClassification.ETF and o.ter_date == date(2026, 11, 4) for o in staged)
    assert all(not o.direct_plan_applicable for o in staged)    # Direct Plan 0 == not applicable, never a verified zero
    by_name = {o.scheme_name: o for o in staged}
    assert by_name["SBI Nifty Bank ETF"].regular_total_ter_percent == Decimal("0.22")      # 0.22000000000000003 in file
    assert by_name["SBI BSE Sensex ETF"].regular_total_ter_percent == Decimal("0.046")
    assert by_name["SBI Nifty Midcap 150 Momentum 50 ETF"].regular_total_ter_percent == Decimal("0.45")
    assert all(o.value_issue is None for o in staged if o.scheme_name != "SBI BSE India Defence ETF")
    assert by_name["SBI BSE India Defence ETF"].value_issue == "TOTAL_TER_ABOVE_PLAUSIBLE_LIMIT"


def test_all_real_names_through_strict_resolver_one_exact_match_each_no_duplicate_claims(tmp_path):
    staged = build_staged_observations(build_workbook(tmp_path))
    names = {o.scheme_code: o.scheme_name for o in staged}
    resolver = build_name_based_code_resolver(names, amc=SBI, navall=NAV, master=MASTER, canonical_by_isin=CANONICAL)
    accepted = {c: d for c, d in resolver.decisions.items() if d.accepted}
    assert len(accepted) == 22 and all(d.confidence is IdentityConfidence.VERIFIED_EXACT_NAME for d in accepted.values())
    assert len({d.instrument_id for d in accepted.values()}) == 22            # no instrument claimed twice
    for code, d in accepted.items():                                          # resolved scheme name == workbook name
        assert MASTER.by_code[d.scheme_code].scheme_name.casefold() == names[code].casefold()
        assert d.isin in MASTER.by_code[d.scheme_code].isins and NAV.by_isin[d.isin].scheme_code == d.scheme_code


def test_real_end_to_end_persists_21_and_withholds_the_implausible_row(tmp_path):
    path = build_workbook(tmp_path)
    names = {o.scheme_code: o.scheme_name for o in build_staged_observations(path)}
    resolver = build_name_based_code_resolver(names, amc=SBI, navall=NAV, master=MASTER, canonical_by_isin=CANONICAL)
    store = Store()
    results = BoundedSbiTerAdapter(resolver, max_instruments=30).acquire(
        store, path, source_identity="sbi-ter-sha256:" + REAL["capture"]["sha256"], retrieved_at=NOW)
    outcomes = {r.scheme_name: r.outcome for r in results}
    assert sum(o is AdapterOutcome.INGESTED for o in outcomes.values()) == 21
    assert outcomes["SBI BSE India Defence ETF"] is AdapterOutcome.WITHHELD_IMPLAUSIBLE_VALUE
    assert len(store.saved) == 21
    for evidence, attempt in store.saved:
        (fact,) = evidence
        assert fact.metric == EtfMetric.EXPENSE_RATIO and fact.unit == "percent" and fact.as_of_date == date(2026, 11, 4)
        assert fact.provenance.authority == EtfAuthority.OFFICIAL_FUND and fact.provenance.retrieved_at == NOW
        assert fact.provenance.source_url == REAL["capture"]["url"] and REAL["capture"]["sha256"] in fact.provenance.source_identity
        assert fact.provenance.source_metadata["ter_plan"] == "REGULAR_TOTAL_TER" and Decimal(0) <= fact.value < Decimal(1)
    # the withheld ETF has no fact at all
    defence = CANONICAL["INF200KB1CJ6"]
    assert all(f.instrument_id != defence for ev, _ in store.saved for f in ev)


def test_fof_with_same_name_as_an_etf_can_never_receive_the_etf_value(tmp_path):
    # a hypothetical FoF row named exactly like an ETF is not an ETF-category scheme, so it cannot create a second match
    fof = ("SBI Funds Management Limited,999001,SBI Nifty 50 ETF,Open Ended,Other Scheme - FoF Domestic,x,5000,"
           "01-Jan-2025,,INF200KZZZZ9\n")
    master = parse_amfi_scheme_master((FX / "amfi_scheme_master_sbi_subset.csv").read_text(encoding="utf-8") + fof)
    resolver = build_name_based_code_resolver({"C1": "SBI Nifty 50 ETF"}, amc=SBI, navall=NAV, master=master,
                                              canonical_by_isin={**CANONICAL, "INF200KZZZZ9": UUID(int=999)})
    assert resolver("C1") == CANONICAL["INF200KA1FS1"] and resolver.decisions["C1"].scheme_code == "135106"
    # ...and an FoF-named workbook row is rejected outright, even when an ETF of that exact name exists
    r2 = build_name_based_code_resolver({"C2": "SBI Nifty 50 ETF FOF"}, amc=SBI, navall=NAV, master=MASTER,
                                        canonical_by_isin=CANONICAL)
    assert r2("C2") is None and r2.decisions["C2"].rejection is IdentityRejection.FUND_OF_FUND


def test_scheme_without_isin_in_either_source_is_never_enriched():
    # real case: scheme 154504 HDFC Nifty Metal ETF has no ISIN in NAVAll ("-;-") and an empty ISIN in the master
    nav = parse_amfi_navall("Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date\r\n\r\n"
        "Open Ended Schemes(Exchange Traded Funds (ETFs) - Equity ETF)\r\n\r\nHDFC Mutual Fund\r\n\r\n"
        "154504;-;-;HDFC Nifty Metal ETF;;Growth Option;126.03;05-Nov-2026\r\n")
    master = parse_amfi_scheme_master("AMC,Code,Scheme Name,Scheme Type,Scheme Category,Scheme NAV Name,Scheme Minimum Amount,"
        "Launch Date, Closure Date,ISIN Div Payout/ ISIN GrowthISIN Div Reinvestment\n"
        "HDFC Asset Management Company Limited,154504,HDFC Nifty Metal ETF,Open Ended,Exchange Traded Funds (ETFs) - Equity ETF,"
        "HDFC Nifty Metal ETF - Growth Option,500,20-Jul-2026,24-Jul-2026,\n")
    assert nav.by_isin == {} and master.by_isin == {} and master.by_code == {}


# Independently written golden table: (ISIN per AMFI NAVAll for that exact scheme name, Regular Total TER % published on 2026-11-04).
GOLDEN = {
    "SBI BSE 100 ETF": ("INF200KA1572", "0.15"), "SBI BSE PSU BANK ETF": ("INF200KB1712", "0.42"),
    "SBI BSE Sensex ETF": ("INF200K01VT2", "0.046"), "SBI BSE Sensex Next 50 ETF": ("INF200KA1VQ2", "0.29"),
    "SBI Gold ETF": ("INF200KA16D8", "0.65"), "SBI Nifty 10 yr Benchmark G-Sec ETF": ("INF200KA1JT1", "0.14"),
    "SBI NIFTY 1D Rate Liquid ETF - Growth": ("INF200KB1969", "0.29"), "SBI NIFTY 1D Rate Liquid ETF - IDCW": ("INF200KA13Z8", "0.36"),
    "SBI NIFTY 200 Quality 30 ETF": ("INF200KA1WX6", "0.33"), "SBI Nifty 50 ETF": ("INF200KA1FS1", "0.049"),
    "SBI Nifty Bank ETF": ("INF200KA1580", "0.22"), "SBI Nifty Consumption ETF": ("INF200KA1X17", "0.31"),
    "SBI Nifty IT ETF": ("INF200KA1S14", "0.24"), "SBI Nifty Midcap 150 ETF": ("INF200KB1AK8", "0.26"),
    "SBI Nifty Midcap 150 Momentum 50 ETF": ("INF200KB1AJ0", "0.45"), "SBI Nifty Next 50 ETF": ("INF200KA1598", "0.18"),
    "SBI Nifty Private Bank ETF": ("INF200KA1S22", "0.18"), "SBI Nifty Smallcap 250 ETF": ("INF200KB1AY9", "0.6"),
    "SBI Nifty200 Value 30 ETF": ("INF200KB1AX1", "0.55"), "SBI Nifty50 Equal Weight ETF": ("INF200KB1282", "0.21"),
    "SBI Silver ETF": ("INF200KB1217", "0.41"),
}


def test_all_21_accepted_records_match_the_independent_golden_table(tmp_path):
    path = build_workbook(tmp_path)
    names = {o.scheme_code: o.scheme_name for o in build_staged_observations(path)}
    resolver = build_name_based_code_resolver(names, amc=SBI, navall=NAV, master=MASTER, canonical_by_isin=CANONICAL)
    store = Store()
    BoundedSbiTerAdapter(resolver, max_instruments=30).acquire(store, path, source_identity="sbi-ter-sha256:" + REAL["capture"]["sha256"],
                                                               retrieved_at=NOW)
    inverse = {v: k for k, v in CANONICAL.items()}
    got = {}
    for evidence, attempt in store.saved:
        (fact,) = evidence
        name = fact.provenance.source_metadata["scheme_name"]
        got[name] = (inverse[fact.instrument_id], str(fact.value))
        assert (fact.unit, fact.as_of_date, fact.metric) == ("percent", date(2026, 11, 4), EtfMetric.EXPENSE_RATIO)
        assert fact.provenance.source_metadata["nsdl_scheme_code"].startswith("SBIM/O/O/")
        assert attempt.provider == "SBI_MUTUAL_FUND" and attempt.instrument_id == fact.instrument_id
    assert got == GOLDEN and len(got) == 21
