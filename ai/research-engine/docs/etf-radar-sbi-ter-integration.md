# ETF Radar: verified SBI TER integration (read-only evidence verification, 2026-11-05/06)

## Sources captured on the Windows host
* SBI TER workbook `https://www.sbimf.com/docs/default-source/ter_allschemes/current-year-ter.xlsx`: SHA-256
  `7d02e65a60a18dc8390a4366ba31c51d988de0a0f4a4977fb4fb1372f19b4085`, 1,857,508 bytes, Last-Modified 2026-11-05 03:23 GMT.
  NSDL codes are strings (`SBIM/O/O/EET/13/01/0037`); 22 non-FoF ETF codes (27 code|name variants differ only by a trailing
  space), 4,490 ETF rows, 337 FoF rows; every code's latest TER date is 2026-11-04; Direct Plan Total TER is 0 for every ETF.
* AMFI NAVAll + scheme master (`portal.amfiindia.com`): 369 / 375 ETF rows; scheme codes agree for all 369.

## The single NAVAll / scheme-master disagreement
Scheme **154504 "HDFC Nifty Metal ETF"** (NAVAll fund house "HDFC Mutual Fund"; master AMC "HDFC Asset Management Company
Limited"; category "Exchange Traded Funds (ETFs) - Equity ETF"; NFO 20-Jul-2026 to 24-Jul-2026). NAVAll carries `-;-` in both ISIN
columns and the master's ISIN column is empty: there is no ISIN conflict, AMFI simply publishes no ISIN yet. It is never matched or
enriched. (This row made the previous NAVAll parser raise `AMFI_NAVALL_BAD_ISIN` for the whole file; fixed: ISIN-less ETF rows
are counted in `no_isin_rows` and skipped.)

## Strict resolution of the real SBI names (22 of 22 exact, read-only)
All 22 workbook ETF names equal (whitespace/case-normalised) exactly one AMFI ETF scheme of "SBI Funds Management Limited";
each ISIN is confirmed in both AMFI files; no instrument is claimed twice; the three FoF schemes
(`ETF Fund of Fund`, `ETF FOF`) are excluded. Canonical-ETF existence (NSE) is checked at run time, not in the fixture.
One row is withheld by the plausibility gate: SBI BSE India Defence ETF, Regular Total TER 7.77 % (statutory levies 7.2 %,
10 observations since launch). The other 21 values reconcile with their components.

## Persistence path (normal cycle, default off)
`research_etf_sbi_ter_enabled` (or `sbi_ter_fetchers=`) -> after the AMFI NAVAll step, one workbook GET + one scheme-master GET
(size-capped) -> exact-name identity -> `EXPENSE_RATIO` fact (Regular Plan Total TER, percent, TER date, OFFICIAL_FUND, source
URL, SHA-256, AMFI scheme code, identity confidence) via the fenced `save_etf_acquisition`. Canonical SBI ETFs left without a value get a
`SUCCESS_EMPTY` attempt with the reason. Any source failure is recorded in `selection.sourceDiagnostics.SBI_MUTUAL_FUND` and never
fails the cycle. No other AMC's TER and no AUM is acquired.
