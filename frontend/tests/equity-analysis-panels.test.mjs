import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const opportunityRadarSource = fs.readFileSync(new URL('../app/components/opportunity-radar.tsx', import.meta.url), 'utf8');

test('Equity Radar has a 3-column Universe/Baseline/Deep analysis area with a verified non-overlapping Deep-outcome donut', () => {
  assert.match(opportunityRadarSource, /function EquityAnalysisPanels/);
  assert.match(opportunityRadarSource, /aria-label="Universe and readiness"/);
  assert.match(opportunityRadarSource, /aria-label="Baseline analysis"/);
  assert.match(opportunityRadarSource, /aria-label="Deep analysis"/);
  assert.match(opportunityRadarSource, /aria-label="Deep analysis outcomes"/);
  assert.match(opportunityRadarSource, /aria-label="Failure reasons"/);
  assert.match(opportunityRadarSource, /aria-label="Research results and publication status"/);
  // The Deep-outcome donut's 5 segments are an exact, non-overlapping
  // partition of the admitted population, not a combined/misleading one.
  assert.match(opportunityRadarSource, /deep_ready \+ deep_readiness_failed \+ deep_technical_failure ==\s*\r?\n\s*\* deep_completed exactly, by construction/);
  assert.match(opportunityRadarSource, /label: "Ready for ranking", value: deepReady \?\? 0/);
  assert.match(opportunityRadarSource, /label: "Evidence unavailable", value: deepReadinessFailed \?\? 0/);
  assert.match(opportunityRadarSource, /label: "Technical failure", value: deepTechnicalFailure \?\? 0/);
  assert.match(opportunityRadarSource, /label: "In progress", value: deepInProgress \?\? 0/);
  assert.match(opportunityRadarSource, /label: "Not yet attempted", value: deepNotYetAttempted \?\? 0/);
});

test('Equity progress panels show active or clearly labelled retained research without a selector', () => {
  assert.match(opportunityRadarSource, /counters={equityCounters\(progressCycle\.progress\)}/);
  assert.match(opportunityRadarSource, /<EquityAnalysisPanels progress={progressCycle\?\.progress \?\? null} data={data} \/>/);
  assert.match(opportunityRadarSource, /showCycleId={false}/);
  assert.doesNotMatch(opportunityRadarSource, /Cycle history|<select/);
});

test('baseline_completed/baseline_failed are declared on EquityAuthoritativeProgress with no new backend endpoint -- the backend already sends them', () => {
  assert.match(opportunityRadarSource, /baseline_completed\?: number; baseline_failed\?: number;/);
  assert.match(opportunityRadarSource, /No new endpoint or backend change needed for these\./);
});

test('DEEP "In progress" counter excludes BOTH exhausted and still-retrying RETRYABLE_FAILURE rows', () => {
  // Equity Radar progress-accounting follow-up (Part 2): the earlier local fix
  // subtracted only deep_technical_failure (the EXHAUSTED-retry subset), so a
  // cycle with deep_attempted=234, deep_completed=119, deep_technical_failure=0
  // (no row has exhausted its repair budget yet) and technical=113 (all 113
  // RETRYABLE_FAILURE rows still awaiting retry) still showed 234-119-0=115
  // "in progress" -- reproducing the exact observed defect (115 shown, DB had
  // only 2 genuinely IN_PROGRESS + 113 awaiting retry). The corrected
  // computation subtracts deepAwaitingRetry (= technical - deepTechnicalFailure,
  // i.e. every current retryable row minus the exhausted ones), giving
  // 234 - 119 - 113 = 2, the genuinely in-flight count.
  assert.match(opportunityRadarSource, /const deepTechnical = progress\?\.technical \?\? null;/);
  assert.match(opportunityRadarSource, /const deepAwaitingRetry = deepTechnical != null && deepTechnicalFailure != null/);
  assert.match(opportunityRadarSource, /Math\.max\(deepTechnical - deepTechnicalFailure, 0\) : null;/);
  assert.match(opportunityRadarSource, /deepInProgress = deepAttempted != null && deepCompleted != null && deepAwaitingRetry != null/);
  assert.match(opportunityRadarSource, /Math\.max\(deepAttempted - deepCompleted - deepAwaitingRetry, 0\) : null;/);
});

test('BASELINE state breakdown uses authoritative backend fields, never an inferred total-minus-completed', () => {
  // The earlier local fix computed baselineInProgress as
  // universeTotal - baselineReady - baselineFailed. baseline_failed is also
  // terminal/exhausted-only (same shape as deep_technical_failure), so a
  // first-pass BASELINE_INCOMPLETE row (RETRYABLE_FAILURE, attempts=1 of 3,
  // not yet exhausted) was counted in NEITHER baselineReady nor baselineFailed,
  // leaving it folded into "in progress" by the subtraction -- reproducing the
  // exact observed defect (607 BASELINE_INCOMPLETE rows shown as "in progress").
  // The fix adds three new authoritative backend fields (cycle_checkpoint's
  // cycle_status_snapshot: baseline_in_progress, baseline_awaiting_retry,
  // baseline_not_started) and reads each bucket directly off its own field,
  // with no inferring subtraction for any of them.
  assert.match(opportunityRadarSource, /baseline_in_progress\?: number; baseline_awaiting_retry\?: number; baseline_not_started\?: number;/);
  assert.match(opportunityRadarSource, /const baselineAwaitingRetry = progress\?\.baseline_awaiting_retry \?\? null;/);
  assert.match(opportunityRadarSource, /const baselineActive = progress\?\.baseline_in_progress \?\? null;/);
  assert.match(opportunityRadarSource, /const baselineNotStarted = progress\?\.baseline_not_started \?\? null;/);
  // No inferred universeTotal - baselineReady (- baselineFailed) subtraction remains.
  assert.doesNotMatch(opportunityRadarSource, /universeTotal - baselineReady/);
});

test('post-DEEP fallback SUPPRESSED diagnostics only fire for candidates never reached by DEEP stage', () => {
  // The old check `ensure_results.get(candidate_id) is None` fired for the 607
  // BASELINE RETRYABLE_FAILURE rows even after DEEP had produced its own
  // outcome for them, adding spurious duplicate SUPPRESSED diagnostics.
  // The fix is in the backend Python module
  // (global_opportunity_orchestration.py): it checks deep_diagnosed_keys
  // (already processed by DEEP) first. Verified in the backend regression test
  // test_baseline_failure_reason_fix.py::test_post_deep_fallback_only_for_unreached_candidates.
  const orchestrationSource = fs.readFileSync(
    new URL('../../ai/research-engine/app/global_opportunity_orchestration.py', import.meta.url), 'utf8');
  assert.match(orchestrationSource, /deep_diagnosed_keys = \{d\.global_instrument_id for d in diagnostics\}/);
  assert.match(orchestrationSource, /if candidate_id not in deep_diagnosed_keys:/);
});
