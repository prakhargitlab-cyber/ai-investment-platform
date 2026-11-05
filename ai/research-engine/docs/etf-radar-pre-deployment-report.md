# ETF Radar - pre-deployment report (branch `fix/etf-radar-evidence-product-quality`)

Nothing in this report was merged, deployed, reset or run as a full ETF cycle. Base `1f4b6fc`.

## Commits (oldest first)
`2d5b6c0` backend evidence quality / 5y history / AMFI NAVAll identity / phase progress · `6b73068` frontend ·
`483a5fa` reset scripts · `8010871` bounded AMFI failures + TER/AUM findings · `d48056f` AMFI scheme-master identity ·
`19c531b` verified SBI TER in the cycle · the final commit adds hardening, tests, helm flag wiring, this report.

## Feature flags (all default OFF; enabling does not start a cycle)
| Helm value (`research.*`) | Env var | Intended value |
|---|---|---|
| `etfAmfiNavallEnabled` | `AIP_RESEARCH_ETF_AMFI_NAVALL_ENABLED` | `true` for the first fresh run (fund-house + AMFI scheme code evidence) |
| `etfSbiTerEnabled` | `AIP_RESEARCH_ETF_SBI_TER_ENABLED` | `true` for the first fresh run (implies the NAVAll step) |
| `etfManualExecutionEnabled`, `etfFullUniverseExecutionEnabled` | existing gates | unchanged: LOCAL/DEV `true`, production `false` until explicitly authorised |
The cycle also needs outbound HTTPS to `portal.amfiindia.com` and `www.sbimf.com`; if blocked, the steps fail bounded (below).

## Failure diagnostics
SBI/AMFI source failures are durable: `selection.sourceDiagnostics.SBI_MUTUAL_FUND` on the cycle run (saved with the checkpoint,
readable after completion) and, when NAVAll identifies the instruments, a per-ETF `TECHNICAL_FAILURE` attempt for EXPENSE_RATIO.
Without NAVAll no instrument can be attributed, so only the cycle record remains. AMFI NAVAll failures keep their per-instrument attempts.

## Defence ETF 7.77 % (kept withheld; guard unchanged)
Workbook rows for SBI BSE India Defence ETF (`SBIM/O/O/EET/26/07/0209`): total 0.35 % (BER 0.30 + levies 0.05) through
2026-10-31; from 2026-11-01 the levies column reads 7.2 (brokerage 0.07, transaction 0.20) giving 7.77 % on four consecutive days.
The same column spiked once for SBI Nifty200 Value 30 ETF (2026-05-20: levies 40.75, total 42.52) and then returned to 0.28.
This indicates unreliable levies/transaction-cost data in the source for newly launched ETFs; it is not verifiable from the
workbook alone. No fallback to the earlier 0.35 % is used (it would hide a changed filing). Action for the operator: ask SBI MF to
confirm; the value appears automatically once a plausible figure is published.

## Unresolved-data matrix
| Field | State |
|---|---|
| Expense ratio | 21/22 SBI ETFs from the SBI workbook; Defence ETF withheld; every non-SBI AMC unavailable (no verified ISIN-bearing source reachable) |
| AUM | unavailable for all (AMFI scheme-wise AAUM has no scheme code/ISIN; AMC-level only elsewhere) |
| Tracking error / difference | unavailable (AMFI APIs key on name + benchmark; `www.amfiindia.com` unreachable from the host) |
| Fund inception / listing date | unavailable (AMFI Launch Date is the NFO date; never used) |
| Bid/ask spread, benchmark history, holdings | unavailable |
| AMFI scheme 154504 (HDFC Nifty Metal ETF) | no ISIN in either AMFI file; never matched |

## Fresh-initialisation compatibility
No migration was added (Flyway V1-V25 unchanged). New evidence uses existing ETF tables; verified on PostgreSQL: SBI TER facts,
attempts and diagnostics persist and `20_reset.sql` clears them (`tests/test_etf_sbi_ter_postgres.py`); fresh cycle after reset
rebuilds identically (`test_etf_reset_rebuild_postgres.py`); history targets five years in <=731-day windows bounded by listing date.

## Deployment instructions (after explicit authorisation only)
1. Merge the branch through the normal review; build/tag images. 2. Roll out with the flags above (dev first; the chart is single-replica).
3. Flyway reports no new migrations. 4. Operator reset (dry-run first): `scripts/database/maintenance/etf-radar-reset/README.md`.
5. Start one authorised full ETF cycle from the UI/API. 6. Run the checklist below.

## Read-only post-deployment checklist
* Cycle run: phases UNIVERSE_DISCOVERY..PUBLICATION all terminal; `selection.sourceDiagnostics.SBI_MUTUAL_FUND.status` is `OK` (or a named failure).
* SQL (read-only): `SELECT count(*) FROM research.etf_fact_observations WHERE metric='EXPENSE_RATIO' AND provider='SBI_MUTUAL_FUND'` = 21 (or fewer, never more);
  all rows `unit='percent'`, `as_of_date` = the workbook TER date, `source_url` the SBI workbook, value < 1.
* No `EXPENSE_RATIO`/`AUM` facts for non-SBI providers; the Defence ETF has none; AUM/TRACKING_* count = 0.
* `etf_acquisition_attempts`: SBI ETFs without a fact have `SUCCESS_EMPTY` + reason, none with `TECHNICAL_FAILURE` unless a source failed.
* UI: unavailable metrics render as unavailable, completeness counts exclude them; five-year chart present for ETFs with enough history.
