"""Guardian Review (Issue 6, STAGE2_LIVE_RUN_DEFECTS_20260930.md) -- DEFERRED.

An initial same-cycle Ticker/`.info` reuse fix was implemented and then
reverted in this same pass after it was caught by this repo's own regression
suite: tests/test_baseline_market_reuse.py encodes an explicit, deliberate
contract that every `collect()`/`collect_baseline()` call (including an
immediate retry after a TimeoutError, and an immediate retry after a PARTIAL
result) must independently re-verify data freshness with a truly fresh
provider read -- see test_failed_baseline_provider_remains_retryable and
test_successful_partial_snapshot_does_not_satisfy_valuation_or_prevent_retry
in particular, which both mutate the underlying (mocked) provider data
between two back-to-back calls and require the SECOND call to observe the
mutation, not a cached value from the first.

A same-cycle reuse window keyed only by (ticker symbol, elapsed wall-clock
time) cannot distinguish "the same baseline->fallback dispatch within one
readiness cycle" (the evidenced duplicate-work case this issue targets) from
"a distinct, later ensure()/retry call for the same ticker" (which these
existing tests correctly require to be fully fresh) -- both can happen only
milliseconds apart. Solving this safely needs an explicit reuse token passed
down the SAME call chain (portfolio_orchestration.py's execute_primary ->
execute_approved_fallbacks for one candidate) rather than an implicit
time-based cache, which is a larger change than this pass's evidence
justified taking on without further scoping.

Left deliberately unfixed this pass (see the Guardian Review's item 16);
app/structured_market.py and app/settings.py are unchanged from before this
turn. This file is kept (rather than deleted -- this session lacks delete
permission in the connected folder) as a record of why, so a future pass
does not have to re-discover the same conflict from scratch.
"""
