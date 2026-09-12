import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const workspace = readFileSync(new URL("../app/components/investment-workspace.tsx", import.meta.url), "utf8");
const homePage = readFileSync(new URL("../app/page.tsx", import.meta.url), "utf8");
const styles = readFileSync(new URL("../app/styles.css", import.meta.url), "utf8");
const portfolioApiSource = readFileSync(new URL("../app/lib/portfolio-api.ts", import.meta.url), "utf8");
const marketEnsureReadyDeclaration = workspace.indexOf("const marketEnsureAuthReady =");
const marketEnsureCall = workspace.indexOf('portfolioApi.ensureMarketData("INDIA")', marketEnsureReadyDeclaration);
const marketEnsureEffectStart = workspace.lastIndexOf("  useEffect(() => {", marketEnsureCall);
const marketEnsureEffectEnd = workspace.indexOf("\n  }, [marketEnsureAuthReady]);", marketEnsureEffectStart);
const marketEnsureEffectBody = workspace.slice(
  marketEnsureEffectStart + "  useEffect(() => {".length,
  marketEnsureEffectEnd
);
const marketEnsureEffectJavaScript = ts.transpileModule(`
  export function runMarketEnsureEffect(
    marketEnsureAuthReady,
    portfolioApi,
    console,
    marketEnsureErrorCategory
  ) {
    ${marketEnsureEffectBody}
  }
`, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 }
}).outputText;
const { runMarketEnsureEffect } = await import(
  `data:text/javascript;base64,${Buffer.from(marketEnsureEffectJavaScript).toString("base64")}`
);

function createMarketEnsureEffectRenderer(ensureMarketData) {
  let initialized = false;
  let previousAuthReady;
  const logs = [];
  const portfolioApi = { ensureMarketData };
  const diagnosticConsole = { info: (...args) => logs.push(args) };
  const category = (error) => typeof error?.status === "number" ? `HTTP_${error.status}` : "UNKNOWN";

  return {
    logs,
    render(authReady) {
      if (initialized && Object.is(previousAuthReady, authReady)) return false;
      initialized = true;
      previousAuthReady = authReady;
      runMarketEnsureEffect(authReady, portfolioApi, diagnosticConsole, category);
      return true;
    }
  };
}

test("every holding row opens provenance-rich stock research details", () => {
  assert.match(workspace, /onClick=\{\(\) => setDetail\(\{ position, researchInstrumentId: research\?\.instrumentId \?\? position\.instrument\.globalInstrumentId \}\)\}/);
  for (const section of ["Overview", "Market Data", "Valuation", "Quality / Fundamentals", "Analyst View", "Latest Quarterly Result", "Financial History", "Shareholding Pattern", "Debt & Balance Sheet", "Cash Flow", "Current Quarter Catalysts", "News", "Research & Evidence"]) {
    assert.match(workspace, new RegExp(`>${section}<`));
  }
  assert.match(workspace, /target="_blank" rel="noreferrer"/);
  assert.match(workspace, /External public analyst consensus; not an application recommendation/);
  assert.match(workspace, /Provider ticker:/);
  assert.match(workspace, /Price freshness/);
  assert.match(workspace, /financialResultHistory/);
  assert.match(workspace, /balanceSheetHistory/);
  assert.match(workspace, /cashFlowHistory/);
});

test("holdings keep research fields in the drawer and the table concise", () => {
  for (const heading of ["P/E", "Valuation", "Latest result", "Shareholding", "Catalyst", "Research"]) {
    assert.doesNotMatch(workspace, new RegExp(`<th>${heading}<\\/th>`));
  }
  for (const heading of ["Company", "Ticker", "Quantity", "Average cost", "Latest price", "Market value", "Unrealized P/L", "Unrealized P/L %", "Allocation", "Currency", "Research & price status"]) {
    assert.match(workspace, new RegExp(`<th>${heading.replace("/", "\\/")}<\\/th>`));
  }
  assert.match(workspace, /research\?\.valuation\.state/);
  assert.match(workspace, /Not publicly available/);
  assert.match(workspace, /Awaiting research/);
});

test("holdings present research, imported-price, and live-quote states as distinct text", () => {
  assert.match(workspace, /function ResearchStatusBadge/);
  assert.match(workspace, /RESOLVED_RESEARCH_AVAILABLE/);
  assert.match(workspace, /RESOLVED_PARTIAL_DATA/);
  assert.match(workspace, /ETF_UNSUPPORTED/);
  assert.match(workspace, /Research: \{label\}/);
  assert.match(workspace, /Position price: .*Imported snapshot/);
  assert.match(workspace, /Live quote:/);
  assert.match(workspace, /Research price:/);
  assert.match(workspace, /Valuation:/);
  assert.match(workspace, /Ownership increase:/);
  assert.match(workspace, /function ResearchCoverage/);
  assert.match(workspace, /Research coverage:/);
  assert.match(workspace, /\{available\} available/);
  assert.match(workspace, /\{partial\} partial/);
  assert.match(workspace, /\{etfUnsupported\} ETF unsupported/);
  assert.match(workspace, /\{fresh\} fresh/);
  assert.match(workspace, /\{stale\} stale/);
});

test("missing live prices render N/A and never drive market value or profit-loss presentation", () => {
  assert.match(portfolioApiSource, /currentPrice: Money \| null/);
  assert.match(portfolioApiSource, /marketValue: Money \| null/);
  assert.match(portfolioApiSource, /unrealizedProfitLoss: Money \| null/);
  assert.match(workspace, /return money \? formatMoney\(money\.amount, money\.currency\) : "N\/A"/);
  assert.match(workspace, /positions\.every\(\(position\) => position\.marketValue && position\.unrealizedProfitLoss\)/);
  assert.match(workspace, /<td>\{formatBackendMoney\(position\.currentPrice\)\}<\/td>/);
  assert.match(workspace, /<td>\{formatBackendMoney\(position\.marketValue\)\}<\/td>/);
  assert.match(workspace, /researchCurrentPrice > 0/);
  assert.match(workspace, /position\.currentPrice\.amount > 0/);
  assert.match(workspace, /: "N\/A"/);
  assert.doesNotMatch(workspace, /position\.currentPrice\.amount, position\.currentPrice\.currency/);
});

test("research readiness stays disabled without a safe canonical global instrument identity", () => {
  assert.match(workspace, /return Boolean\(globalInstrumentId\?\.trim\(\)\)/);
  assert.match(workspace, /\["COMPANY_NOT_RESOLVED", "RESEARCH_NOT_APPLICABLE", "ETF_UNSUPPORTED"\]\.includes\(normalizedStatus\)/);
  assert.match(workspace, /disabled=\{loading \|\| !refreshEligible\}/);
});

test("shareholding drawer renders a dynamic oldest-to-newest four-quarter table without inventing missing values", () => {
  assert.match(workspace, /function ShareholdingPatternTable/);
  assert.match(workspace, /\.slice\(0, 4\)\.sort\(\(left, right\) =>/);
  assert.match(workspace, /new Date\(left\.periodEnd\).*new Date\(right\.periodEnd\)/s);
  assert.match(workspace, /\["PROMOTER", "Promoters"\]/);
  assert.match(workspace, /\["FII_FPI", "FII \/ FPI"\]/);
  assert.match(workspace, /\["DII", "DII"\]/);
  assert.match(workspace, /\["PUBLIC_RETAIL", "Retail Public"\]/);
  assert.match(workspace, /\["PROMOTER_PLEDGE", "Promoter Pledge\*"\]/);
  assert.match(workspace, /Resident individual shareholders holding nominal share capital up to \u20B92 lakh\./);
  assert.match(workspace, /if \(value === undefined\) return "\u2014"/);
  assert.match(workspace, /percentage\.toFixed\(2\).*%/);
  assert.match(workspace, /\* % of promoter holding/);
  assert.match(workspace, /newest\.sourceProvider} Shareholding XBRL/);
  assert.match(workspace, /research\?\.shareholdingSnapshots\?\.length \? <ShareholdingPatternTable snapshots=\{research\.shareholdingSnapshots\}/);
  assert.match(workspace, /: <p>Unavailable\.<\/p>/);
  assert.doesNotMatch(workspace, /100\s*-\s*other|public.*100\s*-/i);
});

test("shareholding drawers resolve the current portfolio company by instrumentId instead of retaining a stale company copy", () => {
  assert.match(workspace, /researchInstrumentId: research\?\.instrumentId \?\? position\.instrument\.globalInstrumentId/);
  assert.match(workspace, /portfolioResearch\?\.companies\.find\(\(company\) => company\.instrumentId === detail\.researchInstrumentId\)/);
  assert.match(workspace, /researchInstrumentId: company\.instrumentId/);
  assert.match(workspace, /portfolioResearchSummary\?\.companies\.find\(\(company\) => company\.instrumentId === researchDetail\.researchInstrumentId\)/);
  assert.doesNotMatch(workspace, /setResearchDetail\(\{ position: holding, research: company \}\)/);
});

test("legacy portfolio refresh UI and polling are removed after readiness cutover", () => {
  assert.doesNotMatch(workspace, /refreshPortfolio|getRefreshJob|getActiveRefreshJob/);
  assert.doesNotMatch(workspace, /GlobalResearchRefreshProgress|activeResearchRefreshJob/);
  assert.doesNotMatch(workspace, /Refresh portfolio research|Updating research/);
  assert.match(workspace, />\s*Research readiness\s*<\/Button>/);
  assert.match(workspace, /findResearchData\(requirements\)/);
  assert.match(workspace, /portfolioResearch\?\.companies\.find\(\(company\) => company\.instrumentId === detail\.researchInstrumentId\)/);
});

test("portfolio summary failure keeps persisted company research independently available", () => {
  assert.match(workspace, /setPortfolioResearchError\("Portfolio research summary unavailable\. Existing company research remains available\."\)/);
  assert.match(workspace, /const positionsRef = useRef<PortfolioPosition\[\]>\(\[\]\);/);
  assert.match(workspace, /positionsRef\.current = positions;/);
  assert.match(workspace, /setSelectedResearchInstrumentId\(\(current\) => current \|\| positionsRef\.current\.find/);
  assert.match(workspace, /\{researchContext\.kind === "PORTFOLIO" && portfolioResearchError \? <p className="research-refresh-notice" role="alert">\{portfolioResearchError\}<\/p> : null\}/);
  assert.match(workspace, /\) : summary \|\| researchContext.kind === "SEARCH" \? null : \(/);
  const effectStart = workspace.indexOf("async function loadPortfolioResearchSummary()");
  const effectEnd = workspace.indexOf("async function loadPortfolioDetail()", effectStart);
  const effect = workspace.slice(effectStart, effectEnd);
  const failureBranch = effect.slice(effect.indexOf("} catch {"), effect.indexOf("} finally {"));
  assert.doesNotMatch(failureBranch, /setSelectedResearchInstrumentId\(""\)/);
});

test("portfolio research summary fetch is keyed only by portfolio selection, not positions hydration", () => {
  const start = workspace.indexOf("async function loadPortfolioResearchSummary()");
  const end = workspace.indexOf("async function loadPortfolioDetail()", start);
  const effect = workspace.slice(start, end);

  assert.equal((effect.match(/researchApi\.getPortfolioSummary\(selectedPortfolioId\)/g) ?? []).length, 1);
  assert.match(effect, /\}, \[selectedPortfolioId\]\);/);
  assert.doesNotMatch(effect, /\}, \[selectedPortfolioId, positions\]\);/);
  assert.match(effect, /positionsRef\.current\.find/);
});

test("research intelligence uses category-linked evidence rather than the generic recent-event slice", () => {
  assert.match(workspace, /function categorySupportingEvents/);
  assert.match(workspace, /evidence\?\.supportingEvents/);
  assert.match(workspace, /categorySupportingEvents\(summary, "CAPEX"/);
  assert.match(workspace, /CAPEX: "CAPEX & Capacity"/);
  assert.match(workspace, /GUIDANCE: "Guidance"/);
});

test("ownership border combinations are independent and reduced-motion safe", () => {
  for (const name of ["promoter", "fii", "dii", "promoter-dii", "promoter-fii", "fii-dii", "promoter-fii-dii"]) {
    assert.match(styles, new RegExp(`\\.ownership-${name}\\b`));
  }
  assert.match(styles, /@keyframes ownership-dots/);
  assert.match(styles, /@media \(prefers-reduced-motion: reduce\)[\s\S]*ownership-/);
  for (const state of ["cheap", "fair", "expensive", "unknown"]) {
    assert.match(styles, new RegExp(`valuation-${state}`));
  }
});

test("explicit INR research metrics use canonical compact Indian monetary display without relabeling foreign values", () => {
  assert.match(workspace, /function metricDisplay/);
  assert.match(workspace, /unit\?\.toUpperCase\(\) === "INR"/);
  assert.match(workspace, /numeric \/ 10_000_000\).*Cr/s);
  assert.match(workspace, /numeric \/ 100_000\).*Lakh/s);
  assert.match(workspace, /new Intl\.NumberFormat\("en-IN"/);
  assert.match(workspace, /title: exact/);
  assert.match(workspace, /unit && unit !== "ratio"/);
  assert.match(workspace, /<MetricValue metric=\{period\.revenue\}/);
  assert.match(workspace, /<MetricValue metric=\{period\.pat\}/);
  assert.match(workspace, /<MetricValue metric=\{keys\.map/);
});

test("dashboard sector performance is backend-authoritative and holdings presentation is absent", () => {
  assert.match(workspace, /portfolioApi\.getSectorPerformance\(sectorPerformanceRegion, sectorPerformanceSector, sectorPerformancePeriod\)/);
  assert.match(workspace, />Sector Performance</);
  assert.match(workspace, />Top 5 Performers</);
  assert.match(workspace, />Worst 5 Performers</);
  assert.match(workspace, /aria-label="Sector performance region"/);
  assert.match(workspace, /aria-label="Sector performance sector"/);
  assert.match(workspace, /aria-label="Sector performance period"/);
  assert.match(workspace, /onClick=\{\(\) => onOpenResearch\(stock\)\}/);
  const start = workspace.indexOf("function MultiPortfolioDashboard");
  const end = workspace.indexOf("function PortfolioCreatePanel", start);
  const dashboard = workspace.slice(start, end);
  assert.doesNotMatch(dashboard, /\.sort\(/);
  assert.doesNotMatch(dashboard, /\.slice\(0,\s*5\)/);
  assert.doesNotMatch(dashboard, /All holdings|combinedHoldings|Matching ISINs are combined/);
  assert.doesNotMatch(dashboard, /Score \{?stock/);
});

test("market-data ensure uses a dedicated primitive-auth effect and the shared authenticated API route", () => {
  assert.notEqual(marketEnsureReadyDeclaration, -1);
  assert.notEqual(marketEnsureEffectStart, -1);
  assert.notEqual(marketEnsureEffectEnd, -1);
  assert.match(workspace, /const marketEnsureAuthReady = authenticatedUser !== null && Boolean\(accessToken\);/);
  assert.match(marketEnsureEffectBody, /portfolioApi\.ensureMarketData\("INDIA"\)/);
  assert.match(marketEnsureEffectBody, /event: "EFFECT", authReady: marketEnsureAuthReady/);
  assert.match(marketEnsureEffectBody, /event: "DISPATCH"/);
  assert.match(marketEnsureEffectBody, /event: "RESOLVED"/);
  assert.match(marketEnsureEffectBody, /event: "REJECTED", category:/);
  assert.doesNotMatch(marketEnsureEffectBody, /sectorPerformanceRegion|sectorPerformanceSector|sectorPerformancePeriod|getSectorPerformance/);
  assert.match(workspace.slice(marketEnsureEffectEnd), /^\n  \}, \[marketEnsureAuthReady\]\);/);
  assert.doesNotMatch(workspace, /marketDataEnsureDispatchedRef|oneShotGuard|ensureIndiaMarketDataForAuthenticatedUser|market-data-ensure-lifecycle|onDispatched/);

  assert.match(portfolioApiSource, /ensureMarketData: \(region: "INDIA"\) =>[\s\S]*?\/api\/v1\/research\/market-data\/ensure\?region=\$\{region\}[\s\S]*?method: "POST"/);
  assert.match(portfolioApiSource, /async function request<T>\(path: string, init\?: RequestInit\)[\s\S]*?const token = currentAuthenticatedApiToken\(\)[\s\S]*?Authorization: `Bearer \$\{token\}`/);
  assert.doesNotMatch(portfolioApiSource, /onDispatched|responsePromise/);
});

test("the root Dashboard route mounts the sole runtime owner of Sector Performance and market-data ensure", () => {
  assert.match(homePage, /import \{ InvestmentWorkspace \} from "\.\/components\/investment-workspace";/);
  assert.match(homePage, /return <InvestmentWorkspace \/>;/);
  assert.equal((workspace.match(/portfolioApi\.getSectorPerformance\(/g) ?? []).length, 1);
  assert.equal((workspace.match(/portfolioApi\.ensureMarketData\("INDIA"\)/g) ?? []).length, 1);
  assert.ok(marketEnsureCall < workspace.indexOf("portfolioApi.getSectorPerformance("));
});

test("successful login reloads the active root build before authenticated Dashboard effects mount", () => {
  const loginStart = workspace.indexOf("async function login(");
  const loginEnd = workspace.indexOf("function logout()", loginStart);
  const loginPaths = workspace.slice(loginStart, loginEnd);

  assert.equal((loginPaths.match(/window\.location\.replace\("\/"\);/g) ?? []).length, 2);
  assert.doesNotMatch(loginPaths, /setAccessToken\(session\.accessToken\)/);
  assert.doesNotMatch(loginPaths, /setAuthenticatedUser\(session\.user\)/);
});

test("authReady false performs zero INDIA ensure calls", () => {
  let calls = 0;
  const renderer = createMarketEnsureEffectRenderer(async () => { calls += 1; });
  assert.equal(renderer.render(false), true);
  assert.equal(calls, 0);
  assert.deepEqual(renderer.logs[0], ["[AIP_MARKET_ENSURE]", { event: "EFFECT", authReady: false }]);
});

test("false-to-true auth transition dispatches exactly once", () => {
  let calls = 0;
  const renderer = createMarketEnsureEffectRenderer(async () => { calls += 1; });
  renderer.render(false);
  renderer.render(true);
  assert.equal(calls, 1);
  assert.equal(renderer.logs.some((entry) => entry[1]?.event === "DISPATCH"), true);
});

test("initially authenticated mount dispatches ensure", () => {
  let calls = 0;
  const renderer = createMarketEnsureEffectRenderer(async () => { calls += 1; });
  renderer.render(true);
  assert.equal(calls, 1);
});

test("ordinary rerenders and Region Sector Period changes do not retrigger ensure", () => {
  let calls = 0;
  const renderer = createMarketEnsureEffectRenderer(async () => { calls += 1; });
  renderer.render(true);
  for (const unrelatedChange of ["rerender", "INDIA", "Financials", "DAY", "Technology", "YEAR"]) {
    assert.ok(unrelatedChange);
    assert.equal(renderer.render(true), false);
  }
  assert.equal(calls, 1);
});

test("logout and subsequent login produce a new false-to-true lifecycle", () => {
  let calls = 0;
  const renderer = createMarketEnsureEffectRenderer(async () => { calls += 1; });
  renderer.render(true);
  renderer.render(false);
  assert.equal(calls, 1);
  renderer.render(true);
  assert.equal(calls, 2);
});

test("rejected ensure remains non-blocking and reports only a safe category", async () => {
  const renderer = createMarketEnsureEffectRenderer(() => Promise.reject({ status: 503 }));
  assert.doesNotThrow(() => renderer.render(true));
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(renderer.logs.at(-1), ["[AIP_MARKET_ENSURE]", { event: "REJECTED", category: "HTTP_503" }]);
  assert.match(workspace, /const dashboard = await portfolioApi\.getDashboard\(\)/);
});

test("Sector Performance waits for a backend-authoritative valid sector selection", () => {
  assert.match(workspace, /portfolioApi\.getSectorPerformance\(sectorPerformanceRegion, sectorPerformanceSector, sectorPerformancePeriod\)/);
  assert.match(workspace, /portfolioApi\.getMarketUniverseSectors\(sectorPerformanceRegion\)/);
  assert.match(workspace, /sectorOptions\.some\(\(option\) => option\.name === sectorPerformanceSector\)/);
  assert.match(workspace, /\[accessToken, authenticatedUser, sectorOptions, sectorOptionsRegion, sectorPerformanceRegion, sectorPerformanceSector, sectorPerformancePeriod\]/);
  assert.match(portfolioApiSource, /getSectorPerformance:[\s\S]*?request<SectorPerformance>\(`\/api\/v1\/research\/sector-performance/);
  assert.equal((workspace.match(/portfolioApi\.ensureMarketData\("INDIA"\)/g) ?? []).length, 1);
});
