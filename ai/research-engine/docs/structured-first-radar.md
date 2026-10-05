# Structured-first Radar acquisition

The production request's `shortlist_limit` bounds live acquisition and deep investigation.
The existing default is 25 (range 1–100); scheduler construction accepts the same
limit. Legacy production jobs carrying `None` use that default. All eligible
equities receive provider-free persisted scanner evaluation and comparable pre-scores. Discovery
retains its deterministic multi-path nominations and rotation within the budget.
Explicit diagnostic orchestrator calls can still request an exhaustive pool.
Only admitted candidates execute baseline structured/history readiness ensures,
then an admitted-only persisted rescan, then deep investigation. There is no
post-enrichment replacement queue or second acquisition budget. For a limit of
25, at most 25 distinct candidates enter any live acquisition in the cycle.
Prior recommendations share this admission budget. Unadmitted reviews retain
persisted technical lifecycle evidence only and cannot enter rule analysis,
evaluated entries or ranked results. Publication includes qualifying investigated entries;
`top_n` remains a display concern.

Unselected candidates have a `DEFERRED` scheduling disposition and per-requirement
`acquisition_state=DEFERRED, attempted=false`. This is not a durable provider
observation, evidence absence, SUCCESS_EMPTY, failure, or a scoring status.
`build_plan(..., deep_selected=False)` lists deferred requirements separately.
The orchestration matrix marks all requirements, including baseline requirements,
deferred for non-admitted candidates; no readiness observation is fabricated.
Once selected, the ordinary readiness and rule-engine gates apply unchanged:
applicable catalyst/governance coverage is required; no neutral evidence is made.

Official discovery and attachment processing stay behind deep requirements and
their existing due, provenance, applicability and sufficiency checks. Existing
announcement metadata/title classification remains intact. The current narrative
contracts need genuine events, parsed documents or completed authoritative checks;
discovery titles alone are not new quantitative evidence. Warm durable evidence
avoids the fallback. PDF queue ownership, timeouts, single-flight, late persistence,
memory release, periodic GC and extraction concurrency are unchanged.

Yahoo historical acquisition shares a provider-owned, bounded context/ticker
pool with structured acquisition. Access to each mutable ticker is serialized
off the event loop. The same returned `.info` is reused, but canonical historical
identity is verified on every history result. New contexts retry; there is no
global ticker cache. Baseline `.pop()` invalidation remains in place. Yahoo's
existing `.info` API bundles quote and valuation, so a due class may retrieve
both; freshness of one class does not itself make another class due.

Explicit structured readiness calls assess requested classes separately, including
missing/invalid values and their original dates. A missing statement history can
still invoke the full financial fallback after a baseline `.info` call. Provider
failure and omitted fields never advance those classes' timestamps. Genuine
exchange-close quotes retain their existing next-session validity.

## Provider preparation — EXTERNAL_VERIFICATION_REQUIRED

- `StructuredClassProvider.collect_classes` accepts only the due classes and
  returns the existing `StructuredMarketSnapshot`. Omitted classes retain their
  provenance/timestamps. A verified future `NseQuoteProvider` can implement it.
- `OfficialFinancialProvider.collect` emits existing `FinancialFact` values.
  Production now injects the verified NSE integrated financial XBRL adapter using
  existing clients and the existing live-research setting. It persists through
  current precedence, reassesses readiness and uses PDFs when insufficient.
  See [the verified contract and limits](nse-structured-financial-contract.md).
- NSE quotes, additional financial taxonomies, revision handling and specialized
  lender coverage still require verification; no client or payload is invented.
- Existing NSE shareholding XBRL and historical transport are unchanged. Extracting
  their session/bootstrap/retry machinery before the new feeds' contracts are
  verified would risk changing working behavior without a demonstrated benefit.
- `NseHistoricalDailyProvider` already validates canonical mapping, date windows,
  CSV identity and OHLC data, with session bootstrap, throttling and retries. It
  writes `DailyMarketBar`, whereas the current readiness history path consumes
  `MarketPriceObservation`. It is not a drop-in primary provider: the next step
  must validate that adapter, range coverage, failure handling and provider
  precedence before changing routing. No additional history provider runs here.
- Financial persistence uses a generic `eps` metric key. The semantic extractor
  distinguishes `eps_basic` and `eps_diluted`, but existing projection prefers
  diluted, otherwise basic. A new feed must not collapse both or silently replace
  their meaning. An explicit semantic mapping/schema decision is required. No
  financial key, unit, reporting basis, source tier or downstream schema changed.

## Validation counters

`radar_acquisition_count` logs contain fixed operation/reason/provider labels and
numeric counts, without document bodies or instrument identifiers:

- `baseline_candidates_evaluated`, `deep_candidates_selected`;
- `deep_candidates_acquired`: distinct selected candidates per run whose completed
  ensure reported planned provider work (retries are deduplicated; background news
  that completes later is not included);
- `official_documents_discovered`, `pdf_downloaded`, `pdf_parsed`;
- `pdf_fallback_avoided`: sufficient durable/structured requirements or stopped
  document dispatches, not an estimate of unmade network calls;
- `yahoo_structured_live_acquisition`: uncached info/HTTP bundle attempts;
- `yahoo_structured_reuse` (context/cache), `structured_durable_reuse` (provider);
- `yahoo_identity_live_fetch`, `historical_series_live_acquisition`.

These measure acquisition decisions/attempts, not proof of evidence completeness.
Readiness remains authoritative. Local tests do not establish full-universe runtime.

`baseline_acquisition_outcomes` distinguishes evaluated, admitted, deferred,
acquisition needed, attempted, ready, incomplete, timed out and failed candidates.
Ready requires every baseline requirement to be READY_FRESH or NOT_APPLICABLE,
with no acquisition failure; returning an ensure result is not proof of readiness.
Attempted means the ensure reported executed capabilities; warm reuse can be ready
without a provider attempt. These are candidate outcomes, not HTTP request counts.

Selection is checkpointed before live work with `BOUNDED_ACQUISITION_V1`.
Resume preserves ordered membership, truncates legacy oversized selections to the
effective limit, and restores only admitted progress. Legacy baseline COMPLETED
records without readiness metadata are reassessed for admitted candidates only.
Missing selected identities are reported without admitting replacements. Existing
ownership fences, bounded repair and cancellation/drain behavior remain in place.

## Separate freshness follow-up

This boundary change does not change timestamps, TTLs or session alignment.
Yahoo regularMarketTime used for valuation/fundamental observations and historical
midnight versus session-close alignment can still cause repeated due decisions or
readiness timeouts within the admitted pool. They require a separate semantic fix;
no observation is marked fresh merely because it was retrieved again.
