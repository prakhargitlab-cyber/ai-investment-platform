import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const workspace = readFileSync(new URL("../app/components/investment-workspace.tsx", import.meta.url), "utf8");
const api = readFileSync(new URL("../app/lib/portfolio-api.ts", import.meta.url), "utf8");
const styles = readFileSync(new URL("../app/styles.css", import.meta.url), "utf8");
const capabilitySource = readFileSync(new URL("../app/lib/manual-evidence-capability.ts", import.meta.url), "utf8");
const capabilityModule = { exports: {} };
new Function(
  "module",
  "exports",
  ts.transpileModule(capabilitySource, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
  }).outputText
)(capabilityModule, capabilityModule.exports);
const { manualEvidenceTypeForRequirement, needsManualEvidence, shouldOfferManualEvidenceUpload } = capabilityModule.exports;

test("portfolio API exposes one strongly typed manual-evidence contract", () => {
  assert.match(api, /MANUAL_EVIDENCE_TYPES = \[/);
  assert.match(api, /"SHAREHOLDING"/);
  assert.match(api, /"CURRENT_NEWS"/);
  assert.match(api, /"BUSINESS_QUALITY_FACTS"/);
  assert.match(api, /"GROWTH_FACTS"/);
  assert.match(api, /"BALANCE_SHEET_FACTS"/);
  assert.match(api, /"QUARTERLY_FINANCIALS"/);
  assert.doesNotMatch(api, /"VALUATION_INPUTS"/);
  assert.match(api, /EvidenceFileType = "CSV" \| "PDF" \| "TXT"/);
  assert.doesNotMatch(api, /EvidenceFileType = [^;]*(?:PNG|JPG|DOCX)/);
  assert.match(api, /supportedFileTypes: EvidenceFileType\[\]/);
  assert.match(api, /supportedEvidenceTypes: ManualEvidenceType\[\]/);
  assert.match(api, /uploadEvidence: \(globalInstrumentId: string, evidenceType: ManualEvidenceType, file: File\)/);
  assert.match(api, /getEvidenceDraft:/);
  assert.match(api, /acceptEvidence:/);
});

test("uploadEvidence uses the existing multipart draft endpoint and FastAPI query contract", () => {
  assert.match(api, /global_instrument_id: globalInstrumentId/);
  assert.match(api, /evidence_type: evidenceType/);
  assert.match(api, /const formData = new FormData\(\)/);
  assert.match(api, /formData\.append\("file", file\)/);
  assert.match(api, /\/api\/v1\/research\/evidence\/draft\?\$\{params\.toString\(\)\}/);
  assert.match(api, /method: "POST"/);
});

test("acceptance remains a separate explicit JSON request", () => {
  assert.match(api, /acceptEvidence: \(draftId: string, payload\?: EvidenceAcceptRequest\)/);
  assert.match(api, /\/api\/v1\/research\/evidence\/draft\/\$\{draftId\}\/accept/);
  assert.match(api, /body: JSON\.stringify\(payload \?\? \{\}\)/);
  const ingest = workspace.slice(workspace.indexOf("async function ingestEvidence"), workspace.indexOf("async function acceptEvidenceDraft"));
  assert.match(ingest, /portfolioApi\.uploadEvidence/);
  assert.doesNotMatch(ingest, /acceptEvidence/);
});

test("upload availability is gated by the server-supported evidence type AND by readiness need", () => {
  assert.match(workspace, /portfolioApi\.getEvidenceFileTypes\(\)/);
  assert.match(workspace, /shouldOfferManualEvidenceUpload\(requirement, supportedEvidenceTypes\)/);
  const row = workspace.slice(workspace.indexOf("function ResearchReadinessRow"), workspace.indexOf("function StockRuleEngineBreakdown"));
  assert.match(row, /\{evidenceType \? <Button[\s\S]*?>Upload Evidence<\/Button> : null\}/);
  assert.doesNotMatch(row, /supportedActions\.includes\("UPLOAD_EVIDENCE"\)/);
  assert.match(workspace, /onUploadEvidence\(requirement\.requirementId, evidenceType\)/);
  assert.doesNotMatch(workspace, /dialog\.globalInstrumentId,\s*"SHAREHOLDING"/);
  assert.match(workspace, /dialog\.globalInstrumentId,\s*dialog\.evidenceType/);
});

// Capability != need. A requirement can be capable of manual evidence
// (SHAREHOLDING) yet not currently need it (READY_FRESH).
test("1. SHAREHOLDING + READY_FRESH + UPLOAD_EVIDENCE supported action => Upload Evidence NOT offered", () => {
  assert.equal(
    shouldOfferManualEvidenceUpload(
      { requirementId: "SHAREHOLDING", status: "READY_FRESH", mandatory: true },
      ["SHAREHOLDING"]
    ),
    null
  );
});

test("2. SHAREHOLDING + READY_FRESH + missingInputs=[PROMOTER_PLEDGE] => Upload Evidence NOT offered", () => {
  // missingInputIds is irrelevant to the gate: only requirement.status drives it.
  assert.equal(
    shouldOfferManualEvidenceUpload(
      { requirementId: "SHAREHOLDING", status: "READY_FRESH", mandatory: true },
      ["SHAREHOLDING"]
    ),
    null
  );
  assert.equal(needsManualEvidence("READY_FRESH"), false);
});

test("3. SHAREHOLDING + READY_STALE => Upload Evidence offered", () => {
  assert.equal(
    shouldOfferManualEvidenceUpload({ requirementId: "SHAREHOLDING", status: "READY_STALE", mandatory: true }, ["SHAREHOLDING"]),
    "SHAREHOLDING"
  );
});

test("4. SHAREHOLDING + PARTIAL => Upload Evidence offered", () => {
  assert.equal(
    shouldOfferManualEvidenceUpload({ requirementId: "SHAREHOLDING", status: "PARTIAL", mandatory: true }, ["SHAREHOLDING"]),
    "SHAREHOLDING"
  );
});

test("5. SHAREHOLDING + MISSING/FAILED => Upload Evidence offered", () => {
  for (const status of ["MISSING", "FAILED"]) {
    assert.equal(
      shouldOfferManualEvidenceUpload({ requirementId: "SHAREHOLDING", status, mandatory: true }, ["SHAREHOLDING"]),
      "SHAREHOLDING"
    );
  }
});

test("6. unsupported evidence type + stale/missing => Upload Evidence NOT offered", () => {
  for (const status of ["MISSING", "READY_STALE", "PARTIAL", "FAILED"]) {
    assert.equal(
      shouldOfferManualEvidenceUpload({ requirementId: "CURRENT_NEWS", status, mandatory: true }, ["SHAREHOLDING"]),
      null
    );
  }
});

test("7. CURRENT_NEWS + READY_STALE: Find Data preserved, Upload Evidence absent without genuine backend support", () => {
  assert.equal(
    shouldOfferManualEvidenceUpload({ requirementId: "CURRENT_NEWS", status: "READY_STALE", mandatory: true }, ["SHAREHOLDING"]),
    null
  );
  const row = workspace.slice(workspace.indexOf("function ResearchReadinessRow"), workspace.indexOf("function StockRuleEngineBreakdown"));
  assert.match(row, /supportedActions\.includes\("FIND_DATA"\)/);
});

// Task G: the Manual Evidence UI fix is generalized beyond SHAREHOLDING.
// CURRENT_NEWS now has genuine backend USER_UPLOAD support (confirmed by
// the server's supportedEvidenceTypes, never guessed client-side), so the
// production example (Infosys NEWS GEOPOLITICAL EVENTS, CURRENT_NEWS,
// READY_STALE) must show BOTH Find Data and Upload Evidence.
test("G1. CURRENT_NEWS + READY_STALE + backend capability => Find Data AND Upload Evidence both offered", () => {
  assert.equal(
    shouldOfferManualEvidenceUpload(
      { requirementId: "CURRENT_NEWS", status: "READY_STALE", mandatory: true },
      ["SHAREHOLDING", "CURRENT_NEWS"]
    ),
    "CURRENT_NEWS"
  );
  const row = workspace.slice(workspace.indexOf("function ResearchReadinessRow"), workspace.indexOf("function StockRuleEngineBreakdown"));
  assert.match(row, /supportedActions\.includes\("FIND_DATA"\)/);
});

test("G2. CURRENT_NEWS + READY_FRESH => Upload Evidence hidden even with backend capability", () => {
  assert.equal(
    shouldOfferManualEvidenceUpload(
      { requirementId: "CURRENT_NEWS", status: "READY_FRESH", mandatory: true },
      ["SHAREHOLDING", "CURRENT_NEWS"]
    ),
    null
  );
});

test("G3. non-mandatory requirement => Upload Evidence never offered, regardless of status or capability", () => {
  // Proves the new `requirement.mandatory` gate actually gates something,
  // independently of needsManualEvidence/manualEvidenceTypeForRequirement
  // (every canonical Research Readiness requirement is mandatory in
  // production today, but the predicate must not silently depend on that).
  for (const status of ["MISSING", "PARTIAL", "READY_STALE", "FAILED"]) {
    assert.equal(
      shouldOfferManualEvidenceUpload(
        { requirementId: "CURRENT_NEWS", status, mandatory: false },
        ["SHAREHOLDING", "CURRENT_NEWS"]
      ),
      null
    );
    assert.equal(
      shouldOfferManualEvidenceUpload(
        { requirementId: "SHAREHOLDING", status, mandatory: false },
        ["SHAREHOLDING", "CURRENT_NEWS"]
      ),
      null
    );
  }
});

test("G4. an evidence type still unsupported by the backend never exposes a dead Upload button", () => {
  // e.g. VALUATION_INPUTS: mandatory, and may well be MISSING/STALE, but the
  // backend genuinely does not support USER_UPLOAD for it (see TASK 5 of
  // the manual-evidence generalization), so supportedEvidenceTypes never
  // lists it and the predicate must stay null.
  for (const status of ["MISSING", "PARTIAL", "READY_STALE", "FAILED"]) {
    assert.equal(
      shouldOfferManualEvidenceUpload(
        { requirementId: "VALUATION_INPUTS", status, mandatory: true },
        ["SHAREHOLDING", "CURRENT_NEWS"]
      ),
      null
    );
  }
});

test("needsManualEvidence is true only for MISSING, PARTIAL, READY_STALE, FAILED", () => {
  const expectTrue = ["MISSING", "PARTIAL", "READY_STALE", "FAILED"];
  const expectFalse = ["READY_FRESH", "CONFLICTING", "UNSUPPORTED", "REFRESHING", "NOT_APPLICABLE"];
  for (const status of expectTrue) {
    assert.equal(needsManualEvidence(status), true, `${status} should need manual evidence`);
  }
  for (const status of expectFalse) {
    assert.equal(needsManualEvidence(status), false, `${status} should not need manual evidence`);
  }
});

test("unsupported requirements such as CURRENT_NEWS cannot open a usable uploader", () => {
  assert.equal(
    manualEvidenceTypeForRequirement({ requirementId: "CURRENT_NEWS", status: "MISSING" }, ["SHAREHOLDING"]),
    null
  );
  assert.match(workspace, /if \(!evidenceCapabilities\?\.supportedEvidenceTypes\.includes\(evidenceType\)\)/);
  assert.match(workspace, /Manual evidence upload is not supported for this requirement/);
  assert.match(api, /ManualEvidenceType = \(typeof MANUAL_EVIDENCE_TYPES\)\[number\]/);
});

test("Find Data remains independent from manual upload capability", () => {
  const row = workspace.slice(workspace.indexOf("function ResearchReadinessRow"), workspace.indexOf("function StockRuleEngineBreakdown"));
  assert.match(row, /supportedActions\.includes\("FIND_DATA"\)/);
  assert.match(row, /shouldOfferManualEvidenceUpload\(requirement, supportedEvidenceTypes\)/);
});

test("Browse and Paste converge on the same File ingestion function using standard clipboard APIs", () => {
  assert.match(workspace, /function UploadEvidenceDialog/);
  assert.match(workspace, /addEventListener\("paste", handlePaste\)/);
  assert.match(workspace, /item\.kind === "file"/);
  assert.match(workspace, /item\.getAsFile\(\)/);
  assert.doesNotMatch(workspace, /item\.getType\(/);
  assert.equal([...workspace.matchAll(/void ingestFile\(file\)/g)].length, 2);
  assert.match(workspace, /type="file"/);
});

test("PNG, JPEG and DOCX behavior is truthful and stops before draft upload", () => {
  assert.match(workspace, /REVIEWABLE_EVIDENCE_FILE_TYPES = \["PDF", "CSV", "TXT"\]/);
  assert.match(workspace, /typeLabel === "PNG" \|\| typeLabel === "JPG"/);
  assert.match(workspace, /Image text extraction is not available/);
  assert.match(workspace, /typeLabel === "DOCX"/);
  assert.match(workspace, /DOCX text extraction is not available/);
  assert.match(workspace, /accept=\{inputAccept\}/);
  assert.doesNotMatch(workspace, /Paste an image \(Ctrl\+V\)/);
});

test("an empty or invalid draft cannot be accepted or silently persisted", () => {
  assert.match(workspace, /draft\.proposedFacts\.length > 0/);
  assert.match(workspace, /draft\.validationResults\.valid/);
  assert.match(workspace, /disabled=\{accepting \|\| !canAccept\}/);
  assert.match(workspace, /No extractable facts were produced from this file/);
});

test("CURRENT_NEWS and other no-auto-extraction types get a manual fields form, not a dead facts table", () => {
  assert.match(workspace, /draft\.requiresManualFields/);
  assert.match(workspace, /draft\.manualFieldSchema\.map/);
  assert.match(workspace, /manualFieldsComplete/);
  assert.match(workspace, /This evidence type has no safe automatic extraction/);
  assert.match(workspace, /function UploadEvidenceDialog/);
});

test("review shows filename, evidence type, provenance, facts, corrections and validation", () => {
  assert.match(workspace, /<dt>Filename<\/dt>/);
  assert.match(workspace, /<dt>Evidence type<\/dt>/);
  assert.match(workspace, /<th>Source \/ provenance<\/th>/);
  assert.match(workspace, /draft\.proposedFacts\.map/);
  assert.match(workspace, /<th>Your correction<\/th>/);
  assert.match(workspace, /draft\.validationResults\.errors/);
  assert.match(workspace, /draft\.validationResults\.warnings/);
  assert.match(workspace, /draft\.validationResults\.conflicts/);
});

test("trusted-evidence conflicts require explicit reconciliation", () => {
  assert.match(workspace, /checked=\{reconcileConflicts\}/);
  assert.match(workspace, /payload\.reconcileWithConflicts = true/);
  assert.match(workspace, /!hasConflicts \|\| reconcileConflicts/);
});

test("review requires explicit Accept and offers Cancel without direct persistence", () => {
  assert.match(workspace, /Nothing is persisted until you explicitly accept this draft/);
  assert.match(workspace, /<Button onClick=\{handleAccept\}/);
  assert.match(workspace, /Accept evidence/);
  assert.match(workspace, />Cancel<\/Button>/);
  assert.match(workspace, /Choose another file/);
});

test("evidence dialog remains accessible and styled", () => {
  const dialog = workspace.slice(workspace.indexOf("function UploadEvidenceDialog"));
  assert.match(dialog, /return createPortal\(/);
  assert.match(dialog, /role="dialog"/);
  assert.match(dialog, /aria-modal="true"/);
  assert.match(dialog, /document\.body\.style\.overflow = "hidden"/);
  assert.match(dialog, /event\.key === "Escape"/);
  assert.match(styles, /\.evidence-upload-popup/);
  assert.match(styles, /\.evidence-facts-table/);
  assert.match(styles, /\.evidence-conflict-consent/);
});

// ---------------------------------------------------------------------------
// FinancialFact family (BUSINESS_QUALITY_FACTS / GROWTH_FACTS /
// BALANCE_SHEET_FACTS / QUARTERLY_FINANCIALS) structured editor.
// VALUATION_INPUTS remains unsupported by the backend and stays hidden.
// ---------------------------------------------------------------------------

test("A. BALANCE_SHEET_FACTS + MISSING => Upload Evidence visible", () => {
  assert.equal(
    shouldOfferManualEvidenceUpload(
      { requirementId: "BALANCE_SHEET_FACTS", status: "MISSING", mandatory: true },
      ["BALANCE_SHEET_FACTS"]
    ),
    "BALANCE_SHEET_FACTS"
  );
});

test("B. BALANCE_SHEET_FACTS + READY_FRESH => Upload Evidence hidden", () => {
  assert.equal(
    shouldOfferManualEvidenceUpload(
      { requirementId: "BALANCE_SHEET_FACTS", status: "READY_FRESH", mandatory: true },
      ["BALANCE_SHEET_FACTS"]
    ),
    null
  );
});

test("C. BUSINESS_QUALITY_FACTS + PARTIAL => Upload Evidence visible", () => {
  assert.equal(
    shouldOfferManualEvidenceUpload(
      { requirementId: "BUSINESS_QUALITY_FACTS", status: "PARTIAL", mandatory: true },
      ["BUSINESS_QUALITY_FACTS"]
    ),
    "BUSINESS_QUALITY_FACTS"
  );
});

test("D. GROWTH_FACTS + READY_STALE => Upload Evidence visible", () => {
  assert.equal(
    shouldOfferManualEvidenceUpload(
      { requirementId: "GROWTH_FACTS", status: "READY_STALE", mandatory: true },
      ["GROWTH_FACTS"]
    ),
    "GROWTH_FACTS"
  );
});

test("E. QUARTERLY_FINANCIALS + FAILED => Upload Evidence visible", () => {
  assert.equal(
    shouldOfferManualEvidenceUpload(
      { requirementId: "QUARTERLY_FINANCIALS", status: "FAILED", mandatory: true },
      ["QUARTERLY_FINANCIALS"]
    ),
    "QUARTERLY_FINANCIALS"
  );
});

test("F. VALUATION_INPUTS + MISSING => hidden because backend never advertises it", () => {
  // The server's supportedEvidenceTypes list genuinely never contains
  // VALUATION_INPUTS (app.manual_evidence.EvidenceType has no such member);
  // this models that real response shape rather than guessing client-side.
  const realisticSupportedEvidenceTypes = [
    "SHAREHOLDING", "CURRENT_NEWS",
    "BUSINESS_QUALITY_FACTS", "GROWTH_FACTS", "BALANCE_SHEET_FACTS", "QUARTERLY_FINANCIALS"
  ];
  assert.equal(
    shouldOfferManualEvidenceUpload(
      { requirementId: "VALUATION_INPUTS", status: "MISSING", mandatory: true },
      realisticSupportedEvidenceTypes
    ),
    null
  );
  assert.doesNotMatch(api, /"VALUATION_INPUTS"/);
});

const dialogSource = workspace.slice(workspace.indexOf("function UploadEvidenceDialog"));

test("G. the structured editor renders one row per fact and supports adding more", () => {
  assert.match(dialogSource, /factRows\.map\(\(row, index\) => \{/);
  assert.match(dialogSource, /addFactRow/);
  assert.match(dialogSource, /\+ Add fact/);
  assert.match(dialogSource, /removeFactRow\(index\)/);
  assert.match(dialogSource, /Proposed Financial Facts/);
});

test("H. the user can edit metric, value, period end, period type, unit and source per row", () => {
  assert.match(dialogSource, /updateFactRow\(index, \{ metric: event\.target\.value \}\)/);
  assert.match(dialogSource, /updateFactRow\(index, \{ value: event\.target\.value \}\)/);
  assert.match(dialogSource, /updateFactRow\(index, \{ periodEnd: event\.target\.value \}\)/);
  assert.match(dialogSource, /updateFactRow\(index, \{ periodType: event\.target\.value as FinancialFactRow\["periodType"\] \}\)/);
  assert.match(dialogSource, /updateFactRow\(index, \{ unit: event\.target\.value \}\)/);
  assert.match(dialogSource, /updateFactRow\(index, \{ sourceUrl: event\.target\.value \}\)/);
  // Metric choices come from the server's own allowed-metrics list, never
  // an arbitrary free-text field that could manufacture an unknown metric.
  assert.match(dialogSource, /allowedMetrics\.map\(\(metric\) =>/);
  assert.match(dialogSource, /<select[\s\S]*?aria-label=\{`Metric for row/);
});

test("I. an invalid row displays its validation error next to that row", () => {
  assert.match(workspace, /function financialFactRowErrors/);
  assert.match(dialogSource, /factRowErrorsByIndex\[index\]/);
  assert.match(dialogSource, /evidence-fact-row-invalid/);
  assert.match(dialogSource, /className="evidence-fact-row-error" role="alert"/);
  assert.match(workspace, /if \(!row\.metric\.trim\(\)\) issues\.push/);
  assert.match(workspace, /if \(Number\.isNaN\(Number\(row\.value\)\)\) issues\.push/);
});

test("J. each row keeps its own independent period end -- quarters are never collapsed", () => {
  // One <input type="date"> bound to row.periodEnd per row (via .map), not
  // a single shared field -- distinct quarters/periods stay distinct rows.
  assert.match(dialogSource, /type="date"[\s\S]{0,120}value=\{row\.periodEnd\}/);
  assert.match(dialogSource, /facts: factRows\.map\(\(row\) => \{/);
});

test("K. Accept sends the exact reviewed rows, backend field names verbatim", () => {
  assert.match(dialogSource, /payload\.corrections = \{\s*facts: factRows\.map/);
  assert.match(dialogSource, /const \{ reportingBasis, \.\.\.rest \} = row;/);
  assert.match(dialogSource, /reportingBasis\?\.trim\(\) \? \{ \.\.\.rest, reportingBasis: reportingBasis\.trim\(\) \} : rest/);
  // The backend row contract: metric/value/periodEnd/periodType/unit/sourceUrl,
  // never a frontend-invented name.
  assert.match(api, /metric: string;\s*value: string;\s*periodEnd: string;\s*periodType: "QUARTERLY" \| "ANNUAL" \| "";\s*reportingBasis\?: string;\s*unit: string;\s*sourceUrl: string;/);
});

test("L. a successful Accept refreshes Research Readiness from the server response", () => {
  assert.match(workspace, /const result = await portfolioApi\.acceptEvidence\(draft\.draftId, corrections \?\? \{\}\);/);
  assert.match(workspace, /setResearchReadiness\(result\.readiness \?\? null\);/);
});

test("M. a READY_FRESH readiness response removes Upload Evidence for that requirement", () => {
  assert.equal(
    shouldOfferManualEvidenceUpload(
      { requirementId: "BALANCE_SHEET_FACTS", status: "READY_FRESH", mandatory: true },
      ["BALANCE_SHEET_FACTS"]
    ),
    null
  );
});

test("N. an incomplete readiness response keeps a truthful non-fresh status and Upload Evidence available", () => {
  for (const status of ["PARTIAL", "MISSING", "READY_STALE", "FAILED"]) {
    assert.equal(
      shouldOfferManualEvidenceUpload(
        { requirementId: "QUARTERLY_FINANCIALS", status, mandatory: true },
        ["QUARTERLY_FINANCIALS"]
      ),
      "QUARTERLY_FINANCIALS"
    );
  }
  // Readiness is only ever taken from the server's own response -- never
  // optimistically hardcoded to READY_FRESH anywhere in the accept flow.
  assert.doesNotMatch(workspace, /setResearchReadiness\(\{[^}]*READY_FRESH/);
});

test("O. SHAREHOLDING manual-evidence behavior is unchanged by the FinancialFact editor", () => {
  assert.equal(
    shouldOfferManualEvidenceUpload({ requirementId: "SHAREHOLDING", status: "PARTIAL", mandatory: true }, ["SHAREHOLDING"]),
    "SHAREHOLDING"
  );
  assert.equal(
    shouldOfferManualEvidenceUpload({ requirementId: "SHAREHOLDING", status: "READY_FRESH", mandatory: true }, ["SHAREHOLDING"]),
    null
  );
  // SHAREHOLDING still renders the proposed-facts correction table, never
  // the financial-fact row editor.
  assert.match(dialogSource, /!isFinancialFacts && !draft\.requiresManualFields && draft\.proposedFacts\.length/);
});

test("P. CURRENT_NEWS manual-evidence behavior is unchanged by the FinancialFact editor", () => {
  assert.equal(
    shouldOfferManualEvidenceUpload({ requirementId: "CURRENT_NEWS", status: "READY_STALE", mandatory: true }, ["CURRENT_NEWS"]),
    "CURRENT_NEWS"
  );
  // CURRENT_NEWS still takes the flat manual-fields branch, not the editor.
  assert.match(dialogSource, /\) : draft\.requiresManualFields \? \(/);
  assert.match(dialogSource, /draft\.manualFieldSchema\.map/);
});

test("Q. ORDER_BOOK_CAPEX_GUIDANCE + MISSING => Upload Evidence visible", () => {
  assert.equal(
    shouldOfferManualEvidenceUpload(
      { requirementId: "ORDER_BOOK_CAPEX_GUIDANCE", status: "MISSING", mandatory: true },
      ["ORDER_BOOK_CAPEX_GUIDANCE"]
    ),
    "ORDER_BOOK_CAPEX_GUIDANCE"
  );
});

test("R. ORDER_BOOK_CAPEX_GUIDANCE + READY_FRESH => Upload Evidence hidden", () => {
  assert.equal(
    shouldOfferManualEvidenceUpload(
      { requirementId: "ORDER_BOOK_CAPEX_GUIDANCE", status: "READY_FRESH", mandatory: true },
      ["ORDER_BOOK_CAPEX_GUIDANCE"]
    ),
    null
  );
});

test("S. GOVERNANCE_HISTORY + PARTIAL => Upload Evidence visible", () => {
  assert.equal(
    shouldOfferManualEvidenceUpload(
      { requirementId: "GOVERNANCE_HISTORY", status: "PARTIAL", mandatory: true },
      ["GOVERNANCE_HISTORY"]
    ),
    "GOVERNANCE_HISTORY"
  );
});

test("T. GOVERNANCE_HISTORY + READY_FRESH => Upload Evidence hidden", () => {
  assert.equal(
    shouldOfferManualEvidenceUpload(
      { requirementId: "GOVERNANCE_HISTORY", status: "READY_FRESH", mandatory: true },
      ["GOVERNANCE_HISTORY"]
    ),
    null
  );
});

test("U. ORDER_BOOK_CAPEX_GUIDANCE / GOVERNANCE_HISTORY render a restricted-event-type structured editor, not free text", () => {
  // The restricted branch renders BEFORE the generic requiresManualFields
  // branch and is gated on isRestrictedEventType, which is driven by
  // isRestrictedEventTypeEvidenceType (ORDER_BOOK_CAPEX_GUIDANCE /
  // GOVERNANCE_HISTORY only -- CURRENT_NEWS is excluded, see test P/W).
  assert.match(dialogSource, /draft\.requiresManualFields && isRestrictedEventType \? \(/);
  assert.match(dialogSource, /const isRestrictedEventType = Boolean\(draft && isRestrictedEventTypeEvidenceType\(draft\.evidenceType\)\)/);
  assert.match(dialogSource, /const allowedEventTypes = draft \? allowedEventTypesByEvidenceType\[draft\.evidenceType\] \?\? \[\] : \[\]/);
});

test("V. eventType/impact/timeHorizon render as server-driven <select> dropdowns, never free text, for restricted types", () => {
  const restrictedBranch = dialogSource.slice(
    dialogSource.indexOf("draft.requiresManualFields && isRestrictedEventType"),
    dialogSource.indexOf("draft.requiresManualFields ? (", dialogSource.indexOf("draft.requiresManualFields && isRestrictedEventType"))
  );
  assert.match(restrictedBranch, /if \(field === "eventType"\)/);
  assert.match(restrictedBranch, /allowedEventTypes\.map\(\(option\) => \(/);
  assert.match(restrictedBranch, /if \(field === "impact"\)/);
  assert.match(restrictedBranch, /eventImpactValues\.map\(\(option\) => \(/);
  assert.match(restrictedBranch, /if \(field === "timeHorizon"\)/);
  assert.match(restrictedBranch, /timeHorizonValues\.map\(\(option\) => \(/);
  // No bare free-text <input type="text"> is used for eventType/impact/
  // timeHorizon in this branch -- they are all <select> driven by the
  // server's own canonical lists.
  assert.doesNotMatch(restrictedBranch, /field === "eventType"[\s\S]{0,40}<input/);
});

test("W. CURRENT_NEWS keeps its existing free-text manual fields -- isRestrictedEventType is false for it", () => {
  // CURRENT_NEWS is not a member of RESTRICTED_EVENT_TYPE_EVIDENCE_TYPES,
  // so isRestrictedEventTypeEvidenceType("CURRENT_NEWS") is false and the
  // pre-existing generic free-text branch (tested in P) still renders.
  assert.match(api, /RESTRICTED_EVENT_TYPE_EVIDENCE_TYPES = \[\s*"ORDER_BOOK_CAPEX_GUIDANCE",\s*"GOVERNANCE_HISTORY"\s*\]/);
  assert.doesNotMatch(api, /RESTRICTED_EVENT_TYPE_EVIDENCE_TYPES = \[[^\]]*"CURRENT_NEWS"/);
});

test("X. Accept for restricted event types reuses the generic manual-fields submission path verbatim", () => {
  // handleAccept's requiresManualFields branch (shared by CURRENT_NEWS,
  // ORDER_BOOK_CAPEX_GUIDANCE and GOVERNANCE_HISTORY alike) builds
  // corrections from manualFields generically -- no separate payload
  // builder was introduced for the restricted types, so there is no risk
  // of the UI inventing a different field name for the same backend
  // contract (MANUAL_FIELD_SCHEMA is identical for all three).
  assert.match(workspace, /else if \(draft\?\.requiresManualFields\) \{/);
  assert.match(workspace, /Object\.entries\(manualFields\)\.forEach/);
});

test("Y. an evidence type still unsupported by the backend (e.g. ORDER_BOOK_CAPEX_GUIDANCE before rollout) never offers a dead button", () => {
  assert.equal(
    shouldOfferManualEvidenceUpload(
      { requirementId: "ORDER_BOOK_CAPEX_GUIDANCE", status: "MISSING", mandatory: true },
      ["SHAREHOLDING", "CURRENT_NEWS"]
    ),
    null
  );
  assert.equal(
    shouldOfferManualEvidenceUpload(
      { requirementId: "GOVERNANCE_HISTORY", status: "PARTIAL", mandatory: true },
      ["SHAREHOLDING", "CURRENT_NEWS"]
    ),
    null
  );
});

test("Z. portfolio API advertises the two new ResearchEvent-backed evidence types and their capability fields", () => {
  assert.match(api, /"ORDER_BOOK_CAPEX_GUIDANCE"/);
  assert.match(api, /"GOVERNANCE_HISTORY"/);
  assert.match(api, /allowedEventTypesByEvidenceType\?: Partial<Record<RestrictedEventTypeEvidenceType, string\[\]>>/);
  assert.match(api, /eventImpactValues\?: string\[\];/);
  assert.match(api, /timeHorizonValues\?: string\[\];/);
});
