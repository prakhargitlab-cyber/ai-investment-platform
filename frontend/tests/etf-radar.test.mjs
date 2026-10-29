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
  assert.match(html, /No ETF instruments were evaluated/);
  assert.doesNotMatch(html, /No ETF currently ranks/);
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
    initialTab: 'screener',
  }));
  assert.equal((html.match(/class="opportunity-card etf-radar-card"/g) ?? []).length, 2);
  assert.match(html, /GOLDBEES/);
});

test('Dashboard embeds still use RadarTabs; the dedicated Equity/ETF Radar workspaces use their own components directly, never duplicated', () => {
  // Dashboard-level embeds (the loading/error fallback and the compact
  // shortcut inside DashboardView / MultiPortfolioDashboard) are unchanged --
  // still RadarTabs, so the Equity/ETF tab switch inside the Dashboard embed
  // behaves exactly as it always did.
  assert.match(workspaceSource, /import \{ RadarTabs \} from "\.\/radar-tabs";/);
  const radarTabsCallSites = workspaceSource.match(/<RadarTabs\b/g) ?? [];
  assert.ok(radarTabsCallSites.length >= 3, `expected >=3 RadarTabs call sites, found ${radarTabsCallSites.length}`);

  // The dedicated "Equity Radar" / "ETF Radar" sidebar workspaces (added so
  // each radar is a first-class, directly navigable page rather than only
  // reachable through the Dashboard embed) render OpportunityRadar/EtfRadar
  // directly -- exactly once each, never re-wrapped in another RadarTabs
  // switcher (that would nest a redundant Equity/ETF toggle inside a
  // sidebar entry that already made the choice).
  assert.match(workspaceSource, /import \{ OpportunityRadar \} from "\.\/opportunity-radar";/);
  assert.match(workspaceSource, /import \{ EtfRadar \} from "\.\/etf-radar";/);
  const opportunityRadarCallSites = workspaceSource.match(/<OpportunityRadar\b/g) ?? [];
  assert.equal(opportunityRadarCallSites.length, 1, `expected exactly 1 direct <OpportunityRadar> call site, found ${opportunityRadarCallSites.length}`);
  const etfRadarCallSites = workspaceSource.match(/<EtfRadar\b/g) ?? [];
  assert.equal(etfRadarCallSites.length, 1, `expected exactly 1 direct <EtfRadar> call site, found ${etfRadarCallSites.length}`);
  assert.match(workspaceSource, /view === "equity-radar" \?/);
  assert.match(workspaceSource, /view === "etf-radar" \?/);
});

test('RadarTabs is a lightweight Dashboard summary: one-shot reads, no full radar terminal mounted', () => {
  // Dashboard cards must stay useful without pulling in OpportunityRadar's/
  // EtfRadar's full tabbed research terminals (search, filters, run/cancel
  // controls, continuous 2.5s polling) -- that belongs only to the
  // dedicated, directly-navigable Equity/ETF Radar pages.
  assert.doesNotMatch(radarTabsSource, /import \{ OpportunityRadar \} from "\.\/opportunity-radar";/);
  assert.doesNotMatch(radarTabsSource, /import \{ EtfRadar \} from "\.\/etf-radar";/);
  assert.match(radarTabsSource, /Equity Radar/);
  assert.match(radarTabsSource, /ETF Radar/);
  assert.match(radarTabsSource, /Open Equity Radar/);
  assert.match(radarTabsSource, /Open ETF Radar/);
});

test('RadarTabs polls boundedly only while a cycle is running, stops at terminal, and cleans up its timer on unmount', () => {
  // Bounded self-scheduling poll (setTimeout re-armed from inside its own
  // .then, exactly like the dedicated Radar pages' own pattern), never a
  // raw setInterval that would keep firing after unmount or after terminal.
  assert.match(radarTabsSource, /pollTimer = useRef<ReturnType<typeof setTimeout> \| null>\(null\);/);
  assert.doesNotMatch(radarTabsSource, /setInterval/);
  // Polling stops once the cycle reaches a terminal status.
  assert.match(radarTabsSource, /if \(isTerminalRadarStatus\(value\.status\)\) \{/);
  // Timer is cleared on unmount via the effect cleanup.
  assert.match(radarTabsSource, /return \(\) => \{ active = false; if \(pollTimer\.current\) clearTimeout\(pollTimer\.current\); \};/);
  // Slower than the dedicated page's own 2.5s poll, to reduce (not fully
  // eliminate) the chance both race on the exact same tick when open together.
  assert.match(radarTabsSource, /DASHBOARD_RADAR_SUMMARY_POLL_MS = 6000/);
});

test('RadarTabs reuses the Radar type/normalizer and the existing lightweight status endpoints, never a bespoke heavy fetch', () => {
  assert.match(radarTabsSource, /import \{ normalizeRadar, type Radar \} from "\.\/opportunity-radar";/);
  assert.match(radarTabsSource, /import type \{ EtfCycleResult \} from "\.\/etf-radar";/);
  assert.match(radarTabsSource, /\/api\/v1\/research\/opportunities\/current/);
  assert.match(radarTabsSource, /\/api\/v1\/etf-radar\/current/);
  assert.match(radarTabsSource, /\/api\/v1\/research\/opportunities\/cycles\/\$\{cycleId\}\/status/);
  assert.match(radarTabsSource, /\/api\/v1\/etf-radar\/cycles\/\$\{cycleId\}\/status/);
});

test('RadarTabs navigates to the dedicated pages via callbacks rather than mounting them inline', () => {
  assert.match(radarTabsSource, /onOpenEquityRadar/);
  assert.match(radarTabsSource, /onOpenEtfRadar/);
});

test('switching tabs cannot target the wrong radar\'s refresh action: "Run full NSE ETF Radar" is scoped to etf-radar.tsx only', () => {
  const etfRadarSource = fs.readFileSync(new URL('../app/components/etf-radar.tsx', import.meta.url), 'utf8');
  const opportunityRadarSource = fs.readFileSync(new URL('../app/components/opportunity-radar.tsx', import.meta.url), 'utf8');
  assert.match(etfRadarSource, /Run full NSE ETF Radar/);
  assert.doesNotMatch(radarTabsSource, /Run full NSE ETF Radar/);
  assert.doesNotMatch(opportunityRadarSource, /Run full NSE ETF Radar/);
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


for (const [disposition, reason, message] of [
  ['INSUFFICIENT_DATA', 'MISSING_USABLE_PRICE', /data prerequisites are missing/],
  ['TECHNICAL_FAILURE', 'MARKET_DATA_LOOKUP_FAILED', /technical failures/],
  ['INELIGIBLE', 'FAILED_NSE_ETF_ACTIVE_PREFILTER', /Review the evaluation and exclusion counts/],
]) {
  test(`empty ETF rankings distinguish ${disposition} and expose reasons`, () => {
    const dataShape = {
      cycle_id: 'legacy', as_of: '2026-10-11T12:00:00Z',
      candidates: [candidate({ disposition, reason, recommendation: null })], ranked: [],
      excluded_by_reason: { [reason]: 1 },
    };
    const overviewHtml = renderToStaticMarkup(React.createElement(EtfRadarContent, { data: dataShape }));
    assert.match(overviewHtml, message);
    assert.match(overviewHtml, /Universe size<\/dt><dd>1/);
    assert.match(overviewHtml, /Evaluated ETFs<\/dt><dd>0/);
    assert.doesNotMatch(overviewHtml, /No ETF currently ranks as a qualifying opportunity/);
    const qualityHtml = renderToStaticMarkup(React.createElement(EtfRadarContent, { data: dataShape, initialTab: 'quality' }));
    assert.match(qualityHtml, new RegExp(reason));
  });
}

test('ETF ranks are not represented as qualifying opportunities', () => {
  const item = candidate({ recommendation: 'INSUFFICIENT_DATA' });
  const html = renderToStaticMarkup(React.createElement(EtfRadarContent, { data: {
    cycle_id: 'c1', as_of: '2026-10-11T12:00:00Z', candidates: [item], ranked: [item], excluded_by_reason: {},
  } }));
  assert.match(html, /Evaluated ETFs<\/dt><dd>1/);
  assert.match(html, /Opportunity recommendations<\/dt><dd>0/);
  assert.match(html, /Insufficient data<\/dt><dd>1/);
  assert.match(html, /Ranked ETFs<\/dt><dd>1/);
  assert.doesNotMatch(html, /Top ETF opportunities/);
});

test('Full universe coverage and peer ranks remain distinct from shortlist size', () => {
  const item = candidate({ peer_group: 'GOLD' });
  const dataShape = {
    cycle_id: 'full', as_of: '2026-10-11T12:00:00Z', scope: 'FULL_OFFICIAL_NSE_ETF',
    universe_count: 352, candidates: [item], ranked: [item], excluded_by_reason: {},
    coverage: { official: 352, attempted: 352, acquired: 351, ranked: 1, shortlisted: 1 },
  };
  const overviewHtml = renderToStaticMarkup(React.createElement(EtfRadarContent, { data: dataShape }));
  assert.match(overviewHtml, /Rankings compare verified peers only/);
  const qualityHtml = renderToStaticMarkup(React.createElement(EtfRadarContent, { data: dataShape, initialTab: 'quality' }));
  assert.match(qualityHtml, /official<\/dt><dd>352/);
  const screenerHtml = renderToStaticMarkup(React.createElement(EtfRadarContent, { data: dataShape, initialTab: 'screener' }));
  assert.match(screenerHtml, /GOLD: peer rank 1/);
});

test('Regular (full-universe) ETF action and the bounded admin action each target only their own endpoint', () => {
  const source = fs.readFileSync(new URL('../app/components/etf-radar.tsx', import.meta.url), 'utf8');
  // The full-universe runCycle() action still only ever posts to its own endpoint.
  const runCycleBody = source.slice(source.indexOf('const runCycle = ()'), source.indexOf('const running ='));
  assert.match(runCycleBody, /etf-radar\/full-universe-cycles/);
  assert.doesNotMatch(runCycleBody, /controlled-cycles/);
  assert.doesNotMatch(source, /top_n: 10/);
  // The separate bounded admin action (checkBoundedPreflight/submitBoundedCycle)
  // only ever uses /preflight and /controlled-cycles, never full-universe-cycles.
  const boundedBody = source.slice(source.indexOf('const checkBoundedPreflight ='), source.indexOf('const terminalData ='));
  assert.match(boundedBody, /etf-radar\/preflight/);
  assert.match(boundedBody, /etf-radar\/controlled-cycles/);
  assert.doesNotMatch(boundedBody, /full-universe-cycles/);
});


test('ETF opportunity cards disclose gaps, valuation dates and actual source references', () => {
  const html = renderToStaticMarkup(React.createElement(EtfRadarCard, { item: candidate({
    missing_data: ['TRACKING_QUALITY'], limitations: ['Corporate-action adjustments unavailable'],
    source_references: [{metric:'NAV',value:'100',as_of:'2026-10-09',provider:'NSE',url:'https://www.nseindia.com/'}],
  }) }));
  assert.match(html, /TRACKING_QUALITY/);
  assert.match(html, /Corporate-action adjustments unavailable/);
  assert.match(html, /2026-10-09/);
  assert.match(html, /https:\/\/www.nseindia.com/);
});

test('ETF Radar result tabs mount only the active panel -- true conditional unmount, never a CSS-hide pattern', () => {
  const etfRadarSource = fs.readFileSync(new URL('../app/components/etf-radar.tsx', import.meta.url), 'utf8');
  assert.doesNotMatch(etfRadarSource, /hidden=\{tab/);
  const ternaries = etfRadarSource.match(/\{tab === "[a-z]+" \? \(<div className="radar-tab-panel">/g) ?? [];
  assert.equal(ternaries.length, 7, `expected 7 tab ternaries (overview/top/leaders/universe/screener/compare/quality), found ${ternaries.length}`);
  const item = candidate();
  const data = {
    cycle_id: 'c1', as_of: '2026-10-11T12:00:00Z', candidates: [item], ranked: [item],
    top_opportunities: [item], excluded_by_reason: {},
  };
  const overviewHtml = renderToStaticMarkup(React.createElement(EtfRadarContent, { data, initialTab: 'overview' }));
  assert.doesNotMatch(overviewHtml, /class="opportunity-card etf-radar-card"/);
  const topHtml = renderToStaticMarkup(React.createElement(EtfRadarContent, { data, initialTab: 'top' }));
  assert.match(topHtml, /class="opportunity-card etf-radar-card"/);
});
