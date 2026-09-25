# DI-18R: actual StockRuleEngineV1 input audit

Scope: persisted-input interpretation only. No provider acquisition, UI population,
identity changes, thresholds, or live NCC query. NCC (b2652ca8-1635-4e6b-aa9c-e675caafbbb4,
INE868B01028) is a subsequent runtime validation subject, not a code exception.

## Actual path

`StockRuleEngineService` -> `StockRuleEngineInputAdapter.load` -> repository
`financial_facts_for_instruments`, `structured_market_snapshots_for_instruments`,
`market_price_observations_for_instruments`, `events_for`, `shareholding_for`,
`news_records_for` -> existing persistence loaders -> `StockRuleEngineInput` ->
`StockRuleEngineV1.evaluate` and the eleven area evaluators below.

MarketFundamentals is NOT an input to this engine. Its DI-18 OCF mapping in
portfolio_orchestration is independent and unchanged. FinancialFact selection
uses the existing `fact_source_authority`; there is no new provider precedence model.
`financial_statement_history_from_facts` is a presentation projection, not an
additional mandatory hop for the rule engine's existing fact-series path.

## Matrix conventions

Every listed scoring metric is optional at area aggregation level. `R` means
required to compute that particular subrule, not globally mandatory. `O` means
an optional alternative or modifier. Missing operands omit the subrule, never
produce numeric zero. No available metrics means UNSCORABLE; readiness gaps
remain visible, with existing eligibility/confidence semantics unchanged.

Objects: F = persisted FinancialFact; S = StructuredMarketSnapshotRecord.facts;
P = MarketPriceObservation; E = ResearchEvent; H = ShareholdingSnapshot;
N = persisted news features/search run; C = canonical metadata/profile;
Rdy = ResearchReadinessResult. These are members of StockRuleEngineInput.

Paths below are functions in app/stock_rule_engine.py unless named otherwise.
"Fixed" describes the DI-18R delta; "No" means no proven input-mapping gap.

| RULE | INPUT FIELD | REQUIRED / OPTIONAL | SOURCE OBJECT | CURRENT VALUE PATH | CAN USE PERSISTED FINANCIAL FACTS? | CURRENT GAP? | EFFECT WHEN MISSING |
|---|---|---|---|---|---|---|---|
| TRAILING_PE_V1 | trailingPE/pe/priceToEarnings; compatible price + trailingEps alternative | R; O derivation | S/P | _valuation -> _structured_data; materialize_valuation | No direct F path; raw quarterly EPS is not proven TTM | No safe new TTM definition | Omit |
| EARNINGS_YIELD_V1 | positive trailing PE | R | S/P | _valuation: 100/PE | As above | No | Omit |
| FORWARD_PE_V1 | forwardPE | R | S | _valuation -> _pick | No expected-earnings F contract | No | Omit |
| PRICE_TO_BOOK_GENERAL/FINANCIAL_V1 | priceToBook/pb; compatible price + bookValue alternative; sector | R; O derivation | S/P/C | _valuation; materialize_valuation; _is_financial | No direct F path | No safe per-share derivation from totals alone | Omit |
| EV_EBITDA_NON_FINANCIAL_V1 | evToEbitda/enterpriseValueToEbitda; nonfinancial classification | R | S/C | _valuation | No direct F path | No safe new enterprise-value contract | Omit; financial businesses excluded |
| PEG_V1 | pegRatio/peg | R | S | _valuation | No expected-growth F contract | No derivation added | Omit |
| FCF_YIELD_V1 | annual/TTM free_cash_flow or reported freeCashFlow; marketCap; matching currency/unit | R | F/S | _valuation -> _financial_or_structured | Yes, explicit FCF only | Fixed summary masking of authoritative annual FCF | Omit |
| ROE_V1 | reported roe/return_on_equity/returnOnEquity | R | F/S | _quality -> _financial_or_structured | Yes, explicit ratio | Fixed summary masking; no denominator derivation definition | Omit |
| ROCE_NON_FINANCIAL_V1 | reported roce/return_on_capital_employed; sector | R | F/S/C | _quality -> _financial_or_structured | Yes, explicit ratio | Fixed summary masking | Omit; financial businesses excluded |
| OPERATING_MARGIN_V1 | operatingMargin/operating_margin | R | F/S | _quality -> _financial_or_structured | Yes | Fixed summary masking and EBITDA-margin substitution | Omit |
| NET_MARGIN_V1 | profitMargin/netMargin/profit_margin/net_margin | R | F/S | _quality -> _financial_or_structured | Yes | Fixed summary masking | Omit |
| MARGIN_STABILITY_STDDEV_V1 | >=3 aligned annual PAT and revenue observations | R | F | _quality -> _fact_series -> _aligned_ratios | Yes | No; compatibility already checked | Omit |
| OPERATING_CASH_TO_PAT_V1 | annual operating_cash_flow/cash_flow_from_operating_activities; nonzero annual PAT | R | F | _quality -> _latest_fact -> _compatible_financial_data | Yes, directly | No OCF mapping gap in engine | Omit |
| FCF_TO_PAT_V1 | explicit annual FCF; compatible nonzero annual PAT | R | F/S | _quality -> _financial_or_structured -> compatibility | Yes | Fixed summary masking | Omit; never derive from investing cash flow |
| POSITIVE_PAT_PERIOD_SHARE_V1 | >=3 annual PAT/net_income/net_profit observations | R | F | _quality -> _fact_series | Yes | No | Omit |
| REVENUE_CAGR_MULTI_YEAR_V1 | >=3 annual revenue/total_revenue observations; positive endpoints | R | F | _growth -> _series_cagr | Yes | No; existing elapsed-year definition | Omit |
| EARNINGS_CAGR_MULTI_YEAR_V1 | annual PAT/net_income/net_profit; EPS alternative if no PAT | R; O alternative | F | _growth -> _series_cagr | Yes | No | Omit |
| REVENUE_YOY_COMPARABLE_QUARTER_V1 | quarterly revenue and prior-year same-quarter revenue | R | F | _growth -> _comparable_yoy | Yes | Tightened old 300-430-day tolerance to same quarter | Omit |
| EARNINGS_YOY_COMPARABLE_QUARTER_V1 | quarterly PAT; EPS alternative; prior-year same quarter | R; O alternative | F | _growth -> _comparable_yoy | Yes | Same-quarter guard; derivation already existed | Omit |
| RECENT_QOQ_LIMITED_WEIGHT_V1 | consecutive available quarterly revenue observations, nonzero prior | R | F | _growth -> _sequential_change | Yes | No new YoY/QoQ interchange | Omit |
| POSITIVE_ANNUAL_CHANGE_SHARE_V1 | annual revenue and/or earnings history | R at least one series | F | _growth -> _growth_consistency | Yes | No | Omit |
| INDUSTRIAL_DEBT_TO_EQUITY_V1 | compatible annual total_debt/debt_or_borrowings + positive equity/total_equity; reported debtToEquity alternative | R; O alternative | F/S | _balance_sheet -> _debt_equity_datum | Yes | Fixed summary masking; reuse source authority | Omit if no safe pair or reported ratio |
| NET_DEBT_TO_EQUITY_V1 | annual debt, cash_and_cash_equivalents/cash_and_equivalents, positive equity, all compatible | R | F | _balance_sheet -> _latest_fact -> compatibility | Yes | Fixed summary debt/cash masking | Omit |
| EBITDA_INTEREST_COVERAGE_V1 | annual EBITDA and positive finance_cost/interest_expense, compatible | R | F | _balance_sheet -> _latest_fact -> compatibility | Yes | Removed EBIT/operating-income substitution | Omit |
| CURRENT_RATIO_NON_FINANCIAL_V1 | annual current_assets and positive current_liabilities, compatible | R | F | _balance_sheet -> _latest_fact -> compatibility | Yes | No | Omit |
| ANNUAL_DEBT_TREND_V1 | >=2 annual total_debt/debt_or_borrowings; positive prior | R | F | _balance_sheet -> _fact_series | Yes | Already mapped; no quarterly debt substitution | Omit |
| FINANCIAL_CAPITAL_ADEQUACY_V1 | capital_adequacy/capital_adequacy_ratio; financial sector | R | F/C | _balance_sheet -> _latest_fact | Yes | No | Omit |
| GROSS_NPA_FINANCIAL_V1 | gross_npa/gross_npa_ratio; financial sector | R | F/C | _balance_sheet -> _latest_fact | Yes | No | Omit |
| NET_NPA_FINANCIAL_V1 | net_npa/net_npa_ratio; financial sector | R | F/C | _balance_sheet -> _latest_fact | Yes | No | Omit |
| COMPARABLE_QUARTER_REVENUE_YOY_V1 | quarterly revenue, prior-year same quarter | R | F | _quarterly -> _comparable_yoy | Yes | Same-quarter guard | Omit |
| COMPARABLE_QUARTER_PAT_YOY_V1 | quarterly PAT/net_income/net_profit, prior-year same quarter | R | F | _quarterly -> _comparable_yoy | Yes | Same-quarter guard | Omit |
| COMPARABLE_QUARTER_EPS_YOY_V1 | quarterly EPS, prior-year same quarter | R | F | _quarterly -> _comparable_yoy | Yes | Same-quarter guard | Omit |
| SEQUENTIAL_REVENUE_QOQ_LIMITED_WEIGHT_V1 | latest two available quarterly revenue observations | R | F | _quarterly -> _sequential_change | Yes | No; explicitly QoQ, not YoY | Omit |
| SEQUENTIAL_PAT_QOQ_LIMITED_WEIGHT_V1 | latest two available quarterly PAT observations | R | F | _quarterly -> _sequential_change | Yes | No | Omit |
| QUARTERLY_MARGIN_TREND_V1 | >=2 aligned quarterly operating_income/operating_profit and revenue observations | R | F | _quarterly -> _aligned_ratios | Yes | Removed EBITDA as operating-profit substitute | Omit |
| POSITIVE_QUARTER_SHARE_V1 | >=3 quarterly PAT observations | R | F | _quarterly -> _fact_series | Yes | No | Omit |
| MATERIAL_CATALYST_EVENT_V1 | actual validated REAL event, supported type, confidence >=.60, A/B reliability, materiality or authoritative class | R | E/Rdy | _catalysts -> _material_catalyst; concept N/A exclusions | No | Actual events already required; fixed missing/stale concept area reporting | Omit; no invented CAPEX/guidance |
| PRICE_VS_50_OBSERVATION_MEDIAN_V1 | >=50 usable persisted prices | R | P | _technical -> _usable_prices | No | No | Area unscorable |
| PRICE_VS_150_OBSERVATION_MEDIAN_V1 | >=150 usable prices | R | P | _technical | No | No | Omit |
| PRICE_DRAWDOWN_V1 | latest and peak in available 50/150 observation window | R | P | _technical | No | No | Omit |
| DURABLE_PRICE_TREND_V1 | latest and start of usable observation window | R | P | _technical | No | No | Omit |
| COMPANY_EXPOSURE_IMPACT_V2 | persisted valid news impact features; search state | R features; O search diagnostic | N | _news -> aggregate_impact/search_state | No | No | Try event fallback; stale search makes partial |
| CURRENT_EVENT_IMPACT_V1 | issuer/proven-exposure event within 30 days, impact, confidence, reliability, horizon, materiality | R | E/C | _news -> _issuer_or_proven_exposure -> _event_metric | No | No | Omit; no negative default |
| PROMOTER_HOLDING_LEVEL_V1 | latest promoter percentage; India applicability | R | H/C | _shareholding | No | No | Omit |
| PROMOTER_PLEDGE_V1 | latest promoter pledge percentage | R | H | _shareholding | No | No | Omit |
| PROMOTER_TREND_QUARTERLY_V1 | current and previous promoter percentages | R | H | _shareholding | No | No | Omit |
| FII_FPI_TREND_QUARTERLY_V1 | current and previous FII/FPI percentages | R | H | _shareholding | No | No | Omit |
| DII_TREND_QUARTERLY_V1 | current and previous DII percentages | R | H | _shareholding | No | No | Omit |
| UNRESOLVED_GOVERNANCE_HISTORY_V1 / RESOLVED_GOVERNANCE_EVIDENCE_V1 | legitimate governance event; resolution/status; impact operands | R | E | _governance -> _governance_event | No | No | Omit; missing is not misconduct |
| PROMOTER_PLEDGE_GOVERNANCE_V1 | latest pledge percentage | R | H | _governance | No | No | Omit |
| CANONICAL_SECTOR_NEUTRAL_BASIS_V1 | canonical sector or existing structured classification fallback | R | C/S | _sector_macro -> _sector | No | No | Omit |
| DURABLE_SECTOR_PERFORMANCE_V1 | sectorPerformance/sectorReturn | R | S | _sector_macro -> _structured_data | No | No | Omit |
| PROVEN_MACRO_EXPOSURE_IMPACT_V1 | macro event plus matching sector exposure and impact operands | R | E/C | _sector_macro -> _proven_macro_exposure | No | No | Omit |
| VALIDATED_news-type override | severe_validated feature, live impact | R | N | _risk_overrides | No | No | No override |
| CONFIRMED_FRAUD_OR_ACCOUNTING_CRISIS | authoritative unresolved strong-negative fraud/accounting event | R | E | _risk_overrides | No | No | No override |
| CRITICAL_REGULATORY_ACTION | authoritative unresolved strong-negative regulatory event and critical terms | R | E | _risk_overrides | No | No | No override |
| SEVERE_UNRESOLVED_GOVERNANCE | authoritative unresolved strong-negative auditor management event | R | E | _risk_overrides | No | No | No override |
| EXTREME_BALANCE_SHEET_STRESS | safe debt/equity plus EBITDA/interest, official operands, nonfinancial sector | R | F/S/C | _extreme_balance_sheet_evidence -> _debt_equity_datum | Yes | Same debt fix; weakest-operand authority; no EBIT substitution | No override |
| BROKEN_INVESTMENT_THESIS | >=2 independent authoritative negative guidance/cancellation/delay events | R | E | _risk_overrides | No | No | No override |
| Full/partial eligibility | mandatory and critical requirement statuses, critical completeness, latest price, conflicts | R | Rdy | StockRuleEngineEligibilityPolicy.evaluate | Facts feed readiness upstream, not synthetic metrics | No threshold change | Existing block/partial/HOLD policy |
| Confidence / area status | applicability, coverage, source tier, freshness, conflicts, evidence IDs, concept states | R | Rdy | _confidence / _finish | Indirect | Fixed concept-level missing/stale reporting | Existing confidence semantics; no fabricated score |

## DI-18 A-F disposition

- A OCF: consumed directly from annual facts. Existing summary patch preserved;
  no duplicate summary backfill added.
- B ROE: reported ratio consumed. Prefer authoritative persisted ratio. No safe
  derivation from PAT/equity added: V1 does not define average versus closing
  equity, and the available fields do not settle that domain decision.
- C ROA: not consumed. No implementation.
- D/E revenue/PAT YoY: already derived from quarterly fact histories. No new
  MarketFundamentals fields. Prior quarter cannot become YoY; same prior-year
  calendar quarter and matching basis/type/unit are required. Existing nonzero
  prior and absolute-prior arithmetic are retained, not redefined.
- F total debt: annual aliases already supported, but summary-first selection
  masked their use. Fixed only rule-input selection and leverage reuse.

Explicit annual/quarterly/basis/unit partitions are retained. UNKNOWN never
equals CONSOLIDATED/STANDALONE. Different normalized currencies do not share a
fact selection key. Derived authority cannot exceed the weakest operand.
No FCF-from-investing-flow, EBITDA-from-EBIT, operating-profit-from-EBITDA,
PEG expected-growth fabrication, or average-denominator substitution is added.

This is a code/fixture audit, not a claim about NCC's current database values or
NSE SUCCESS_EMPTY cause. Runtime evidence must establish those separately.

## Offline validation

Working directory: `ai/research-engine`. The existing temporary runner blocks
external socket connections; all provider/persistence integration uses fixtures
and temporary SQLite. Counts overlap and must not be summed.

Initial new regressions: 23 passed. The new module ultimately contains 28 cases.

Affected group (before updating the leverage fixture): **152 passed, 3 failed**.
One task-related expectation relied on Yahoo overriding unchanged official debt;
the fixture now supplies genuinely stressed official debt. The other two are the
known pre-existing API fixture `reason_out` signature failures and were untouched.

```powershell
.\.venv-validation\Scripts\python.exe "$env:TEMP/nse-foundation-baseline/pytest_offline.py" tests/test_di18r_rule_inputs.py tests/test_stock_rule_engine.py tests/test_di18_financial_readiness_hardening.py tests/test_nse_data_quality_foundation.py tests/test_di15_financial_authority_upgrade.py tests/test_financial_facts_read_boundary.py -q --tb=short
```

Correction check: **53 passed, 2 known failures deselected**.

```powershell
.\.venv-validation\Scripts\python.exe "$env:TEMP/nse-foundation-baseline/pytest_offline.py" tests/test_di18r_rule_inputs.py tests/test_stock_rule_engine.py -k "not test_analysis_api_accepts_global_identity_and_performs_no_provider_or_private_mutation and not test_held_and_non_held_callers_receive_same_global_company_score" -q --tb=short
```

Final changed modules plus DI-15B production composition: **66 passed, 2 known
failures deselected**; no outstanding task-caused test failures.

```powershell
.\.venv-validation\Scripts\python.exe "$env:TEMP/nse-foundation-baseline/pytest_offline.py" tests/test_di18r_rule_inputs.py tests/test_stock_rule_engine.py tests/test_di15b_financial_upgrade_execution.py -k "not test_analysis_api_accepts_global_identity_and_performs_no_provider_or_private_mutation and not test_held_and_non_held_callers_receive_same_global_company_score" -q --tb=short
```

Scoped `git diff --check` passed (Windows line-ending notice only).
