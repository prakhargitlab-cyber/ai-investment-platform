# ETF Radar: BSE recovery boundary

Research date: 2026-11-04. Live BSE fallback deployment readiness: **BLOCKED**.

## Candidate mappings are unverified

| Canonical NSE symbol | Candidate BSE security code | Status |
|---|---|---|
| ABSL10BANK | 544719 | Unverified candidate only |
| ABSLMSCIN | 544706 | Unverified candidate only |
| MSCIADD | 544620 | Unverified candidate only |
| LIQUIDBETA | 544244 | Unverified candidate; conflicting historical source text |

These candidates are research inputs, not identity authority, approved provider mappings, or actual trading evidence. The canonical NSE instrument UUID, ISIN, ETF listing, and existing NSE EQ/RL validation remain authoritative and unchanged. A ticker or candidate code cannot establish a BSE identity.

## Official source investigation

Only official BSE sources are used for the following source findings. Search-index excerpts were available for several official documents whose complete bodies could not be retrieved in this environment; those excerpts do not constitute an approved current feed contract or a downloaded fixture.

| Official source | What was established | Access and verification limits |
|---|---|---|
| [BSE Self Data Feed](https://marketdata.bseindia.com/) | BSE offers equity, delayed, EOD, and reference data through its data platform. The public page describes registration/KYC, plan selection, pricing/agreements, payment, and access, and advertises feed details/manuals and trial access. | The public landing page did not establish exact API endpoints, parameters, response fields, authentication, price semantics, volume semantics, or timestamps. Direct retrieval returned HTTP 403; indexed public text was available. No subscription, credentials, trial, or agreement was created. |
| [Standardised Security Master file format, notice 20220531-44](https://www.bseindia.com/markets/MarketInfo/DownloadAttach.aspx?attachedId=e99e2ca5-95d7-4173-83da-7774412bbb2d&id=20220531-44) | Indexed official text describes `BSE_EQ_SCRIP_DDMMYYYY.csv` with numeric Scrip Code, Instrument Code, Scrip Name, ISIN CODE, and Security Type Flag. | Direct retrieval returned HTTP 403. This historical specification identifies a possible reference-data source; it does not independently prove any candidate mapping, current ETF status, INR currency, or executed trade. No current security-master artifact was obtained. |
| [BSE Equity Segment Master Circular, notice 20230630-19](https://www.bseindia.com/markets/MarketInfo/DownloadAttach.aspx?attachedId=cb125690-a0f8-452a-ba6b-86cab98004bc&id=20230630-19) | Indexed official text describes security-master distribution through the member extranet and a `SCRIP.ZIP` link on the BSE member help desk. | This is a historical distribution description, not a verified current download URL, accessible artifact, or trading-feed response contract. |
| [BSE UDiFF Standards for Generation and Implementation](https://www.bseindia.com/downloads1/Annexure_A_SEBI_guidance_document_having_the_UDiFF_Standards_for_Generation_and_Implementation.pdf) | Indexed official text describes generic UDiFF file naming and generation conventions. | Direct retrieval returned HTTP 403. A generic guidance document does not establish the current BSE cash-market field catalogue, price/volume semantics, or a live response contract. |
| [Official BSE bhavcopy page](https://www.bseindia.com/markets/marketinfo/BhavCopy.aspx) | A possible official EOD market-data entry point was identified. | The page could not be retrieved through the research tool. No current official cash-bhavcopy schema or source fixture was established. No guessed download URL or parser is authorized by this finding. |

### Historical conflict for candidate 544244

Search-index text for a [BSE-hosted issuer filing dated March 19, 2025](https://www.bseindia.com/xml-data/corpfiling/AttachHis/72ccb207-fea6-4cf0-b5d4-3482b25c9d5e.pdf) identifies Scrip Code `544244`, Scrip Name `GALAPREC`, and Gala Precision Engineering Limited. [Another indexed BSE-hosted filing dated September 4, 2025](https://www.bseindia.com/xml-data/corpfiling/AttachHis/c2fbf3ec-ed66-435f-ae5e-f6504d566b35.pdf) contains the same code/name association.

These historical indexed excerpts contradict the proposed LIQUIDBETA association at those dates. They are not a verified current mapping: complete source artifacts and current BSE reference records were not obtained. The conflict requires independent resolution before any candidate can become trusted. LIQUIDBETA remains blocked without verified eligible traded-price evidence.

## Implemented boundary and evidence meaning

The isolated `app/bse_etf_market.py` boundary validates the application's normalized offline evidence contract. It does not implement or imply an official BSE API response schema. No BSE network fetcher or live parser is enabled.

Validation must bind a trusted canonical/provider mapping to one unambiguous BSE security and matching ISIN, verify ETF identity and INR currency independently, require a valid session timestamp, and require an actual positive traded price with positive executed volume. MARKET_PRICE and TRADING_VOLUME must belong to the same verified security, session, and BSE venue. NAV, iNAV, previous close, stale last price, zero-volume data, and incompatible venue/session pairs cannot repair readiness.

Offline fixtures use `source_mode=DEMO` and are explicitly synthetic. A successful synthetic validation is a unit-test result about the normalized application contract. It is not identity approval, REAL evidence, official exchange provenance, a verified API schema, or actual BSE trading. Synthetic preflight data must not be persisted or promoted to readiness.

The internal `EtfAcquisitionService.preflight_bse_recovery(rows, listings=..., nse_attempts=...)` accepts at most five canonical instruments and preserves the original NSE acquisition attempt and reason. It performs no network or persistence operations. `acquire_bse_recovery()` is an explicit internal acquisition boundary, separate from existing NSE ingestion; it also cannot persist evidence while the live contract gate is closed. Every alternate attempt is attributed to BSE, and failed/unimplemented attempts are returned as in-memory diagnostics. There is no public endpoint, scheduler activation, or full-universe execution.

The initial binding accepts numeric BSE security-code mappings only. If populated, `providerSymbol` and `providerInstrumentId` must each identify the same six-digit code; symbolic aliases remain unsupported until their relationship is independently established. This deliberately conservative adapter restriction does not change the repository's general provider-mapping contract.

Future eligible REAL BSE evidence must use `EtfAcquisitionService._record_async()` and the existing transactional persistence interface for a validated price/volume pair. There is no second persistence mechanism. No database schema changes or migrations are required for this offline boundary. The unfinished AMFI adapter and all Kubernetes deployments remain outside this work.

## Prerequisites for live integration

Live BSE fallback remains blocked until all of the following are reviewable:

1. The exact official market-data source, access method, authentication/entitlement, documented request parameters, versioned response/file schema, and failure behavior are established without inferred endpoints or fields.
2. Official field semantics establish the exchange/session, actual traded-price eligibility, executed volume and units, currency, timestamp/timezone, stale/non-trading-day behavior, and how a prior-session last price is distinguished from a current-session trade. An independently verified exchange calendar/session contract must establish holidays and any special sessions; the offline timestamp coherence checks do not prove that a dated session actually occurred.
3. Current independent official reference data establishes each approved security-code/ISIN/ETF/currency mapping and resolves duplicates or ambiguity. Canonical NSE identity is preserved; candidate mappings alone are insufficient.
4. Genuine official fixtures have recorded provenance and known session dates, and pass the same identity, session, freshness, price, volume, and pair-compatibility gates as unit fixtures.
5. A separate authorized review approves any live execution, persistence, or deployment. This implementation does not commit, deploy, change PostgreSQL records, run migrations, or persist BSE evidence automatically.

The current delivery is an offline validation/preflight boundary. It is not ready for deployment as a functioning live BSE market-data fallback.

## Offline test verification

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
