import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const workspace = readFileSync(new URL("../app/components/investment-workspace.tsx", import.meta.url), "utf8");
const api = readFileSync(new URL("../app/lib/portfolio-api.ts", import.meta.url), "utf8");

test("search endpoint is provider-neutral and backend-owned, no NSE/Yahoo text search from the browser", () => {
  assert.match(api, /searchInstruments:[\s\S]*?\/api\/v1\/research\/instruments\/search/);
  assert.doesNotMatch(api, /fetch\("https:\/\/yahoo|fetch\("https:\/\/www\.yahoo|window\.open.*yahoo/i);
  assert.doesNotMatch(workspace, /\.fetch\("https:\/\/(finance|query1).*yahoo/);
});

test("search contract returns a canonical instrument match with a global identity", () => {
  assert.match(
    api,
    /export type ResearchInstrumentMatch = \{[\s\S]*?globalInstrumentId: string;[\s\S]*?companyName: string;[\s\S]*?symbol: string;[\s\S]*?region: SectorPerformance\["region"\];[\s\S]*?\}/
  );
  assert.match(
    api,
    /searchInstruments: \(region: SectorPerformance\["region"\], query: string, limit = 20\) =>[\s\S]*?new URLSearchParams\(\{ region, q: query, limit: String\(limit\) \}\)/
  );
});

test("research context supports a global search kind keyed on a globalInstrumentId", () => {
  assert.match(workspace, /kind: "SEARCH"/);
  assert.match(workspace, /setResearchContext\(\{ kind: "SEARCH", region, globalInstrumentId: stock\.globalInstrumentId \}\)/);
  assert.match(workspace, /function handleSearchSelect\(match: ResearchInstrumentMatch\)/);
  assert.match(workspace, /setSelectedResearchInstrumentId\(match\.globalInstrumentId\)/);
  assert.match(workspace, /void loadSearchPresentation\(match\.globalInstrumentId, match\.companyName, match\.region\)/);
  assert.match(workspace, /void openResearchReadiness\(match\.globalInstrumentId, match\.companyName\)/);
});

test("search uses a debounced autocomplete with min length, bounded results, and keyboard navigation", () => {
  assert.match(workspace, /function StockSearchField\(/);
  assert.match(workspace, /const DEBOUNCE_MS = 300;/);
  assert.match(workspace, /trimmed\.length < 3/);
  assert.match(workspace, /researchApi\.searchInstruments\(region, trimmed, 20\)/);
  assert.match(workspace, /results\.slice\(0, 20\)/);
  assert.match(workspace, /aria-autocomplete="list"/);
  assert.match(workspace, /aria-expanded=\{open\}/);
  assert.match(workspace, /onSelect: \(match: ResearchInstrumentMatch\) => void/);
  assert.match(workspace, /"ArrowDown"[\s\S]*?setHighlight[\s\S]*?"ArrowUp"[\s\S]*?setHighlight[\s\S]*?"Enter"[\s\S]*?commit\([\s\S]*?"Escape"/);
  assert.match(workspace, /onSelect=\{handleSearchSelect\}/);
});

test("searched stock is added to the region-aware watchlist reusing the existing service", () => {
  const toggle = workspace.match(/function toggleSearchWatchlist\(\)[\s\S]*?\n  }/)?.[0] ?? "";
  assert.match(toggle, /researchApi\.ensureDefaultWatchlist\(match\.region\)/);
  assert.match(toggle, /researchApi\.addWatchlistInstrument\(list\.watchlistId, \{/);
  assert.match(toggle, /researchApi\.removeWatchlistInstrument\(list\.watchlistId, match\.globalInstrumentId\)/);
  assert.match(workspace, /Saved to \$\{regionalWatchlistName\(searchSelectedMatch.region\)\}/);
  assert.match(workspace, /selectedGlobalInstrumentId=\{selectedResearchInstrumentId\}/);
});

test("searched stock is never presented as a portfolio position (no quantity/cost/P&L)", () => {
  const drawerStart = workspace.indexOf("function StockResearchDrawer");
  const drawerEnd = workspace.indexOf("function AllocationCharts", drawerStart);
  const drawer = workspace.slice(drawerStart, drawerEnd);
  assert.match(drawer, /position\?: PortfolioPosition/);
  const nonHeld = drawer.slice(drawer.indexOf("<h3>Watchlist status"), drawer.indexOf('<section><h3>Market Data'));
  assert.doesNotMatch(nonHeld, /Quantity|Average Cost|Market Value|Unrealized P\/L/);
  assert.match(drawer, /Watchlist status<\/h3><p>Public company research<\/p>/);
});

test("drawer projection reads durable provider identity, sector, industry, and market-data facts", () => {
  const drawerStart = workspace.indexOf("function StockResearchDrawer");
  const drawerEnd = workspace.indexOf("function AllocationCharts", drawerStart);
  const drawer = workspace.slice(drawerStart, drawerEnd);
  assert.match(drawer, /market\?\.resolution\.providerTicker/);
  assert.match(workspace, /research\?\.structuredMarket\?\.facts\[key\]/);
  assert.match(drawer, /research\?\.valuation\.state/);
  assert.match(drawer, /structuredFact\(research, "sector"\)/);
  assert.match(drawer, /structuredFact\(research, "industry"\)/);
  assert.match(drawer, /structuredFact\(research, "latestPrice"\)/);
  assert.match(drawer, /currentPrice/);
});

test("the portfolio/watchlist company select is preserved for non-search contexts", () => {
  assert.match(workspace, /className="research-company-select"/);
  assert.match(workspace, /<option key=\{`\$\{option\.value\}-\$\{option\.companyName\}`\} value=\{option\.value\} title=\{option\.companyName\}>/);
  assert.match(workspace, /\{option\.companyName\}/);
  assert.match(workspace, /title=\{option\.companyName\}/);
  assert.match(workspace, /join\(" · "\)/);
  assert.match(workspace, /function legacyCompositeCompanyName\(/);
  assert.match(workspace, /segments\.length >= 3 && segments\[2\] \? segments\[2\] : null/);
});

test("canRefreshResearchIdentity and the global identity selector are unchanged", () => {
  assert.match(workspace, /selectedResearchInstrumentId=\{selectedResearchInstrumentId\}/);
  assert.match(workspace, /selectedContextResearchCompany\?\.status\s*\?\? \(selectedContextResearchCompany \? undefined : selectedResearchOption\?\.status\)/);
  assert.match(workspace, /"COMPANY_NOT_RESOLVED"/);
  assert.match(workspace, /"RESEARCH_NOT_APPLICABLE"/);
  assert.match(workspace, /"ETF_UNSUPPORTED"/);
});

test("sector performance remains backend-owned and the dashboard keeps its single call", () => {
  assert.equal((workspace.match(/portfolioApi\.getSectorPerformance\(/g) ?? []).length, 1);
  assert.match(api, /getSectorPerformance: \(region: SectorPerformance\["region"\], sector: string, period: SectorPerformance\["period"\]\) =>/);
});

test("selected search identity is readable and never shows position-style wording", () => {
  const smallStart = workspace.indexOf('<small className="research-search-selected"');
  const smallEnd = workspace.indexOf(") : null}", smallStart);
  const small = workspace.slice(smallStart, smallEnd);
  assert.match(small, /className="research-search-name"/);
  assert.match(small, /searchPresentation\?\.companyName \?\? searchSelectedMatch\?\.companyName \?\? selectedResearchInstrumentId/);
  assert.match(small, /className="research-search-symbol"/);
  assert.match(small, /searchSelectedMatch\?\.canonicalSymbol \?\? searchSelectedMatch\?\.symbol/);
  assert.match(small, /Public company research/);
  assert.match(small, /Not held/);
  assert.doesNotMatch(small, /Quantity|Average Cost|Market Value|Unrealized P\/L|Qty/);
});

test("research filters are grouped as a secondary block below the primary identity", () => {
  assert.match(workspace, /className="research-controls-filters"/);
  assert.match(workspace, /flexBasis: "100%"/);
  assert.match(workspace, /<div className="research-controls">[\s\S]*<div className="research-controls-filters"/);
  assert.match(workspace, /className="research-company-select"/);
});

test("search autocomplete debounces input and discards stale responses", () => {
  assert.match(workspace, /const DEBOUNCE_MS = 300;/);
  assert.match(workspace, /trimmed\.length < 3/);
  assert.match(workspace, /let cancelled = false;/);
  assert.match(workspace, /if \(cancelled\) return;/);
  assert.match(workspace, /clearTimeout\(active\)/);
  assert.match(workspace, /researchApi\.searchInstruments\(region, trimmed, 20\)/);
  assert.match(workspace, /results\.slice\(0, 20\)/);
  assert.match(workspace, /onSelect=\{handleSearchSelect\}/);
});

test("research search input is full width and its listbox is position-contained", () => {
  const styles = readFileSync(new URL("../app/styles.css", import.meta.url), "utf8");
  assert.match(styles, /research-search-input \{[\s\S]*?width: 100%/);
  assert.match(styles, /research-search-control \{[\s\S]*?position: relative/);
  assert.match(styles, /research-search-input \{[\s\S]*?min-height: 52px/);
});

test("autocomplete result rows have explicit theme-safe colors in all states", () => {
  const styles = readFileSync(new URL("../app/styles.css", import.meta.url), "utf8");
  assert.match(styles, /\.research-search-listbox li \{[\s\S]*?background:[\s\S]*?color:/);
  assert.match(styles, /\.research-search-listbox li:hover \{[\s\S]*?background:[\s\S]*?color:/);
  assert.match(styles, /\.research-search-listbox li\.highlighted[\s\S]*?color:/);
  assert.match(styles, /li\[aria-selected="true"\]/);
  assert.match(styles, /\.research-search-listbox li\.selected/);
  assert.match(styles, /li:focus-visible/);
});

test("research option colors have theme-safe dark-mode overrides", () => {
  const styles = readFileSync(new URL("../app/styles.css", import.meta.url), "utf8");
  assert.match(styles, /--research-option-bg: #f8fafc;/);
  assert.equal((styles.match(/--research-option-bg: #1e2b3a;/g) ?? []).length, 2);
  assert.equal((styles.match(/--research-option-hover: #334457;/g) ?? []).length, 2);
  assert.equal((styles.match(/--research-option-text: #f1f5f9;/g) ?? []).length, 2);
  assert.equal((styles.match(/--research-option-muted: #94a3b8;/g) ?? []).length, 2);
});
