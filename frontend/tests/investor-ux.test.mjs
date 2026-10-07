import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { investorLabel } from "../app/lib/investor-labels.ts";

const radar = readFileSync(new URL("../app/components/opportunity-radar.tsx", import.meta.url), "utf8");
const workspace = readFileSync(new URL("../app/components/investment-workspace.tsx", import.meta.url), "utf8");

test("internal evidence codes map to investor-readable labels without mutation", () => {
  const codes = [
    "SUPPORT:BALANCE_SHEET",
    "SUPPORT:FUNDAMENTAL_BUSINESS_QUALITY",
    "SUPPORT:GROWTH",
    "SUPPORT:SHAREHOLDING",
    "UNAVAILABLE:NEWS_GEOPOLITICAL_EVENTS",
    "UNAVAILABLE:SECTOR"
  ];
  const original = [...codes];
  assert.deepEqual(codes.map(investorLabel), [
    "Balance sheet strength",
    "Business quality",
    "Growth",
    "Shareholding",
    "News and geopolitical evidence unavailable",
    "Sector evidence unavailable"
  ]);
  assert.deepEqual(codes, original);
});

test("recommendation cards prioritize the investor decision fields", () => {
  for (const label of [
    "Current price",
    "Entry range",
    "Accumulation range",
    "Target",
    "Horizon",
    "Evidence quality",
    "Why",
    "Risks"
  ]) {
    assert.ok(radar.includes(label), `missing recommendation-card label: ${label}`);
  }
  assert.match(radar, /top_positive_reasons[^\n]+investorLabel/);
  assert.match(radar, /top_negative_reasons[^\n]+investorLabel/);
});

test("dashboard hierarchy places portfolio state before Radar and market intelligence", () => {
  const multi = workspace.slice(
    workspace.indexOf("function MultiPortfolioDashboard"),
    workspace.indexOf("function MarketPerformanceRow")
  );
  const portfolioIndex = multi.indexOf("My portfolios");
  const multiRadarIndex = multi.indexOf("{radar}", portfolioIndex);
  assert.ok(portfolioIndex < multiRadarIndex);
  assert.ok(multiRadarIndex < multi.indexOf("Market intelligence"));

  const single = workspace.slice(
    workspace.indexOf("function DashboardView"),
    workspace.indexOf("function MovementPanel")
  );
  const snapshotIndex = single.indexOf("Portfolio snapshot");
  const singleRadarIndex = single.indexOf("{radar}", snapshotIndex);
  assert.ok(snapshotIndex < singleRadarIndex);
  assert.ok(singleRadarIndex < single.indexOf("Attention"));
});
