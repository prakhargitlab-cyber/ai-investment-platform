import type {
  ManualEvidenceType,
  ResearchReadinessRequirement,
  ResearchRequirementStatus
} from "./portfolio-api";

// Mirrors the UPLOAD_EVIDENCE literal in ResearchSupportedAction
// (app.research_readiness.ResearchSupportedAction) without importing a
// runtime enum across the backend/frontend boundary.
const UPLOAD_EVIDENCE_ACTION = "UPLOAD_EVIDENCE" as const;

// The frontend has a dedicated structured-row editor (see
// UploadEvidenceDialog's FinancialFactEditor in investment-workspace.tsx)
// for the FinancialFact-backed types (BUSINESS_QUALITY_FACTS, GROWTH_FACTS,
// BALANCE_SHEET_FACTS, QUARTERLY_FINANCIALS, VALUATION_INPUTS) and dedicated
// structured field schemas for the remaining non-event/non-document types
// (LATEST_PRICE, HISTORICAL_PRICE_SERIES, SECTOR_MACRO -- see
// MANUAL_FIELD_SCHEMA in app.manual_evidence). Visibility is now exactly:
// mandatory AND a genuine evidence gap (needsManualEvidence, i.e. status !=
// READY_FRESH) AND the server's own supportedEvidenceTypes list includes
// this requirement's id AND the server's own supportedActions for this
// requirement include UPLOAD_EVIDENCE. All four are server-driven or
// structural; none is a frontend-only guess.
export function manualEvidenceTypeForRequirement(
  requirement: Pick<ResearchReadinessRequirement, "requirementId" | "status">,
  supportedEvidenceTypes: readonly ManualEvidenceType[]
): ManualEvidenceType | null {
  return supportedEvidenceTypes.find(
    (candidate) => candidate === requirement.requirementId
  ) ?? null;
}

// Authoritative product rule: for EVERY requirement, READY_FRESH hides
// Upload Evidence and every other status shows it (READY_STALE, PARTIAL,
// MISSING, FAILED, CONFLICTING, UNSUPPORTED/unavailable automated
// acquisition, etc.) -- see app.research_readiness.ResearchRequirement
// Readiness._result() on the backend, which is the actual authority this
// mirrors: it zeroes out supported_actions (so UPLOAD_EVIDENCE disappears
// from requirement.supportedActions) only for READY_FRESH, REFRESHING
// (an acquisition is already in flight -- uploading now would race it) and
// NOT_APPLICABLE (nothing applies to this instrument, so there is nothing a
// manual upload could resolve). UNSUPPORTED deliberately keeps
// UPLOAD_EVIDENCE (only FIND_DATA is stripped), matching the task's
// explicit "unsupported/unavailable automated acquisition => Upload
// Evidence MUST be shown" requirement.
//
// This is intentionally NOT a frontend-only status allowlist: the only
// status literal compared here is READY_FRESH itself, and the exclusions
// above (REFRESHING, NOT_APPLICABLE) are enforced by requirement.
// supportedActions already omitting UPLOAD_EVIDENCE for those statuses on
// the server -- shouldOfferManualEvidenceUpload below still requires that
// flag, so the frontend can never show Upload Evidence for a status the
// backend itself has decided should not offer it, and can never hide it
// for a status the backend has decided should.
export function needsManualEvidence(status: ResearchRequirementStatus): boolean {
  return status !== "READY_FRESH";
}

// Upload availability is the AND of three independent conditions, kept
// deliberately separate rather than folded into one flag:
//   1. mandatory      - only a mandatory requirement is ever offered manual
//                        repair; an IMPORTANT/SUPPORTING gap does not block
//                        research readiness, so there is nothing to repair.
//   2. need           - does the requirement's current readiness state
//                        actually call for evidence (see needsManualEvidence).
//   3. capability      - does this requirement's evidence type genuinely
//                        support USER_UPLOAD on the backend right now (see
//                        manualEvidenceTypeForRequirement, driven by the
//                        server's own supportedEvidenceTypes list -- never
//                        guessed client-side, and never missingInputIds).
// All three are required; capability or need alone must never be sufficient.
export function shouldOfferManualEvidenceUpload(
  requirement: Pick<ResearchReadinessRequirement, "requirementId" | "status" | "mandatory"> &
    Partial<Pick<ResearchReadinessRequirement, "supportedActions">>,
  supportedEvidenceTypes: readonly ManualEvidenceType[]
): ManualEvidenceType | null {
  if (!requirement.mandatory) {
    return null;
  }
  if (!needsManualEvidence(requirement.status)) {
    return null;
  }
  // Belt-and-suspenders against the server's own authority: when the
  // caller supplies supportedActions (every real
  // ResearchReadinessRequirement does -- the field is only optional here so
  // call sites that only need the status rule can omit it), never show
  // Upload Evidence unless the server's own supportedActions for THIS
  // requirement (computed by ResearchRequirementReadiness._result(), not
  // guessed here) actually lists UPLOAD_EVIDENCE. This is what keeps
  // REFRESHING and NOT_APPLICABLE correctly hidden without hardcoding those
  // statuses in this file.
  if (requirement.supportedActions && !requirement.supportedActions.includes(UPLOAD_EVIDENCE_ACTION)) {
    return null;
  }
  return manualEvidenceTypeForRequirement(requirement, supportedEvidenceTypes);
}
