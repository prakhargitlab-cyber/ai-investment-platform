import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const workspace = readFileSync(new URL("../app/components/investment-workspace.tsx", import.meta.url), "utf8");
const api = readFileSync(new URL("../app/lib/portfolio-api.ts", import.meta.url), "utf8");
const styles = readFileSync(new URL("../app/styles.css", import.meta.url), "utf8");
const runStart = workspace.indexOf("async function runStockRuleEngineAnalysis");
const runEnd = workspace.indexOf("function openMarketIntelligenceResearch", runStart);
const runPath = workspace.slice(runStart, runEnd);
const breakdownStart = workspace.indexOf("function StockRuleEngineBreakdown");
const breakdownEnd = workspace.indexOf("function readinessStatusTone", breakdownStart);
const breakdown = workspace.slice(breakdownStart, breakdownEnd);

test("Run Analysis uses the provider-free deterministic analysis endpoint", () => {
  assert.match(api, /analyze: \(globalInstrumentId: string, allowPartial: boolean\) =>[\s\S]*?\/api\/v1\/research\/analysis\/\$\{globalInstrumentId\}[\s\S]*?method: "POST"/);
  assert.match(runPath, /researchApi\.analyze\(dialog\.globalInstrumentId, allowPartial\)/);
  assert.doesNotMatch(runPath, /NSE|Yahoo|SEC|EODHD|provider|refresh|ensureReadiness|prefetch/);
});

test("backend eligibility exclusively controls full and partial analysis actions", () => {
  assert.match(workspace, /analysisEligibility\?\.fullAnalysisAllowed/);
  assert.match(workspace, /onRunAnalysis\(false\)/);
  assert.match(workspace, /analysisEligibility\?\.partialAnalysisAllowed/);
  assert.match(workspace, /onRunAnalysis\(true\)/);
});

test("score summary renders decision, quality, opportunity, risk, and separate confidence", () => {
  assert.match(breakdown, /analysis\.overallScore/);
  assert.match(breakdown, /analysis\.decisionSignal/);
  assert.match(breakdown, />Quality</);
  assert.match(breakdown, />Opportunity</);
  assert.match(breakdown, />Risk</);
  assert.match(breakdown, /analysis\.confidence} · \{analysis\.confidenceScore/);
  assert.doesNotMatch(breakdown, /overallScore\s*\*\s*confidence|confidenceScore\s*\*\s*overall/);
});

test("all explainable areas render weight, score, contribution, status, and details", () => {
  assert.match(breakdown, /analysis\.areaScores\.map/);
  assert.match(breakdown, /area\.weight/);
  assert.match(breakdown, /area\.rawScore/);
  assert.match(breakdown, /area\.weightedContribution/);
  assert.match(breakdown, /area\.status/);
  assert.match(breakdown, /area\.positiveFactors/);
  assert.match(breakdown, /area\.negativeFactors/);
  assert.match(breakdown, /area\.missingInputs/);
});

test("metric provenance links are generic and open in a safe new tab", () => {
  assert.match(breakdown, /metric\.source/);
  assert.match(breakdown, /metric\.rule/);
  assert.match(breakdown, /href=\{metric\.sourceUrl\} target="_blank" rel="noreferrer">View source/);
  assert.match(breakdown, /area\.sourceReferences/);
  assert.match(breakdown, /href=\{source\.sourceUrl\} target="_blank" rel="noreferrer"/);
  assert.doesNotMatch(breakdown, /sourceProvider\s*===|NSE|Yahoo|SEC|EODHD/);
});

test("risk overrides are visibly presented above the area table", () => {
  assert.ok(breakdown.indexOf("rule-engine-overrides") < breakdown.indexOf("rule-engine-area-table"));
  assert.match(breakdown, /override\.severity/);
  assert.match(breakdown, /override\.effect/);
  assert.match(styles, /\.rule-engine-overrides/);
});

test("frontend has presentation semantics but no scoring, LLM, or provider rules", () => {
  assert.doesNotMatch(breakdown, /trailingPE|ROCE|debtToEquity|CAGR|prompt|LLM|MCP/);
  assert.match(styles, /\.rule-engine-result-ready/);
  assert.match(styles, /\.rule-engine-result-danger/);
  assert.match(api, /ruleEngineVersion: string/);
});
