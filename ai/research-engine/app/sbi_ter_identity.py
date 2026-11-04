"""Fail-closed SBI NSDL scheme-code identity crosswalk.

Does not discover or certify real scheme-code -> ISIN links.
Only independently reviewed, source-backed declarations may be supplied.
A reviewer attests the documentary link; this module validates structure
and matches the attested ISIN + symbol to the current canonical ETF universe.
No fuzzy matching, network calls, or persistence.
"""
from dataclasses import dataclass
from datetime import date
import re
from urllib.parse import urlsplit
from uuid import UUID


@dataclass(frozen=True)
class ReviewedSbiSchemeIdentity:
    scheme_code: str
    isin: str
    ticker: str
    evidence_url: str
    evidence_sha256: str
    reviewed_by: str
    reviewed_on: date
    # Reviewer must confirm the SAME authoritative source connects the
    # NSDL scheme code to this ISIN, not merely a similar scheme name.
    approval_reference: str


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ValueError(code)


def _validated_review(record: ReviewedSbiSchemeIdentity) -> None:
    _require(isinstance(record.scheme_code, str) and bool(record.scheme_code.strip())
             and record.scheme_code == record.scheme_code.strip(), "SCHEME_CODE_INVALID")
    _require(isinstance(record.isin, str) and bool(re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", record.isin)),
             "REVIEWED_ISIN_INVALID")
    _require(isinstance(record.ticker, str) and bool(re.fullmatch(r"[A-Z0-9][A-Z0-9._-]*", record.ticker)),
             "REVIEWED_TICKER_INVALID")
    uri = urlsplit(record.evidence_url)
    _require(uri.scheme == "https" and uri.hostname is not None and uri.username is None
             and uri.password is None and uri.port in (None, 443) and not uri.fragment,
             "IDENTITY_EVIDENCE_URL_INVALID")
    _require(bool(re.fullmatch(r"[a-f0-9]{64}", record.evidence_sha256)),
             "IDENTITY_EVIDENCE_HASH_INVALID")
    _require(isinstance(record.reviewed_on, date) and record.reviewed_on <= date.today(),
             "IDENTITY_REVIEW_DATE_INVALID")
    _require(bool(record.reviewed_by.strip()) and bool(record.approval_reference.strip()),
             "IDENTITY_APPROVAL_MISSING")


def build_reviewed_sbi_resolver(records, canonical_etfs):
    """Build exact-code resolver for BoundedSbiTerAdapter(identity_resolver=...).

    Reject ambiguous or mismatched canonical identities. Unknown scheme codes
    return None. Review metadata is NOT checked against source document bytes
    here: independently verify code -> ISIN before constructing a review record.
    """
    canonical_by_pair = {}
    occupied_ids = set()
    for row in canonical_etfs:
        _require(isinstance(row, dict), "CANONICAL_ROW_INVALID")
        _require(row.get("assetType") == "ETF" and row.get("status") == "ACTIVE"
                 and (row.get("primaryExchange") or row.get("exchange")) == "NSE",
                 "CANONICAL_NOT_ACTIVE_NSE_ETF")
        pair = (row.get("isin"), row.get("primarySymbol"))
        try:
            instrument_id = UUID(str(row["globalInstrumentId"]))
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError("CANONICAL_UUID_INVALID") from exc
        _require(pair not in canonical_by_pair and instrument_id not in occupied_ids,
                 "CANONICAL_IDENTITY_AMBIGUOUS")
        canonical_by_pair[pair] = instrument_id
        occupied_ids.add(instrument_id)

    mappings = {}
    used_instruments = set()
    for item in records:
        _require(isinstance(item, ReviewedSbiSchemeIdentity), "REVIEW_RECORD_INVALID")
        _validated_review(item)
        _require(item.scheme_code not in mappings, "DUPLICATE_SBI_SCHEME_CODE")
        instrument_id = canonical_by_pair.get((item.isin, item.ticker))
        _require(instrument_id is not None, "REVIEWED_IDENTITY_NOT_IN_CANONICAL_UNIVERSE")
        _require(instrument_id not in used_instruments, "MULTIPLE_SBI_SCHEME_CODES_FOR_CANONICAL_ETF")
        mappings[item.scheme_code] = instrument_id
        used_instruments.add(instrument_id)

    def resolve(scheme_code: str):
        if not isinstance(scheme_code, str):
            return None
        return mappings.get(scheme_code.strip())

    return resolve
