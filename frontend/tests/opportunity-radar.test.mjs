import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import ts from 'typescript';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const require = createRequire(import.meta.url);
function component(name) {
  const source = fs.readFileSync(new URL(`../app/components/${name}.tsx`, import.meta.url), 'utf8');
  const output = ts.transpileModule(source, { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} };
  new Function('require', 'module', 'exports', output)(path => path.includes('portfolio-api') ? {} : require(path), module, module.exports);
  return module.exports;
}
const { RadarContent, normalizeRadar, price, INITIAL_DISPLAY, PreviousRecommendationsList } = component('opportunity-radar');
const { Backtesting, BacktestResults } = component('backtesting');
const item = i => ({ global_instrument_id: `${i}`, company_name: `Company ${i}`, symbol: `C${i}`, current_price: 100,
  opportunity_score: 80, opportunity_confidence: 85, score_coverage: 90, new_investor_action: 'BUY_CANDIDATE',
  existing_holder_action: 'HOLD', current_short_action: 'BUY', current_long_action: 'ACCUMULATE',
  short_entry_low: null, short_entry_high: null, top_positive_reasons: ['QUALITY'], top_negative_reasons: ['MISSING_ATR'],
  data_state: 'PARTIAL', missing_areas: ['ATR'], stale_areas: [], short_horizon: '1–3 months', long_horizon: '6–12 months' });
const empty = { generated_at: null, best_buy_today: null, top_short_term: [], top_long_term: [], previous_recommendations: [] };

test('radar renders persisted 0–4 cards per horizon and missing ranges', () => {
  // 0 cards: empty state messages, zero opportunity-card elements.
  for (const count of [0, 1, 2, 3, 4]) {
    const picks = Array.from({ length: count }, (_, i) => item(i));
    const html = renderToStaticMarkup(React.createElement(RadarContent, { data: { ...empty, top_short_term: picks, top_long_term: picks }, heldIds: ['0'], watchlistedIds: ['1'] }));
    assert.equal((html.match(/class="opportunity-card"/g) ?? []).length, count * 2);
    if (count > 0) {
      assert.match(html, /Insufficient evidence/);
      assert.match(html, /Held/);
      assert.doesNotMatch(html, /₹0/);
      // "Watchlisted" label only appears when the watchlisted item (item 1,
      // global_instrument_id "1") is among the rendered picks, which requires
      // count >= 2. Item 0 is held-only, item 1 is watchlisted-only.
      if (count >= 2) assert.match(html, /Watchlisted/);
    }
    // No "More" button when count <= INITIAL_DISPLAY.
    assert.doesNotMatch(html, /opportunity-show-more/);
  }
  assert.equal(price(null), 'Insufficient evidence');
});

test('radar shows More button only when more than 4 cards per horizon', () => {
  // 5+ cards: first 4 render initially, "More" button appears.
  for (const count of [5, 9, 20]) {
    const picks = Array.from({ length: count }, (_, i) => item(i));
    const html = renderToStaticMarkup(React.createElement(RadarContent, { data: { ...empty, top_short_term: picks, top_long_term: picks }, heldIds: [], watchlistedIds: [] }));
    // Only INITIAL_DISPLAY cards per horizon render (4 short + 4 long = 8).
    assert.equal((html.match(/class="opportunity-card"/g) ?? []).length, INITIAL_DISPLAY * 2);
    // "More" buttons: one for short-term, one for long-term.
    const moreCount = (html.match(/opportunity-show-more/g) ?? []).length;
    assert.equal(moreCount, 2, `expected 2 More buttons for count=${count}, got ${moreCount}`);
    // Only one "No qualifying" message per empty horizon — these are not empty.
    assert.doesNotMatch(html, /No qualifying short-term/);
    assert.doesNotMatch(html, /No qualifying long-term/);
  }
});

test('radar does not show Show-less initially (only after More click)', () => {
  const picks = Array.from({ length: 9 }, (_, i) => item(i));
  const html = renderToStaticMarkup(React.createElement(RadarContent, { data: { ...empty, top_short_term: picks, top_long_term: picks }, heldIds: [], watchlistedIds: [] }));
  // Initially only first 4 shown; no Show-less button (only appears after expansion).
  assert.doesNotMatch(html, /opportunity-show-less/);
  // But More buttons are present.
  const moreCount = (html.match(/opportunity-show-more/g) ?? []).length;
  assert.equal(moreCount, 2);
});

test('radar empty and lifecycle render', () => {
  assert.match(renderToStaticMarkup(React.createElement(RadarContent, { data: empty })), /No persisted opportunity cycle/);
  const html = renderToStaticMarkup(React.createElement(RadarContent, { data: { ...empty, previous_recommendations: [{ ...item(1), current_short_action: 'PARTIAL_PROFIT', short_term_state: 'PARTIAL_PROFIT', current_long_action: 'HOLD', long_term_state: 'HOLDING' }] } }));
  assert.match(html, /PARTIAL PROFIT/); assert.match(html, /Long: HOLD/);
});

test('normalizeRadar defends against the actual backend shape, which omits previous_recommendations and count fields', () => {
  // GET /api/v1/research/opportunities/current returns
  // repository.persistence.global_opportunity_radar() verbatim (no response_model).
  // Both OpportunityPersistenceMixin.global_opportunity_radar()
  // (ai/research-engine/app/opportunity_persistence.py) and
  // DisabledResearchPersistence.global_opportunity_radar()
  // (ai/research-engine/app/persistence.py) build that dict without a
  // `previous_recommendations` key — and prior to this task, also without
  // top_short_term_count / top_long_term_count / top_exit_count fields.
  const backendShape = { generated_at: null, best_buy_today: null, top_short_term: [], top_long_term: [] };
  assert.equal('previous_recommendations' in backendShape, false);
  assert.equal('top_short_term_count' in backendShape, false);
  assert.equal('top_long_term_count' in backendShape, false);

  // RadarContent is now defensive: missing previous_recommendations no longer
  // crashes rendering (the `!data.previous_recommendations ||` guard
  // short-circuits before `.length`).
  assert.doesNotThrow(
    () => renderToStaticMarkup(React.createElement(RadarContent, { data: backendShape }))
  );

  // normalizeRadar fills in ALL omitted fields with safe defaults.
  const normalized = normalizeRadar(backendShape);
  assert.deepEqual(normalized.previous_recommendations, []);
  assert.equal(normalized.top_short_term_count, 0);
  assert.equal(normalized.top_long_term_count, 0);
  assert.equal(normalized.top_exit_count, 0);
  assert.deepEqual(normalized.top_exit, []);
  assert.deepEqual(normalized.top_short_term, []);
  assert.deepEqual(normalized.top_long_term, []);
  // Rendering the normalized data must not throw.
  assert.doesNotThrow(() => renderToStaticMarkup(React.createElement(RadarContent, { data: normalized })));
});

test('normalizeRadar count fields reflect backend-provided counts when present', () => {
  const backendShape = { generated_at: null, best_buy_today: null,
    top_short_term: [item(0), item(1), item(2), item(3), item(4)],
    top_long_term: [item(5), item(6), item(7), item(8)],
    top_exit: [item(9)],
    top_short_term_count: 5, top_long_term_count: 4, top_exit_count: 1 };
  const normalized = normalizeRadar(backendShape);
  // Counts from the backend are preserved, not recomputed.
  assert.equal(normalized.top_short_term_count, 5);
  assert.equal(normalized.top_long_term_count, 4);
  assert.equal(normalized.top_exit_count, 1);
  // Arrays are preserved as-is.
  assert.equal(normalized.top_short_term.length, 5);
  assert.equal(normalized.top_long_term.length, 4);
  assert.equal(normalized.top_exit.length, 1);
});

test('backtesting page and all horizon results render', () => {
  assert.match(renderToStaticMarkup(React.createElement(Backtesting)), /Run backtest/);
  const metric = { evaluated_count: 1, missing_count: 0, hit_rate: 100, average_return: 10, median_return: 10 };
  const run = { recommendation_count: 1, methodology: 'Recorded evidence', winners: [], losers: [], metrics: Object.fromEntries(['1W', '1M', '3M', '6M', '1Y'].map(h => [h, metric])) };
  const html = renderToStaticMarkup(React.createElement(BacktestResults, { run }));
  for (const h of ['1W', '1M', '3M', '6M', '1Y']) assert.ok(html.includes(h));
  assert.match(html, /100.00%/);
});

test('previous recommendations paginate: first page shows 10, More shows next', () => {
  const picks = Array.from({ length: 25 }, (_, i) => ({ ...item(i), current_short_action: 'BUY', short_term_state: 'BUY' }));
  const html = renderToStaticMarkup(React.createElement(RadarContent, {
    data: { ...empty, previous_recommendations: picks }
  }));
  // Page 1 of 3, first 10 shown.
  assert.match(html, /Page 1 of 3/);
  assert.equal((html.match(/class="recommendation-item"/g) ?? []).length, 0); // list items don't have that class
  // Previous button disabled on first page: the pagination-prev button
  // has the disabled attribute (and no onClick).
  assert.match(html, /pagination-prev[^>]*disabled/);
  // Next button enabled: the pagination-next button has no disabled attribute.
  assert.doesNotMatch(html, /pagination-next[^>]*disabled/);
  // Pagination controls present.
  assert.match(html, /pagination-controls/);
  assert.match(html, /pagination-next/);
  assert.match(html, /pagination-prev/);
});

test('previous recommendations empty shows no pagination', () => {
  const html = renderToStaticMarkup(React.createElement(RadarContent, {
    data: { ...empty, previous_recommendations: [] }
  }));
  assert.match(html, /No previous recommendations/);
  assert.doesNotMatch(html, /pagination-controls/);
});

test('previous recommendations single page: no next, previous disabled', () => {
  const picks = Array.from({ length: 5 }, (_, i) => ({ ...item(i), current_short_action: 'BUY', short_term_state: 'BUY' }));
  const html = renderToStaticMarkup(React.createElement(RadarContent, {
    data: { ...empty, previous_recommendations: picks }
  }));
  assert.match(html, /Page 1 of 1/);
  // Both buttons disabled on single page (no more, no prev).
  assert.match(html, /pagination-prev[^>]*disabled/);
  assert.match(html, /pagination-next[^>]*disabled/);
});

test('previous recommendations render all when under page size', () => {
  const picks = Array.from({ length: 10 }, (_, i) => ({ ...item(i), current_short_action: 'BUY', short_term_state: 'BUY' }));
  const html = renderToStaticMarkup(React.createElement(RadarContent, {
    data: { ...empty, previous_recommendations: picks }
  }));
  // All 10 visible, page 1 of 1
  assert.match(html, /Page 1 of 1/);
  for (let i = 0; i < 10; i++) {
    assert.match(html, new RegExp(`Company ${i}`));
  }
});
