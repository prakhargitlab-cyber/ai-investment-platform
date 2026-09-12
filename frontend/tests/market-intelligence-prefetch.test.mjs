import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const workspace = readFileSync(new URL("../app/components/investment-workspace.tsx", import.meta.url), "utf8");
const apiSource = readFileSync(new URL("../app/lib/portfolio-api.ts", import.meta.url), "utf8");
const helperSource = readFileSync(new URL("../app/lib/market-intelligence.ts", import.meta.url), "utf8");
const styles = readFileSync(new URL("../app/styles.css", import.meta.url), "utf8");
const helperJavaScript = ts.transpileModule(helperSource, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 }
}).outputText;
const helpers = await import(
  `data:text/javascript;base64,${Buffer.from(helperJavaScript).toString("base64")}`
);

test("positive, negative, zero, and unavailable returns retain explicit accessible direction", () => {
  assert.equal(helpers.performanceRowTone(4.25), "positive");
  assert.equal(helpers.performanceRowTone(-6.8), "negative");
  assert.equal(helpers.performanceRowTone(0), "neutral");
  assert.equal(helpers.performanceRowTone(null), "neutral");
  assert.equal(helpers.performanceRowTone(Number.NaN), "neutral");
  assert.equal(helpers.formatSignedPerformancePct(4.25), "+4.25%");
  assert.equal(helpers.formatSignedPerformancePct(-6.8), "-6.80%");
  assert.equal(helpers.formatSignedPerformancePct(0), "0.00%");
  assert.equal(helpers.formatSignedPerformancePct(undefined), "--");
});

test("Sector Performance commits independently without an automatic research acquisition effect", () => {
  const performanceCall = workspace.indexOf("portfolioApi.getSectorPerformance(");
  const performanceEffectStart = workspace.lastIndexOf("  useEffect(() => {", performanceCall);
  const performanceEffectEnd = workspace.indexOf("\n  }, [", performanceCall);
  const performanceEffect = workspace.slice(performanceEffectStart, performanceEffectEnd);
  assert.match(performanceEffect, /\.then\(\(value\) => \{ if \(!cancelled\) setSectorPerformance\(value\); \}\)/);
  assert.doesNotMatch(workspace, /researchApi\.prefetchVisible\(/);
  assert.doesNotMatch(performanceEffect, /getReadiness|ensureReadiness|researchApi\.refresh/);
});

test("the frontend uses provider-neutral readiness and targeted ensure routes", () => {
  assert.match(apiSource, /getReadiness: \(globalInstrumentId: string\) =>[\s\S]*?`\/api\/v1\/research\/readiness\/\$\{globalInstrumentId\}`/);
  assert.match(apiSource, /ensureReadiness: \(globalInstrumentId: string, requirements\?: string\[\]\) =>[\s\S]*?`\/api\/v1\/research\/readiness\/\$\{globalInstrumentId\}\/ensure`[\s\S]*?JSON\.stringify\(requirements\?\.length \? \{ requirements \} : \{\}\)/);
  const readinessStart = workspace.indexOf("async function openResearchReadiness");
  const readinessEnd = workspace.indexOf("function selectResearchContext", readinessStart);
  const readinessPath = `${helperSource}\n${workspace.slice(readinessStart, readinessEnd)}`;
  assert.doesNotMatch(readinessPath, /NSE|SEC_EDGAR|EODHD|Yahoo|country\s*===|region\s*===/);
});

test("clicking a visible stock opens read-only readiness without creating a holding or watchlist membership", () => {
  assert.match(workspace, /onClick=\{\(\) => onOpenResearch\(stock\)\}/);
  assert.match(workspace, /onOpenResearch=\{\(selection\) => \{ void openMarketIntelligenceResearch\(selection\); \}\}/);
  const clickStart = workspace.indexOf("function openMarketIntelligenceResearch");
  const clickEnd = workspace.indexOf("  async function openRegionalWatchlist", clickStart);
  const click = workspace.slice(clickStart, clickEnd);
  assert.match(click, /setSelectedResearchInstrumentId\(stock\.globalInstrumentId\)/);
  assert.match(click, /openResearchReadiness\(stock\.globalInstrumentId, stock\.companyName\)/);
  assert.doesNotMatch(click, /ensureDefaultWatchlist|addWatchlistInstrument|prefetchVisible|ensureReadiness|createPortfolio|createPosition|addPosition|updateHolding/);
  assert.match(workspace, /held: false,[\s\S]*?quantity: 0/);
  assert.match(workspace, /Qty 0 · Not held/);
  const rowStart = workspace.indexOf("function MarketPerformanceRow");
  const rowEnd = workspace.indexOf("function PortfolioCreatePanel", rowStart);
  const row = workspace.slice(rowStart, rowEnd);
  assert.doesNotMatch(row, /averageCost|average buy|invested|profitLoss|createPortfolio|createHolding/);
});

test("legacy visible-stock prefetch state and polling are absent", () => {
  assert.doesNotMatch(apiSource, /prefetchVisible|getPrefetchState|\/api\/v1\/research\/prefetch/);
  assert.doesNotMatch(workspace, /MarketIntelligenceResearchState|marketIntelligenceResearchStates|getPrefetchState|selectedMarketIntelligenceResearchState/);
  assert.doesNotMatch(helperSource, /visibleSectorPerformanceInstrumentIds|researchStateLabel/);
});

test("the whole row has positive, negative, and neutral styling with usable hover, selection, and focus", () => {
  assert.match(workspace, /className=\{`market-performance-row market-performance-row-\$\{tone\}`\}/);
  assert.match(workspace, /data-performance-direction=\{tone\}/);
  assert.match(workspace, /aria-label=\{`\$\{stock\.companyName\}, \$\{signedPerformance\}, \$\{performanceDirectionLabel\(stock\)\}`\}/);
  for (const tone of ["positive", "negative", "neutral"]) {
    assert.match(styles, new RegExp(`\\.market-performance-row-${tone}\\b`));
  }
  assert.match(styles, /\.market-performance-row:hover/);
  assert.match(styles, /\.market-performance-row\[aria-current="true"\]/);
  assert.match(styles, /\.market-performance-row:focus-visible/);
  assert.match(styles, /\.market-performance-row-positive \.market-performance-return \{ color: var\(--positive\); \}/);
  assert.match(styles, /\.market-performance-row-negative \.market-performance-return \{ color: var\(--negative\); \}/);
});
