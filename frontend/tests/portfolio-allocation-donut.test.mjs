import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const workspaceSource = fs.readFileSync(new URL('../app/components/investment-workspace.tsx', import.meta.url), 'utf8');

// Section 5 of the correction task requires three mandatory, interactive
// allocation donut modes (asset class / sector / P&L) on the Portfolio page,
// replacing the previous bar-list-only AllocationCharts. These tests assert
// the structural contract rather than rendering (the harness's
// renderToStaticMarkup cannot drive a tab click), matching the pattern
// already used elsewhere in this suite for interactive components.

test('Portfolio exposes three allocation donut modes: asset class, sector, and profit/loss', () => {
  assert.match(workspaceSource, /function AllocationDonutPanel/);
  assert.match(workspaceSource, /"ASSET_CLASS" \| "SECTOR" \| "PNL"/);
  assert.match(workspaceSource, /\["ASSET_CLASS", "Asset class"\]/);
  assert.match(workspaceSource, /\["SECTOR", "Sector"\]/);
  assert.match(workspaceSource, /\["PNL", "Profit \/ Loss"\]/);
});

test('sector and asset-class donut slices come from verified backend allocation metadata, never a name-based guess', () => {
  assert.match(workspaceSource, /summary\?\.allocation\.assetType/);
  assert.match(workspaceSource, /summary\?\.allocation\.sector/);
  // No heuristic sector-from-name helper introduced for this feature.
  assert.doesNotMatch(workspaceSource, /guessSector|inferSector|sectorFromName/);
});

test('the profit/loss donut never shows a signed loss as an ordinary positive allocation slice: it measures share of invested capital in winning vs losing positions, with exact amounts and counts', () => {
  assert.match(workspaceSource, /Gaining \(\$\{gainCount\}\)/);
  assert.match(workspaceSource, /Losing \(\$\{lossCount\}\)/);
  assert.match(workspaceSource, /Flat \(\$\{flatCount\}\)/);
  assert.match(workspaceSource, /Unvalued \(\$\{unvaluedCount\}\)/);
  assert.match(workspaceSource, /signed gains and losses cannot be shown as ordinary positive allocation slices/);
  // Exact amounts, not just percentages, are rendered per legend entry.
  assert.match(workspaceSource, /segment\.amount != null \? ` .*formatMoney\(segment\.amount, currency\)/);
});

test('Portfolio Allocation card renders the donut panel and keeps the prior country/currency/broker breakdowns available, not deleted', () => {
  assert.match(workspaceSource, /<AllocationDonutPanel summary=\{summary\} positions=\{rawPositions\} \/>/);
  assert.match(workspaceSource, /More breakdowns \(country, currency, broker\)/);
  assert.match(workspaceSource, /<AllocationCharts summary=\{summary\} \/>/);
});

test('the Dashboard radar summary cards never mount the full Equity/ETF Radar research terminals', () => {
  const radarTabsCallSites = workspaceSource.match(/<RadarTabs\b/g) ?? [];
  assert.ok(radarTabsCallSites.length >= 3);
  assert.match(workspaceSource, /onOpenEquityRadar=\{\(\) => setView\("equity-radar"\)\}/);
  assert.match(workspaceSource, /onOpenEtfRadar=\{\(\) => setView\("etf-radar"\)\}/);
});

test('Watchlist is a first-class, directly navigable sidebar workspace with its own dedicated presentation, reusing the existing watchlist data/service, never a duplicate store', () => {
  assert.match(workspaceSource, /\{ id: "watchlist", label: "Watchlist", icon: Eye \}/);
  assert.match(workspaceSource, /view === "watchlist" \?/);
  // Dedicated component (no longer the generic, Portfolio/Search-shaped
  // ResearchView) -- but it is fed the SAME state (watchlists,
  // watchlistResearch, researchContext) and the SAME selectResearchContext
  // entry point already used by Research: no second watchlist data model
  // or fetch path is introduced for it.
  assert.match(workspaceSource, /function WatchlistWorkspace/);
  assert.match(workspaceSource, /<WatchlistWorkspace\b/);
  assert.match(workspaceSource, /watchlistResearch=\{watchlistResearch\}/);
  assert.match(workspaceSource, /watchlists=\{watchlists\}/);
  assert.match(workspaceSource, /researchContext=\{researchContext\}/);
  // Exactly the 3 pre-existing getWatchlistResearch call sites -- the new
  // component only consumes props, it never fetches on its own.
  const fetchCallSites = (workspaceSource.match(/researchApi\.getWatchlistResearch\(/g) ?? []).length;
  assert.equal(fetchCallSites, 3);
  assert.match(workspaceSource, /onSelectWatchlist=\{\(watchlistId\) => selectResearchContext\(`watchlist:\$\{watchlistId\}`\)\}/);
  // The watchlist's own name is rendered exactly once in its dedicated
  // overview header, never repeated per instrument row.
  const watchlistStart = workspaceSource.indexOf('function WatchlistWorkspace');
  const watchlistEnd = workspaceSource.indexOf('function ResearchView', watchlistStart);
  const watchlistSource = workspaceSource.slice(watchlistStart, watchlistEnd);
  assert.match(watchlistSource, /<h2>\{researchContext\.name\}<\/h2>/);
  assert.doesNotMatch(watchlistSource, /positions\b/);
});
