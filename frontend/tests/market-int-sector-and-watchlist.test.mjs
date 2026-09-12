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
const helpers = await import(`data:text/javascript;base64,${Buffer.from(helperJavaScript).toString("base64")}`);

test("sector options remain backend-owned exact values and single encoded", () => {
  const dashboardStart = workspace.indexOf("function MultiPortfolioDashboard");
  const dashboardEnd = workspace.indexOf("function PortfolioCreatePanel", dashboardStart);
  const dashboard = workspace.slice(dashboardStart, dashboardEnd);
  assert.match(apiSource, /getMarketUniverseSectors:[\s\S]*?\/api\/v1\/research\/market-universe\/sectors/);
  assert.match(dashboard, /sectorOptions\.map\(\(value\) => <option key=\{value\.name\} value=\{value\.name\}>\{value\.name\} \(\{value\.instrumentCount\}\)<\/option>/);
  assert.doesNotMatch(dashboard, /Basic Materials|Consumer Defensive/);
  const method = apiSource.slice(apiSource.indexOf("getSectorPerformance:"), apiSource.indexOf("createPortfolio:"));
  assert.match(method, /new URLSearchParams\(\{ region, sector, period, limit: "5" \}\)/);
  assert.doesNotMatch(method, /encodeURIComponent/);
});

test("region reload clears stale sectors and invalid sectors never dispatch performance", () => {
  const discoveryCall = workspace.indexOf("portfolioApi.getMarketUniverseSectors(sectorPerformanceRegion)");
  const discovery = workspace.slice(workspace.lastIndexOf("  useEffect(() => {", discoveryCall), workspace.indexOf("\n  }, [", discoveryCall));
  assert.match(discovery, /setSectorOptions\(\[\]\)/);
  assert.match(discovery, /setSectorPerformanceSector\(""\)/);
  assert.match(discovery, /setSectorPerformanceSector\(value\.sectors\[0\]\?\.name \?\? ""\)/);
  const performanceCall = workspace.indexOf("portfolioApi.getSectorPerformance(");
  const performance = workspace.slice(workspace.lastIndexOf("  useEffect(() => {", performanceCall), workspace.indexOf("\n  }, [", performanceCall));
  assert.match(performance, /sectorOptionsRegion === sectorPerformanceRegion/);
  assert.match(performance, /sectorOptions\.some\(\(option\) => option\.name === sectorPerformanceSector\)/);
});

test("regional defaults map canonically without provider routing", () => {
  assert.equal(helpers.regionalWatchlistName("INDIA"), "WATCHLIST-IND");
  assert.equal(helpers.regionalWatchlistName("EUROPE"), "WATCHLIST-EU");
  assert.equal(helpers.regionalWatchlistName("USA"), "WATCHLIST-USA");
  const helper = helperSource.slice(helperSource.indexOf("export function regionalWatchlistName"));
  assert.doesNotMatch(helper, /NSE|SEC|EODHD|Yahoo/);
});

test("card click opens public readiness without changing a regional watchlist or position", () => {
  const start = workspace.indexOf("function openMarketIntelligenceResearch");
  const end = workspace.indexOf("\n  async function openRegionalWatchlist", start);
  const click = workspace.slice(start, end);
  assert.match(click, /setSelectedResearchInstrumentId\(stock\.globalInstrumentId\)/);
  assert.match(click, /openResearchReadiness\(stock\.globalInstrumentId, stock\.companyName\)/);
  assert.doesNotMatch(click, /ensureDefaultWatchlist|addWatchlistInstrument|prefetchVisible|createPortfolio|createPosition|addPosition|updateHolding|portfolioApi\./);
});

test("Research context selector visibly distinguishes portfolios from watchlists", () => {
  const start = workspace.indexOf("function ResearchContextSelector");
  const end = workspace.indexOf("function LoadingView", start);
  const selector = workspace.slice(start, end);
  assert.match(selector, /<span>Research context<\/span>/);
  assert.match(selector, /<optgroup label="Portfolios">/);
  assert.match(selector, /<optgroup label="Watchlists">/);
  assert.match(selector, /portfolio:\$\{portfolio\.portfolioId\}/);
  assert.match(selector, /watchlist:\$\{watchlist\.watchlistId\}/);
});

test("portfolio and watchlist research rows are mutually exclusive", () => {
  const start = workspace.indexOf("function ResearchView");
  const end = workspace.indexOf("function ResearchOverview", start);
  const research = workspace.slice(start, end);
  assert.match(research, /researchContext\.kind === "WATCHLIST" \? \(watchlistResearch\?\.instruments \?\? \[\]\)\.map/);
  assert.match(research, /researchContext\.kind === "PORTFOLIO" \? \(portfolioResearchSummary\?\.companies \?\? \[\]\)\.map/);
  assert.doesNotMatch(research, /Portfolio holdings plus the active regional Market Intelligence context/);
  assert.doesNotMatch(research, /transientMarketIntelligence/);
  assert.match(research, /This research context contains actual portfolio holdings only/);
});

test("watchlist row is canonical non-held presentation with separated market return", () => {
  const start = workspace.indexOf("key={`watchlist-${item.globalInstrumentId}`}");
  const end = workspace.indexOf("researchContext.kind === \"PORTFOLIO\"", start);
  const row = workspace.slice(start, end);
  assert.match(row, /data-held="false"/);
  assert.match(row, /data-global-instrument-id=\{item\.globalInstrumentId\}/);
  assert.match(row, /Public company research · Not held/);
  assert.match(row, /Market return \(\{item\.sourcePeriod\}\)/);
  assert.doesNotMatch(row, /Qty 0|Portfolio P&amp;L|Average buy price|Invested amount/);
  assert.match(row, /watchlistInstrumentId: item\.globalInstrumentId/);
  assert.match(row, /researchInstrumentId: company\.instrumentId \?\? item\.globalInstrumentId/);
});

test("same drawer represents non-held state without fabricating a portfolio position", () => {
  const start = workspace.indexOf("function StockResearchDrawer");
  const end = workspace.indexOf("function AllocationCharts", start);
  const drawer = workspace.slice(start, end);
  assert.match(drawer, /position\?: PortfolioPosition/);
  assert.match(drawer, /watchlistItem\?: WatchlistResearchInstrument/);
  assert.match(drawer, /Watchlist status<\/h3><p>Public company research<\/p>/);
  const nonHeld = drawer.slice(drawer.indexOf("<h3>Watchlist status"), drawer.indexOf('<section><h3>Market Data'));
  assert.doesNotMatch(nonHeld, /Quantity|Average Cost|Market Value|Unrealized P/);
  assert.match(drawer, /Market return \(\{watchlistItem\.sourcePeriod\}\)/);
});

test("direct navigation clears watchlist presentation but not durable membership", () => {
  const clear = workspace.slice(workspace.indexOf("function clearMarketIntelligenceContext"), workspace.indexOf("async function openResearchReadiness"));
  assert.match(clear, /setResearchContext\(\{ kind: "PORTFOLIO", portfolioId: selectedPortfolioId \}\)/);
  assert.match(clear, /setWatchlistResearch\(null\)/);
  assert.doesNotMatch(clear, /removeWatchlistInstrument|DELETE|localStorage/);
  const nav = workspace.slice(workspace.indexOf("<nav aria-label=\"Primary\">"), workspace.indexOf("</nav>"));
  assert.match(nav, /clearMarketIntelligenceContext\(\);[\s\S]*?setView\(item\.id\)/);
});

test("positive negative and neutral watchlist rows retain explicit signs and interaction states", () => {
  for (const tone of ["positive", "negative", "neutral"]) {
    assert.match(styles, new RegExp(`\\.market-intelligence-research-row-${tone}\\b`));
  }
  assert.match(styles, /\.market-intelligence-research-row:hover/);
  assert.match(styles, /\.market-intelligence-research-row\[aria-current="true"\]/);
  assert.equal(helpers.formatSignedPerformancePct(13.43), "+13.43%");
  assert.equal(helpers.formatSignedPerformancePct(-6.8), "-6.80%");
  assert.equal(helpers.formatSignedPerformancePct(0), "0.00%");
});

test("watchlist API is provider neutral and separate from portfolio holdings", () => {
  assert.match(apiSource, /listWatchlists:[\s\S]*?\/api\/v1\/research\/watchlists/);
  assert.match(apiSource, /ensureDefaultWatchlist:[\s\S]*?\/api\/v1\/research\/watchlists\/default\/ensure/);
  assert.match(apiSource, /addWatchlistInstrument:[\s\S]*?\/api\/v1\/research\/watchlists\/\$\{watchlistId\}\/instruments/);
  const watchlistApi = apiSource.slice(apiSource.indexOf("listWatchlists:"), apiSource.indexOf("getPortfolioSummary:"));
  assert.doesNotMatch(watchlistApi, /NSE|SEC|EODHD|createPortfolio|positions/);
});

for (const [region, name, performancePct] of [["INDIA", "WATCHLIST-IND", 8], ["INDIA", "WATCHLIST-IND", -8], ["USA", "WATCHLIST-USA", 3], ["EUROPE", "WATCHLIST-EU", -3]]) {
  test(`ranked ${region} ${performancePct} persists canonical membership and refreshes Saved state`, async () => {
    const start = workspace.indexOf("  async function addRankedStockToWatchlist");
    const end = workspace.indexOf("  async function removeWatchlistStock", start);
    const js = ts.transpileModule(workspace.slice(start, end), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
    const calls = [];
    let saved = {};
    const list = { watchlistId: "list-id", region, name };
    const api = {
      ensureDefaultWatchlist: async (value) => { assert.equal(value, region); return list; },
      addWatchlistInstrument: async (...args) => { calls.push(args); },
      getWatchlistResearch: async () => ({ watchlist: list, instruments: [{ globalInstrumentId: "canonical-id" }] }),
    };
    const handler = new Function("researchApi", "watchlistMutationRef", "setWatchlistMutation", "setWatchlistActionError", "setWatchlists", "setSavedWatchlistIds", "setWatchlistRevision", "getApiFailure", `${js}; return addRankedStockToWatchlist;`)(
      api, { current: false }, () => {}, (error) => assert.equal(error, null), () => {}, (update) => { saved = update(saved); }, () => {}, (error) => { throw error; }
    );
    await handler({ region, period: "WEEK", stock: { globalInstrumentId: "canonical-id", companyName: "Display only", ticker: "LOOSE", performancePct } });
    assert.deepEqual(calls, [["list-id", { globalInstrumentId: "canonical-id", sourcePeriod: "WEEK", sourcePerformancePct: performancePct }]]);
    assert.deepEqual(saved, { "list-id": ["canonical-id"] });
    await handler({ region, period: "WEEK", stock: { globalInstrumentId: "", performancePct } });
    assert.equal(calls.length, 1);
  });
}

test("both ranked groups expose Saved/Add and Research exposes all regional lists and removal", () => {
  assert.equal((workspace.match(/onAddWatchlist=\{\(\) => onAddWatchlist\(marketIntelligenceSelection\(performance, stock\)\)\}/g) ?? []).length, 2);
  assert.match(workspace, /disabled=\{saved \|\| busy \|\| !stock.globalInstrumentId/);
  assert.match(workspace, /Saved in \$\{watchlistName\}/);
  assert.match(workspace, /aria-label="Regional watchlists"/);
  assert.match(workspace, /\["INDIA", "USA", "EUROPE"\] as const/);
  assert.match(workspace, /researchApi.removeWatchlistInstrument\(watchlistId, globalInstrumentId\)/);
  assert.match(workspace, /marketEnsureAuthReady, researchContext, watchlistRevision/);
});
