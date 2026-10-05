# NSE structured financial provider verification

Verified on 2026-10-03 against NSE's own public site; no Radar cycle was run.

## Existing components reused

- `OfficialFilingDiscovery.client`: the repository-owned NSE HTTP client and its
  lifecycle/timeout settings. Announcement discovery, title classification,
  shareholding discovery and their caches are unchanged.
- `HttpResearchFetcher.fetch_network`: existing URL/DNS validation, redirects,
  retry, streaming byte limit and provider telemetry. XML requests use the same
  NSE navigation headers as shareholding XBRL. Content type is checked before
  parsing, so a PDF response cannot invoke PDF extraction through this provider.
- `FinancialFact` / `FinancialFactKey` / `ProvenancedValue`: existing periods,
  reporting basis, source identity, authority and durable upsert precedence.
- `ExistingResearchCapabilityExecutor.official_financial_provider`: the existing
  injection point persists official facts and reassesses real readiness before
  scheduling `FINANCIAL_RESULTS`. Narrative categories retain their own route.

The existing shareholding XBRL parser cannot parse financial statements. The
DI-20E financial parser consumes PDF text/structure, not XBRL. The new parser is
limited to verified XML semantics; neither existing parser was modified.

## Primary sources and captured fixtures

- [NSE financial-results page](https://www.nseindia.com/companies-listing/corporate-filings-financial-results)
- [NSE corporate-filings JavaScript](https://www.nseindia.com/dist/js/sections/corporate-filings.js?v=01102026)
  defines `/api/integrated-filing-results`, the `symbol`, `type`, `page`, `size`
  parameters and `Integrated Filing- Financials` filter. It reads the `data`
  array and `totalCount`. Verified responses for GHCL and INFY had current filings.
- [Verified GHCL metadata](https://www.nseindia.com/api/integrated-filing-results?symbol=GHCL&type=Integrated%20Filing-%20Financials&page=1&size=20)
- [GHCL June 2026 XML](https://nsearchives.nseindia.com/corporate/xbrl/INTEGRATED_FILING_INDAS_1706581_01082026040421_WEB.xml)
- [GHCL March 2026 standalone XML](https://nsearchives.nseindia.com/corporate/xbrl/INTEGRATED_FILING_INDAS_1663782_05052026064325_WEB.xml)

The two unmodified XML responses and selected metadata rows are captured in
`tests/fixtures/nse_financial_xbrl`. Tests run offline. An additional INFY XML
was inspected to confirm the same taxonomy, identity and reporting-basis fields.
The old `/api/corporates-financial-results` endpoint returned older filings and
is not the production route added here.

## Accepted contract and limits

Only Indian NSE profiles with a trusted NSE mapping and ISIN are requested.
Metadata must identify that symbol, the financial filing type and an HTTPS NSE
archive XML. XML must independently match symbol, ISIN, reporting basis and
context entity identity. Supported namespace:
`http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt`.

Only dimensionless company totals are accepted. Duration contexts must match
explicit reporting dates and span complete three- or twelve-month periods;
instant balances retain `AS_AT`. Interim cumulative periods are omitted.
Currency is resolved from XBRL units. `decimals` is accuracy, never a scale.
Nil/non-finite values, unknown units, dimensional subtotals and conflicting
duplicates are omitted. DTD/entity declarations and identity conflicts fail closed.

Direct facts cover revenue from operations, total PAT, finance costs, total EPS,
explicit assets/equity/liabilities/current balances/cash and cash-flow totals.
EPS uses the existing DI-20E projection: diluted total EPS, otherwise basic total
EPS, with the exact tag retained in provenance. No EBITDA, debt, bank ratios,
quarter subtraction or annualization is inferred. The generic EPS schema remains
unchanged; preserving separate basic/diluted series is a separate schema decision.

One metadata page (20 rows) and at most the existing singleton document constant
(four XML artifacts) are considered per collection. Latest publication wins
selection within each period/basis; unsupported revisions stop structured
collection and leave official document fallback in place. No PDF document budget,
concurrency, timeout or freshness policy was increased. The candidate boundary
remains the same effective shortlist limit, including checkpoint restoration.

Structured insufficiency is expected: older taxonomies, revised filings,
specialized lender facts, missing annual comparisons and unsupported metrics may
still need the existing financial document path. No readiness threshold is
relaxed. A structured timeout/provider exception survives an insufficient PDF
fallback as retryable; an actually satisfied official requirement clears that
failure. Cancellation propagates without starting fallback.

Exact deterministic parser no-supported-facts reasons skip identical repair;
bare parser errors, unknown reasons, timeouts and mixed technical failures remain
retryable. This classification never makes unavailable evidence ready.
