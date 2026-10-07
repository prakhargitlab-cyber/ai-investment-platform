import assert from "node:assert/strict";
import fs from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const require = createRequire(import.meta.url);
const componentUrl = new URL("../app/components/portfolio-radar-signal.tsx", import.meta.url);
const componentSource = fs.readFileSync(componentUrl, "utf8");
const output = ts.transpileModule(componentSource, {
  compilerOptions: {
    jsx: ts.JsxEmit.ReactJSX,
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2022
  }
}).outputText;
const loaded = { exports: {} };
new Function("require", "module", "exports", output)(require, loaded, loaded.exports);
const { PortfolioRadarCompanyName, portfolioRadarPresentation } = loaded.exports;

const workspace = fs.readFileSync(new URL("../app/components/investment-workspace.tsx", import.meta.url), "utf8");
const api = fs.readFileSync(new URL("../app/lib/portfolio-api.ts", import.meta.url), "utf8");
const styles = fs.readFileSync(new URL("../app/styles.css", import.meta.url), "utf8");

function signal(shortTermRecommendation, longTermRecommendation = null) {
  return {
    globalInstrumentId: "gid-1",
    shortTermRecommendation,
    longTermRecommendation,
    recommendationAt: "2026-10-01T12:00:00Z",
    evaluatedAt: "2026-10-01T12:00:00Z",
    recommendationId: "recommendation-1",
    cycleId: "cycle-1",
    source: "RECOMMENDATION_CURRENT_STATE"
  };
}

for (const [recommendation, tone] of [
  ["BUY", "buy"],
  ["STRONG_BUY", "strong-buy"],
  ["HOLD", "hold"],
  ["PARTIAL_EXIT", "partial-exit"],
  ["FULL_EXIT", "full-exit"]
]) {
  test(`${recommendation} maps to its semantic portfolio stock-name tone`, () => {
    const presentation = portfolioRadarPresentation(signal(recommendation));
    assert.equal(presentation.recommendation, recommendation);
    assert.equal(presentation.className, `portfolio-radar-name portfolio-radar-${tone}`);
    assert.match(styles, new RegExp(`\\.portfolio-radar-${tone} \\{ color: var\\(--radar-${tone}\\); \\}`));
  });
}

test("no recommendation keeps the default company-name styling", () => {
  assert.deepEqual(portfolioRadarPresentation(null), {
    recommendation: null,
    className: "",
    detail: null,
    conflicting: false
  });
  const html = renderToStaticMarkup(React.createElement(PortfolioRadarCompanyName, { companyName: "ABC Ltd" }));
  assert.doesNotMatch(html, /portfolio-radar-/);
  assert.match(html, />ABC Ltd<\/strong>/);
});

test("matching short-term and long-term recommendations use their normal tone", () => {
  const presentation = portfolioRadarPresentation(signal("STRONG_BUY", "STRONG_BUY"));
  assert.equal(presentation.recommendation, "STRONG_BUY");
  assert.equal(presentation.conflicting, false);
  assert.match(presentation.detail, /Short-term: Strong Buy/);
  assert.match(presentation.detail, /Long-term: Strong Buy/);
});

test("conflicting horizons remain neutral and expose both meanings", () => {
  const presentation = portfolioRadarPresentation(signal("HOLD", "STRONG_BUY"));
  assert.equal(presentation.recommendation, null);
  assert.equal(presentation.className, "");
  assert.equal(presentation.conflicting, true);
  assert.match(presentation.detail, /Short-term: Hold/);
  assert.match(presentation.detail, /Long-term: Strong Buy/);
});

test("recommendation meaning and audit identity are accessible without color", () => {
  const html = renderToStaticMarkup(React.createElement(PortfolioRadarCompanyName, {
    companyName: "Polycab India Ltd.",
    signal: signal("BUY", "BUY")
  }));
  assert.match(html, /title="Short-term: Buy · Long-term: Buy · Recommendation:/);
  assert.match(html, /class="sr-only">\. Radar: Short-term: Buy · Long-term: Buy/);
  assert.match(html, /Radar cycle: cycle-1/);
});

test("portfolio matches Radar only by canonical global instrument ID", () => {
  const start = workspace.indexOf("const radarSignal = position.instrument.globalInstrumentId");
  const match = workspace.slice(start, workspace.indexOf("return (", start));
  assert.match(match, /radarSignalsByInstrumentId\.get\(position\.instrument\.globalInstrumentId\)/);
  assert.doesNotMatch(match, /displayName|ticker|companyName|isin|replace|toLowerCase/);
});

test("portfolio loads recommendations with one bulk request rather than per holding", () => {
  assert.match(api, /getPortfolioRadarSignals: \(globalInstrumentIds: string\[\]\)/);
  assert.match(api, /params\.append\("global_instrument_id", globalInstrumentId\)/);
  assert.match(api, /request<PortfolioRadarSignal\[\]>\(\s*`\/api\/v1\/research\/recommendations\/current\?/);
  assert.equal((workspace.match(/portfolioApi\.getPortfolioRadarSignals\(globalInstrumentIds\)/g) ?? []).length, 1);
});

test("portfolio refetches the bulk projection on page refresh signals", () => {
  assert.match(workspace, /view !== "portfolio"/);
  assert.match(workspace, /window\.addEventListener\("focus", loadRadarSignals\)/);
  assert.match(workspace, /document\.addEventListener\("visibilitychange", loadVisibleRadarSignals\)/);
});

test("portfolio valuation and P&L cells remain sourced from existing position values", () => {
  assert.match(workspace, /getAllocationValue\(summary, position\)/);
  assert.match(workspace, /formatBackendMoney\(position\.marketValue\)/);
  assert.match(workspace, /formatBackendMoney\(position\.unrealizedProfitLoss\)/);
  assert.match(workspace, /formatPercent\(position\.unrealizedProfitLossPercent\)/);
});

// -- ETF Radar counterpart: Phase 19 portfolio ETF integration --------------

const { EtfRadarCompanyName, etfPortfolioRadarPresentation } = loaded.exports;

function etfSignal(recommendation, overrides = {}) {
  return {
    globalInstrumentId: "gid-etf-1",
    symbol: "NIFTYBEES",
    recommendation,
    score: 71.5,
    confidence: "MEDIUM",
    dataCompleteness: 0.9,
    disposition: "EVALUATED",
    radarVersion: "ETF_RADAR_V1",
    cycleId: "etf-cycle-1",
    asOf: "2026-10-01T12:00:00Z",
    source: "ETF_RADAR_CURRENT_CYCLE",
    ...overrides,
  };
}

for (const [recommendation, tone] of [
  ["STRONG_OPPORTUNITY", "strong-buy"],
  ["OPPORTUNITY", "buy"],
  ["WATCH", "hold"],
  ["AVOID", "full-exit"],
]) {
  test(`ETF ${recommendation} maps to its own semantic tone, distinct from Equity's vocabulary`, () => {
    const presentation = etfPortfolioRadarPresentation(etfSignal(recommendation));
    assert.equal(presentation.recommendation, recommendation);
    assert.equal(presentation.className, `portfolio-radar-name portfolio-radar-${tone}`);
  });
}

test("ETF INSUFFICIENT_DATA renders with no tone class (never a fabricated buy/sell color)", () => {
  const presentation = etfPortfolioRadarPresentation(etfSignal("INSUFFICIENT_DATA"));
  assert.equal(presentation.className, "");
});

test("missing ETF signal renders company name with no radar styling at all", () => {
  const presentation = etfPortfolioRadarPresentation(null);
  assert.equal(presentation.recommendation, null);
  assert.equal(presentation.className, "");
  assert.equal(presentation.detail, null);
});

test("EtfRadarCompanyName never uses Equity's BUY/HOLD/EXIT label vocabulary", () => {
  const html = renderToStaticMarkup(
    React.createElement(EtfRadarCompanyName, { companyName: "Nifty BeES ETF", signal: etfSignal("OPPORTUNITY") })
  );
  assert.match(html, /Nifty BeES ETF/);
  assert.match(html, /Opportunity/);
  assert.doesNotMatch(html, /Strong Buy|Partial Exit|Full Exit/);
});

test("Phase 19: ETF portfolio signal state/fetch is fully independent of the Equity one", () => {
  assert.match(workspace, /const \[etfPortfolioRadarSignals, setEtfPortfolioRadarSignals\]/);
  assert.match(workspace, /portfolioApi\.getEtfPortfolioRadarSignals\(etfGlobalInstrumentIds\)/);
  // The ETF fetch is gated on instrument.assetType === "ETF", never merged
  // into the Equity globalInstrumentIds list.
  assert.match(workspace, /position\.instrument\.assetType === "ETF" && position\.instrument\.globalInstrumentId/);
  // The Equity call site is untouched: same function name, same bulk shape.
  assert.match(workspace, /portfolioApi\.getPortfolioRadarSignals\(globalInstrumentIds\)/);
});

test("Phase 19: a holding row renders the ETF or Equity radar name component based on assetType, never both", () => {
  assert.match(workspace,
    /position\.instrument\.assetType === "ETF"\s*\n\s*\? <EtfRadarCompanyName companyName=\{position\.displayName\} signal=\{etfRadarSignal\} \/>\s*\n\s*: <PortfolioRadarCompanyName companyName=\{position\.displayName\} signal=\{radarSignal\} \/>/);
});

test("Phase 19: getEtfPortfolioRadarSignals calls the ETF-only bulk endpoint, a distinct route from Equity's", () => {
  assert.match(api, /getEtfPortfolioRadarSignals: \(globalInstrumentIds: string\[\]\) => \{/);
  assert.match(api, /\/api\/v1\/research\/etf-recommendations\/current\?/);
  assert.match(api, /\/api\/v1\/research\/recommendations\/current\?/);
});

test("Phase 19: private portfolio fields never appear on the EtfPortfolioRadarSignal type", () => {
  const typeBlockMatch = api.match(/export type EtfPortfolioRadarSignal = \{[\s\S]*?\};/);
  assert.ok(typeBlockMatch, "EtfPortfolioRadarSignal type not found");
  const typeBlock = typeBlockMatch[0];
  for (const privateField of ["quantity", "averageCost", "positionSize", "gainLoss", "account", "broker", "portfolioId"]) {
    assert.doesNotMatch(typeBlock, new RegExp(privateField));
  }
});
