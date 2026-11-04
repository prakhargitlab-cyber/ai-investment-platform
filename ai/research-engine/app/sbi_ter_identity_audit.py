"""Audit staged SBI TER identities without guessing scheme-code-to-ISIN links.

This module does NOT certify identity documents: it consumes a separately
reviewed crosswalk, checks it against the current canonical ETF universe, and
reports which live staged SBI schemes remain unresolved. Scheme names are
display-only and NEVER used as join keys.

Supply staged observations from the freshly downloaded official workbook
(e.g. SbiTerRuntimeDownloader result workflow). No database writes or network.
"""
from dataclasses import dataclass
from uuid import UUID

from app.sbi_ter_identity import ReviewedSbiSchemeIdentity, build_reviewed_sbi_resolver


@dataclass(frozen=True)
class SbiIdentityAuditRow:
    scheme_code: str
    scheme_name: str
    status: str
    canonical_instrument_id: UUID | None
    reviewer_reference: str | None


def audit_staged_sbi_schemes(staged_observations, reviewed_records, canonical_etfs):
    """Return one output per current staged scheme; all unknown codes unresolved.

    Invalid, duplicate, ambiguous, or mismatched reviewed records raise
    via build_reviewed_sbi_resolver; none are silently accepted.
    """
    records = list(reviewed_records)
    resolver = build_reviewed_sbi_resolver(records, canonical_etfs)
    references = {item.scheme_code: item.approval_reference for item in records}
    seen = set()
    result = []
    for staged in staged_observations:
        code = staged.scheme_code
        if code in seen:
            raise ValueError("DUPLICATE_STAGED_SBI_SCHEME_CODE")
        seen.add(code)
        instrument_id = resolver(code)
        result.append(SbiIdentityAuditRow(
            scheme_code=code,
            scheme_name=staged.scheme_name,
            status="REVIEWED_CANONICAL_MATCH" if instrument_id else "IDENTITY_UNRESOLVED_STAGED_ONLY",
            canonical_instrument_id=instrument_id,
            reviewer_reference=references.get(code) if instrument_id else None,
        ))
    return tuple(sorted(result, key=lambda row: row.scheme_code))
