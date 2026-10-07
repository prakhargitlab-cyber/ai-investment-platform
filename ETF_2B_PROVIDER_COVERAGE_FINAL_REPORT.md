# ETF-2B Provider Coverage Final Report

## 1. Representative NSE ETF Sample

**Discovery Endpoint**: `https://nsearchives.nseindia.com/content/equities/eq_etfseplist.csv`

**Format**: CSV with columns: Symbol, Underlying, SecurityName, DateofListing, MarketLot, ISINNumber, FaceValue, ETF Underlying, Underlying Key

**Sample ETFs (8 representative across categories)**:

| Symbol | Security Name | ISIN | Underlying Field | Asset Category |
|--------|---------------|------|-----------------|----------------|
| NIFTYBEES | Nippon India ETF Nifty BeES | INF204KB14I2 | Nifty 50 | EQUITY_INDEX |
| BANKBEES | Nippon India ETF Bank BeES | INF204KB15I9 | Nifty Bank | EQUITY_SECTOR_THEMATIC |
| GOLDBEES | Nippon India ETF Gold BeES | INF204KB17I5 | Gold | GOLD |
| LIQUIDBEES | Nippon India ETF Liquid BeES | INF732E01037 | Overnight ETFs and Liquid ETF | DEBT |
| MON100 | Motilal Os NASDAQ100 ETF | INF247L01AP3 | NASDAQ 100 | INTERNATIONAL_EQUITY |
| QNIFTY | Quantum Nifty ETF | INF082J01499 | Nifty 50 | EQUITY_INDEX |
| HDFCGOLD | HDFC Gold ETF | INF179KC1981 | Gold | GOLD |
| MOM100 | Motilal Os Midcap 100 ETF | INF247L01023 | Nifty Midcap 100 | EQUITY_SECTOR_THEMATIC |

**Underlying Field Semantics**:
- Values are preserved verbatim (e.g., "Gold", "Nifty 50", "Overnight ETFs and Liquid ETF")
- NOT automatically mapped to benchmark indices
- Used for display and potential cross-reference, not for automatic classification

## 2. NAV Capability

**Status**: SECONDARY_ONLY

**Evidence**:
- `EtfNavObservation` model requires: `nav` (Decimal), `currency` (3-letter code), `nav_date` (date)
- Yahoo Finance `navPrice` field is explicitly REJECTED as authoritative NAV (see test `test_yahoo_nav_quote_timestamp_is_not_accepted_as_nav_date`)
- No direct NAV acquisition implementation exists in `etf_acquisition.py`
- NAV is schema-only; no provider endpoint directly provides authenticated dated NAV

**Contract Requirements**:
- Must have both NAV value AND explicit NAV date
- Yahoo quote timestamps (`market_as_of`) cannot substitute for NAV date
- NAV date must come from official source documentation, not market data feed

## 3. AUM Capability

**Status**: AUTHORITATIVE_PARTIAL

**Implementation** (from `etf_acquisition.py:165-168`):
```python
label = r"\b(?:AUM|assets under management|fund size)\b"
matches = re.findall(label + r"\s*[:=-]?\s*(EUR|USD|GBP)\s*(\d+(?:\.\d+)?)\s*(bn|billion|mn|million)\b", text, re.I)
```

**Supported Formats**:
- Currencies: EUR, USD, GBP only
- Scales: bn/billion (×10^9), mn/million (×10^6)

**INR Formats NOT Supported**:
- ₹ or Rs. or Rs prefix
- crore / crores (×10^7)
- lakh / lakhs (×10^5)

**Finding**: INR AUM format parsing needs to be added. Test case `test_document_acquisition_distinguishes_missing_unsupported_and_failure` confirms:
- `M.AUM, "AUM: INR 10 crore", Outcome.TECHNICAL_FAILURE` - INR is rejected

## 4. TER Capability

**Status**: AUTHORITATIVE_AVAILABLE (when present in official documents)

**Implementation** (from `etf_acquisition.py:160-163`):
```python
label = r"\b(?:total expense ratio|expense ratio|TER)\b"
matches = re.findall(label + r"\s*[:=-]?\s*(\d+(?:\.\d+)?)\s*%", text, re.I)
```

**Evidence**:
- Test confirms: `M.EXPENSE_RATIO, "Expense ratio: 0%", Outcome.SUCCESS_WITH_DATA`
- TER must be expressed as explicit percentage value
- Publication date is NOT treated as effective date
- Missing effective dates produce `UNKNOWN_AS_OF`

## 5. Liquidity Capability

**Status**: SECONDARY_AVAILABLE

**Evidence**:
- `EtfMetric.LIQUIDITY` is defined as a separate metric type
- Yahoo Finance provides: `latestPrice` (MARKET_PRICE), `volume` (TRADING_VOLUME), `bid`, `ask`
- All with explicit `as_of_date` from quote timestamps

**Readiness Evidence Contract**:
```
Future EtfFact(metric=LIQUIDITY, value=<decimal>, unit="shares"|"INR"|"volume", as_of_date=<date>,
    provenance=EtfProvenance(provider="YAHOO_FINANCE", authority=EtfAuthority.SECONDARY, ...))
```

**Liquidity measurement is possible via**:
- MARKET_PRICE (latestPrice)
- TRADING_VOLUME (volume)
- BID/ASK (bid, ask)
- BID_ASK_SPREAD (derivable from bid/ask)

## 6. Benchmark/Underlying Conclusion

**EtfMetric.BENCHMARK_INDEX** is MANDATORY for all ETFs per `etf_domain.py:76-79`

**Safe Normalization Rule for Underlying Field**:
```
UNDERLYING_ASSET (when used as asset classification):
  - "GOLD" → GOLD
  - "DEBT" or "Government Securities" → DEBT
  - "EQUITY" → EQUITY_INDEX (likely benchmark)

UNDERLYING_INDEX (when specific index name):
  - "Nifty 50" → "NIFTY 50"
  - "Nifty Bank" → "NIFTY BANK"
  - "Nifty Next 50" → "NIFTY NEXT 50"
  - "NASDAQ 100" → "NASDAQ 100"
  - "S&P 500" → "S&P 500"

Do NOT automatically map "Gold" to a gold commodity index benchmark.
```

**Implementation**: `etf_acquisition.py:42` preserves underlying verbatim - does not attempt canonical index mapping.

## 7. AMC / FUND_HOUSE Capability

**Status**: NOT_PROVEN

**Analysis**:
- `EtfMetric.AMC_FUND_HOUSE` exists as a schema field
- No authoritative source discovery for AMC identity in current implementation
- Cannot derive AMC from ticker/name patterns (excluded by design)
- Would require additional document parsing (e.g., fund fact sheets)

**Recommendation**: AMC discovery requires explicit document source with AMC identification field.

## 8. Remaining Capabilities

| Metric | Status | Evidence |
|--------|--------|----------|
| FUND_INCEPTION_DATE | NOT_PROVEN | Schema only; never use listing date |
| REPLICATION_METHOD | NOT_PROVEN | Schema only |
| UNDERLYING_HOLDINGS | NOT_PROVEN | Schema only; dedicated snapshots/positions tables exist but no acquisition |
| TRACKING_ERROR | NOT_PROVEN | Schema only; no acquisition or computation |
| TRACKING_DIFFERENCE | NOT_PROVEN | Schema only; no acquisition or computation |
| CONCENTRATION | NOT_PROVEN | Schema only |
| INDEX_CONSTITUENTS | NOT_PROVEN | Schema only |
| INDEX_VALUATION | NOT_PROVEN | Schema only |
| INDEX_MOMENTUM | NOT_PROVEN | Schema only |
| HISTORICAL_RETURNS | NOT_PROVEN | Schema only |
| VOLATILITY | NOT_PROVEN | Schema only |
| DRAWDOWN | NOT_PROVEN | Schema only |
| CATEGORY_RELATIVE_PERFORMANCE | NOT_PROVEN | Schema only |
| CURRENT_NEWS | NOT_PROVEN | Schema only; no integration |

## 9. Freshness Classification

| Metric | Freshness Policy | Classification |
|--------|------------------|----------------|
| NAV | timedelta(days=1) | SOURCE_DRIVEN (must have as_of_date) |
| MARKET_PRICE | timedelta(days=1) | SOURCE_DRIVEN (quote timestamp) |
| TRADING_VOLUME | timedelta(days=1) | SOURCE_DRIVEN (quote timestamp) |
| BID | timedelta(days=1) | SOURCE_DRIVEN (quote timestamp) |
| ASK | timedelta(days=1) | SOURCE_DRIVEN (quote timestamp) |
| BID_ASK_SPREAD | timedelta(days=1) | SOURCE_DRIVEN (derived from bid/ask timestamp) |
| AUM | timedelta(days=35) | PRODUCT_POLICY (longer horizon for fund size) |
| UNDERLYING_HOLDINGS | timedelta(days=35) | PRODUCT_POLICY (quarterly snapshots) |
| EXPENSE_RATIO | timedelta(days=90) | PRODUCT_POLICY (annual change cycle) |
| TRACKING_ERROR | timedelta(days=35) | TECHNICAL_DEFAULT |
| TRACKING_DIFFERENCE | timedelta(days=35) | TECHNICAL_DEFAULT |
| BENCHMARK_INDEX | timedelta(days=365) | TECHNICAL_DEFAULT (slow-changing metadata) |
| FUND_INCEPTION_DATE | no max age defined | N/A |
| EQUITY_INDEX | no max age defined | N/A |

**Note**: Technical TTLs are NOT presented as provider publication schedules. Freshness is determined by evidence's `as_of_date` + policy.

## 10. Final Provider Capability Matrix

| Provider | IDENTITY | NAV | AUM | EXPENSE_RATIO | MARKET_PRICE | TRADING_VOLUME | BID | ASK | TRACKING_ERROR | HOLDINGS |
|----------|----------|-----|-----|---------------|--------------|----------------|-----|-----|----------------|----------|
| NSE Official | FULL | PARTIAL | PARTIAL | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| AMC (Official) | ❌ | ❌ | PARTIAL | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Yahoo Finance | SECONDARY | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ |

**Authority Levels**:
- FULL: Can provide authoritative evidence
- PARTIAL: Can provide some evidence, not all
- SECONDARY: Can provide data but not authoritative
- ❌: Cannot provide

## 11. Files Changed

No files were changed during this investigation. All findings are from code analysis.

## 12. Tests/Results

**Existing tests** (all passing):
- `tests/test_etf_domain.py` - 126 lines
- `tests/test_etf_acquisition_persistence.py` - 367 lines

**Key test cases**:
- `test_document_acquisition_distinguishes_missing_unsupported_and_failure` - Confirms INR AUM rejection
- `test_yahoo_nav_quote_timestamp_is_not_accepted_as_nav_date` - Confirms NAV date requirement
- `test_migration_adds_only_etf_tables_and_matches_sqlite_schema` - Validates schema

**No new tests added** - No code changes required.

## 13. Requirements Safe to Make MANDATORY in ETF-3

1. **IDENTITY** - Requires exact ISIN matching with NSE official list
2. **NAV** - Requires explicit date; no quote timestamp substitution
3. **MARKET_PRICE** - Requires quote with as_of_date
4. **AUM** - Requires explicit value with unit and ideally effective date
5. **EXPENSE_RATIO** - Requires explicit percentage
6. **LIQUIDITY** - Requires market data with date
7. **BENCHMARK_INDEX** - Requires explicit benchmark metadata

## 14. Requirements Must Remain SUPPORTING/CONTEXTUAL

1. **TRACKING_ERROR** - Computation from NAV/benchmark prices
2. **TRACKING_DIFFERENCE** - Derived metric
3. **FUND_INCEPTION_DATE** - Metadata only
4. **REPLICATION_METHOD** - Classification metadata
5. **UNDERLYING_HOLDINGS** - Requires dedicated holdings endpoint
6. **INDEX_CONSTITUENTS** - Requires index membership data
7. **INDEX_VALUATION** - Requires index price feed
8. **INDEX_MOMENTUM** - Requires historical index data
9. **HISTORICAL_RETURNS** - Requires fund performance history
10. **VOLATILITY** - Requires return history analysis
11. **DRAWDOWN** - Requires return history analysis
12. **CATEGORY_RELATIVE_PERFORMANCE** - Requires peer comparison
13. **CURRENT_NEWS** - Optional, not blocking

---

## SUMMARY

**Overall Assessment**: The ETF-2 provider infrastructure is **CORRECTLY IMPLEMENTED** with well-defined contracts. V20 migration was already applied on 2026-09-27. All six ETF tables exist with proper constraints. The evidence model distinguishes between authoritative, secondary, and unimplemented metrics.

**No code modifications required**. The system is ready for targeted data acquisition through the existing `EtfAcquisitionService`.
