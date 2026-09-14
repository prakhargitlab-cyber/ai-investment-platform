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
const { RadarContent, price } = component('opportunity-radar');
const { Backtesting, BacktestResults } = component('backtesting');
const item = i => ({ global_instrument_id: `${i}`, company_name: `Company ${i}`, symbol: `C${i}`, current_price: 100,
  opportunity_score: 80, opportunity_confidence: 85, score_coverage: 90, new_investor_action: 'BUY_CANDIDATE',
  existing_holder_action: 'HOLD', current_short_action: 'BUY', current_long_action: 'ACCUMULATE',
  short_entry_low: null, short_entry_high: null, top_positive_reasons: ['QUALITY'], top_negative_reasons: ['MISSING_ATR'],
  data_state: 'PARTIAL', missing_areas: ['ATR'], stale_areas: [], short_horizon: '1–3 months', long_horizon: '6–12 months' });
const empty = { generated_at: null, best_buy_today: null, top_short_term: [], top_long_term: [], previous_recommendations: [] };

test('radar renders persisted 2–4 cards per horizon and missing ranges', () => {
  for (const count of [2, 3, 4]) {
    const picks = Array.from({ length: count }, (_, i) => item(i));
    const html = renderToStaticMarkup(React.createElement(RadarContent, { data: { ...empty, top_short_term: picks, top_long_term: picks }, heldIds: ['0'], watchlistedIds: ['1'] }));
    assert.equal((html.match(/class="opportunity-card"/g) ?? []).length, count * 2);
    assert.match(html, /Insufficient evidence/);
    assert.match(html, /Held/); assert.match(html, /Watchlisted/);
    assert.doesNotMatch(html, /₹0/);
  }
  assert.equal(price(null), 'Insufficient evidence');
});
test('empty and lifecycle render', () => {
  assert.match(renderToStaticMarkup(React.createElement(RadarContent, { data: empty })), /No persisted opportunity cycle/);
  const html = renderToStaticMarkup(React.createElement(RadarContent, { data: { ...empty, previous_recommendations: [{ ...item(1), current_short_action: 'PARTIAL_PROFIT', short_term_state: 'PARTIAL_PROFIT', current_long_action: 'HOLD', long_term_state: 'HOLDING' }] } }));
  assert.match(html, /PARTIAL PROFIT/); assert.match(html, /Long: HOLD/);
});
test('backtesting page and all horizon results render', () => {
  assert.match(renderToStaticMarkup(React.createElement(Backtesting)), /Run backtest/);
  const metric = { evaluated_count: 1, missing_count: 0, hit_rate: 100, average_return: 10, median_return: 10 };
  const run = { recommendation_count: 1, methodology: 'Recorded evidence', winners: [], losers: [], metrics: Object.fromEntries(['1W', '1M', '3M', '6M', '1Y'].map(h => [h, metric])) };
  const html = renderToStaticMarkup(React.createElement(BacktestResults, { run }));
  for (const h of ['1W', '1M', '3M', '6M', '1Y']) assert.ok(html.includes(h));
  assert.match(html, /100.00%/);
});
