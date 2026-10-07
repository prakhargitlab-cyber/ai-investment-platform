import type {
  ManualEvidenceType,
  ResearchReadinessRequirement,
  ResearchRequirementStatus
} from "./portfolio-api";

// The frontend now has a dedicated structured-row editor (see
// UploadEvidenceDialog's FinancialFactEditor in investment-workspace.tsx)
// for the FinancialFact-backed types (BUSINESS_QUALITY_FACTS, GROWTH_FACTS,
// BALANCE_SHEET_FACTS, QUARTERLY_FINANCIALS), so the temporary UI-capable
// allowlist that used to hide them pending that editor's existence has been
// removed. Visibility is now exactly: mandatory AND a genuine evidence gap
// (needsManualEvidence) AND the server's own supportedEvidenceTypes list
// includes this requirement's id -- VALUATION_INPUTS stays hidden because
// the backend never advertises it (see app.manual_evidence.EvidenceType),
// not because of any separate frontend restriction.
export function manualEvidenceTypeForRequirement(
  requirement: Pick<ResearchReadinessRequirement, "requirementId" | "status">,
  supportedEvidenceTypes: readonly ManualEvidenceType[]
): ManualEvidenceType | null {
  return supportedEvidenceTypes.find(
    (candidate) => candidate === requirement.requirementId
  ) ?? null;
}

// Statuses that represent a genuine evidence gap: missing, partial,
// stale, or a failed acquisition. A requirement in one of these states
// may benefit from manually supplied evidence.
const STATUSES_NEEDING_MANUAL_EVIDENCE: ReadonlySet<ResearchRequirementStatus> = new Set([
  "MISSING",
  "PARTIAL",
  "READY_STALE",
  "FAILED"
]);

// Whether the requirement's current readiness state indicates a need for
// manual evidence. This is independent of whether manual evidence is even
// supported for the requirement (see manualEvidenceTypeForRequirement) and
// independent of whether the requirement is mandatory (see
// shouldOfferManualEvidenceUpload). READY_FRESH never needs manual
// evidence, even if some non-mandatory supporting input (e.g.
// PROMOTER_PLEDGE for SHAREHOLDING) is still missing: missing a supporting
// input does not make the requirement itself stale or incomplete under the
// existing readiness policy.
export function needsManualEvidence(status: ResearchRequirementStatus): boolean {
  return STATUSES_NEEDING_MANUAL_EVIDENCE.has(status);
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
  requirement: Pick<ResearchReadinessRequirement, "requirementId" | "status" | "mandatory">,
  supportedEvidenceTypes: readonly ManualEvidenceType[]
): ManualEvidenceType | null {
  if (!requirement.mandatory) {
    return null;
  }
  if (!needsManualEvidence(requirement.status)) {
    return null;
  }
  return manualEvidenceTypeForRequirement(requirement, supportedEvidenceTypes);
}
