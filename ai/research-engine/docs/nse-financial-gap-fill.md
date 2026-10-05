# NSE-first financial gap fill — Slice 1

With the official financial provider installed (as in main.py) and a verified NSE
mapping on an Indian NSE equity, the MCP wrapper now handles
QUARTERLY_FINANCIALS, GROWTH_FACTS, BUSINESS_QUALITY_FACTS and BALANCE_SHEET_FACTS as:

1. Read persisted evidence with the existing readiness service. Reuse READY_FRESH
   and NOT_APPLICABLE results; preserve planner-requested official authority upgrades.
2. Give the existing official structured financial collector an opportunity to
   persist facts through the existing financial upsert. No new NSE client or endpoint.
3. Re-read evidence and calculate absent or stale input coverage using the existing
   requirement definitions, applicability exclusions and freshness policies.
4. If supported gaps remain, request the existing Yahoo requirement capability.
   Its external tool contract cannot express field selection. Filter the response
   before persistence to explicit period/basis-labelled financial facts contributing
   to the unresolved inputs. Discard unrelated structured summaries, quotes and events.
5. Re-read actual persisted readiness. Useful partial responses can persist without
   certifying completeness. Unsatisfied targets retain the existing document fallback;
   this slice does not change document discovery, parsing or budgets. Suppress the
   later whole-category direct-Yahoo financial fallback, which would bypass filtering.

The internal research-engine → MCP-gateway HTTP command has an optional
`financialGapFill` flag (default false). Only INDIA and the four requirements above
may use it. It permits partial normalized financial responses; identity, capability,
schema and freshness validation remain. It is **not forwarded to the Yahoo tool**.
The research-engine filter decides whether any returned fact is useful. Both service
changes must be available for runtime validation; an older gateway rejects the flag
and financial readiness remains fail-closed.

## Identity, authority and missing data

Filtering retains FinancialFactKey identity (instrument, metric, period end, period
type, reporting basis). Units are values, not part of that key: explicit compatible
units and consistent units within a series are required separately. Existing
merge_fact() decisions also filter populated keys, protecting valid zero and
derived official facts without inventing another source ranking.
Ambiguous duplicate keys in a response are rejected. Annual, quarterly and AS_AT
facts do not substitute for one another. Unlabelled/UNKNOWN basis, unsupported units,
future/noncanonical period dates and nonfinite values cannot close a gap.

Persistence still exclusively owns source precedence via merge_fact() and
fact_source_authority(). The persisted enum numbers are not the authority order:
the current authority function ranks OFFICIAL_NSE above STRUCTURED_FUNDAMENTALS.
This slice changes neither function nor persisted identities.

Readiness is input-level, with the repository's existing series/comparability gates;
it does not specify a new required quarter matrix. The filter never invents an
expected period or treats a missing Q2 value as permission to replace Q1.

Attempt failures remain in capability progress and provider acquisition observations.
Only final blocking failures are cleared, and only when a fresh durable reassessment
proves READY_FRESH or NOT_APPLICABLE. Generic wrapper reverification also applies to
existing regional fallbacks without changing their acquisition or persistence.

## Scope and limitations

- VALUATION_INPUTS retains its quote/multiple routing. Shareholding and narrative
  requirements retain their existing routes. No full-universe work is introduced.
- Compositions without the optional structured provider retain their existing
  authority/document route; an absent collector is never counted as an NSE attempt.
- Yahoo summary ratios without a period/basis are not promoted into financial facts.
  Unsupported lender capital metrics and derivation-component discovery are not
  added to the Yahoo contract; remaining gaps remain explicit.
- NSE collection is still bounded capability-level collection. A repair can collect
  that capability again if gaps remain, but Yahoo persistence reuses current gaps and
  never reacquires a completed requirement. Existing acquisition-context reuse remains.
- Exact keys with stale observations are not made fresh by retrieval or overwritten
  by a lower-authority provider. Existing timestamp/TTL/session issues remain separate.
- PDF safety, extraction concurrency, timeouts, document budgets, schema, scoring,
  recommendation completeness and the 2609 → 25 admission boundary are unchanged.
