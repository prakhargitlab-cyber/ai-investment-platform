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

const etfRadarUrl = new URL('../app/components/etf-radar.tsx', import.meta.url);
const etfRadarSource = fs.readFileSync(etfRadarUrl, 'utf8');
const { EtfRadarContent } = loadLocalModule(etfRadarUrl);

function etfCandidate(overrides = {}) {
  return {
    global_instrument_id: 'id-1', symbol: 'NIFTYBEES', disposition: 'EVALUATED', reason: 'EVALUATED',
    subtype: 'EQUITY_INDEX', rule_engine_result: null, risk_gates: [], recommendation: 'OPPORTUNITY', rank: 1,
    peer_group: 'EQUITY_INDEX',
    ...overrides,
  };
}

test('ETF Radar has its own 3-column analysis area, deliberately never using BASELINE/DEEP terminology', () => {
  assert.match(etfRadarSource, /function EtfAnalysisPanels/);
  assert.match(etfRadarSource, /aria-label="Fund universe and coverage"/);
  assert.match(etfRadarSource, /aria-label="Research outcomes"/);
  assert.match(etfRadarSource, /aria-label="Category coverage"/);
  assert.match(etfRadarSource, /aria-label="Failure reason distribution"/);
  assert.match(etfRadarSource, /aria-label="Research results and publication status"/);
  // Never copies Equity Radar's own two-stage pipeline vocabulary.
  assert.doesNotMatch(etfRadarSource, /\bBASELINE\b|\bbaseline_completed\b|\bdeep_completed\b|\bdeep_ready\b/);
});

test('EtfAnalysisPanels never imports from opportunity-radar.tsx and uses its own local donut', () => {
  assert.doesNotMatch(etfRadarSource, /from ["']\.\/opportunity-radar["']/);
  assert.match(etfRadarSource, /function EtfDonut\(/);
  assert.match(etfRadarSource, /function EtfDonutLegend\(/);
});

test('EtfAnalysisPanels is additive -- wired into the overview tab alongside the existing summary tiles', () => {
  assert.match(etfRadarSource, /<EtfAnalysisPanels data={data} evaluated={evaluated} eligible={eligible} dataGaps={dataGaps} failures={failures} excluded={excluded} coveragePct={coveragePct} leaders={leaders} categories={categories} \/>/);
});

test('ETF research-outcomes donut segments are a genuine partition of evaluated funds', () => {
  const data = {
    cycle_id: 'c1', as_of: '2026-10-11T12:00:00Z', universe_count: 5,
    candidates: [
      etfCandidate({ global_instrument_id: 'a', recommendation: 'OPPORTUNITY', disposition: 'EVALUATED' }),
      etfCandidate({ global_instrument_id: 'b', recommendation: 'INSUFFICIENT_DATA', disposition: 'INSUFFICIENT_DATA' }),
      etfCandidate({ global_instrument_id: 'c', recommendation: null, disposition: 'TECHNICAL_FAILURE' }),
      etfCandidate({ global_instrument_id: 'd', recommendation: null, disposition: 'EVALUATED' }),
      etfCandidate({ global_instrument_id: 'e', recommendation: null, disposition: 'INELIGIBLE' }),
    ],
    ranked: [etfCandidate({ global_instrument_id: 'a', recommendation: 'OPPORTUNITY' })],
    excluded_by_reason: { FAILED_NSE_ETF_ACTIVE_PREFILTER: 1 },
  };
  const html = renderToStaticMarkup(React.createElement(EtfRadarContent, { data, initialTab: 'overview' }));
  assert.match(html, /aria-label="Fund universe and coverage"/);
  assert.match(html, /aria-label="Research outcomes"/);
  assert.match(html, /FAILED NSE ETF ACTIVE PREFILTER/);
  assert.match(html, /A genuine partition of \d+ evaluated funds/);
});

test('ETF analysis panels render category leaders from real peer_group ranks, never fabricated', () => {
  const data = {
    cycle_id: 'c1', as_of: '2026-10-11T12:00:00Z', universe_count: 2,
    candidates: [etfCandidate({ global_instrument_id: 'a', peer_group: 'GOLD', rank: 1 }), etfCandidate({ global_instrument_id: 'b', peer_group: 'DEBT', rank: 1 })],
    ranked: [etfCandidate({ global_instrument_id: 'a', peer_group: 'GOLD', rank: 1 }), etfCandidate({ global_instrument_id: 'b', peer_group: 'DEBT', rank: 1 })],
    excluded_by_reason: {},
  };
  const html = renderToStaticMarkup(React.createElement(EtfRadarContent, { data, initialTab: 'overview' }));
  assert.match(html, /Category coverage/);
  assert.match(html, /Peer categories<\/dt><dd>2/);
  assert.match(html, /Category leaders<\/dt><dd>2/);
});
