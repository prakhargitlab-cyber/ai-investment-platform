# Business exposure and news intelligence V2

## Acquisition and scoring boundaries

`ResearchRepository.refresh_news_intelligence(globalInstrumentId)` is an explicit
worker operation used by the existing CURRENT_NEWS ensure capability. It uses
the existing search adapters, safe document fetcher, persistence connection and
request-spacing setting. Scanner/ranker/readiness reads do not acquire evidence.
No new GET endpoint or background all-universe refresh is introduced.

The worker serializes requests per repository, bounds providers to three,
queries to twenty (the existing configured budget defaults to six), and fetched
documents to twenty (the existing configured document budget applies). Query
selection interleaves company, company-event, exposure and sector tiers. The
default profile query plan is limited to 3/4/3/4 queries and three sufficiently
confident MEDIUM/HIGH exposures. An empty response describes the configured
search scope, never the whole internet. Search snippets are discovery only.

Explicit fresh Yahoo MCP acquisition observations are supplemental company-news
checks. They never replace web/exposure queries. No Yahoo call is made to reuse
these observations. An empty general-purpose quote snapshot is not evidence of
a successful news search. Other search adapters can be injected into the same
bounded worker. Disabled search is a failure; Searxng unresponsive engines make
coverage partial. Fetch failure or document-budget truncation also prevents a
complete/no-events result. Exceptions become bounded failure codes.

## Evidence-derived profiles

`CompanyBusinessExposureProfile` uses canonical instrument identity and immutable
versioned snapshots (`BUSINESS_EXPOSURE_V1`). Official persisted report/company
text has priority over verified Yahoo/NSE/BSE `businessSummary` facts. Canonical
or structured industry metadata can supply query context; it does not establish
a raw-material relationship. No company-to-commodity lookup is present.

The extractor requires an explicit uses/consumes/raw-material/produces/sells
relationship in source text. Unsupported or negated relationships are excluded.
Equal-confidence contradictory input/producer relationships have no assumed
price direction. Importance is HIGH only with explicit importance wording.
Each exposure retains source references and source-capped confidence. Explicit
labelled lists populate products, segments, drivers, geography, customers,
competitors and risks; unavailable fields remain empty.

Exposures share a typed list (`exposure_type` distinguishes raw material,
commodity, energy, currency, interest rate, demand, regulation, geography,
competition and supply chain). `app/config/exposure_ontology_v1.json` defines
normalized keys and aliases. Adding ontology entries does not require rewriting
the extractor. `source_reliability_v1.json` versions classification confidence
and optional domain overrides. Publisher reliability is separate from relevance;
OTHER sources can supply lower-confidence candidate impacts.

## Events, impact and revisions

`NEWS_IMPACT_V2` features retain source-document/event identifiers and
the exact profile snapshot ID. Direct-company actions require a company mention
in the action sentence; another company's action elsewhere is not attributed.
Verified exposure matches can establish SECTOR_EXPOSURE relevance without a
company headline mention. They do not assert that the article named the company.

Impact is `100 × direction × magnitude × company relevance × source confidence
× event confidence × freshness decay`, bounded to -100..100. The initial
conservative magnitude is 0.5, not an estimated earnings effect. Direct-event
confidence is 0.9; exposure-event confidence is 0.75, with relevance capped at
0.8. Linear decay uses each event's public date and validity boundary. Multiple
events combine by the deterministic mean of active latest-known revisions, so
mitigation does not overwrite an earlier adverse event.

Input costs and supply disruption use 21-day windows; demand/orders/competition
use 30 days; guidance/capacity use 90 days. Short, medium and long horizon flags
are stored separately. Validated direct severe official/regulatory/company
events can remain unresolved with no expiry. A correction must append a new
revision of its logical event; only the latest revision known at evaluation is
used, including for severe overrides. There is no automatic fuzzy linking of
unrelated resolution articles to a severe event.

Publication, public availability, discovery and computation timestamps are
distinct. Missing publication time uses discovery conservatively. Feature public
availability cannot precede profile evidence availability. Historical reads
require computation, discovery and public availability at/before the requested
cutoff; later revisions cannot leak into earlier output. Training consumers must
retain these availability cutoffs, not use a backfill's database insertion date
as the original publication date. No prediction model is implemented.

## Readiness and scoring

Search freshness is one day, independent of event validity:

| Search state | Meaning |
| --- | --- |
| READY_WITH_EVENTS | Complete fresh search with qualifying events |
| READY_NO_EVENTS | Complete fresh search, no qualifying events |
| PARTIAL_SEARCH | Incomplete provider/document coverage |
| FAILED_SEARCH | No usable completed provider coverage |
| STALE_SEARCH | Search completeness must be refreshed |

Fresh complete no-event evidence satisfies CURRENT_NEWS and produces impact 0
(news metric 50/100), unless independently active events still exist. Failed
search never fabricates a neutral metric. CURRENT_NEWS is optional for full V1
analysis; missing/failed coverage reduces confidence. Active event impact can
remain PARTIAL/scorable while search freshness degrades. Only severe validated
events enter the existing critical risk-override boundary. Ordinary commodity
cost pressure does not suppress ranking.

V1's seven-percent news weight and all ranker weights are unchanged. Technical
and sector scoring are unchanged. V1 retains its version; its input fingerprint
contract advances to `STOCK_RULE_ENGINE_V1_INPUT_2_NEWS` and includes news evidence,
search coverage and evaluation instant (continuous decay), invalidating obsolete
cached results. This can reduce cache hits for news-bearing evaluations.

## Valuation and TMCV freshness

Persisted fresh price can materialize PE from a valid trailing-EPS basis and PB
from book value. Instrument and currency must agree; conflicting simultaneous
prices are rejected. Quarterly EPS is not annualized into trailing EPS. Bases
use a 120-day reporting window; price-derived ratios retain the price timestamp
and market-session validity. Refreshing price does not reset the basis age.
Existing annual fundamental valuation evidence retains its 400-day policy.

The local TMCV audit found June 30 income/finance-cost support alongside March 31
mandatory debt/equity. Readiness selected the newer supporting date for display,
although stale mandatory inputs caused READY_STALE. The diagnostic now reports
the selected stale mandatory dates and `FRESHNESS_POLICY_EXPIRED:DEBT,EQUITY`.
It does not relabel stale March facts as fresh. June 30 mandatory facts evaluated
September 14 are fresh under the unchanged 120-day policy. No verified issuer
release calendar was present: period end remains the fallback anchor, with an
explicit valid-until honored when supplied. No release date or longer TTL is
invented.

## Persistence and validation

Java research-service remains Flyway owner. Approved additive V12 creates only:

- `company_business_exposure_profiles`: versioned evidence snapshots.
- `research_news_search_runs`: immutable provider coverage and outcome history.
- `research_event_impact_features`: immutable event-impact revisions.

Core identity, impact, confidence, horizon and temporal fields use typed indexed
columns; extensible details use JSONB. Database triggers reject UPDATE/DELETE.
The Python adapter uses INSERT and deterministic revision identities; repeat
identical records reuse the existing row. PostgreSQL startup requires V12.
SQLite mirrors the append-only repository boundary for tests. No migration-time
provider call, data rewrite or backfill occurs.

The migration was executed in a separate PostgreSQL schema inside a transaction;
all three tables and mutation guards validated, followed by ROLLBACK. It was not
applied to the deployed research schema. The generic cable/COPPER fixture
discovers and scores adverse exposure impact without a company headline mention,
retains multi-day relevance, and creates no severe override. Focused tests cover
provider failure/empty aggregation, immutability, timestamps, scoring, valuation
and the reproduced balance-sheet dates. Tests require no live news provider.

Remaining operational work: deployment of V12 before this Python version,
scheduled bounded profile/news refresh policy, expanded issuer-document coverage,
authoritative release calendars and explicit resolution-event linking. Optional
read-only ranking smoke was not run because this slice requires a migration.
No frontend, prediction, broad refresh or recommendation redesign is included.
