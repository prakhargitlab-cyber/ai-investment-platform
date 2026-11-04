# ETF Radar: BSE recovery boundary

Research update: **2026-11-05, Europe/Berlin**. Live BSE fallback deployment readiness: **BLOCKED**.

Official listing identities, a November 4 security-master snapshot, and published market-data specifications were retrieved in this investigation. No actual eligible BSE execution/positive-volume pair or authorized live feed was obtained. Runtime source gates remain closed. This supersedes the November 4 report's source-access limitations; it does not establish current universe readiness or approve provider-mapping changes.

Retrieval timestamps below use UTC unless an explicit offset is shown. `2026-11-04T23:xxZ` is November 5 in Berlin. Documents were fetched over HTTPS and inspected in memory; raw exchange files and samples were not saved as application evidence. Browser-tool 403 responses were not treated as proof of source absence: direct HTTPS retrieval succeeded for several official artifacts.

## Official BSE identity-evidence matrix

The independent [production security master](https://www.bseindia.com/downloads/Help/file/SCRIP.zip), member `SCRIP/BSE_EQ_SCRIP_04112026.csv`, contains exactly one scoped reference row for each code below. Combining those rows with the linked official BSE listing notices establishes code/ISIN/symbol/ETF identity. The snapshot establishes reference status on **2026-11-04**, not future-session eligibility.

| Canonical NSE symbol | Official BSE code / ISIN | Official ETF identity and listing evidence | Admission / group | November 4 reference status |
|---|---|---|---|---|
| ABSL10BANK | **544719 / INF209KC1670** | Aditya Birla Sun Life BSE Top 10 Banks ETF; [notice 20260302-27](https://www.bseindia.com/markets/MarketInfo/DispNewNoticesCirculars.aspx?page=20260302-27), dated 2026-03-02 | 2026-03-04 / B | BOLT ABSL10BANK; MF; BSE; A |
| ABSLMSCIN | **544706 / INF209KC1662** | Aditya Birla Sun Life MSCI India ETF; [notice 20260219-19](https://www.bseindia.com/markets/MarketInfo/DispNewNoticesCirculars.aspx?page=20260219-19), dated 2026-02-19 | 2026-02-20 / B | BOLT ABSLMSCIN; MF; BSE; A |
| MSCIADD | **544620 / INF740KA1WV7** | DSP MSCI India ETF; [notice 20251128-13](https://www.bseindia.com/markets/MarketInfo/DispNewNoticesCirculars.aspx?page=20251128-13), dated 2025-11-28 | 2025-12-01 / B | BOLT MSCIADD; MF; BSE; A |
| LIQUIDBETA | **544828 / INF789F1AG68** | UTI Nifty 1D Rate Liquid ETF - Growth; [notice 20260720-19](https://www.bseindia.com/downloads/UploadDocs/Notices/20260720-19/20260720-19.pdf), dated 2026-07-20; PDF page 2 contains the identity table | 2026-07-21 / F | BOLT LIQUIDBETA; MF; BSE; A |

All four master rows have `Xchg=BSE`, `Sts=A`, `FinInstrmTp=E`, `NewBrdLotQty=1`, and `ElgbltyClsgAuctnSsn=0`. The master marks them MF; the separate listing notices establish ETF classification. The [historical master specification](https://www.bseindia.com/markets/MarketInfo/DownloadAttach.aspx?attachedId=e99e2ca5-95d7-4173-83da-7774412bbb2d&id=20220531-44), field 60, defines A as active, S as suspended, and I as inactive. These reference flags establish no executed trade.

**Currency remains incomplete for trade admission:** `Ccy` and `SttlmCcy` are blank in all four current master rows. Listing notices give rupee-denominated face values of Rs.10 for the first three and Rs.1000 for LIQUIDBETA. Nominal values do not independently supply the currency and price of a live execution. They must not become MARKET_PRICE.

**544244 is rejected for LIQUIDBETA.** The same master snapshot identifies it as `GALAPREC / INE0RE001014 / EQ / BSE / A`. A separately retrieved [BSE-hosted Gala Precision filing dated 2025-03-19](https://www.bseindia.com/xml-data/corpfiling/AttachHis/72ccb207-fea6-4cf0-b5d4-3482b25c9d5e.pdf), page 1, corroborates Gala Precision Engineering / GALAPREC / 544244. This resolves the proposed code conflict; it does not authorize inserting a 544828 mapping into the application or make LIQUIDBETA market-ready.

Independent official corroboration of the preserved NSE identities is available in [CML73081](https://nsearchives.nseindia.com/content/circulars/CML73081.pdf), [CML72891](https://nsearchives.nseindia.com/content/circulars/CML72891.pdf), [CML71525](https://nsearchives.nseindia.com/content/circulars/CML71525.pdf), and [CML75299](https://nsearchives.nseindia.com/content/circulars/CML75299.pdf), respectively. Their identity tables are on page 2. The [DSP issuer addendum dated 2025-12-01](https://www.dspim.com/media/pages/downloads/9559b2a3ef-1764569153/41.-notice-cum-addendum-to-sid-kim-issuance-of-symbol-of-dsp-msci-india-etf.pdf) independently binds MSCIADD / INF740KA1WV7 / 544620. No canonical UUIDs, ISINs, NSE listings, or trusted provider mappings were changed.

### Identity retrieval proof

These HTTP 200 bodies were retrieved and parsed, rather than inferred from search snippets. HTML hashes identify that particular retrieved representation, including dynamic page content.

| Source above | Retrieved | Body bytes | SHA-256 |
|---|---|---:|---|
| ABSL10BANK BSE notice, HTML | `2026-11-05T00:06:32.558975+01:00` | 64,525 | `0e62996c5b8022e4451806f860ebc62e4b0a1ce44256f4c68aa76ae954429726` |
| ABSLMSCIN BSE notice, HTML | `2026-11-05T00:05:54.703841+01:00` | 79,049 | `b169b972fac30a36af3d0b0b2b38245e4ceb432f6a3338b06fec5a88b43234ce` |
| MSCIADD BSE notice, HTML | `2026-11-05T00:06:32.701077+01:00` | 67,065 | `150525ab5f24c9c9715bc7b0909436430df92fa9e1bf0cf34d03459708fb9497` |
| LIQUIDBETA BSE notice, 3-page PDF | `2026-11-05T00:07:02.301202+01:00` | 171,649 | `b84e56dd5b5cd5d04afd4f93d367e927cd70ccdc36523b20e30a7fa7513c4b1b` |
| Gala conflict filing, PDF | `2026-11-05T00:07:02.588752+01:00` | 4,145,016 | `403c2f499b580887a49be7c46e9271c43a1e4f7d7eedb10a7a4c36f7bbbb006e` |
| SCRIP.zip, ZIP | `2026-11-04T23:12:07.965310Z` | 2,212,288 | `ae3798b43a0c0ec34c80fcc3457747095a9e2c2c2e5e551b21742ba911c23e24` |
| `SCRIP/BSE_EQ_SCRIP_04112026.csv`, archive member | Same archive retrieval | 4,408,117 | `cb04fc581d1b91570061e47b5c8991e69af8311fc36cd73ccd8bf598e97fbcfd` |

The SCRIP response's Last-Modified was `2026-11-04T14:53:17 GMT`; the member filename independently supplies the November 4 reference date. An independent request with custom Accept/Referer headers returned **404**. Repeating the original ordinary-header recipe at `2026-11-04T23:16:41.559391Z` returned HTTP 200, no redirects, and the identical archive/member hashes and five unique scoped rows. Thus the earlier reference snapshot is reproducible with that recipe; arbitrary request-header behavior is not interchangeable. The production link was discovered in BSE's published BOLTPlus Live Setup links, not constructed from a candidate code.

The successful read-only recipe used system Python 3.12.10 and httpx 0.28.1: `httpx.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30, follow_redirects=True)`. It used default Accept `*/*`, no cookies, no authorization and no Referer. Only the four ETF codes and the single conflicted comparator code were selected from the reference CSV. No application acquisition, ETF cycle or persistence ran.

The archive also contains reference member `DP051126` (SHA-256 `630c9c2313522ebe1d398bde8a4e4ac4aff663176145d8c2c7c933b687758a49`). Its raw last-trade-date fields for these four symbols are `30102026`, `04112026`, `27102026`, and `02112026`. It has previous-close information and no executed-volume pair. These reference fields are not admitted trading evidence and cannot repair price readiness.

## Verified documentation and remaining live-source contract gaps

[SEBI's UDiFF directory](https://www.sebi.gov.in/curation/pdf/17.1%20UDiFF%20Catalogue%20Links.pdf) identifies the [BSE UDiFF page](https://www.bseindia.com/static/members/udiff.aspx). Its HTML is an application shell; official linked JavaScript exposes catalogue, format and sample download links. Following those actual links established the artifacts below. Their names and field definitions are not guesses about an undocumented website API.

### BhavCopy and reference formats

The official [catalogue v2.0](https://www.bseindia.com/downloads1/UDiFF_Catalogue_Ver2.0.xlsx), Field Master rows 333-366, and [Trade/BhavCopy workbook](https://www.bseindia.com/downloads1/Annexure_B_UDiFF_Trade_and_BhavCopy_format.xlsx), BhavCopy Format rows 2-35, establish these cash-market fields:

| Field | Documented meaning / admission limit |
|---|---|
| `TradDt`, `BizDt` | Trade date and session-start date; dates do not supply an aware execution timestamp. |
| `Sgmt`, `Src` | Segment and source; CM is cash market. |
| `FinInstrmId`, `ISIN`, `TckrSymb`, `SctySrs` | Scrip identifier, ISIN, symbol and BSE group. |
| `FinInstrmTp` | STK for cash instruments; insufficient to establish ETF classification. |
| `LastPric` | Last traded price; source eligibility still needs trading/session proof. |
| `TtlTradgVol`, `TtlNbOfTxsExctd` | Total traded quantity and number of trades. |
| `SsnId` | I1/I2/F1/F2 identify interim/final sessions; these labels are not execution times. |

The 34-column BhavCopy format contains no execution timestamp, currency, ETF discriminator, or current trading-status field. It cannot by itself satisfy the current adapter's aware session/last-trade timestamp contract. No timestamp was manufactured from a date or market close.

The [BSE-hosted UDiFF guidance V2023.10](https://www.bseindia.com/downloads1/Annexure_A_SEBI_guidance_document_having_the_UDiFF_Standards_for_Generation_and_Implementation.pdf), page 5, defines generic rupee amounts and ISO date/time conventions. Pages 7-9 distinguish final-file naming from observation time: `F_0000` supplies no trade timestamp. Page 13 permits masked development samples. Thus the official [BhavCopy](https://www.bseindia.com/downloads1/Bhav_copy_sample_file.zip) and [Trade](https://www.bseindia.com/downloads1/Trade_sample_file.zip) sample archives verify format examples, not actual ETF trading.

### Timestamped BSE market-data protocol

The [official EMDI/MDI manual archive](https://www.bseindia.com/downloads1/EMDI_market_data_manual.zip) contains `BSE_market data_manual_v 1.4.5.pdf`, **version 1.4.5, dated 2026-05-06**, 142 pages, marked BSE PUBLIC. This is a genuine **FAST-encoded UDP multicast** protocol specification, not an HTTP JSON endpoint. Relevant definitions are on pages 49, 92, and 105-111:

| Tag / field | Documented purpose |
|---|---|
| 48 / `SecurityID` | Instrument identifier; independent cash-security reference binding remains necessary. |
| 15 / `Currency` | ISO 4217 currency in product reference data. |
| 269 / `MDEntryType` | Value 2 identifies a trade entry. |
| 270 / `MDEntryPx`; 271 / `MDEntrySize` | Entry price and quantity. |
| 273 / `MDEntryTime` | Official execution time for trades; UTC nanoseconds since Unix epoch. |
| 277 / `TradeCondition` | U identifies ExchangeLast; statistical/reference conditions differ. |
| 1020 / `TradeVolume`; 897 / `NoTrades` | Cumulative daily traded units and trade count. |
| 965 / 326 | Instrument and security trading-status fields. |

`LastUpdateTime` can reflect book/status changes and cannot replace execution time. Snapshot statistics lack an official trade timestamp. The protocol documentation therefore supplies a possible timestamped transport, but **no authorized feed connectivity, decoded actual candidate records, current reference-currency records, complete equity/ETF applicability proof, or session/recovery behavior was validated**. No live transport, subscription, decoder, or parser was added or enabled. Published documentation alone does not establish an operational live acquisition contract.

The [Self Data Feed portal](https://marketdata.bseindia.com/) still returned direct HTTP 403. Its previously indexed offering and enrolment material does not establish an accessible session API, entitlement, request parameters, or response schema. No account, trial, paid subscription, credentials, or exchange contact was created.

### Format/protocol retrieval proof

All rows were direct HTTP 200 retrievals on November 5 Berlin / November 4 UTC. Full artifacts stayed in memory. The sample archives have not been represented as actual trading fixtures.

| Official artifact | Retrieved UTC, 2026-11-04 | Body bytes | SHA-256 |
|---|---|---:|---|
| UDiFF catalogue v2.0 | `23:07:12Z` | 98,721 | `43e5e1aafef2fc6e6bbef3894ac3d1f09e20722f7492ad83ab0fc66999c91895` |
| Trade/BhavCopy workbook | `23:07:31Z` | 36,357 | `f30391483f486cf245286912bd44ab94902f8cc8a2dd913d3b3e82e492d70161` |
| BhavCopy test archive | `23:07:12Z` | 10,185 | `68220a6d4f32d87d9dcb2aaf641579a1e0ae4512715150b76b6759ff09fa21f4` |
| Trade test archive | `23:07:12Z` | 10,942 | `9a980f4694223b12d479938501280672510d80c448a1e77a1535ae61d6969384` |
| EMDI manual archive | `23:08:55Z` | 2,985,901 | `0204c4266a3ec9173e4f0320ebb95fd89732050fa7e5a646ff43431a82b52c05` |
| Historical security-master PDF | `23:08:06Z` | 234,396 | `82db7936e235cb93409c06f60a46de787ad860947ab75bc68b365a866f8055e1` |
| UDiFF guidance PDF | `23:06:02.059378Z` | 143,959 | `2d805109d6ba3b03cb4784907b308c200fef479ea8f785aad89563db1524079d` |

## Calendar and eligible-session verification

The [official 2026 BSE equity calendar](https://www.bseindia.com/static/markets/marketinfo/listholi), delivered in its linked [calendar bundle](https://www.bseindia.com/assets/includenew/js/chunk-YLSEME5J.js), lists November 10 and 24 as holidays and **Sunday November 8 as Muhurat trading**. The retrieved presentation still says special-session timings will be notified later. No exact November 8 timing notice was obtained. A weekend-only rule would be incorrect.

The [normal trading presentation](https://www.bseindia.com/static/markets/equity/eqreports/tra-trading) and [BSE Closing Auction Session FAQ](https://www.bseindia.com/downloads1/Annexurev1_FAQs_Closing_Auction_Session.pdf), page 6/Q35, document continuous trading until 15:30 for non-F&O securities. FAQ page 1/Q5 defines the master CAS indicator: field 24 of `BSE_EQ_SCRIP_DDMMYYYY.csv`, 0 for not CAS and 1 for CAS. The retrieved four ETF master rows have `ElgbltyClsgAuctnSsn=0`, supporting non-CAS classification **at that snapshot date**. This does not prove absence of later exceptional-session/outage notices or any actual execution.

The [2026-01-16 SEBI CAS circular](https://www.sebi.gov.in/sebi_data/attachdocs/jan-2026/1768576287344.pdf), pages 3 and 11, specifies the then-applicable CAS schedule and August 3 effective date. The [2026-10-15 revision](https://www.sebi.gov.in/sebi_data/attachdocs/oct-2026/1792079571420.pdf), page 6/paragraph 6.1, changes timings from **December 7**, so those future timings must not be applied in November.

Displayed/reference prices remain insufficient: the CAS FAQ page 6/Q32 permits reference-price fallback without equilibrium; [SEBI's 2026-06-15 ETF circular](https://www.sebi.gov.in/sebi_data/attachdocs/jun-2026/1781524158363.pdf), page 2/paragraph 4.2, permits base-price fallback to LTP or closing NAV. The [August 28 extension](https://www.sebi.gov.in/sebi_data/attachdocs/aug-2026/1787914585756.pdf), page 1, makes those ETF rules effective September 7. Neither base price nor a session/status indicator proves an executed trade.

| Calendar/session source | Retrieved UTC, 2026-11-04 | Body bytes | SHA-256 |
|---|---|---:|---|
| Linked calendar bundle | `23:11:59.654836Z` | 852,362 | `22f353a212dc3994b2ff071354a72a9115062ebcbf05cd64145b47536d40350b` |
| [Linked trading bundle](https://www.bseindia.com/assets/includenew/js/chunk-YTT42YJX.js) | `23:11:59.635190Z` | 320,176 | `65645ff733783276b2c8fdf0dec062fe92b478fafbfd17cf51189c49a4a80e0e` |
| BSE CAS FAQ PDF | `23:11:59.677846Z` | 235,608 | `272bde6c67cf9bdbf1ed7b39b24196a5df75e6b2dd4e6cba4aa8b193ccd8f03d` |
| SEBI January CAS PDF | `23:12:02.491523Z` | 629,060 | `cdd882291c3e788e1f8dcf478b4284604e9d939e12adf1b103e6a50a9d618571` |
| SEBI October CAS PDF | `23:12:02.581709Z` | 666,732 | `b2bd9c92fdc8bd0b0076792a64cc087dd921037085ff402065acafbe3590f244` |
| SEBI June ETF PDF | `23:12:01.282292Z` | 255,438 | `02a17a0314b23d8f92959db249aea9c1a9cf683ea42a951dd017ebc35fbfc7c5` |
| SEBI August extension PDF | `23:12:01.086823Z` | 121,766 | `456ad5f299578a455d280befb7d9643b7b8d97c0f7f64970f5757c1f8206be16` |

## Implemented boundary and evidence meaning

The isolated `app/bse_etf_market.py` boundary validates the application's normalized offline evidence contract. It does not implement or imply an official BSE API response schema. No BSE network fetcher or live parser is enabled.

Validation must bind a trusted canonical/provider mapping to one unambiguous BSE security and matching ISIN, verify ETF identity and INR currency independently, require a valid session timestamp, and require an actual positive traded price with positive executed volume. MARKET_PRICE and TRADING_VOLUME must belong to the same verified security, session, and BSE venue. NAV, iNAV, previous close, stale last price, zero-volume data, and incompatible venue/session pairs cannot repair readiness.

Offline fixtures use `source_mode=DEMO` and are explicitly synthetic. A successful synthetic validation is a unit-test result about the normalized application contract. It is not identity approval, REAL evidence, official exchange provenance, a verified API schema, or actual BSE trading. Synthetic preflight data must not be persisted or promoted to readiness.

The internal `EtfAcquisitionService.preflight_bse_recovery(rows, listings=..., nse_attempts=...)` accepts at most five canonical instruments and preserves the original NSE acquisition attempt and reason. It performs no network or persistence operations. `acquire_bse_recovery()` is an explicit internal acquisition boundary, separate from existing NSE ingestion; it also cannot persist evidence while the live contract gate is closed. Every alternate attempt is attributed to BSE, and failed/unimplemented attempts are returned as in-memory diagnostics. There is no public endpoint, scheduler activation, or full-universe execution.

The initial binding accepts numeric BSE security-code mappings only. If populated, `providerSymbol` and `providerInstrumentId` must each identify the same six-digit code; symbolic aliases remain unsupported until their relationship is independently established. This deliberately conservative adapter restriction does not change the repository's general provider-mapping contract.

Future eligible REAL BSE evidence must use `EtfAcquisitionService._record_async()` and the existing transactional persistence interface for a validated price/volume pair. There is no second persistence mechanism. No database schema changes or migrations are required for this offline boundary. The unfinished AMFI adapter and all Kubernetes deployments remain outside this work.

## Prerequisites for live integration

Live BSE fallback remains blocked until the following operational requirements are reviewable. The published schemas and reference identities above are established; they do not close these remaining requirements:

1. An authorized, accessible timestamped feed and its equity/ETF applicability, instrument/product binding, reference messages, transport configuration, sequencing, recovery, and failure behavior. The documented multicast protocol is not an accessible HTTP feed. BhavCopy alone cannot meet the existing timestamp contract.
2. Actual candidate reference-currency records and same-security, same-exchange, same-session executions with positive price and strictly positive executed quantity. No candidate trade was retrieved. Master currency blanks, nominal face value, previous close, NAV, iNAV, base price and book-update timestamps cannot repair this gap.
3. Eligible-session checks covering source timezone, dated status/CAS membership, holidays, exceptional/outage sessions and November 8 Muhurat timings. Publication of a calendar and a normal-session schedule does not prove an execution occurred.
4. Canonical UUID ownership and existing trusted providerMappings must independently agree with the verified BSE identity before admission. Historical listing notices and the dated master supply review evidence, not authorization to change application identities. Code 544244 is excluded for LIQUIDBETA; 544828 remains unpersisted and unapproved as an application provider mapping.
5. Genuine execution fixtures with original provenance and known timestamps must pass the unchanged identity, currency, freshness, price, volume and pair-compatibility requirements. Exchange development samples do not meet this requirement.
6. Separate explicit release acceptance before any Kubernetes deployment or live persistence. No Helm action occurred, so no stored-release diff or release approval is implied.

The current delivery is an offline validation/preflight boundary. It is not ready for deployment as a functioning live BSE market-data fallback.

## Remaining ETF readiness gaps and change scope

No current database readiness evaluation was performed. There is no 359/359 readiness claim. `docs/etf-final-closure-bundle.md` records a historical November 4 baseline (351/359 mandatory-ready, eight blocked; 349 stale NAVs and ten missing; no fresh holdings/TER/AUM/tracking evidence). That report precedes subsequent NSE EQ/RL remediation and ages at its own clock, so its instrument failure counts must not be presented as current.

The four BSE recovery paths remain blocked on operational trade-source admission. Supporting readiness still needs source-backed fresh NAV, holdings, AUM, TER and tracking observations where absent or stale; current counts are unknown. `INDEX_VALUATION` and `INDEX_CONSTITUENTS` remain explicitly NOT_IMPLEMENTED. Existing current code already routes dedicated holdings evidence and implements tracking parsing/ingestion; older notes about missing wiring are superseded. Accessible approved issuer documents and direct scheme/ISIN proof remain evidence requirements.

This closure investigation changes **only `docs/etf-bse-recovery.md`**. `app/bse_etf_market.py`, `app/etf_acquisition.py` and `app/etf_readiness.py` remain unchanged with `LIVE_SOURCE_CONTRACT=None` and unconditional live collection/admission blockers. There is no speculative live parser, no new endpoint, no scheduler, and no broad ETF run. Existing transactional `_record_async()` persistence remains the only prospective writer after a complete source contract is approved. No database schema change is required for this documentation/offline boundary.

No Kubernetes command, deployment, Helm upgrade, recovery-cluster creation, PostgreSQL read/write or migration, AMFI edit, backup access/modification, application mapping change, evidence persistence or commit was performed by this investigation. Existing untracked files were left untouched.

## Current offline test verification, 2026-11-05

The current unchanged runtime code passed **320 tests, 0 failed, 0 skipped**, no warnings, in 85.08 seconds. These are isolated offline tests; they do not certify actual exchange trading or deployment readiness.

```text
.venv/Scripts/python.exe -m pytest tests/test_bse_etf_market.py tests/test_bse_etf_recovery.py tests/test_etf_market_pair_readiness.py tests/test_nse_etf_market.py tests/test_etf_readiness.py tests/test_etf_identity_readiness.py -q
```

Coverage includes original NSE EQ/RL validation, canonical identity/mapping trust, price/volume venue/session compatibility, synthetic exclusion, failed-source preservation, bounded requests and no persistence on failed validation. No live parser was added, so no development sample has been relabeled as a genuine execution fixture or used to claim a successful live integration.

## Previous offline test verification, 2026-11-04

Run from `ai/research-engine` using its existing virtual environment. The final three new suites pass with **198 passed, 0 failed, 0 skipped**:

```text
.venv/Scripts/python.exe -m pytest tests/test_bse_etf_market.py tests/test_bse_etf_recovery.py tests/test_etf_market_pair_readiness.py -q
```

These tests use synthetic fixtures, mocked source failures, and read-only stores. They do not verify current BSE listings, sessions, or actual trading and do not persist BSE evidence. Existing NSE regression suites are run separately as part of the broader offline regression check.

The broader regression run passed with **553 passed, 0 failed, 0 skipped** (one existing FastAPI/Starlette TestClient deprecation warning):

```text
.venv/Scripts/python.exe -m pytest tests/test_bse_etf_market.py tests/test_bse_etf_recovery.py tests/test_etf_market_pair_readiness.py tests/test_nse_etf_market.py tests/test_etf_readiness.py tests/test_etf_ingestion.py tests/test_etf_acquisition_persistence.py tests/test_etf_preflight.py tests/test_etf_identity_readiness.py tests/test_etf_identity_failure_closure.py tests/test_etf_pre_deployment_correctness.py tests/test_etf_opportunity_cycle.py tests/test_etf_rule_engine.py -q
```

The 198-test BSE-specific command was rerun against the final code after additional currency and BSE case/MIC checks were added during the broader run; these counts overlap and must not be added together. Existing NSE EQ/RL tests pass. The touched tracked files pass scoped `git diff --check`; pre-existing infrastructure edits were left untouched.
