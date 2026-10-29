import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import ts from 'typescript';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const require = createRequire(import.meta.url);
const moduleCache = new Map();

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

const { EtfRadarContent } = component('etf-radar');

function ruleResult(overrides = {}) {
  return {
    rule_engine_version: 'ETF_RULE_ENGINE_V1', instrument_id: 'x', as_of: '2026-09-27T12:00:00Z',
    factor_results: [], overall_score: 62.5, data_completeness: 0.375, confidence: 'LOW',
    ...overrides,
  };
}

function candidate(overrides = {}) {
  return {
    global_instrument_id: 'id-1', symbol: 'NIFTYBEES', disposition: 'EVALUATED', reason: 'EVALUATED',
    subtype: 'EQUITY_INDEX', peer_group: 'EQUITY_INDEX', rule_engine_result: ruleResult(), risk_gates: [],
    recommendation: 'OPPORTUNITY', rank: 1,
    ...overrides,
  };
}

// A representative cycle: 1 ranked (qualified) ETF, plus several unranked/
// insufficient-data/technical-failure ETFs -- mirrors the reported
// production shape (357 universe, 345 evaluated, 1 ranked).
function universeCycle() {
  const goldbees = candidate({
    global_instrument_id: 'id-goldbees', symbol: 'GOLDBEES', peer_group: 'GOLD', rank: 1,
    recommendation: 'OPPORTUNITY',
    historical_acquisition: { status: 'PROVIDER_UNAVAILABLE', reason: 'ETF_HISTORY_LISTING_NOT_VERIFIED', observations: 59 },
  });
  const niftybees = candidate({
    global_instrument_id: 'id-niftybees', symbol: 'NIFTYBEES', peer_group: 'EQUITY_INDEX', rank: null,
    recommendation: null, disposition: 'INSUFFICIENT_DATA', reason: 'INSUFFICIENT_ANALYTICAL_DATA',
    rule_engine_result: null, missing_data: ['TRACKING_QUALITY'], limitations: ['Price history incomplete.'],
  });
  const bankbees = candidate({
    global_instrument_id: 'id-bankbees', symbol: 'BANKBEES', peer_group: 'EQUITY_SECTOR', rank: null,
    recommendation: null, disposition: 'TECHNICAL_FAILURE', reason: 'BASELINE_ACQUISITION_TECHNICAL_FAILURE',
    rule_engine_result: null,
  });
  const unclassified = candidate({
    global_instrument_id: 'id-unclassified', symbol: 'UNKNOWNETF', peer_group: undefined, rank: null,
    recommendation: null, disposition: 'EVALUATED', reason: 'EVALUATED', rule_engine_result: ruleResult({ overall_score: 40 }),
  });
  return {
    radar_version: 'ETF_RADAR_V1', cycle_id: 'c1', correlation_id: null, as_of: '2026-10-28T12:00:00Z',
    universe_count: 4, candidates: [goldbees, niftybees, bankbees, unclassified], ranked: [goldbees],
    top_opportunities: [goldbees], excluded_by_reason: { INSUFFICIENT_ANALYTICAL_DATA: 1, BASELINE_ACQUISITION_TECHNICAL_FAILURE: 1 },
  };
}

test('1. Research Universe tab makes every candidate accessible, including insufficient-data and unranked ones', () => {
  const html = renderToStaticMarkup(React.createElement(EtfRadarContent, { data: universeCycle(), initialTab: 'universe' }));
  assert.match(html, /GOLDBEES/);
  assert.match(html, /NIFTYBEES/);
  assert.match(html, /BANKBEES/);
  assert.match(html, /UNKNOWNETF/);
  assert.match(html, /4 of 4 result/);
});

test('2. An unranked ETF (NIFTYBEES, rank: null) is selectable for comparison in the Research Universe tab', () => {
  const html = renderToStaticMarkup(React.createElement(EtfRadarContent, { data: universeCycle(), initialTab: 'universe' }));
  assert.match(html, /aria-label="Compare NIFTYBEES"/);
  assert.doesNotMatch(html, /aria-label="Compare NIFTYBEES"[^>]*disabled/);
});

test('3. Comparison resolves against data.candidates and includes up to 5 ETFs, not just data.ranked', () => {
  const source = fs.readFileSync(new URL('../app/components/etf-radar.tsx', import.meta.url), 'utf8');
  assert.match(source, /const compared = data\.candidates\.filter/);
  assert.doesNotMatch(source, /const compared = data\.ranked\.filter/);
  assert.match(source, /prev\.length < 5 \? \[\.\.\.prev, id\] : prev/);
});

test('4. Qualified Opportunities (Top Opportunities / Category Leaders) remain restricted to ranked candidates', () => {
  const data = universeCycle();
  const topHtml = renderToStaticMarkup(React.createElement(EtfRadarContent, { data, initialTab: 'top' }));
  assert.match(topHtml, /GOLDBEES/);
  assert.doesNotMatch(topHtml, /NIFTYBEES/);
  assert.doesNotMatch(topHtml, /BANKBEES/);
  const leadersHtml = renderToStaticMarkup(React.createElement(EtfRadarContent, { data, initialTab: 'leaders' }));
  assert.match(leadersHtml, /GOLDBEES/);
  assert.doesNotMatch(leadersHtml, /NIFTYBEES/);
});

test('5. Missing metrics render as "Unavailable" (the house em-dash convention), never a fabricated value', () => {
  const data = universeCycle();
  const html = renderToStaticMarkup(React.createElement(EtfRadarContent, { data, initialTab: 'universe' }));
  assert.match(html, /—/);
  assert.doesNotMatch(html, />0</);
  assert.doesNotMatch(html, />0%</);
});

test('6. No recommendation is fabricated from incomplete evidence: insufficient-data/technical-failure rows show their honest status', () => {
  const data = universeCycle();
  const html = renderToStaticMarkup(React.createElement(EtfRadarContent, { data, initialTab: 'universe' }));
  assert.match(html, /Insufficient data/);
  assert.match(html, /Technical failure/);
  assert.match(html, /Qualified opportunity/);
  assert.doesNotMatch(html, /NIFTYBEES[\s\S]{0,400}Qualified opportunity/);
  assert.doesNotMatch(html, /BANKBEES[\s\S]{0,400}Qualified opportunity/);
});

test('7. Existing Top Opportunities and Category Leaders behavior is unchanged (still ranked-only, unaffected by the new tab)', () => {
  const data = universeCycle();
  const topHtml = renderToStaticMarkup(React.createElement(EtfRadarContent, { data, initialTab: 'top' }));
  assert.match(topHtml, /Top opportunities/);
  assert.equal((topHtml.match(/class="opportunity-card etf-radar-card"/g) ?? []).length, 1);
  const leadersHtml = renderToStaticMarkup(React.createElement(EtfRadarContent, { data, initialTab: 'leaders' }));
  assert.match(leadersHtml, /Category leaders/);
  assert.match(leadersHtml, /GOLD/);
});

test('Research Universe category filter includes an explicit "Unclassified" bucket for a missing peer_group', () => {
  const html = renderToStaticMarkup(React.createElement(EtfRadarContent, { data: universeCycle(), initialTab: 'universe' }));
  assert.match(html, /<option value="Unclassified">Unclassified<\/option>/);
});

test('Historical acquisition diagnostics (status/reason) are surfaced per-row in the Research Universe tab', () => {
  const html = renderToStaticMarkup(React.createElement(EtfRadarContent, { data: universeCycle(), initialTab: 'universe' }));
  assert.match(html, /PROVIDER_UNAVAILABLE \(ETF_HISTORY_LISTING_NOT_VERIFIED\)/);
});

test('ETF comparison table distinguishes qualified recommendations from incomplete research candidates via "Research status"', () => {
  const source = fs.readFileSync(new URL('../app/components/etf-radar.tsx', import.meta.url), 'utf8');
  assert.match(source, /\["Research status", \(c\) => researchStatusLabel\(c\)\]/);
});

test('Evaluation-coverage wording does not imply full evidence completeness', () => {
  const html = renderToStaticMarkup(React.createElement(EtfRadarContent, { data: universeCycle() }));
  assert.match(html, /of the universe evaluated/);
  assert.doesNotMatch(html, /coverage \d+% evaluated/);
});

test('EtfRadarCard distinguishes persisted subtype from current verified peer group', () => {
  const html = renderToStaticMarkup(React.createElement(EtfRadarContent, { data: universeCycle(), initialTab: 'universe' }));
  assert.match(html, /Subtype: EQUITY_INDEX/);
  assert.match(html, /Peer group: EQUITY_INDEX/);
});

test('Large metric values render without raw floating-point artifacts (maximumFractionDigits applied)', () => {
  const source = fs.readFileSync(new URL('../app/components/etf-radar.tsx', import.meta.url), 'utf8');
  assert.match(source, /maximumFractionDigits: 2/);
});
