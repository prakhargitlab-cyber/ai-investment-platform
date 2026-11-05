"""SBI TER values reach an ETF only through the approved strict exact-name identity resolution."""
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import openpyxl
from app.etf_amfi_navall import parse_amfi_navall
from app.etf_amfi_scheme_master import parse_amfi_scheme_master
from app.etf_evidence import EtfAuthority, EtfMetric
from app.etf_identity_resolution import build_name_based_code_resolver
from app.sbi_ter_workbook_adapter import (BoundedSbiTerAdapter, EXPECTED_HEADERS, AdapterOutcome,
                                           build_staged_observations)
from tests.test_etf_identity_resolution import MASTER_TEXT, NAV_TEXT, SBI, U

NOW = datetime(2026, 11, 5, 22, 0, tzinfo=timezone.utc)


class Store:
    def __init__(self): self.saved = []
    def etf_facts(self, *_): return []
    def save_etf_acquisition(self, evidence, attempt): self.saved.append((evidence, attempt))


def workbook(tmp_path, rows):
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Sheet1"; ws.append(EXPECTED_HEADERS)
    for code, name, d, ter in rows:
        ws.append([code, name, datetime(*d), ter, 0, 0, 0, ter, 0, 0, 0, 0, 0])
    p = tmp_path / "ter.xlsx"; wb.save(p); return p


def test_only_exact_verified_names_get_ter_everything_else_stays_missing(tmp_path):
    path = workbook(tmp_path, [
        (111, "SBI BSE SENSEX ETF", (2026, 11, 3), 0.07),
        (111, "SBI BSE SENSEX ETF", (2026, 11, 4), 0.05),
        (222, "SBI Nifty 50 ETF ", (2026, 11, 4), 0.04),
        (333, "SBI Gold ETF", (2026, 11, 4), 0.65),             # master only, not in NAVAll -> rejected
        (444, "SBI Nifty Fifty ETF", (2026, 11, 4), 0.1),       # different name -> no fuzzy match
        (555, "SBI Silver ETF Fund of Fund", (2026, 11, 4), 0.3),
    ])
    staged = build_staged_observations(path)
    names = {s.scheme_code: s.scheme_name for s in staged}
    resolver = build_name_based_code_resolver(names, amc=SBI, navall=parse_amfi_navall(NAV_TEXT),
                                              master=parse_amfi_scheme_master(MASTER_TEXT), canonical_by_isin=U)
    store = Store()
    results = {r.scheme_code: r for r in BoundedSbiTerAdapter(resolver, max_instruments=5).acquire(
        store, path, source_identity="sbi-ter-sha256-test", retrieved_at=NOW)}
    assert set(results) == {"111", "222", "333", "444"}          # FoF dropped by the adapter's classification
    assert results["111"].outcome is AdapterOutcome.INGESTED and results["222"].outcome is AdapterOutcome.INGESTED
    for code in ("333", "444"):
        assert results[code].outcome is AdapterOutcome.IDENTITY_UNRESOLVED_STAGED_ONLY
    fact = results["111"].fact
    assert (fact.metric, fact.value, fact.unit, fact.as_of_date) == (EtfMetric.EXPENSE_RATIO, Decimal("0.05"), "percent", date(2026, 11, 4))
    assert fact.instrument_id == U["INF200K01VT2"] and fact.provenance.authority == EtfAuthority.OFFICIAL_FUND
    assert fact.provenance.source_url.endswith("current-year-ter.xlsx") and fact.provenance.retrieved_at == NOW
    assert len(store.saved) == 2
    # AUM is never produced by this source
    assert all(f.metric is not EtfMetric.AUM for ev, _ in store.saved for f in ev)
