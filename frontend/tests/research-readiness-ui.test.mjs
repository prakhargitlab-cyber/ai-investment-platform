import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const workspace = readFileSync(new URL("../app/components/investment-workspace.tsx", import.meta.url), "utf8");
const api = readFileSync(new URL("../app/lib/portfolio-api.ts", import.meta.url), "utf8");
const styles = readFileSync(new URL("../app/styles.css", import.meta.url), "utf8");

const openStart = workspace.indexOf("async function openResearchReadiness");
const openEnd = workspace.indexOf("async function findResearchData", openStart);
const readPath = workspace.slice(openStart, openEnd);
const ensureStart = openEnd;
const ensureEnd = workspace.indexOf("function openMarketIntelligenceResearch", ensureStart);
const ensurePath = workspace.slice(ensureStart, ensureEnd);
const dialogStart = workspace.indexOf("function ResearchReadinessDialog");
const dialogEnd = workspace.indexOf("function AuthPasswordInput", dialogStart);
const dialog = workspace.slice(dialogStart, dialogEnd);

test("Research opens the provider-free readiness popup by global instrument identity", () => {
  assert.match(workspace, /Research readiness\s*<\/Button>/);
  assert.match(readPath, /setResearchReadinessDialog\(\{ globalInstrumentId, companyName \}\)/);
  assert.match(readPath, /researchApi\.getReadiness\(globalInstrumentId\)/);
  assert.doesNotMatch(readPath, /ensureReadiness|\.refresh\(|prefetchVisible|ensureDefaultWatchlist|addWatchlistInstrument/);
});

test("readiness API separates read-only GET from targeted POST", () => {
  assert.match(api, /getReadiness: \(globalInstrumentId: string\) =>\s*request<ResearchReadiness>\(`\/api\/v1\/research\/readiness\/\$\{globalInstrumentId\}`\)/);
  assert.match(api, /ensureReadiness: \(globalInstrumentId: string, requirements\?: string\[\]\) =>[\s\S]*?method: "POST"[\s\S]*?JSON\.stringify\(requirements\?\.length \? \{ requirements \} : \{\}\)/);
});

test("Find Data sends only selected requirement IDs", () => {
  assert.match(ensurePath, /researchApi\.ensureReadiness\(dialog\.globalInstrumentId, requirements\)/);
  assert.match(dialog, /onFindData\(\[requirement\.requirementId\]\)/);
  assert.match(dialog, /findable\.map\(\(item\) => item\.requirementId\)/);
  assert.doesNotMatch(ensurePath, /NSE|SEC|EODHD|Yahoo|provider/);
});

test("readiness statuses map to the required visual tones", () => {
  assert.match(dialog, /status === "READY_FRESH"\) return "ready"/);
  assert.match(dialog, /\["READY_STALE", "PARTIAL", "REFRESHING"\]\.includes\(status\).*return "warning"/s);
  assert.match(dialog, /status === "UNSUPPORTED" \|\| status === "NOT_APPLICABLE"\) return "neutral"/);
  assert.match(dialog, /return "danger"/);
  for (const tone of ["ready", "warning", "danger", "neutral"]) {
    assert.match(styles, new RegExp(`\\.readiness-status-${tone}\\b`));
  }
});

test("popup shows provenance, freshness, reasons, upload placeholder, and analysis actions", () => {
  assert.match(dialog, /requirement\.area\.replaceAll/);
  assert.match(dialog, /requirement\.status\.replaceAll/);
  assert.match(dialog, /As of/);
  assert.match(dialog, /Source: \{requirement\.sourceProvider/);
  assert.match(dialog, /href=\{requirement\.sourceUrl\} target="_blank" rel="noreferrer"/);
  assert.match(dialog, /requirement\.missingReason/);
  assert.match(dialog, /requirement\.conflictReason/);
  assert.match(dialog, /"Finding…" : "Find Data"/);
  assert.match(dialog, />Upload Evidence<\/Button>/);
  assert.match(dialog, /analysisEligibility\?\.fullAnalysisAllowed/);
  assert.match(dialog, /analysisEligibility\?\.partialAnalysisAllowed/);
  assert.match(dialog, /"Run Partial Analysis"/);
});

test("quarterly projection opens a generic authoritative filing link", () => {
  assert.match(workspace, /Source: \{filingSourceLabel\(result\.sourceName\)\}/);
  assert.match(workspace, /href=\{result\.sourceUrl\} target="_blank" rel="noreferrer">View \{filingSourceLabel\(result\.sourceName\)\} Filing ↗<\/a>/);
  assert.match(workspace, /function filingSourceLabel\(sourceName: string\)/);
  assert.doesNotMatch(workspace.slice(workspace.indexOf("function filingSourceLabel"), workspace.indexOf("function AuthPasswordInput")), /NSE|SEC|EODHD/);
});

test("readiness popup reports completeness and data confidence without a stock score", () => {
  assert.match(dialog, /readiness\.overallCompletenessPct/);
  assert.match(dialog, /readiness\.criticalCompletenessPct/);
  assert.match(dialog, /readiness\.confidence/);
  assert.doesNotMatch(dialog, /stock score|investment score|final score/i);
});

test("readiness dialog is portaled, focus-contained, and locks background scrolling", () => {
  assert.match(workspace, /import \{ createPortal \} from "react-dom"/);
  assert.match(dialog, /return createPortal\([\s\S]*?document\.body/);
  assert.match(dialog, /document\.body\.style\.overflow = "hidden"/);
  assert.match(dialog, /appShell\.inert = true/);
  assert.match(dialog, /appShell\.setAttribute\("aria-hidden", "true"\)/);
  assert.match(dialog, /event\.key === "Escape"[\s\S]*?onClose\(\)/);
  assert.match(dialog, /event\.key !== "Tab"[\s\S]*?first[\s\S]*?last/);
  assert.match(dialog, /role="dialog"[\s\S]*?aria-modal="true"/);
  assert.match(dialog, /className="readiness-popup-header"/);
  assert.match(dialog, /className="readiness-popup-body"/);
});

test("readiness dialog has an opaque surface and one viewport-constrained scrolling body", () => {
  const backdrop = styles.match(/\.readiness-popup-backdrop \{[\s\S]*?\n\}/)?.[0] ?? "";
  const popup = styles.match(/\.readiness-popup \{[\s\S]*?\n\}/)?.[0] ?? "";
  const body = styles.match(/\.readiness-popup-body \{[\s\S]*?\n\}/)?.[0] ?? "";

  assert.match(backdrop, /position: fixed/);
  assert.match(backdrop, /z-index: 1000/);
  assert.match(backdrop, /background: rgb\(8 15 25 \/ 68%\)/);
  assert.match(popup, /max-height: min\(920px, calc\(100dvh/);
  assert.match(popup, /grid-template-rows: auto minmax\(0, 1fr\)/);
  assert.match(popup, /overflow: hidden/);
  assert.match(popup, /background: var\(--surface-raised\)/);
  assert.doesNotMatch(popup, /overflow(?:-y)?: auto/);
  assert.match(body, /overflow-y: auto/);
  assert.match(body, /overscroll-behavior: contain/);
});
