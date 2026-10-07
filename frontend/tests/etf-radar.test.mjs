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
    // request() is only ever called from inside a useEffect or event
    // handler, never during a synchronous render -- renderToStaticMarkup
    // never runs effects, so an empty stub is safe here (same pattern as
    // tests/opportunity-radar.test.mjs).
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

const { EtfRadarContent, EtfRadarCard } = component('etf-radar');
const radarTabsSource = fs.readFileSync(new URL('../app/components/radar-tabs.tsx', import.meta.url), 'utf8');
const workspaceSource = fs.readFileSync(new URL('../app/components/investment-workspace.tsx', import.meta.url), 'utf8');

function factor(name, overrides = {}) {
  return { factor: name, weight: 10, status: 'SCORED', raw_metric: 'x', normalized_score: 70,
    weighted_contribution: 7, freshness: '2026-09-27', source_provenance: 'NSE', confidence: 1, reason: null,
    ...overrides };
}

function ruleResult(overrides = {}) {
  return {
    rule_engine_version: 'ETF_RULE_ENGINE_V1', instrument_id: 'x', as_of: '2026-09-27T12:00:00Z',
    factor_results: [
      factor('PERFORMANCE_MOMENTUM'), factor('RISK_DRAWDOWN_VOLATILITY'),
      factor('LIQUIDITY', { status: 'UNAVAILABLE', normalized_score: null }),
      factor('TRACKING_QUALITY', { status: 'UNAVAILABLE', normalized_score: null }),
      factor('COST', { status: 'UNAVAILABLE', normalized_score: null }),
      factor('NAV_PREMIUM_DISCOUNT', { status: 'UNAVAILABLE', normalized_score: null }),
      factor('DIVERSIFICATION', { status: 'UNAVAILABLE', normalized_score: null }),
      factor('DATA_COMPLETENESS'),
    ],
    overall_score: 62.5, data_completeness: 0.375, confidence: 'LOW',
    ...overrides,
  };
}

function candidate(overrides = {}) {
  return {
    global_instrument_id: 'id-1', symbol: 'NIFTYBEES', disposition: 'EVALUATED', reason: 'EVALUATED',
    subtype: 'EQUITY_INDEX', rule_engine_result: ruleResult(), risk_gates: [], recommendation: 'OPPORTUNITY', rank: 1,
    ...overrides,
  };
}

test('ETF radar card shows unavailable factors as em-dash, never a fake zero', () => {
  const html = renderToStaticMarkup(React.createElement(EtfRadarCard, { item: candidate() }));
  assert.match(html, /NIFTYBEES/);
  assert.match(html, /EQUITY_INDEX/);
  assert.match(html, /OPPORTUNITY/);
  assert.match(html, /62\.5/);
  // Every UNAVAILABLE factor renders as em-dash, never "0".
  assert.match(html, /—/);
  assert.doesNotMatch(html, />0</);
});

test('ETF radar card never renders stock-only fields', () => {
  const html = renderToStaticMarkup(React.createElement(EtfRadarCard, { item: candidate() }));
  for (const stockOnlyField of ['ROE', 'ROCE', 'PAT', 'Order book', 'Promoter holding']) {
    assert.doesNotMatch(html, new RegExp(stockOnlyField, 'i'));
  }
});

test('ETF radar card surfaces triggered risk gates but hides untriggered ones', () => {
  const item = candidate({
    risk_gates: [
      { gate_id: 'EXTREME_ILLIQUIDITY', category: 'POOR_CHARACTERISTICS', severity: 'BLOCKING', triggered: true, reason: 'Volume floor breached.' },
      { gate_id: 'EXCESSIVE_DRAWDOWN', category: 'POOR_CHARACTERISTICS', severity: 'WARNING', triggered: false, reason: 'n/a' },
    ],
  });
  const html = renderToStaticMarkup(React.createElement(EtfRadarCard, { item }));
  assert.match(html, /EXTREME ILLIQUIDITY/);
  assert.match(html, /Volume floor breached/);
  assert.doesNotMatch(html, /EXCESSIVE DRAWDOWN/);
});

test('ETF radar content renders an empty-ranking message with no cards', () => {
  const html = renderToStaticMarkup(React.createElement(EtfRadarContent, {
    data: { radar_version: 'ETF_RADAR_V1', cycle_id: 'c1', correlation_id: null, as_of: '2026-09-27T12:00:00Z',
      candidates: [], ranked: [], excluded_by_reason: {} },
  }));
  assert.match(html, /No ETF currently ranks/);
  assert.equal((html.match(/class="opportunity-card etf-radar-card"/g) ?? []).length, 0);
});

test('ETF radar content renders one card per ranked candidate', () => {
  const html = renderToStaticMarkup(React.createElement(EtfRadarContent, {
    data: {
      radar_version: 'ETF_RADAR_V1', cycle_id: 'c1', correlation_id: null, as_of: '2026-09-27T12:00:00Z',
      candidates: [candidate(), candidate({ global_instrument_id: 'id-2', symbol: 'GOLDBEES', rank: 2 })],
      ranked: [candidate(), candidate({ global_instrument_id: 'id-2', symbol: 'GOLDBEES', rank: 2 })],
      excluded_by_reason: {},
    },
  }));
  assert.equal((html.match(/class="opportunity-card etf-radar-card"/g) ?? []).length, 2);
  assert.match(html, /GOLDBEES/);
});

test('Dashboard tabs: RadarTabs is used in place of OpportunityRadar at every Equity Radar call site', () => {
  assert.match(workspaceSource, /import \{ RadarTabs \} from "\.\/radar-tabs";/);
  assert.doesNotMatch(workspaceSource, /import \{ OpportunityRadar \} from "\.\/opportunity-radar";/);
  // Every former <OpportunityRadar ... /> render call site is now a RadarTabs call.
  assert.doesNotMatch(workspaceSource, /<OpportunityRadar\b/);
  const radarTabsCallSites = workspaceSource.match(/<RadarTabs\b/g) ?? [];
  assert.ok(radarTabsCallSites.length >= 3, `expected >=3 RadarTabs call sites, found ${radarTabsCallSites.length}`);
});

test('RadarTabs renders both tab buttons and defaults to the Equity Radar tab', () => {
  assert.match(radarTabsSource, /Equity Radar/);
  assert.match(radarTabsSource, /ETF Radar/);
  assert.match(radarTabsSource, /useState<"equity" \| "etf">\("equity"\)/);
  // Equity Radar's own component is imported unmodified, never re-implemented here.
  assert.match(radarTabsSource, /import \{ OpportunityRadar \} from "\.\/opportunity-radar";/);
  assert.match(radarTabsSource, /import \{ EtfRadar \} from "\.\/etf-radar";/);
});

// -- Dashboard tab isolation closure -----------------------------------------
//
// This repo's frontend test harness (ts.transpileModule + renderToStaticMarkup)
// never runs React effects or state updates -- renderToStaticMarkup is a pure,
// one-shot synchronous render, so it cannot drive a live loading -> ready/error
// transition or a real tab switch. These tests instead prove the STRUCTURAL
// guarantee that makes cross-tab leakage impossible in the first place:
// RadarTabs mounts exactly one radar component at a time via a plain ternary
// (never both, never a CSS-hidden second copy), so each tab's loading/error/
// result state is that component's own useState -- destroyed on unmount and
// freshly re-initialized on remount, with nothing shared in between.

test('RadarTabs mounts exactly one radar at a time via a ternary, never a CSS-hide pattern', () => {
  const ternaries = radarTabsSource.match(/\{tab === "equity" \? <OpportunityRadar[\s\S]*?: <EtfRadar \/>\}/g) ?? [];
  assert.equal(ternaries.length, 1, 'expected exactly one equity/etf render ternary');
  // Never both rendered simultaneously with visibility toggled by CSS/style
  // (that would leak state across tabs even while visually hidden).
  assert.doesNotMatch(radarTabsSource, /display:\s*none/);
  assert.doesNotMatch(radarTabsSource, /hidden=\{tab/);
});

test('RadarTabs holds no loading/error/result state of its own that could leak between tabs', () => {
  // The only state RadarTabs itself owns is which tab is selected; all
  // loading/error/result state lives inside OpportunityRadar/EtfRadar,
  // which RadarTabs never reads or threads between each other.
  const stateDeclarations = radarTabsSource.match(/useState(<[^>]*>)?\(/g) ?? [];
  assert.equal(stateDeclarations.length, 1, `expected exactly one useState in RadarTabs, found ${stateDeclarations.length}`);
  assert.match(radarTabsSource, /useState<"equity" \| "etf">\("equity"\)/);
});

test('switching tabs cannot target the wrong radar\'s refresh action: "Run ETF radar" is scoped to etf-radar.tsx only', () => {
  const etfRadarSource = fs.readFileSync(new URL('../app/components/etf-radar.tsx', import.meta.url), 'utf8');
  const opportunityRadarSource = fs.readFileSync(new URL('../app/components/opportunity-radar.tsx', import.meta.url), 'utf8');
  assert.match(etfRadarSource, /Run ETF radar/);
  assert.doesNotMatch(radarTabsSource, /Run ETF radar/);
  assert.doesNotMatch(opportunityRadarSource, /Run ETF radar/);
  // And the ETF run action calls only the ETF-radar endpoints, never the
  // Equity opportunities endpoints.
  assert.doesNotMatch(etfRadarSource, /\/api\/v1\/research\/opportunities/);
});

test('an ETF radar card never renders a literal "0" for any unavailable factor, across every factor label', () => {
  const allUnavailable = ruleResult({
    factor_results: [
      'PERFORMANCE_MOMENTUM', 'RISK_DRAWDOWN_VOLATILITY', 'LIQUIDITY', 'TRACKING_QUALITY',
      'COST', 'NAV_PREMIUM_DISCOUNT', 'DIVERSIFICATION', 'DATA_COMPLETENESS',
    ].map((name) => factor(name, { status: 'UNAVAILABLE', normalized_score: null, weighted_contribution: null })),
    overall_score: null, data_completeness: 0, confidence: 'LOW',
  });
  const html = renderToStaticMarkup(React.createElement(EtfRadarCard, { item: candidate({ rule_engine_result: allUnavailable }) }));
  // Every one of the 7 displayed factor rows (not the separately-computed,
  // honestly-zero "Data completeness" summary line, which legitimately is
  // 0% when every factor is unavailable) must show an em-dash, never "0".
  const factorsBlock = html.match(/etf-radar-factors">([\s\S]*?)<\/dl>/)[1];
  assert.doesNotMatch(factorsBlock, />0</);
  const dashCount = (factorsBlock.match(/—/g) ?? []).length;
  assert.equal(dashCount, 7);
});

test('EtfRadar never imports or re-implements OpportunityRadar\'s equity-only fields', () => {
  const etfRadarSource = fs.readFileSync(new URL('../app/components/etf-radar.tsx', import.meta.url), 'utf8');
  assert.doesNotMatch(etfRadarSource, /from "\.\/opportunity-radar"/);
  assert.doesNotMatch(etfRadarSource, /\broe\b|\brocE\b|order[_-]?book|promoterHolding|promoter_holding/i);
});
