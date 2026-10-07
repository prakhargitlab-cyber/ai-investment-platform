import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import ts from 'typescript';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const require = createRequire(import.meta.url);
const moduleCache = new Map();

// Resolve a relative TS/TSX specifier against the importing file's own URL
// (so './ui' from app/components/backtesting.tsx resolves to
// app/components/ui.tsx, '../lib/backtest-status' resolves to
// app/lib/backtest-status.ts, etc.) -- generalizes the original
// single-file transpile-and-eval so a component may now import local
// helper modules (shared UI primitives, status-label mappings) and still
// be exercised here without a full bundler/build step.
function resolveLocalModuleUrl(baseUrl, specifier) {
  for (const ext of ['.tsx', '.ts']) {
    const candidate = new URL(specifier + ext, baseUrl);
    if (fs.existsSync(candidate)) return candidate;
  }
  return null;
}

function loadLocalModule(fileUrl) {
  const key = fileUrl.pathname;
  if (moduleCache.has(key)) return moduleCache.get(key);
  const source = fs.readFileSync(fileUrl, 'utf8');
  const output = ts.transpileModule(source, { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const mod = { exports: {} };
  moduleCache.set(key, mod.exports);
  const localRequire = (specifier) => {
    // portfolio-api's request() is only ever called from inside a useEffect
    // or an event handler, never during a synchronous render -- an empty
    // stub is safe here and avoids needing a real fetch/DOM for these
    // render-to-static-markup tests.
    if (specifier.includes('portfolio-api')) return {};
    if (specifier.startsWith('.')) {
      const resolved = resolveLocalModuleUrl(fileUrl, specifier);
      if (resolved) return loadLocalModule(resolved);
    }
    return require(specifier);
  };
  new Function('require', 'module', 'exports', output)(localRequire, mod, mod.exports);
  return mod.exports;
}

function component(name) {
  return loadLocalModule(new URL(`../app/components/${name}.tsx`, import.meta.url));
}
const { RadarContent, normalizeRadar, price, INITIAL_DISPLAY, PreviousRecommendationsList, OpportunityCard, investorLabel } = component('opportunity-radar');
const { Backtesting, BacktestResults } = component('backtesting');
const { normalizeBacktest } = loadLocalModule(new URL('../app/lib/backtest-status.ts', import.meta.url));
const workspaceSource = fs.readFileSync(new URL('../app/components/investment-workspace.tsx', import.meta.url), 'utf8');
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
  assert.match(html, /Partial profit/); assert.match(html, /Long: Hold/);
});

test('recommendation evidence codes render as investor-readable labels without mutating domain values', () => {
  const codes = [
    'SUPPORT:BALANCE_SHEET',
    'SUPPORT:FUNDAMENTAL_BUSINESS_QUALITY',
    'SUPPORT:GROWTH',
    'SUPPORT:SHAREHOLDING',
    'UNAVAILABLE:NEWS_GEOPOLITICAL_EVENTS',
    'UNAVAILABLE:SECTOR'
  ];
  const original = [...codes];
  assert.deepEqual(codes.map(investorLabel), [
    'Balance sheet strength',
    'Business quality',
    'Growth',
    'Shareholding',
    'News and geopolitical evidence unavailable',
    'Sector evidence unavailable'
  ]);
  assert.deepEqual(codes, original);

  const html = renderToStaticMarkup(React.createElement(OpportunityCard, {
    item: { ...item(7), top_positive_reasons: codes.slice(0, 4), top_negative_reasons: codes.slice(4) },
    horizon: 'short', held: false, watchlisted: false
  }));
  assert.match(html, /Balance sheet strength/);
  assert.match(html, /Sector evidence unavailable/);
  assert.doesNotMatch(html, /SUPPORT:BALANCE_SHEET/);
  assert.doesNotMatch(html, /UNAVAILABLE:SECTOR/);
});

test('recommendation cards prioritize action, price levels, horizon and evidence quality', () => {
  const html = renderToStaticMarkup(React.createElement(OpportunityCard, {
    item: { ...item(8), short_entry_low: 95, short_entry_high: 100, short_target_1: 115 },
    horizon: 'short', held: true, watchlisted: true
  }));
  for (const label of ['Buy', 'Current price', 'Entry range', 'Target', 'Horizon', 'Evidence quality', 'Why', 'Risks']) {
    assert.ok(html.includes(label), `missing prioritized label ${label}`);
  }
});

test('dashboard source orders portfolio state before Radar and market intelligence', () => {
  const dashboard = workspaceSource.slice(
    workspaceSource.indexOf('function MultiPortfolioDashboard'),
    workspaceSource.indexOf('function MarketPerformanceRow')
  );
  const portfolioIndex = dashboard.indexOf('My portfolios');
  const radarIndex = dashboard.indexOf('{radar}', portfolioIndex);
  assert.ok(portfolioIndex < radarIndex);
  assert.ok(radarIndex < dashboard.indexOf('Market intelligence'));
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

test('backtesting page renders and distinguishes evaluation statuses truthfully', () => {
  const page = renderToStaticMarkup(React.createElement(Backtesting));
  assert.match(page, /Run backtest/);
  assert.match(page, /RADAR BACKTEST/);
  assert.match(page, /persisted investment type/);

  const sample = (status, extra = {}) => ({
    recommendation_id: 'r1', global_instrument_id: 'g1', company_name: 'Acme', symbol: 'ACME',
    status, evaluation_date: '2024-01-01T00:00:00Z', required_evaluation_date: '2024-02-01T00:00:00Z',
    return_pct: null, max_adverse_excursion_pct: null, benchmark_excess_return_pct: null, ...extra
  });
  const evaluatedMetric = {
    recommendation_count: 1, evaluated_count: 1, not_matured_count: 0, missing_market_data_count: 0,
    outside_evaluation_window_count: 0, insufficient_evidence_count: 0, backend_failure_count: 0,
    hit_rate: 100, average_return: 10, median_return: 10, best_return: 10, worst_return: 10,
    max_adverse_excursion: null, benchmark_excess_return: null
  };
  const notMaturedMetric = {
    recommendation_count: 1, evaluated_count: 0, not_matured_count: 1, missing_market_data_count: 0,
    outside_evaluation_window_count: 0, insufficient_evidence_count: 0, backend_failure_count: 0,
    hit_rate: null, average_return: null, median_return: null, best_return: null, worst_return: null,
    max_adverse_excursion: null, benchmark_excess_return: null
  };
  const run = {
    backtest_id: 'bt1', generated_at: '2024-03-01T00:00:00Z', as_of: '2024-03-01T00:00:00Z',
    start: '2024-01-01T00:00:00Z', end: '2024-02-01T00:00:00Z', horizon: 'SHORT_TERM',
    recommendation_count: 1, excluded_unavailable_evidence: 0, methodology: 'Recorded evidence',
    metrics: { '1M': evaluatedMetric, '1W': notMaturedMetric, '3M': notMaturedMetric, '6M': notMaturedMetric, '1Y': notMaturedMetric },
    samples: {
      '1M': [sample('EVALUATED', { return_pct: 10 })],
      '1W': [sample('NOT_MATURED')], '3M': [sample('NOT_MATURED')],
      '6M': [sample('NOT_MATURED')], '1Y': [sample('NOT_MATURED')]
    },
    winners: [sample('EVALUATED', { return_pct: 10 })], losers: []
  };
  const html = renderToStaticMarkup(React.createElement(BacktestResults, { run }));
  // A short-term run renders only its four relevant horizon tabs. The legacy
  // payload's 1Y bucket is deliberately hidden.
  for (const label of ['1 week', '1 month', '3 months', '6 months']) assert.ok(html.includes(label));
  assert.doesNotMatch(html, />1 year</);
  assert.match(html, /RADAR BACKTEST — SHORT-TERM — 1 WEEK/);
  // The class-specific accuracy table includes real aggregate performance,
  // never a misleading global 0%.
  assert.match(html, /100\.00%/);
  assert.doesNotMatch(html, /\b0\.00%/);
  // Friendly status labels, not raw enum strings.
  assert.match(html, /Not matured/);
  assert.doesNotMatch(html, /NOT_MATURED/);
  assert.doesNotMatch(html, /undefined/i);
});

test('long-term backtesting renders only one-to-three-year horizons', () => {
  const metric = {
    recommendation_count: 1, evaluated_count: 0, not_matured_count: 1,
    missing_market_data_count: 0, outside_evaluation_window_count: 0,
    insufficient_evidence_count: 0, backend_failure_count: 0,
    success_rate: null, hit_rate: null, average_return: null, average_market_return: null,
    median_return: null, best_return: null, worst_return: null,
    max_adverse_excursion: null, benchmark_excess_return: null
  };
  const sample = {
    recommendation_id: 'long-1', global_instrument_id: 'g-long', company_name: 'Long Co',
    action: 'BUY', direction: 'BUY', recommendation_type: 'LONG_TERM', status: 'NOT_MATURED',
    evaluation_date: '2026-01-01T00:00:00Z', required_evaluation_date: '2027-01-01T00:00:00Z',
    return_pct: null, signal_return_pct: null, success: null,
    max_adverse_excursion_pct: null, benchmark_excess_return_pct: null
  };
  const run = {
    backtest_id: 'long-run', engine_version: 'BACKTESTING_V3',
    generated_at: '2026-06-01T00:00:00Z', as_of: '2026-06-01T00:00:00Z',
    start: '2026-01-01T00:00:00Z', end: '2026-01-31T00:00:00Z',
    horizon: 'LONG_TERM', recommendation_type: 'LONG_TERM',
    available_horizons: ['1Y', '2Y', '3Y'], example_horizon: '1Y',
    recommendation_count: 1, selected_recommendation_count: 1,
    excluded_unavailable_evidence: 0, methodology: 'Immutable originals',
    metrics: Object.fromEntries(['1Y', '2Y', '3Y'].map(h => [h, metric])),
    samples: Object.fromEntries(['1Y', '2Y', '3Y'].map(h => [h, [sample]])),
    winners: [], losers: []
  };
  const html = renderToStaticMarkup(React.createElement(BacktestResults, { run }));
  for (const label of ['1 year', '2 years', '3 years']) assert.ok(html.includes(label));
  for (const label of ['1 week', '1 month', '3 months', '6 months']) {
    assert.doesNotMatch(html, new RegExp(`>${label}<`));
  }
  assert.match(html, /RADAR BACKTEST — LONG-TERM — 1 YEAR/);
  assert.match(html, /LONG-TERM RADAR ACCURACY/);
  assert.match(html, /Not matured/);
});

test('persisted V1 samples normalize into current statuses and counters without undefined values', () => {
  const oldSample = (id, status, date) => ({
    recommendation_id: id, global_instrument_id: `g-${id}`, company_name: `Company ${id}`,
    status, evaluation_date: date, return_pct: status === 'AVAILABLE' ? 8 : null,
    max_adverse_excursion_pct: null, benchmark_excess_return_pct: null
  });
  const oldMetric = {
    recommendation_count: 3, evaluated_count: 1, missing_count: 2,
    hit_rate: 100, average_return: 8, median_return: 8, best_return: 8, worst_return: 8,
    max_adverse_excursion: null, benchmark_excess_return: null
  };
  const bucketSamples = [
    oldSample('evaluated', 'AVAILABLE', '2023-01-01T00:00:00Z'),
    oldSample('future', 'FUTURE_PRICE_UNAVAILABLE', '2024-02-15T00:00:00Z'),
    oldSample('missing', 'ENTRY_PRICE_UNAVAILABLE', '2023-01-01T00:00:00Z')
  ];
  const raw = {
    backtest_id: 'legacy', engine_version: 'BACKTESTING_V1', generated_at: '2024-03-01T00:00:00Z',
    start: '2023-01-01T00:00:00Z', end: '2024-02-29T00:00:00Z', horizon: 'SHORT_TERM',
    recommendation_count: 3, excluded_unavailable_evidence: 1, methodology: 'Recorded evidence',
    metrics: Object.fromEntries(['1W', '1M', '3M', '6M', '1Y'].map(h => [h, oldMetric])),
    samples: Object.fromEntries(['1W', '1M', '3M', '6M', '1Y'].map(h => [h, bucketSamples])),
    winners: [], losers: []
  };
  const normalized = normalizeBacktest(raw);
  const metric = normalized.metrics['1M'];
  assert.deepEqual({
    total: metric.recommendation_count,
    evaluated: metric.evaluated_count,
    notMatured: metric.not_matured_count,
    missing: metric.missing_market_data_count,
    insufficient: metric.insufficient_evidence_count,
    failed: metric.backend_failure_count
  }, { total: 4, evaluated: 1, notMatured: 1, missing: 1, insufficient: 1, failed: 0 });
  assert.equal(
    metric.evaluated_count + metric.not_matured_count + metric.missing_market_data_count
      + metric.outside_evaluation_window_count + metric.insufficient_evidence_count + metric.backend_failure_count,
    metric.recommendation_count
  );
  const html = renderToStaticMarkup(React.createElement(BacktestResults, { run: raw }));
  assert.match(html, /Evaluation due/);
  assert.match(html, /Market data unavailable/);
  assert.match(html, /Not matured/);
  assert.doesNotMatch(html, /Future price unavailable/i);
  assert.doesNotMatch(html, /undefined/i);
});

test('backtesting page shows N/A, never a misleading 0%, when nothing is evaluated yet', () => {
  const zeroMetric = {
    recommendation_count: 1, evaluated_count: 0, not_matured_count: 1, missing_market_data_count: 0,
    outside_evaluation_window_count: 0, insufficient_evidence_count: 0, backend_failure_count: 0,
    hit_rate: null, average_return: null, median_return: null, best_return: null, worst_return: null,
    max_adverse_excursion: null, benchmark_excess_return: null
  };
  const run = {
    backtest_id: 'bt2', generated_at: '2024-03-01T00:00:00Z', start: '2024-01-01T00:00:00Z',
    end: '2024-02-01T00:00:00Z', horizon: 'SHORT_TERM', recommendation_count: 1,
    excluded_unavailable_evidence: 0, methodology: 'Recorded evidence',
    metrics: Object.fromEntries(['1W', '1M', '3M', '6M', '1Y'].map(h => [h, zeroMetric])),
    samples: Object.fromEntries(['1W', '1M', '3M', '6M', '1Y'].map(h => [h, [{
      recommendation_id: 'r1', global_instrument_id: 'g1', company_name: 'Acme', status: 'NOT_MATURED',
      evaluation_date: '2024-01-01T00:00:00Z', required_evaluation_date: '2024-02-01T00:00:00Z',
      return_pct: null, max_adverse_excursion_pct: null, benchmark_excess_return_pct: null
    }]])),
    winners: [], losers: []
  };
  const html = renderToStaticMarkup(React.createElement(BacktestResults, { run }));
  assert.match(html, /Not enough evaluated recommendations/);
  assert.doesNotMatch(html, /\b0\.00%/);
  assert.doesNotMatch(html, /\bNaN\b/);
});

test('backtest status mapping renders friendly labels for every known status, never a raw enum string', () => {
  const baseSample = {
    recommendation_id: 'r1', global_instrument_id: 'g1', company_name: 'Acme',
    evaluation_date: '2024-01-01T00:00:00Z', required_evaluation_date: '2024-02-01T00:00:00Z',
    return_pct: null, max_adverse_excursion_pct: null, benchmark_excess_return_pct: null
  };
  const statuses = ['EVALUATED', 'NOT_MATURED', 'MISSING_MARKET_DATA', 'OUTSIDE_EVALUATION_WINDOW', 'INSUFFICIENT_EVIDENCE', 'BACKEND_FAILURE'];
  const friendly = ['Evaluated', 'Not matured', 'Market data unavailable', 'Outside selected window', 'Insufficient evidence', 'Evaluation failed'];
  const emptyMetric = {
    recommendation_count: statuses.length, evaluated_count: 1, not_matured_count: 1, missing_market_data_count: 1,
    outside_evaluation_window_count: 1, insufficient_evidence_count: 1, backend_failure_count: 1,
    hit_rate: null, average_return: null, median_return: null, best_return: null, worst_return: null,
    max_adverse_excursion: null, benchmark_excess_return: null
  };
  const samples = statuses.map((status, i) => ({ ...baseSample, recommendation_id: `r${i}`, status }));
  const run = {
    backtest_id: 'bt3', generated_at: '2024-03-01T00:00:00Z', start: '2024-01-01T00:00:00Z',
    end: '2024-02-01T00:00:00Z', horizon: 'SHORT_TERM', recommendation_count: statuses.length,
    excluded_unavailable_evidence: 1, methodology: 'Recorded evidence',
    metrics: Object.fromEntries(['1W', '1M', '3M', '6M', '1Y'].map(h => [h, emptyMetric])),
    samples: Object.fromEntries(['1W', '1M', '3M', '6M', '1Y'].map(h => [h, samples])),
    winners: [], losers: []
  };
  const html = renderToStaticMarkup(React.createElement(BacktestResults, { run }));
  for (const label of friendly) assert.ok(html.includes(label), `expected friendly label "${label}"`);
  for (const raw of statuses) {
    if (raw === 'EVALUATED') continue; // "Evaluated" the friendly label legitimately contains this substring's casing difference only
    assert.doesNotMatch(html, new RegExp(raw.replace(/_/g, '_')), `raw enum "${raw}" leaked into rendered output`);
  }
  const normalized = normalizeBacktest(run);
  const metric = normalized.metrics['1M'];
  assert.equal(metric.recommendation_count, statuses.length);
  assert.equal(
    metric.evaluated_count + metric.not_matured_count + metric.missing_market_data_count
      + metric.outside_evaluation_window_count + metric.insufficient_evidence_count + metric.backend_failure_count,
    metric.recommendation_count
  );
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
