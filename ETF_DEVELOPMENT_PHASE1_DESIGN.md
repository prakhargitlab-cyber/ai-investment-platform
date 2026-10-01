# ETF Development — Phase 1: Architecture + Data Contract + Gap Analysis

## Executive Summary

This document provides a comprehensive diagnosis and design for adding ETF (Exchange Traded Fund) support to the AI Investment Intelligence Platform. The existing stock pipeline (STOCK_RULE_ENGINE_V1) must NOT be used for ETFs; they require dedicated, ETF-specific research, applicability, scoring, and ranking logic.

---

## 1. CURRENT ETF SUPPORT INVENTORY

### A) Already ETF-Aware

| Component | Evidence | Location | Status |
|-----------|----------|----------|--------|
| `EtfResearchProfile` model | `class EtfResearchProfile(ResearchBaseModel)` | `app/models.py:562` | **Model defined** |
| ETF profile loading | `etf_profiles: list[EtfResearchProfile]` in `ResearchRepository` | `app/repository.py:324` | **Minimal support** |
| Asset type filtering | `asset_type` parameter in `classify_requirements()` | `app/research_applicability.py:69` | **Basic check exists** |

**Key observations:**
- The `EtfResearchProfile` model exists with minimal fields: `instrument_id`, `fund_id`, `fund_name`, `ticker`, `exchange`, `mic`, `provider`, `provider_instrument_id`, `isin`, `currency`, `fund_provider`, `underlying_index`, `known_domains`, `facts`
- ETF categorization by `asset_type` is checked but only affects `ORDER_BOOK_CAPEX_GUIDANCE` requirement
- No dedicated ETF rules, readiness, or scoring pipeline exists

### B) Stock-Only But Reusable

| Component | Why Reusable | Notes |
|-----------|--------------|-------|
| `ShareholdingCategory` enum | `PROMOTER`, `FII_FPI`, `DII`, `PUBLIC_RETAIL`, etc. categories apply to ETFs | ETFs don't have promoter shareholding but have institutional categories |
| `ShareholdingSnapshot` / `ShareholdingSnapshotValue` models | Persistent storage model for ownership patterns | Need adaptation for fund structure rather than promoter holdings |
| `global_shareholding_snapshots` database table | Durable storage pattern | Could store ETF ownership of underlying indices |
| `global_opportunity_snapshot` table | Instrument identification pattern | Already used for stocks, ETFs would follow same pattern |
| `StructuredMarketSnapshotRecord` model | Market price/valuation storage | Directly reusable for ETFs |
| `MarketPriceObservation` model | Price observation pattern | Directly reusable |
| `DailyMarketBar` model | OHLCV storage | Directly reusable |
| `OpenAPI schema`/APIGateway | HTTP orchestration pattern | Could be extended for ETF endpoints |
| PostgreSQL persistence layer | Durable storage with foreign keys | ETFs need their own tables but same patterns |

### C) Stock-Only And Must NOT Be Reused

| Component | Reason |
|-----------|--------|
| `CompanyResearchProfile` | Stocks are companies, ETFs are funds; different entity type |
| `ShareholdingCategory.PROMOTER / PROMOTER_PLEDGE` | ETFs have fund manager/control, not promoter ownership |
| `ShareholdingCategory.MUTUAL_FUNDS` | ETFs ARE funds, not investors in funds |
| `ShareholdingCategory.GOVERNMENT`, `ShareholdingCategory.OTHERS` | Not appropriate for ETF structure |
| `FinancialResultPeriod` / `QuarterlyResult` | ETFs don't publish quarterly P&L like companies |
| `financial_adequacy`, `gross_npa`, `net_npa` metrics | Regulatory ratios for banks/lenders, not ETFs |
| `Order_book`, `contract`, `capex` concepts | ETFs don't have industrial assets |
| `ROE`, `ROCE`, `operating_margin` | ETFs don't have profit / earnings concepts |
| `Business quality` scoring rules | Derived from corporate fundamentals, not applicable |
| `MarketUniverse` discovery by sector/subsector | ETFs are traded instruments, not industries |
| Stock Rule Engine area weights | ETF scoring requires fundamentally different dimensions |

### D) Missing

| Capability | Gap Description |
|------------|-----------------|
| ETF identification / discovery | No mechanism to discover NSE ETF universe |
| ETF classification model | Need `EtfType` enum (EQUITY_index, SECTOR_thematic, GOLD, etc.) |
| ETF-specific metrics storage | No tables for NAV, AUM, expense ratio, tracking error |
| ETF research acquisition | No providers for ETF fact sheets, fund fact documents |
| ETF readiness contract | No requirements defined for fund-specific evidence |
| ETF rule engine | No ETF scoring logic exists |
| ETF opportunity pipeline | No orchestration for ETF cycles |
| ETF frontend views | No UI for ETF research display |

---

## 2. ETF CLASSIFICATION MODEL

### Proposed ETF Subtype Model

```python
class EtfType(StrEnum):
    EQUITY_INDEX = "EQUITY_INDEX"           # Broad market, index-tracking
    EQUITY_SECTOR_THEMATIC = "EQUITY_SECTOR_THEMATIC"  # Sector-specific
    EQUITY_SMART_BETA = "EQUITY_SMART_BETA"  # Factor-based (value, momentum, etc.)
    DEBT = "DEBT"                           # Corporate/government bond funds
    GOLD = "GOLD"                           # Precious metals
    SILVER = "SILVER"                       # Commodity
    INTERNATIONAL_EQUITY = "INTERNATIONAL_EQUITY"  # GlobalDeveloped, EmergingMarkets
    INTERNATIONAL_DEBT = "INTERNATIONAL_DEBT"      # Global bonds
    MONEY_MARKET = "MONEY_MARKET"           # Cash equivalents
   HYBRID = "HYBRID"                         # Mixed asset
    OTHER = "OTHER"                         # Unclassifiable
```

### Classification Approach

**Do not assume all categories need identical rules.** Each ETF type has distinct economics:

| ETF Type | Key Characteristics | Research Implications |
|----------|---------------------|----------------------|
| EQUITY_INDEX | Replicates broad index | Focus on tracking, liquidity |
| SECTOR_THEMATIC | Concentrated sector exposure | Concentration/rebalancing risk |
| SMART_BETA | Factor exposure | Factor performance attribution |
| DEBT | Interest rate sensitivity | Duration, credit quality |
| GOLD/SILVER | Commodity exposure | No earnings, pure asset play |
| INTERNATIONAL | Currency hedging needed | With/without currency exposure |
| MONEY_MARKET | Very low volatility | Income/yield focus |
| HYBRID | Multi-asset | Composite risk/return |

### Metadata Sufficiency

**Current instrument metadata is NOT sufficient** for ETF classification. Missing:

1. **`asset_class`** - Explicit classification (equity, debt, commodity)
2. **`category`** - Index, thematic, smart beta
3. **`underlying_index`** - Benchmark identifier
4. **`replication_method`** - Physical, synthetic, optimized
5. **`fund_structure`** - Open-ended, closed-ended
6. **`issuer_type`** - AMCs, banks, insurance companies

**Required additions to `EtfResearchProfile`:**

```python
class EtfResearchProfile(ResearchBaseModel):
    # Existing fields...
    etf_type: EtfType  # NEW: Classification
    asset_class: str   # NEW: equity/debt/commodity
    category: str      # NEW: index/thematic/smart-beta/etc.
    replication_method: str  # NEW: physical/synthetic/optimized
    benchmark_index: str     # NEW: Underlying index ID
    isin: str | None = None
    fund_structure: str | None = None  # NEW: open-ended/closed-ended
    dividend_policy: str | None = None  # NEW: Accumulating/Distributing
    sector_exposure: dict[str, Decimal] | None = None  # NEW: Top sector weights
    currency_hedged: bool = False  # NEW: For international ETFs
```

---

## 3. ETF RESEARCH REQUIREMENT CONTRACT

### ETF Research Categories Matrix

| Requirement | Applicable ETF Types | Mandatory/Supporting/Contextual | Refresh Cadence | Durable/Ephemeral | Authoritative Source | Currently Supported |
|-------------|---------------------|--------------------------------|-----------------|--------------------|---------------------|---------------------|
| **IDENTITY** | All | Mandatory | N/A | Durable | NSE ETF Circular | ❌ |
| **BENCHMARK_INDEX** | All | Mandatory | Annual | Durable | NSE/NAV documents | ❌ |
| **NAV** | All | Mandatory | Daily | Durable | AMCs, NSE | ❌ |
| **MARKET_PRICE** | All | Mandatory | Daily | Durable | NSE Live Quotes | ✅ |
| **PREMIUM_DISCOUNT** | All (derived) | Supporting | Daily | Ephemeral | NAV + Price | ❌ |
| **AUM** | All | Mandatory | Monthly | Durable | AMCs, NSE | ❌ |
| **EXPENSE_RATIO / TER** | All | Mandatory | Annual | Durable | Fund Facts | ❌ |
| **TRACKING_ERROR** | All | Supporting | Daily | Ephemeral | Constituent calculation | ❌ |
| **TRACKING_DIFFERENCE** | All | Supporting | Daily | Ephemeral | Constituent calculation | ❌ |
| **LIQUIDITY** | All | Mandatory | Daily | Durable | NSE volume data | ❌ |
| **BID_ASK_SPREAD** | All | Supporting | Daily | Ephemeral | Order book data | ❌ |
| **TRADING_VOLUME** | All | Supporting | Daily | Durable | NSE | ❌ |
| **FUND_AGE** | All | Supporting | N/A | Durable | Fund inception date | ❌ |
| **AMC / FUND_HOUSE** | All | Mandatory | N/A | Durable | Fund documents | ❌ |
| **REPLICATION_METHOD** | All | Mandatory | N/A | Durable | Fund facts | ❌ |
| **UNDERLYING_HOLDINGS** | Equity ETFs | Supporting | Daily | Durable | NSE holdings file | ⚠️ Partial (exists in NSE data) |
| **CONCENTRATION** | All | Supporting | Quarterly | Ephemeral | Holdings analysis | ❌ |
| **INDEX_CONSTITUENTS** | Active ETFs | Contextual | Daily | Ephemeral | Index provider | ❌ |
| **INDEX_VALUATION** | Index ETFs | Supporting | Daily | Ephemeral | Index provider | ❌ |
| **INDEX_MOMENTUM** | Index ETFs | Supporting | Daily | Seasonal | Index provider | ❌ |
| **HISTORICAL_RETURNS** | All | Supporting | Daily | Durable | NSE, Yahoo | ✅ |
| **VOLATILITY** | All | Supporting | Daily | Ephemeral | Return history | ❌ |
| **DRAWDOWN** | All | Supporting | Daily | Ephemeral | Return history | ❌ |
| **CATEGORY_RELATIVE_PERFORMANCE** | All | Supporting | Daily | Durable | Peer benchmark | ❌ |

### Key Differences from Stock Requirements

1. **No promoter-related requirements**: ETFs don't have promoter shareholding or pledge
2. **No quarterly corporate earnings**: ETFs report AUM/fund performance, not corporate P&L
3. **No ROCE/ROE**: No capital employed calculations for funds
4. **No order book**: No industrial assets to manage
5. **NAV is central**: Must be primary fund valuation metric
6. **Tracking quality is key**: Difference between NAV and market price performance

### NOT_APPLICABLE Requirements for ETFs

These stock requirements MUST remain NOT_APPLICABLE for ETFs (never "missing"):

- `SHAREHOLDING` (PROMOTER, PROMOTER_PLEDGE - don't apply to ETFs)
- ` BUSINESS_QUALITY_FACTS` (ROE, ROCE, margins - ETFs don't have these)
- `QUARTERLY_FINANCIALS` (Quarterly earnings - ETFs don't file quarterly results)
- `ORDER_BOOK_CAPEX_GUIDANCE` (No industrial capacity to cite)

---

## 4. SOURCE STRATEGY

### Current Provider Capabilities

| Data Source | ETF Capability | Status |
|-------------|----------------|--------|
| **NSE** | Official ETF listings, daily NAV, fund facts, holdings files | ✅ Available |
| **AMC Websites** | Fund fact sheets, TER, AUM, benchmark info | ⚠️ Partially available via structured_market |
| **Yahoo Finance** | Market price, historical returns, basic fund metrics | ⚠️ Available via structured_market |
| **Google Search** | Discovery of fund documents | ✅ Available |
| **RSS/News** | ETF-specific news/events | ✅ Available |
| **BSE** | Alternative exchange data | ⚠️ Limited |

### ETF-Specific Acquisition Needs

| Metric | Authoritative Source | Fallback Source | Implementation Status |
|--------|---------------------|-----------------|---------------------|
| NAV | NSE ETF Daily Disclosures | AMCs directly | ❌ Not implemented |
| AUM | NSE, AMCs | Yahoo estimate | ❌ Not implemented |
| Expense Ratio | Fund Fact Sheet (PDF) | NSE CSV | ❌ Not implemented |
| Tracking Error | Calculated from NAV vs Index | N/A | ❌ Not implemented |
| Holdings | NSE Holdings File (XML) | AMCs website | ⚠️ NSE holdings exist |
| Benchmark Index | NSE ETF Database | Fund fact sheet | ❌ Not implemented |
| Fund Houses | NSE ETF Listings | Manual mapping | ❌ Not implemented |

---

## 5. ETF READINESS CONTRACT

### Proposed Readiness States

```python
# ETF-specific readiness statuses
ETF_FULLY_ANALYZED = "FULLY_ANALYZED"
ETF_DATA_GENUINELY_UNAVAILABLE = "DATA_GENUINELY_UNAVAILABLE"  
ETF_TECHNICAL_FAILURE = "TECHNICAL_FAILURE"
ETF_PARTIAL = "PARTIAL"  # Minimum safe gate available

# ETF-specific requirements (separate from STOCK requirements)
ETF_REQUIREMENTS = {
    "ETF_FUND_PROFILE": "ETF identity, classification, benchmark",
    "ETF_NAV": "Net Asset Value with currency",
    "ETF_AUM": "Assets Under Management",
    "ETF_EXPENSE_RATIO": "Total Expense Ratio",
    "ETF_LIQUIDITY": "Trading volume and bid-ask spread",
    "ETF_VALUATION": "Market price, premium/discount",
    "ETF_HOLDINGS": "Underlying securities (for active/concentrated)",
}
```

### ETF Readiness Contract Philosophy

**Same truthful terminal philosophy as stock pipeline:**

1. **FULLY_ANALYZED**: All mandatory ETF requirements READY_FRESH/READY_STALE
2. **DATA_GENUINELY_UNAVAILABLE**: Evidence not in official domain (e.g., no NAV published)
3. **TECHNICAL_FAILURE**: API timeout, transport error (retry eligible)
4. **PARTIAL**: Minimum safe gate with marketable inputs available

**ETF must NOT require stock concepts:**

- ❌ Promoter shareholding
- ❌ Promoter pledge  
- ❌ ROCE / corporate debt/equity scoring
- ❌ Order book / capacity
- ❌ Quarterly corporate earnings
- ❌ Corporate governance events

**ETF equivalents:**

- ✅ Fund manager AUM (alternative to market cap)
- ✅ NAV premium/discount (alternative to valuation)
- ✅ Liquidity metrics (alternative to order book)

### One Applicability Contract

Readiness and ETF Rule Engine MUST use **ONE applicability contract**:

```python
def classify_etf_applicability(etf_type: EtfType, asset_class: str) -> dict:
    """Single source of truth for ETF requirement applicability."""
    result = {}
    
    # All ETFs need core metrics
    result["ETF_NAV"] = {"applicable": True, "status": "APPLICABLE"}
    result["ETF_AUM"] = {"applicable": True, "status": "APPLICABLE"}
    result["ETF_EXPENSE_RATIO"] = {"applicable": True, "status": "APPLICABLE"}
    result["ETF_LIQUIDITY"] = {"applicable": True, "status": "APPLICABLE"}
    
    # Debt ETFs don't need equity-specific metrics
    if asset_class != "debt":
        result["ETF_TRACKING_ERROR"] = {"applicable": True, "status": "APPLICABLE"}
    else:
        result["ETF_TRACKING_ERROR"] = {"applicable": False, "status": "NOT_APPLICABLE"}
    
    # Gold/Silver don't have earnings
    if asset_class in ("gold", "silver"):
        result["ETF_HOLDINGS"] = {"applicable": False, "status": "NOT_APPLICABLE"}
    
    return result
```

---

## 6. ETF_RULE_ENGINE_V1 PROPOSED MODEL

### ETF-Specific Scoring Dimensions

```
ETF_RULE_ENGINE_V1 weights (total = 100):
├── TRACKING_QUALITY (25): Tracking error, tracking difference, expense ratio
├── COST_EFFICIENCY (15): Expense ratio, TER vs peers, load fees
├── LIQUIDITY (15): Daily volume, bid-ask spread, premium/discount stability
├── FUND_SCALE (10): AUM size, assets under management scale
├── RISK (15): Volatility, max drawdown, concentration, duration (debt)
├── PERFORMANCE (15): 1Y/3Y/5Y returns, category relative performance
├── UNDERLYING_QUALITY (10): Index quality, factor exposure, holdings concentration
└── VALUATION (10): Premium/discount, NAV price alignment
```

### ETF Eligibility Definitions

**ETF is:**
- **eligible for analysis**: Has valid `EtfResearchProfile`, NAV available, AUM > threshold (e.g., ₹10cr)
- **scorable**: All applicable mandatory requirements have READY status
- **rank eligible**: FULLY_ANALYZED or PARTIAL with minimum safe gate

### ETF-Specific Rules by Type

| Rule | Equity Index | Sector Thematic | Debt | Gold | International |
|------|--------------|-----------------|------|------|---------------|
| Expense Ratio Weight | 15% | 15% | 15% | 15% | 15% |
| Tracking Error Weight | 25% | 15% | 15% | NOT APPLICABLE | 15% |
| Duration Weight | NOT APPLICABLE | NOT APPLICABLE | 20% | NOT APPLICABLE | 10% |
| Currency Hedge Weight | NOT APPLICABLE | NOT APPLICABLE | 10% | NOT APPLICABLE | 20% |
| Gold Content Weight | NOT APPLICABLE | NOT APPLICABLE | NOT APPLICABLE | 30% | NOT APPLICABLE |

**No positive points for NOT_APPLICABLE metrics.** Denominator excludes non-applicable rules.

---

## 7. ETF OPPORTUNITY PIPELINE

### Recommendation: Option A - Shared Pipeline with Type Dispatch

**Prefer Option A** for maximum reuse.

#### Reasoning

1. **Existing infrastructure is robust**: Global opportunity cycle, shortlisting, ranking already handle instrument diversity
2. **PostgreSQL schema patterns are consistent**: Same tables, different data types
3. **API endpoints are unified**: Single `/opportunities` endpoint works for both
4. **Portfolio ownership is already separate**: Portfolio context doesn't assume stock semantics

#### Implementation

```python
# In global_opportunity_cycle.py
async def process_instrument(instrument_id: UUID):
    profile = await load_profile(instrument_id)
    instrument_type = profile.get_instrument_type()
    
    if instrument_type == "ETF":
        return await dispatch_to_etf_pipeline(instrument_id)
    else:
        return await dispatch_to_stock_pipeline(instrument_id)

# ETF-specific handlers follow existing StockRuleEngine patterns
class EtfRuleEngineService:
    def __init__(self, etf_repository, etf_readiness_adapter):
        self.repository = etf_repository
        self.readiness = etf_readiness_adapter
        self.engine = EtfRuleEngineV1()
```

#### Benefits of Shared Pipeline

- Single configuration surface for operators
- Consistent observability (metrics, logging, tracing)
- Unified reporting and backtesting
- Same portfolio ownership isolation

#### Required Changes

1. Add `instrument_type` field to `global_opportunity_snapshot`
2. Add ETF-specific profile loading in `entity_resolution.py`
3. Create `etf_rule_engine.py` parallel to `stock_rule_engine.py`
4. Extend `research_applicability.py` for ETF classification
5. Create ETF-specific readiness requirements

---

## 8. PERSISTENCE GAP ANALYSIS

### Current Tables Usable for ETFs

| Table | ETF-Compatible | Modifications Needed |
|-------|----------------|----------------------|
| `global_opportunity_snapshot` | ✅ | Add `instrument_type`, `etf_type`, `asset_class` |
| `global_financial_facts` | ⚠️ Partial | Add ETF metrics (NAV, AUM, TER) |
| `structured_market_snapshots` | ✅ | All fields reusable |
| `market_price_observations` | ✅ | All fields reusable |
| `daily_market_bars` | ✅ | All fields reusable |
| `global_readiness` | ⚠️ Partial | Separate ETF requirement definitions |
| `global_opportunity_cycle_runs` | ✅ | Add `etf_type` filter |

### New Tables Required

| Table | Purpose | Fields |
|-------|---------|--------|
| `global_etf_profiles` | ETF identity and classification | `instrument_id`, `etf_type`, `asset_class`, `benchmark_index`, `fund_provider`, `isin`, `replication_method`, `fund_structure` |
| `global_etf_facts` | ETF-specific metrics | `instrument_id`, `metric` (nav, aum, ter, tracking_error...), `value`, `unit`, `as_of`, `period_end` |
| `global_etf_holdings` | Underlying securities | `snapshot_id`, `security_id`, `weight`, `weight_type`, `as_of` |
| `global_etf_nav_observations` | Daily NAV history | `instrument_id`, `nav_date`, `nav`, `currency`, `source` |

### Schema Migration Strategy

**Do NOT create migrations yet.** Document required changes:

```sql
-- Add ETF classification to opportunity snapshot
ALTER TABLE research.global_opportunity_snapshot 
ADD COLUMN instrument_type VARCHAR(20),
ADD COLUMN etf_type VARCHAR(50),
ADD COLUMN asset_class VARCHAR(30),
ADD COLUMN benchmark_index VARCHAR(100);

-- Create ETF facts table
CREATE TABLE research.global_etf_facts (
    instrument_id UUID REFERENCES global_opportunity_snapshot(global_instrument_id),
    metric VARCHAR(100),
    value NUMERIC(38,12),
    unit VARCHAR(20),
    as_of TIMESTAMP,
    period_end DATE,
    source_name VARCHAR(100),
    source_identity VARCHAR(500),
    evidence_level VARCHAR(50)
);

-- Create ETF NAV observations
CREATE TABLE research.global_etf_nav_observations (
    instrument_id UUID,
    nav_date DATE,
    nav NUMERIC(38,12),
    currency VARCHAR(10),
    source_name VARCHAR(100),
    retrieved_at TIMESTAMP
);
```

---

## 9. FRONTEND GAP ANALYSIS

### Required ETF Research View/Drawer/Table Fields

| Field | Source | Notes |
|-------|--------|-------|
| ETF Name | `global_etf_profiles.fund_name` | Primary display |
| Symbol/Ticker | profile | Exchange symbol |
| Category/Type | `etf_type` | Index / Thematic / Smart Beta / Debt / Gold |
| Benchmark | `benchmark_index` | Underlying index name |
| Market Price | `structured_market_snapshots` | Latest close |
| NAV | `global_etf_nav_observations` | Latest declared |
| Premium/Discount | Calculated | (Price - NAV) / NAV |
| AUM | `global_etf_facts` | Assets under management |
| Expense Ratio | `global_etf_facts` | TER % |
| Tracking Error | `global_etf_facts` | Annualized % |
| Liquidity | Market data | Avg daily volume, bid-ask spread |
| Fund Age | `global_etf_profiles` | Inception date |
| Top Holdings | `global_etf_holdings` | List of top 10 |
| Concentration | Calculated | Herfindahl index of holdings |
| Historical Returns | Market data | 1Y/3Y/5Y/ALL cumulative |
| Risk | Calculated | Volatility, max drawdown |
| ETF Score/Rank | `etf_rule_engine_result` | Aggregated score |
| Evidence Freshness | Readiness state | Last update timestamps |

### Minimal Viable Frontend Features

1. **ETF List View**: Name, ticker, type, current price, AUM, TER, score
2. **ETF Detail Page**: Full research view with all metrics above
3. **Comparison Table**: Multi-ETF side-by-side comparison
4. **Evidence Panel**: View sources, document links, freshness

**Do NOT implement in this phase.** Design is complete.

---

## 10. IMPLEMENTATION PLAN

### ETF-1: Foundation - Classification + Applicability

| Objective | Create ETF subtype model, classification logic, and applicability contract |
|-----------|----------------------------------------------------------|
| Components | - `app/models.py`: `EtfType` enum, extend `EtfResearchProfile`<br>- `app/research_applicability.py`: `classify_etf_applicability()`<br>- `app/research_readiness.py`: ETF requirement registry |
| Tests | Unit tests for classification functions<br>Unit tests for applicability logic |
| Acceptance | `classify_etf_type()` correctly identifies fund categories<br>`classify_etf_applicability()` returns correct NOT_APPLICABLE for stock-only concepts |
| Dependencies | None |

### ETF-2: Persistence + Authoritative Acquisition

| Objective | Create ETF-specific database tables and acquisition logic |
|-----------|-----------------------------------------------------|
| Components | - PostgreSQL schema for ETF tables<br>- `app/etf_facts.py`: NAV, AUM, TER acquisition<br>- `app/etf_holdings.py`: Holdings parsing from NSE<br>- `app/etf_collection.py`: Provider adapters |
| Tests | Integration tests with NSE ETF data<br>Database persistence tests |
| Acceptance | NAV acquired from NSE daily disclosures<br>AUM stored in new tables<br>Holdings parsed from NSE XML |
| Dependencies | ETF-1 |

### ETF-3: Readiness

| Objective | Implement ETF-specific readiness assessment |
|-----------|-----------------------------------------|
| Components | - `app/research_readiness.py`: ETF requirement registry<br>- `app/etf_readiness.py`: Readiness calculator<br>- Evidence freshness policies for ETF metrics |
| Tests | Readiness state transition tests<br>Freshness policy tests |
| Acceptance | `is_fully_analyzed()` returns true when all mandatory ETF requirements READY<br>`data_genuinely_unavailable` correctly identifies missing fund data |
| Dependencies | ETF-1, ETF-2 |

### ETF-4: Rule Engine

| Objective | Create ETF_RULE_ENGINE_V1 with ETF-specific scoring |
|-----------|-----------------------------------------------|
| Components | - `app/etf_rule_engine.py`: EtfRuleEngineV1<br>- ETF scoring dimensions and rules<br>- Input adapter for ETF data |
| Tests | Score calculation unit tests<br>ETF rule engine evaluation tests |
| Acceptance | `EtfRuleEngineV1.evaluate()` returns valid score by ETF type<br>Not applicable rules excluded from denominator |
| Dependencies | ETF-1, ETF-3 |

### ETF-5: Opportunity Integration + Ranking

| Objective | Integrate ETFs into global opportunity pipeline |
|-----------|-----------------------------------------------|
| Components | - `app/global_opportunity_cycle.py`: ETF dispatch<br>- `app/etf_opportunity_ranker.py`: ETF ranking<br>- Cycle configurations for ETF universe |
| Tests | End-to-end cycle tests with ETFs<br>Ranking order verification |
| Acceptance | ETFs cycle through `investment` table<br>Ranked results include ETFs with scores |
| Dependencies | ETF-1, ETF-2, ETF-3, ETF-4 |

### ETF-6: API + Frontend

| Objective | Expose ETF research via API and UI |
|-----------|--------------------------------------------------|
| Components | - API Gateway: `/api/v1/opportunities/etfs` endpoints<br>- Frontend: ETF research views<br>- OpenAPI spec updates |
| Tests | API contract tests<br>Frontend integration tests |
| Acceptance | `/api/v1/opportunities/etfs/{ticker}` returns full research<br>Frontend displays ETF score and metrics |
| Dependencies | ETF-5 |

### ETF-7: Controlled Validation

| Objective | Validate in controlled environment |
|-----------|--------------------------------------------------|
| Components | - Controlled cycle with 3-5 ETFs<br>- Evidence completeness check<br>- Score reasonableness validation |
| Tests | End-to-end validation run<br>Evidence provenance audit |
| Acceptance | All 10 ETFs produce READY/FULLY_ANALYZED<br>Scores align with manual analysis |
| Dependencies | ETF-5, ETF-6 |

### ETF-8: Full NSE ETF Universe Validation

| Objective | Validate against full NSE ETF universe |
|-----------|--------------------------------------------------|
| Components | - NSE ETF discovery integration<br>- Full universe cycle run<br>- Performance benchmarking |
| Tests | Bulk cycle completion<br>Performance metrics |
| Acceptance | All NSE ETFs (>= 50) processed successfully<br>Sub-100ms per ETF processing time |
| Dependencies | ETF-7 |

---

## 11. RISKS AND OPEN QUESTIONS

### Technical Risks

1. **Fund NAV availability**: Some ETFs may not publish daily NAV officially
   - Mitigation: Use market price as proxy, flag as estimated
   
2. **Tracking error calculation cost**: Computing daily requires holdings data
   - Mitigation: Sample daily calculation, annual reconciliation
   
3. **Holdings data granularity**: NSE may delay holdings by a day
   - Mitigation: Accept settlement date, clear in UI

### Product Decisions Required

1. **Minimum AUM threshold**: What size ETFs qualify for analysis?
   - Recommendation: ₹10 crore minimum (below is illiquid)

2. **ETFs vs Mutual Funds**: Should mutual funds be included?
   - Decision: Separate treatment; MFs have different regulations

3. **Regional ETFs**: Should international ETFs (foreign) be included?
   - Decision: Yes, but separate source providers (Bloomberg, etc.)

4. **Closed-ended ETFs**: Have different structure, may not have daily liquidity
   - Decision: Include with special handling for illiquid CEDs

5. **Benchmark index granularity**: How detailed should index breakdown be?
   - Decision: Start with top-level index, optional sector breakdown

### Data Availability Questions

1. **NSE ETF Holdings Format**: Confirm XML structure matches extraction patterns
2. **Premium/Discount Calculation**: Confirm source of accurate NAV timestamps
3. **Foreign ETF Data**: Identify authoritative source for international funds

---

## FINAL REPORT

### 1. Current ETF Support Inventory

- **A) Already ETF-aware**: `EtfResearchProfile` model, basic asset_type check
- **B) Stock-only but reusable**: Persistence patterns, market data models
- **C) Stock-only and must NOT be reused**: Company-specific concepts (promoter, ROCE, etc.)
- **D) Missing**: Classification, ETF-specific metrics, acquisition, readiness, rule engine

### 2. ETF Architecture Recommendation

**Option A: Shared Pipeline with Type Dispatch** (recommended)

Rationale: Maximum reuse of existing infrastructure while allowing ETF-specific behavior through dispatch.

### 3. ETF Subtype Model

```python
class EtfType(StrEnum):
    EQUITY_INDEX, EQUITY_SECTOR_THEMATIC, EQUITY_SMART_BETA,
    DEBT, GOLD, SILVER, INTERNATIONAL_EQUITY, INTERNATIONAL_DEBT,
    MONEY_MARKET, HYBRID, OTHER
```

### 4. ETF Research Requirement Matrix

See Section 3 - comprehensive matrix with applicability, cadence, and source mapping.

### 5. Provider/Source Capability Matrix

See Section 4 - NSE fully supports ETF data; AMCs provide fund facts; Yahoo supports market data.

### 6. ETF Readiness Contract

Three states: FULLY_ANALYZED, DATA_GENUINELY_UNAVAILABLE, TECHNICAL_FAILURE/REFRESH_REQUIRED. NOT_APPLICABLE for stock-only concepts.

### 7. ETF_RULE_ENGINE_V1 Proposed Model

10 scoring dimensions (25% Tracking Quality, 15% Cost Efficiency, etc.) with type-specific weight adjustments.

### 8. Persistence Gaps

- `global_etf_profiles` table needed
- `global_etf_facts` table for NAV, AUM, TER
- `global_etf_holdings` table for underlying securities
- Extension to `global_opportunity_snapshot` for ETF metadata

### 9. Frontend Gaps

ETF list view, detail page, comparison table, evidence panel - all detailed in Section 9.

### 10. Implementation Plan

8 phased plan (ETF-1 through ETF-8) with dependencies, tests, and acceptance criteria.

### 11. Risks/Open Questions

- NAV availability for some ETFs
- Minimum AUM threshold (recommend ₹10cr)
- International ETF data sources
- Closed-ended ETF treatment

---

**STOP.** No code changes, no deployment, no cycles, no modifications to stock pipeline.
