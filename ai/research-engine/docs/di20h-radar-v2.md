# DI-20H Radar V2

## Proven prior flow

| Responsibility | Implementation |
| --- | --- |
| Canonical active equities | `global_scanner.CanonicalEquityUniverse.active_global_equities`; `global_opportunity_cycle.nse_equities` restricts the cycle to canonical NSE equities. |
| Cheap baseline | `GlobalOpportunityOrchestrator._acquire_baseline_requirements`, eight bounded workers, `BASELINE_REQUIREMENT_IDS`; then `GlobalScanner.scan`, one instrument per evidence batch. |
| Preliminary selection | `GlobalPreScore.score`; `_form_dynamic_deep_pool`; previously the first `shortlist_limit` entries of that pool were the only discovery candidates. |
| Deep requirements | `ResearchRequirementRegistry.default`, `ResearchRefreshPlanner.plan`, `ResearchReadinessRuntime.ensure`. Previously deep ensure received `requirement_ids=None` and included optional catalyst and shareholding requirements. Automatic CURRENT_NEWS is already excluded by the existing planner. |
| Broad category work | `ExistingResearchCapabilityExecutor.execute_primary` unions financial, governance and business-event categories. `_refresh_live` / `_refresh_targeted` combine registered sources, official filings and search. `OfficialFilingDiscovery.discover` additionally admits recognized subtypes outside requested categories. |
| Durable evidence and applicability | `RepositoryResearchReadinessAdapter.load_by_global_instrument_id`, `ResearchReadinessService.assess`, `research_applicability.classify_requirements`. Canonical sector/industry is preferred; durable structured industry supplies classification when canonical industry is absent. Unknown classification never means NOT_APPLICABLE. |
| Authoritative financial route | `McpFirstResearchCapabilityExecutor.execute_primary` and the existing NSE authority-upgrade path; repository official fetch/ingestion invokes DI-20C/D/E projection/reconciliation. Shareholding has its separate `OfficialNseShareholdingDiscovery` / XBRL enrichment path. |
| Rules | `StockRuleEngineEligibilityPolicy.evaluate`, `StockRuleEngineService.analyze`, `StockRuleEngineV1.evaluate`. Mandatory requirements cover valuation, business quality, growth, balance sheet, quarterly financials, price/history, governance and sector. Catalyst, shareholding and current news are optional. |
| Ranking and horizons | `GlobalOpportunityRanker`, then `RecommendationEngineV1.evaluate`: short/long recommendations consume the shared V1 analysis and technical evidence. There are no independently eligible short/long rule engines to enable safely in this change. |
| Publication/history | `run_global_opportunity_cycle`, `prepare_cycle`, `OpportunityPersistenceMixin.publish_opportunity_cycle`; existing recommendation fingerprints, lifecycle and current-state logic remain in place. |

## New discovery boundary

Production acquisition-enabled `run_global_opportunity_cycle` explicitly enables
`discovery_v2`. Direct legacy/read-only orchestrator calls retain their previous
default and can opt in with `discovery_v2=True`.

`opportunity_discovery` keeps at most `shortlist_limit` compact nominations per
path. MARKET_TECHNICAL reuses the existing preliminary pool and scores;
FUNDAMENTAL_CHANGE uses available durable revenue/earnings growth dimensions;
EVENT_CATALYST uses dated, real exchange events within the existing catalyst
freshness window. No provider runs during nomination. An event read failure is
isolated and cannot remove market or rotation nominations.

The union reserves `max(1, shortlist_limit // 4)` places for deterministic UUID
rotation and allocates other places round-robin across the first three paths.
Rotation also fills unused capacity. Stocks already selected need no duplicate
exploration slot. All reasons are recomputed for selected stocks so top-K path
truncation cannot hide another nomination reason. No market-cap/liquidity filter
is used. Trusted canonical acquisition identity remains mandatory.

The last traversed UUID is included in the existing atomic selection payload.
Failed and controlled publications do not advance shared exploration. Cursor
reads use SQL LIMIT 1, including on PostgreSQL's buffered cursor. Stable eligible
universes receive eventual coverage across completed cycles; active recommendation
reviews continue through their existing separate lifecycle path.

## Investigation boundary

`DeepInvestigationPlan` records nomination paths, classification provenance,
the existing V1 family, required/optional evidence, applicable exclusions,
fresh evidence and acquisition needs. Missing optional research is not requested
by default. A catalyst nomination can request its applicable catalyst requirement.
Existing business classification is reused, including bank/insurer order-book
exclusion; software is not assigned a new blanket exemption.

Each requirement gets one owned `ensure` call with an explicit singleton ID.
Fresh evidence and NOT_APPLICABLE evidence are reused. Quarterly ensure still
consults the existing NSE authority-upgrade decision even when secondary facts
are fresh. This does not change provider precedence or fallback policy.

A task-local `RequirementAcquisitionBudget` follows owned work through existing
executor APIs without changing unrelated baseline/interactive requests:

- Four document attempts maximum, including failures and reused documents that
  need reconciliation; existing stricter repository caps still apply.
- Two search queries maximum across that requirement's categories; existing
  provider query generation receives the remaining cap.
- Financial result publication lookback of 800 days permits prior-year quarter
  comparatives and annual history. Other requirements use their existing
  freshness/scoring windows. A requirement with no time window retains history.
- Financial/ownership work uses official discovery rather than registered-source
  sweeps or presentation/search substitutes. Scoped financial discovery excludes
  presentations, investor releases and conference material, even when titles
  mention financial results. Existing DI-19 newest-result ordering is retained.
- Before another document/query, check committed evidence for sufficiency.
  `evidence_only=True` removes only the transient REFRESHING overlay from a
  copied snapshot; it retains all source, input, freshness and conflict gates.
  It never clears the owner's lifecycle state and is not used for rule admission.
- Unresolved work records a durable acquisition observation and a compact failure
  reason. PDF and network timeouts remain distinct. Exhausted work stays unready.

After owned completion, normal durable readiness is read again. The unchanged
V1 gate decides whether rules run. Technical enrichment is refreshed for admitted
candidates before ranking. A bounded per-stock matrix records required/optional
evidence, exclusions, missing inputs, source, failure, readiness, evaluation and
suppression. Atomic publication stores that matrix and the nomination reasons.

## Preserved behavior and limits

Baseline worker count, DI-20F streaming, sequential deep processing, PDF permits,
DI-20G ownership/shielding and DISCARDED late results are unchanged. Network/PDF/
interactive timeout values are unchanged. Financial projection, fact identity,
signs, authority, reconciliation, ranking formulas, thresholds and top_n are
unchanged. Empty Radar remains valid.

Bounds limit attempts rather than guarantee readiness. Financial layouts or
mandatory governance evidence may remain unresolved. Existing repository caches
and provider-internal retry limits are not redesigned. A flight already owned by
another caller retains that owner's policy; a follower cannot cancel or rewrite
its budget. Rotation assumes serialized cycle ownership as provided by the
existing cycle worker; this is not a new distributed lock. Separate horizon
eligibility would need a separately specified rule contract.

Validation is offline with synthetic providers and in-memory SQLite. No live
provider, deployment, infrastructure or production/dev database operation is
part of this change. A later explicitly authorized controlled run should inspect
nomination diversity, documents/queries per requirement, authority provenance,
durable readiness transitions and published rotation progression before another
full-universe run.

## Focused validation commands

Run from `ai/research-engine`. All provider traffic in these tests is mocked.

```powershell
python -m pytest -q -p no:cacheprovider tests/test_di20h_discovery_investigation.py tests/test_di20f_stage2_memory.py tests/test_di20g_acquisition_lifecycle.py tests/test_research_readiness_runtime.py tests/test_readiness_applicability_audit.py tests/test_global_opportunity_baseline.py tests/test_global_opportunity_acquisition.py tests/test_global_opportunity_orchestration.py tests/test_global_opportunity_empty_universe.py tests/test_recommendation_lifecycle.py tests/test_opportunity_cycle_identity.py tests/test_global_opportunity_public_actions.py
python -m pytest -q -p no:cacheprovider tests/test_financial_structure.py tests/test_di20d_semantic_extraction.py tests/test_di20e_financial_projection.py tests/test_official_nse_financial_parsing.py tests/test_di19_financial_candidate_order.py tests/test_source_discovery.py tests/test_di11c_official_document_budget.py tests/test_research_readiness.py
python -m pytest -q -p no:cacheprovider tests/test_di20h_discovery_investigation.py
git diff --check
```
