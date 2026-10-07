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
  // Superseded: the authoritative product rule (status != READY_FRESH =>
  // Upload Evidence must be offered, for every requirement) means
  // VALUATION_INPUTS, LATEST_PRICE, HISTORICAL_PRICE_SERIES and
  // SECTOR_MACRO now ALSO have genuine backend contracts -- see
  // app.manual_evidence.EvidenceType and the final report.
  assert.match(api, /"VALUATION_INPUTS"/);
  assert.match(api, /"LATEST_PRICE"/);
  assert.match(api, /"HISTORICAL_PRICE_SERIES"/);
  assert.match(api, /"SECTOR_MACRO"/);
  // Defect 2 closure: PNG/JPG now have a genuine bounded OCR extraction
  // path server-side, so the typed contract includes them; DOCX still has
  // no extraction path at all and must stay excluded.
  // Superseded (reusable DocumentExtractor closure): DOCX now has a
  // genuine native extraction path (paragraphs + tables via python-docx),
  // gated by the same truthful runtime-capability pattern as PNG/JPG.
  assert.match(api, /EvidenceFileType = "CSV" \| "PDF" \| "TXT" \| "PNG" \| "JPG" \| "DOCX"/);
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

test("needsManualEvidence is true for every status except READY_FRESH (the authoritative universal rule)", () => {
  // Superseded: needsManualEvidence is no longer a frontend status
  // allowlist. It is now exactly \`status !== "READY_FRESH\`\`; REFRESHING and
  // NOT_APPLICABLE are correctly excluded from the end-to-end visibility
  // decision by shouldOfferManualEvidenceUpload's separate supportedActions
  // check instead (the server's own authority -- see
  // ResearchRequirementReadiness._result() on the backend), not by this
  // function pretending to know about them.
  const expectTrue = [
    "MISSING", "PARTIAL", "READY_STALE", "FAILED", "CONFLICTING",
    "UNSUPPORTED", "REFRESHING", "NOT_APPLICABLE"
  ];
  for (const status of expectTrue) {
    assert.equal(needsManualEvidence(status), true, `${status} should need manual evidence`);
  }
  assert.equal(needsManualEvidence("READY_FRESH"), false);
});

test("shouldOfferManualEvidenceUpload hides Upload Evidence for REFRESHING/NOT_APPLICABLE via the server's own supportedActions, not a frontend status guess", () => {
  const supportedEvidenceTypes = ["SHAREHOLDING"];
  assert.equal(
    shouldOfferManualEvidenceUpload(
      { requirementId: "SHAREHOLDING", status: "REFRESHING", mandatory: true, supportedActions: ["FIND_DATA"] },
      supportedEvidenceTypes
    ),
    null
  );
  assert.equal(
    shouldOfferManualEvidenceUpload(
      { requirementId: "SHAREHOLDING", status: "NOT_APPLICABLE", mandatory: true, supportedActions: ["NOT_APPLICABLE"] },
      supportedEvidenceTypes
    ),
    null
  );
  // UNSUPPORTED deliberately keeps UPLOAD_EVIDENCE (only FIND_DATA is
  // stripped server-side), matching the task's explicit "unsupported/
  // unavailable automated acquisition => Upload Evidence MUST be shown".
  assert.equal(
    shouldOfferManualEvidenceUpload(
      { requirementId: "SHAREHOLDING", status: "UNSUPPORTED", mandatory: true, supportedActions: ["UPLOAD_EVIDENCE", "RUN_PARTIAL_ANALYSIS"] },
      supportedEvidenceTypes
    ),
    "SHAREHOLDING"
  );
});

// Defect 1 closure: the complete status x capability matrix for every
// evidence-type category the backend genuinely advertises through
// supportedEvidenceTypes (SHAREHOLDING, CURRENT_NEWS,
// ORDER_BOOK_CAPEX_GUIDANCE, GOVERNANCE_HISTORY, BUSINESS_QUALITY_FACTS,
// GROWTH_FACTS, BALANCE_SHEET_FACTS, QUARTERLY_FINANCIALS) crossed with
// every ResearchRequirementStatus value, for both a mandatory and a
// non-mandatory requirement. Upload Evidence must be visible iff:
// mandatory AND status in {MISSING, PARTIAL, READY_STALE, FAILED,
// CONFLICTING} AND the type is in supportedEvidenceTypes.
test("Defect 1/2: complete status x capability matrix for all twelve backend-supported evidence types", () => {
  // Superseded: all twelve evidence types the backend genuinely supports
  // (see app.manual_evidence.SUPPORTED_EVIDENCE_TYPES) now participate in
  // the SAME authoritative rule -- status !== READY_FRESH, no per-type
  // carve-out.
  const SUPPORTED_TYPES = [
    "SHAREHOLDING",
    "CURRENT_NEWS",
    "ORDER_BOOK_CAPEX_GUIDANCE",
    "GOVERNANCE_HISTORY",
    "BUSINESS_QUALITY_FACTS",
    "GROWTH_FACTS",
    "BALANCE_SHEET_FACTS",
    "QUARTERLY_FINANCIALS",
    "VALUATION_INPUTS",
    "LATEST_PRICE",
    "HISTORICAL_PRICE_SERIES",
    "SECTOR_MACRO"
  ];
  // supportedActions modeled exactly as the real backend computes them
  // (ResearchRequirementReadiness._result()): UPLOAD_EVIDENCE is present
  // for every status except READY_FRESH/REFRESHING/NOT_APPLICABLE, and
  // FIND_DATA is additionally stripped for UNSUPPORTED.
  const SUPPORTED_ACTIONS_BY_STATUS = {
    READY_FRESH: [],
    REFRESHING: [],
    NOT_APPLICABLE: [],
    UNSUPPORTED: ["UPLOAD_EVIDENCE", "RUN_PARTIAL_ANALYSIS"],
    READY_STALE: ["FIND_DATA", "UPLOAD_EVIDENCE", "RUN_PARTIAL_ANALYSIS"],
    PARTIAL: ["FIND_DATA", "UPLOAD_EVIDENCE", "RUN_PARTIAL_ANALYSIS"],
    MISSING: ["FIND_DATA", "UPLOAD_EVIDENCE", "RUN_PARTIAL_ANALYSIS"],
    FAILED: ["FIND_DATA", "UPLOAD_EVIDENCE", "RUN_PARTIAL_ANALYSIS"],
    CONFLICTING: ["FIND_DATA", "UPLOAD_EVIDENCE", "RUN_PARTIAL_ANALYSIS"]
  };
  const ALL_STATUSES = Object.keys(SUPPORTED_ACTIONS_BY_STATUS);
  const NEEDS_UPLOAD = new Set(["READY_STALE", "PARTIAL", "MISSING", "FAILED", "CONFLICTING", "UNSUPPORTED"]);

  for (const requirementId of SUPPORTED_TYPES) {
    for (const status of ALL_STATUSES) {
      const supportedActions = SUPPORTED_ACTIONS_BY_STATUS[status];
      const expectedWhenMandatory = NEEDS_UPLOAD.has(status) ? requirementId : null;
      assert.equal(
        shouldOfferManualEvidenceUpload({ requirementId, status, mandatory: true, supportedActions }, SUPPORTED_TYPES),
        expectedWhenMandatory,
        `mandatory ${requirementId} @ ${status} should ${expectedWhenMandatory ? "" : "NOT "}offer Upload Evidence`
      );
      // Non-mandatory never offers upload, regardless of status or capability.
      assert.equal(
        shouldOfferManualEvidenceUpload({ requirementId, status, mandatory: false, supportedActions }, SUPPORTED_TYPES),
        null,
        `non-mandatory ${requirementId} @ ${status} must never offer Upload Evidence`
      );
    }
  }

  // A type the backend does NOT advertise (empty supportedEvidenceTypes) never
  // offers Upload Evidence, however stale/missing/conflicting the status --
  // capability must come from the server, never be assumed client-side.
  for (const requirementId of SUPPORTED_TYPES) {
    for (const status of [...NEEDS_UPLOAD]) {
      assert.equal(
        shouldOfferManualEvidenceUpload(
          { requirementId, status, mandatory: true, supportedActions: SUPPORTED_ACTIONS_BY_STATUS[status] },
          []
        ),
        null,
        `${requirementId} @ ${status} must not offer Upload Evidence when the backend advertises no capability`
      );
    }
  }
});

// Superseded: LATEST_PRICE, HISTORICAL_PRICE_SERIES, VALUATION_INPUTS and
// SECTOR_MACRO now each have a genuine, narrowly-scoped manual contract
// (see app.manual_evidence -- structured price/observation/valuation-row/
// sector-summary fields, USER_UPLOAD provenance, lower authority than
// trusted automated sources) and the backend genuinely advertises all four
// in supportedEvidenceTypes. The authoritative product rule applies to
// them exactly like every other requirement.
test("Defect 1: LATEST_PRICE and the other three newly-supported types DO show Upload Evidence when genuinely non-fresh", () => {
  const NEWLY_SUPPORTED_TYPES = [
    "LATEST_PRICE",
    "HISTORICAL_PRICE_SERIES",
    "VALUATION_INPUTS",
    "SECTOR_MACRO"
  ];
  const SUPPORTED_TYPES = [
    "SHAREHOLDING", "CURRENT_NEWS", "ORDER_BOOK_CAPEX_GUIDANCE", "GOVERNANCE_HISTORY",
    "BUSINESS_QUALITY_FACTS", "GROWTH_FACTS", "BALANCE_SHEET_FACTS", "QUARTERLY_FINANCIALS",
    ...NEWLY_SUPPORTED_TYPES
  ];
  for (const requirementId of NEWLY_SUPPORTED_TYPES) {
    for (const status of ["READY_STALE", "PARTIAL", "MISSING", "FAILED", "CONFLICTING"]) {
      assert.equal(
        shouldOfferManualEvidenceUpload(
          { requirementId, status, mandatory: true, supportedActions: ["FIND_DATA", "UPLOAD_EVIDENCE", "RUN_PARTIAL_ANALYSIS"] },
          SUPPORTED_TYPES
        ),
        requirementId,
        `${requirementId} @ ${status} must offer Upload Evidence -- it now has a genuine manual contract`
      );
    }
    // READY_FRESH still hides it, same as every other requirement.
    assert.equal(
      shouldOfferManualEvidenceUpload({ requirementId, status: "READY_FRESH", mandatory: true }, SUPPORTED_TYPES),
      null
    );
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

test("Defect 2 / reusable extraction closure: PNG/JPG/DOCX all flow through the normal upload path, gated only by server capability", () => {
  assert.match(workspace, /REVIEWABLE_EVIDENCE_FILE_TYPES = \["PDF", "CSV", "TXT", "PNG", "JPG", "DOCX"\]/);
  assert.doesNotMatch(workspace, /typeLabel === "PNG" \|\| typeLabel === "JPG"/);
  assert.doesNotMatch(workspace, /Image text extraction is not available/);
  // Superseded: DOCX no longer has a hardcoded client-side refusal -- it
  // falls through to the same supportedFileTypes.includes(...) gate as
  // every other type (app.document_extraction.docx_extraction_available()
  // drives whether the server actually lists it).
  assert.doesNotMatch(workspace, /typeLabel === "DOCX"/);
  assert.doesNotMatch(workspace, /DOCX text extraction is not available/);
  assert.match(workspace, /accept=\{inputAccept\}/);
});

test("clipboard image paste reuses the exact same ingestFile path as Browse (no separate image-only code path)", () => {
  // The existing generic clipboard-file handler (handlePaste/ingestFile)
  // already supported arbitrary pasted files; Defect 2 enables PNG/JPG
  // through it rather than adding a parallel paste-image implementation.
  assert.match(workspace, /item\.kind === "file"/);
  assert.match(workspace, /void ingestFile\(file\)/);
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

test("F. VALUATION_INPUTS + MISSING => visible once the backend genuinely advertises it (reuses the FinancialFact row editor, EARNINGS_BASIS only)", () => {
  // Superseded: the backend now advertises VALUATION_INPUTS in
  // supportedEvidenceTypes, reusing the FinancialFact "facts" row schema
  // scoped to EARNINGS_BASIS only (eps/pat/net_income/net_profit) --
  // PE/PB/EV_EBITDA/FCF_YIELD/LATEST_USABLE_PRICE remain derived ratios
  // from trusted structured market data and are never accepted as manual
  // rows.
  const realisticSupportedEvidenceTypes = [
    "SHAREHOLDING", "CURRENT_NEWS",
    "BUSINESS_QUALITY_FACTS", "GROWTH_FACTS", "BALANCE_SHEET_FACTS", "QUARTERLY_FINANCIALS",
    "VALUATION_INPUTS"
  ];
  assert.equal(
    shouldOfferManualEvidenceUpload(
      { requirementId: "VALUATION_INPUTS", status: "MISSING", mandatory: true },
      realisticSupportedEvidenceTypes
    ),
    "VALUATION_INPUTS"
  );
  assert.match(api, /"VALUATION_INPUTS"/);
  assert.match(api, /FINANCIAL_FACT_EVIDENCE_TYPES = \[[\s\S]*?"VALUATION_INPUTS"/);
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

// ---------------------------------------------------------------------------
// LATEST_PRICE / HISTORICAL_PRICE_SERIES / SECTOR_MACRO structured field
// schemas and rendering (flat scalar fields, not the FinancialFact row
// editor -- see MANUAL_FIELD_SCHEMA in app.manual_evidence).
// ---------------------------------------------------------------------------

test("AA. LATEST_PRICE + READY_STALE => Upload Evidence visible with its own manual schema", () => {
  assert.equal(
    shouldOfferManualEvidenceUpload(
      { requirementId: "LATEST_PRICE", status: "READY_STALE", mandatory: true },
      ["LATEST_PRICE"]
    ),
    "LATEST_PRICE"
  );
});

test("BB. HISTORICAL_PRICE_SERIES + PARTIAL => Upload Evidence visible", () => {
  assert.equal(
    shouldOfferManualEvidenceUpload(
      { requirementId: "HISTORICAL_PRICE_SERIES", status: "PARTIAL", mandatory: true },
      ["HISTORICAL_PRICE_SERIES"]
    ),
    "HISTORICAL_PRICE_SERIES"
  );
});

test("CC. SECTOR_MACRO + MISSING => Upload Evidence visible", () => {
  assert.equal(
    shouldOfferManualEvidenceUpload(
      { requirementId: "SECTOR_MACRO", status: "MISSING", mandatory: true },
      ["SECTOR_MACRO"]
    ),
    "SECTOR_MACRO"
  );
});

test("DD. LATEST_PRICE/HISTORICAL_PRICE_SERIES/SECTOR_MACRO are NOT FinancialFact row types -- they render as flat manual fields, not the facts-table editor", () => {
  assert.doesNotMatch(api, /FINANCIAL_FACT_EVIDENCE_TYPES = \[[\s\S]*?"LATEST_PRICE"/);
  assert.doesNotMatch(api, /FINANCIAL_FACT_EVIDENCE_TYPES = \[[\s\S]*?"HISTORICAL_PRICE_SERIES"/);
  assert.doesNotMatch(api, /FINANCIAL_FACT_EVIDENCE_TYPES = \[[\s\S]*?"SECTOR_MACRO"/);
});

test("EE. the observations field renders as a textarea (multi-line date,close rows), not a single-line input", () => {
  assert.match(dialogSource, /field === "observations" \? \(/);
  assert.match(dialogSource, /<textarea/);
  assert.match(dialogSource, /YYYY-MM-DD,close/);
});

test("FF. extracted OCR text is shown as reference-only in Review, never auto-applied to a manual field", () => {
  assert.match(workspace, /draft\.extractedText/);
  assert.match(workspace, /Extracted from file \(reference only -- not auto-filled\)/);
  assert.match(styles, /\.evidence-extracted-text/);
});

test("GG. fieldSuggestions is a declared, optional, non-binding type on EvidenceDraft", () => {
  assert.match(api, /fieldSuggestions\?: Record<string, string> \| null;/);
  assert.match(api, /Never a\s*\n\s*\/\/ proposedFact, never auto-applied/);
});

test("HH. CURRENT_NEWS field suggestions pre-fill the editable manual-fields form without bypassing explicit review", () => {
  // The suggestion seeds the SAME editable manualFields state the user
  // can freely overwrite -- it is never submitted directly, and the
  // reset effect only runs when a new draft loads (draft?.draftId).
  assert.match(
    workspace,
    /setManualFields\(draft\?\.fieldSuggestions \? \{ \.\.\.draft\.fieldSuggestions \} : \{\}\);/
  );
  assert.match(workspace, /\[draft\?\.draftId\]\);/);
  // The Review screen tells the user suggested fields were pre-filled
  // and must be reviewed -- it never claims they were auto-accepted.
  assert.match(workspace, /pre-filled from the uploaded\s*\n\s*file as a non-binding suggestion/);
});
