# Persisted OHLCV technical features (TECHNICAL_FEATURES_V2)

## Integration and source selection

`TechnicalFeatureEngine.compute(..., daily_bar_history=...)` evolves the existing
pure engine. No provider, database or network call occurs inside computation.
Stage-B `GlobalScanner.enrich_candidates` reads NSE DailyMarketBar rows in bounded
candidate-ID batches, then passes grouped histories to this input. It retains
batched close reads for fallback and the unchanged sector engine. Phase-1 scan,
preScore, eligibility, sector formulas, and Stage-B score weights are unchanged.

Source policy: only REAL persisted NSE candles for the requested canonical UUID,
matching requested currency/provider allowlist, and known at as_of. Exchange
DATEs are compared to Asia/Kolkata DATE; retrieval timestamps remain UTC. History
trading-date fields expose actual DATEs. Display history timestamps are exchange
midnight metadata only and never become candle-series keys.

A fresh NSE series with at least 20 usable observations takes precedence. When
NSE is shorter than 20 or stale and a current non-conflicting close-only series
has at least 20 observations, the entire close-only series is selected instead,
with `NSE_DAILY_HISTORY_BELOW_20` or `NSE_DAILY_HISTORY_STALE` diagnostics. This
prevents four old candles from disabling hundreds of usable closes. If neither
source can support overall readiness, available NSE evidence remains visible
without fabricating sufficient history. Missing NSE history uses close fallback.
No extension of NSE history with another provider's older closes, and no mixed
high/low/close/volume candles. A selected short NSE series can compute ATR at 15
candles even though overall technical scoring still requires 20.

Within NSE, latest known retrieval wins a date correction. Identical ties
collapse; conflicting same-retrieval OHLCV ties fail that date. A conflicting
latest date blocks current features and fallback. Older conflicts are excluded
from closes with diagnostics; OHLC warmup restarts after them. Other providers,
wrong currencies, future retrievals/dates and DEMO rows cannot contaminate NSE.
Provider symbols are not identity. Missing candle close is treated as a conflict,
not substituted with another provider's price.

Provenance: `DAILY_MARKET_BAR_NSE` or `CLOSE_ONLY_FALLBACK`; conflict diagnostics
include `MIXED_NOT_ALLOWED`. Daily-bar count is reported even when fallback is
selected. Source URLs/cookies are not added to the technical output.

## Formula and readiness contracts

ATR14 uses actual prior chronological candle close, ignoring provider-supplied
previousClose. TR is max(high-low, abs(high-prior close), abs(low-prior close)).
The first candle supplies dependencies only, not an invented TR. Seed ATR with
the mean of 14 TR values (15 complete candles); subsequent ATR is
(previous ATR * 13 + current TR) / 14. ATR percent is 100*ATR/latest close.
Zero volatility is zero, not null. Missing OHLC restarts warmup rather than
compressing out a missing candle and bridging it.

ADX14: upMove=current high-prior high; downMove=prior low-current low. Positive
DM is upMove only when positive and strictly greater than downMove; negative DM
is analogous. Ties give both zero. Wilder-smooth TR/+DM/-DM over 14 changes;
DI = 100*smoothed DM/smoothed TR. DX = 100*abs(+DI - -DI)/(+DI + -DI).
Zero denominators yield zero DX. Seed ADX with 14 DX values, then Wilder-smooth.
Minimum 28 complete chronological candles. Output is bounded 0–100. Flat markets
yield ADX=0 after warmup. No ADX approximation from closes.

Inputs are Decimal-validated; ATR/ADX use the engine's established float numeric
convention and round only at output. Independent tests use hand calculations
and closed-form Decimal weighted sums rather than duplicating the recursion.
Formula references: [Fidelity ATR](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/atr)
and [Fidelity DMI](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/DMI).

Readiness tiers remain <20, 20–49, 50–99, 100–199 and >=200. Feature readiness
separately reports RSI14 (15 closes), ATR14 (15 candles), ADX14 (28 candles),
MA20/50/100/200, breakout (prior 20 + current), VOLUME20 and VOLUME_CONFIRMATION.
Diagnostics distinguish missing OHLC/volume, insufficient history, conflicts,
zero volume baseline and staleness. The existing seven-day technical price-age
policy remains; acquisition freshness settings are not technical-score settings.

## Volume and scoring

Current volume retains its actual BIGINT integer. Average20 uses the prior 20
selected candle observations, excluding current; ratio therefore requires 21
candles. Sum/average/ratio normalization uses Decimal before float output, with
no turnover-based inference. Missing prior volume makes the baseline unavailable;
missing current volume leaves ratio unavailable. Current zero gives ratio zero
against a positive baseline. All-zero baseline leaves average=0 and ratio=null
with ZERO_BASELINE diagnostics. Conflicted dates cannot be compressed out of the
21-observation volume window.

Expansion: ratio >=1.5 (existing confirmation threshold). Contraction: ratio
<=0.75 (explicit TechnicalConfig threshold). Otherwise normal. Thresholds are
validated to bracket one. Confirmation is true/false only when an existing
price breakout/reversal signal and a valid volume ratio are available; missing
volume stays null. Reversal confirmation is diagnostic only. Without a price
signal, confirmation is NOT_APPLICABLE. Legacy explicit PersistedVolumeObservation
input remains supported for compatibility; Stage-B fallback does not supply it.

Existing close-based state rules, MA/RSI/MACD/returns/slopes, close-based rolling
support/resistance/extrema and their ordering remain unchanged. Score stays
0–100: trend alignment/momentum/price position weights 50/30/20, renormalized over
available components; overextension penalty 15; existing confirmed-breakout
bonus 5. ATR is volatility context and ADX is non-directional strength: neither
adds directional score. No reversal bonus or weak-volume penalty is introduced.
Core confidence remains unchanged; separate ohlcvFeatureCoverage reports ATR,
ADX and volume-ratio availability without penalizing close-only fallback.

Audited state precedence (unchanged): OVEREXTENDED when DMA20 distance >=10%
and RSI >=70; otherwise BREAKOUT above the prior-20-close resistance by >1%;
otherwise PULLBACK_IN_UPTREND when broad trend is up, price >DMA50, retreat from
the prior five-close maximum is >=2%, and DMA20 or DMA50 proximity is <=3%;
otherwise REVERSAL_CANDIDATE when slope50 <-0.02%, slope20 >0.02% and price
>DMA20; otherwise UPTREND on broad-up and price >DMA50; otherwise DOWNTREND on
price <DMA50 and slope50 <-0.02%; otherwise BASE_BUILDING on absolute slope20
<=0.02% and the latest 20-close range <=5%; otherwise RANGE_BOUND. Broad-up
requires positive slope50 >0.02% and, when DMA200 exists, DMA50 >DMA200.
Unavailable/stale history keeps INSUFFICIENT_DATA. Price breakdown below the
prior-20 support by >1% remains separate breakout-state evidence.

A deterministic Stage-B engineering test with identical closes changes technical
and Stage-B scores from 84 to 89 only after actual expansion volume confirms an
existing price breakout; preScore and the fallback candidate remain unchanged.
This synthetic comparison is not an investment conclusion.

## Persistence reads and runtime evidence

At default batch size 250, no benchmark references: empty candidates = 0 queries;
1 candidate = 1 NSE daily read + 1 close read; 18 candidates = the same 2 reads.
Larger sets retain bounded batching. Benchmarks participate only in close batches.
Tests count SQLite statements, check deterministic grouping, forbid provider
acquisition and verify Phase-1 objects remain unchanged.

Persisted-only smoke on 2026-09-13 used the prior local backfill SQLite database
(opened read-only) and a read-only snapshot of existing local PostgreSQL closes.
NILKAMAL and POLYCAB each have four persisted NSE candles, September 1–4: not
enough for ATR14, ADX14 or volume20. Candle-only results correctly remain null.
Their longer current close histories are retained through explicit fallback:

| Instrument | NSE bars | Selected closes | Technical score before/after | State |
|---|---:|---:|---|---|
| NILKAMAL | 4 | 272 | 85.67599167 / 85.67599167 | PULLBACK_IN_UPTREND |
| POLYCAB | 4 | 341 | 25.07289151 / 25.07289151 | DOWNTREND |
| PERSISTENT | 0 | 341 | 52.19912039 / 52.19912039 | UPTREND |

All repeated outputs, including reversed input order, were identical. Provider
calls were zero; network connection creation was forbidden during computation.
Numerical ATR/ADX/volume validation uses independent sufficient-history tests,
not invented runtime candles. No live NSE request or new history persistence.
Runtime artifacts are under ignored `.tmp/`.

The local deployed PostgreSQL schema currently lacks the daily-bar table, so
this is not deployed PostgreSQL application validation. Richer live-data sample
conclusions require persisted backfill; this phase does not acquire it.

Remaining scope: deployed PostgreSQL validation, sector benchmark mapping/history,
broad-market benchmark history, and recommendation/ranker/prediction phases.
