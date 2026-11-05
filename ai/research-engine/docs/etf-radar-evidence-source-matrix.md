# ETF Radar evidence sources and bounded history repair

Code/source review: 2026-11-05. This document separates application capability
from source access, approved identity, accepted persistence, and presentation.
No current database inventory or live acquisition was performed for this change.
All new unit fixtures are synthetic and are not actual exchange/AMC evidence.

## Field matrix

| Field | Existing source/adapter and retained provenance | Missing-data cause and current boundary |
|---|---|---|
| Canonical identity | Portfolio-service UUID, ISIN, active NSE ETF and trusted providerMappings; official NSE listing evidence | Ambiguous/unverified mappings stay blocked. NSE requires VERIFIED and excludes broker-import identity. A ticker or a source-domain match is insufficient. |
| Market price, executed volume | Existing NSE market-watch/metadata adapters retain source URL, session envelope, raw record, metadata and retrieval time | Missing trade/identity/session evidence is not repaired by NAV, previous close, zero volume, or ticker aliases. Existing EQ/RL validation is unchanged. BSE live gate remains closed. |
| NAV | Dedicated EtfNavObservation, independent NSE navDate, INR and source provenance | Missing/zero-unavailable/stale values stay explicit. Retrieval time does not date NAV. Price/NAV comparison still requires compatible dates/currency. AMFI is not modified or enabled. |
| TER and AUM | Generic approved-document parser supports labelled values, original source approval, canonical scheme/ISIN, text hash/section/line locators and separate metric dates | Default source registry has only two Nippon Product Notes marked REQUIRES_AUTHENTICATION. Parser capability does not establish usable source coverage. AUM and average AUM, and current TER and monthly-average expense, remain distinct. |
| Tracking error/difference | Approved generic document parser; tracking difference is signed and both require independent reporting dates | No general approved source coverage. Period, benchmark and return methodology must be established before comparing issuer fields. The standalone tracking calculator is not wired into acquisition/persistence and is not a substitute for issuer evidence. |
| Nippon monthly factsheet metrics | Specialized original-PDF review produces AUM, monthly-average expense and one-year tracking fields for two schemes | Explicit REVIEW_ONLY_NOT_INGESTIBLE staging; independent scheme-heading-to-ISIN and authenticated approval capability are unresolved. Persistence deliberately rejects these records. Staging units are not scoring-visible EtfFacts. |
| SBI TER | Original official workbook review/staging and canonical preflight | Historical review documented 22 scheme codes without an independently authenticated scheme-code-to-ISIN crosswalk. Names/circular joins are review candidates, never mapping authority. |
| Bid, ask, spread | Secondary quote adapter can persist bid and ask with quote provenance | No general official ETF quote-depth contract or production spread writer is established. A future spread needs compatible instrument, venue, timestamp, units and valid bid/ask bounds. |
| Benchmark | Official listing underlying reference and identity-verified NSE category/subcategory retained separately; used for conservative peer classification | No general BENCHMARK_INDEX fact writer. An asset label or category cannot automatically become a benchmark identifier. Raw price returns are not comparable benchmark total returns. |
| Issuer, inception, replication | Existing text fact models/read paths; inception is separate from exchange listing | General authoritative source parsers are absent. Legacy in-memory name/text inference is not durable authoritative evidence and is not promoted. |
| Distribution and domicile | Screening previously named these concepts | DOMICILE and DISTRIBUTION_POLICY were absent from the evidence enum at discovery. Missing contracts must be visible; no source or value is invented by this change. |
| Traded value | Screening previously named TRADED_VALUE | No corresponding evidence enum/source contract existed at discovery. A price-times-volume estimate is not automatically exchange-reported turnover. |
| Holdings, concentration, overlap | Dedicated dated holdings snapshots/positions and coverage/overlap calculations preserve constituent identities and source | No general official portfolio acquisition parser is established. Company shareholding/ownership is not the ETF's portfolio. Incomplete weights/ISIN coverage cannot yield complete concentration/overlap. |
| Price performance/risk | Existing daily-bar table and deterministic return/volatility/drawdown/momentum functions | Source selection, completed-session gaps, recent listings and identity failures are separate. Raw unadjusted closes do not establish total returns, complete corporate-action adjustments, or issuer tracking metrics. |

The historical November 4 closure document reports 359 ETFs, 351 mandatory-ready,
and no fresh issuer metrics. Those are dated observations, not current counts.
Runtime ETF-3 mandatory readiness covers identity, market price and volume;
it does not mean all supporting fund-quality evidence is complete.

## Official source discovery and access limits

- [NSE data-sharing/usage policy](https://www.nseindia.com/static/market-data/nse-data-policy),
  reviewed as a policy page on November 5: historical/market-data use and
  redistribution are governed by applicable agreements. An HTTP response alone
  does not establish an entitlement or redistribution right.
- [NSE historical-data product documentation](https://www.nseindia.com/static/market-data/eod-historical-data-subscription),
  page marked updated 2026-09-02, provides technical-specification links and
  subscription information. No subscription, contact, order or new feed was
  created. The existing repository's NSE daily adapter is reused unchanged.
- [Nippon's official factsheet/portfolio disclosure index](https://mf.nipponindiaim.com/investor-service/downloads/factsheet-portfolio-and-other-disclosures),
  reviewed November 5, lists October 2026 fundamentals and September 2026 monthly
  portfolio publications. Index availability does not authorize a PDF or prove
  scheme identity, effective dates, labelled metrics or artifact authenticity.
  No current PDF was fetched or added to the approved registry for this work.
- Existing [official tracking ingestion notes](etf-official-tracking-ingestion.md)
  and [SBI identity gate](sbi-ter-identity-evidence-gate.md) describe the staged
  artifact hashes/reporting periods and unresolved approval gates. Their
  November 4 retrieval claims remain historical; no artifact was re-fetched.
- Existing [BSE source research](etf-bse-recovery.md) remains a separate blocked
  live integration. Neither its reference master nor documentation is executed
  traded-price evidence.

No guessed URL family, unofficial API, authenticated-source bypass, AMFI file,
or newly approved provider mapping is introduced.

## Historical data: fresh initialization, no backfill

ETF Radar does not ship a historical backfill runner, incremental backfill job
or manual backfill endpoint. Historical market data is acquired by the normal
full ETF cycle: the HISTORY_ONBOARDING phase calls the shared
`app.etf_history.acquire_etf_history` and persists verified daily bars to the
shared `global_daily_market_bars` table. The history API and chart read stored
bars through `app.etf_history_selection` / `app.etf_history_read`, which remain
shared services. Missing or unverifiable history is reported as unavailable; it
is never filled, interpolated or fabricated. Chart ranges longer than the stored
depth show only what is stored.

A fresh start is performed by the operator-run, not-auto-applied reset procedure
in `scripts/database/maintenance/etf-radar-reset/`, followed by one normal full
cycle.

## Validation boundary

Unit tests use synthetic canonical rows/provider results. They do not
demonstrate actual NSE or AMC coverage. No full-universe acquisition was executed.
