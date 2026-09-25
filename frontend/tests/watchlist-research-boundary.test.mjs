import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const workspace = readFileSync(
  new URL("../app/components/investment-workspace.tsx", import.meta.url),
  "utf8"
);
const apiSource = readFileSync(
  new URL("../app/lib/portfolio-api.ts", import.meta.url),
  "utf8"
);

// Transpile portfolio-api.ts into a self-contained module so it can be loaded
// via a data: URL (data: URLs cannot resolve the relative `../config` import the
// file normally has). stub the runtime import to a local constant; frontendConfig
// is only read inside request(), never at module load.
const apiJs = ts.transpileModule(
  apiSource.replace(
    /import \{ frontendConfig \} from "\.\.\/config";/,
    'const frontendConfig = { apiBaseUrl: "", authDevLoginEnabled: false };'
  ),
  { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }
).outputText;

const apiModule = await import(
  `data:text/javascript;base64,${Buffer.from(apiJs).toString("base64")}`
);
const { normalizeCompany } = apiModule;

const BASE_COMPANY = {
  companyName: "Demo Co",
  ticker: "DEMO",
  exchange: "NSE",
  status: "STALE",
  evidenceCoverage: {},
  positiveEventsCount: 0,
  negativeEventsCount: 0,
  neutralEventsCount: 0,
  documentCount: 0,
  eventCount: 0,
  sourceCount: 0,
  freshness: "STALE",
  mode: "PARTIAL",
  missingCategories: [],
  valuation: { state: "UNSCORABLE", reason: "" }
};

test("missing ownershipIncreases, shareholdingChanges and currentQuarterCatalysts are normalized to [] at the API boundary", () => {
  // A successful backend response may omit every research-history array.
  const normalized = normalizeCompany({ ...BASE_COMPANY });

  assert.deepEqual(normalized.ownershipIncreases, []);
  assert.deepEqual(normalized.shareholdingChanges, []);
  assert.deepEqual(normalized.currentQuarterCatalysts, []);
  // The other optional histories are coerced too, so consumers never see undefined.
  assert.deepEqual(normalized.financialResultHistory, []);
  assert.deepEqual(normalized.balanceSheetHistory, []);
  assert.deepEqual(normalized.cashFlowHistory, []);
});

test("null research-array fields are normalized to [] and fully-populated arrays are preserved by reference (renders identically)", () => {
  const nulls = normalizeCompany({
    ...BASE_COMPANY,
    ownershipIncreases: null,
    currentQuarterCatalysts: null
  });
  assert.deepEqual(nulls.ownershipIncreases, []);
  assert.deepEqual(nulls.currentQuarterCatalysts, []);

  const populated = {
    ...BASE_COMPANY,
    ownershipIncreases: ["FII_FPI", "DII"],
    shareholdingChanges: [{ period: "Q1", holder: "PROMOTER", pct: 5 }],
    currentQuarterCatalysts: [
      { eventId: "e1", eventType: "CATALYST", impact: "POSITIVE", reliability: "HIGH", eventDate: "2024-01-01" }
    ]
  };
  const same = normalizeCompany(populated);
  // Fully populated: arrays are returned by reference, unchanged -> identical render.
  assert.equal(same.ownershipIncreases, populated.ownershipIncreases);
  assert.equal(same.shareholdingChanges, populated.shareholdingChanges);
  assert.equal(same.currentQuarterCatalysts, populated.currentQuarterCatalysts);
  assert.deepEqual(same.ownershipIncreases, ["FII_FPI", "DII"]);
});

test("company normalization is chained into single-company, watchlist and portfolio fetch methods", () => {
  assert.match(apiSource, /getCompanyPresentation[\s\S]*?\.then\(normalizeCompany\)/);
  assert.match(apiSource, /getWatchlistResearch[\s\S]*?company:\s*normalizeCompany\(/);
  assert.match(apiSource, /getPortfolioSummary[\s\S]*?\.map\(normalizeCompany\)/);
});

test("watchlist render condition safely handles watchlistResearch present without instruments", () => {
  // The crash: `watchlistResearch?.instruments.length` throws when `instruments`
  // is absent. It is now optional-chained, and absent instruments is guarded
  // elsewhere with `?? []`.
  assert.match(workspace, /watchlistResearch\?\.instruments\?\.length/);
  assert.doesNotMatch(workspace, /watchlistResearch\?\.instruments\.length/);
  assert.match(workspace, /watchlistResearch\?\.instruments \?\? \[\]/);
});

test("research rows reuse normalized arrays and never call .length/.map directly on optional backend arrays", () => {
  // No direct .length/.map on the optional company arrays (the crash sources).
  assert.doesNotMatch(workspace, /company\.ownershipIncreases\.length/);
  assert.doesNotMatch(workspace, /company\.shareholdingChanges\.length/);
  assert.doesNotMatch(workspace, /company\.currentQuarterCatalysts\.length/);
  assert.doesNotMatch(workspace, /company\.ownershipIncreases\.map/);
  assert.doesNotMatch(workspace, /company\.currentQuarterCatalysts\.map/);

  // The normalized consts (mirroring the existing Branch A pattern) are present.
  assert.match(workspace, /const ownershipIncreases = company\.ownershipIncreases \?\? \[\]/);
  assert.match(workspace, /const shareholdingChanges = company\.shareholdingChanges \?\? \[\]/);
  assert.match(workspace, /const catalysts = company\.currentQuarterCatalysts \?\? \[\]/);

  // A fully-populated response still renders identically (the .map invocation is retained).
  assert.match(workspace, /ownershipIncreases\.map\(/);
  assert.match(workspace, /catalysts\.length/);
  assert.match(workspace, /shareholdingChanges\.length/);
});
