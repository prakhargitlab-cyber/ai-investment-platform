"""Conservative parsing of an explicitly reviewed, hash-bound scheme section.

Only labelled, single-line AUM/TER values and ISO metric dates are supported.
Unsupported layouts fail closed; this is not a general PDF/table extractor.
"""
from datetime import date
from decimal import Decimal, localcontext
from hashlib import sha256
import ipaddress
import re
from urllib.parse import urlsplit

from app.etf_evidence import EtfAuthority, EtfMetric, evidence_id

MAX_DOCUMENT_CHARACTERS = 1_000_000
NSE_HOSTS = {"nsearchives.nseindia.com", "www.nseindia.com", "nseindia.com"}
LABELS = {
    EtfMetric.AUM: r"(?:AUM|assets under management|fund size)",
    EtfMetric.EXPENSE_RATIO: r"(?:total expense ratio|expense ratio|TER)",
}
NUMBER = r"(?:\d+|[1-9]\d{0,2}(?:,\d{3})+|[1-9]\d?(?:,\d{2})*,\d{3})(?:\.\d+)?"
ISO_DATE = r"\d{4}-\d{2}-\d{2}"
SCALES = {"crore": 10**7, "crores": 10**7, "lakh": 10**5, "lakhs": 10**5,
          "million": 10**6, "mn": 10**6, "billion": 10**9, "bn": 10**9}


def metric_decimal(number):
    digits = number.replace(",", "").replace(".", "")
    if len(digits) > 24:
        raise ValueError("METRIC_NUMERIC_PRECISION_UNSUPPORTED")
    return Decimal(number.replace(",", ""))


def document_host(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or not host or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.fragment or parsed.query
            or "\\" in url or any(c.isspace() for c in url)):
        raise ValueError("UNVERIFIED_ETF_OFFICIAL_DOCUMENT")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host.lower()
    raise ValueError("UNVERIFIED_ETF_OFFICIAL_DOCUMENT")


def verified_scheme_section(store, profile, document, source, *, now):
    """An approval supplies scope/provenance, never a substitute for identity."""
    host = document_host(document.canonical_url)
    nse = host in NSE_HOSTS
    authority = EtfAuthority.OFFICIAL_EXCHANGE if nse else EtfAuthority.OFFICIAL_FUND
    classification = "EXCHANGE" if nse else "OFFICIAL_COMPANY"
    source_type = "EXCHANGE_ANNOUNCEMENT" if nse else "COMPANY_WEBSITE"
    if (document.instrument_id != profile.instrument_id or document.source_mode != "REAL"
            or document.status != "PROCESSED" or document.reliability_level not in {"LEVEL_A", "LEVEL_B"}
            or (not nse and host not in {d.lower() for d in profile.known_domains})
            or document.source_classification != classification or document.source_type != source_type
            or document.original_url != document.canonical_url):
        raise ValueError("UNVERIFIED_ETF_OFFICIAL_DOCUMENT")
    if source is None:
        raise ValueError("OFFICIAL_DOCUMENT_NOT_AUTHORIZED")
    for field in ("instrument_id", "canonical_url", "source_type", "source_classification",
                  "source_name", "document_type", "reliability_level", "published_at", "retrieved_at"):
        if getattr(source, field) != getattr(document, field):
            raise ValueError("DOCUMENT_PROVENANCE_MISMATCH")
    if source.document_url != document.original_url or source.authority != authority:
        raise ValueError("DOCUMENT_PROVENANCE_MISMATCH")
    if document.retrieved_at > now:
        raise ValueError("FUTURE_DOCUMENT_RETRIEVAL_TIME")
    text = document.normalized_text or ""
    if (not text.strip() or len(text) > MAX_DOCUMENT_CHARACTERS
            or re.search(r"captcha|access denied|too many requests", text, re.I)):
        raise ValueError("EMPTY_BLOCKED_OR_OVERSIZED_DOCUMENT")
    digest = sha256(text.encode("utf-8")).hexdigest()
    if digest != source.normalized_text_sha256 or document.content_hash != digest:
        raise ValueError("DOCUMENT_CONTENT_HASH_MISMATCH")

    # Reuse the existing identity conflict/freshness gate before matching content.
    from app.etf_readiness import _check_identity_ready
    if _check_identity_ready(store, profile.instrument_id, now=now).status != "READY_FRESH":
        raise ValueError("CANONICAL_ETF_IDENTITY_NOT_READY")
    listings = store.etf_listings()
    owned = [item for item in listings if item.instrument_id == profile.instrument_id]
    if (not profile.isin or profile.exchange != "NSE" or profile.mic != "XNSE"
            or any((item.isin, item.symbol) != (profile.isin, profile.ticker) for item in owned)
            or any(item.isin == profile.isin and item.instrument_id != profile.instrument_id for item in listings)
            or any(document_host(item.provenance.source_url) not in NSE_HOSTS for item in owned)
            or source.scheme_isin != profile.isin):
        raise ValueError("CANONICAL_ETF_IDENTITY_CONFLICT")

    start, end = source.section_span or (0, len(text))
    if (not 0 <= start < end <= len(text) or (start and text[start - 1] != "\n")
            or (end != len(text) and text[end - 1] != "\n")):
        raise ValueError("INVALID_SCHEME_SECTION")
    section = text[start:end]
    identifiers = set(re.findall(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b", section, re.I))
    labelled = re.findall(r"(?im)^\s*ISIN\s*:\s*(\S+)\s*$", section)
    if {i.upper() for i in identifiers} != {profile.isin} or labelled != [profile.isin]:
        raise ValueError("MISSING_CONFLICTING_OR_AMBIGUOUS_SCHEME_ISIN")
    normalize = lambda value: " ".join(value.casefold().split())
    names = {normalize(profile.fund_name), *(normalize(item.name) for item in owned)}
    headers = re.findall(r"(?im)^\s*(?:scheme(?: name)?|fund name)\s*:\s*(.+)$", section)
    if not headers:
        headers = [line for line in section.splitlines() if normalize(line) in names]
    if len(headers) != 1 or normalize(headers[0]) not in names:
        raise ValueError("MISSING_CONFLICTING_OR_AMBIGUOUS_SCHEME_NAME")
    symbols = re.findall(r"(?im)^\s*(?:NSE symbol|symbol|ticker)\s*:\s*(\S+)\s*$", section)
    if any(symbol != profile.ticker for symbol in symbols):
        raise ValueError("SCHEME_SYMBOL_CONFLICT")
    return section, host, authority, {
        "normalized_text_sha256": digest, "scheme_isin": profile.isin,
        "section_span": [start, end], "approval_reference": source.approval_reference,
        "listing_evidence_ids": sorted(evidence_id(item) for item in owned),
    }


def parse_metric(section, metric, source, *, now, retrieved_at):
    """Return (value, unit, reporting date, locators), or an explicit absence."""
    label = LABELS[metric]
    shared = re.compile(rf"AUM and TER as of\s*:?\s*({ISO_DATE})", re.I)
    date_line = re.compile(rf"{label}\s+(?:reporting date|effective date|as of|as at)\s*:?\s*({ISO_DATE})", re.I)
    suffix = rf"(?:\s+\((?:as of|as at|effective(?: from)?)\s+({ISO_DATE})\))?"
    if metric == EtfMetric.AUM:
        value_pattern = rf"(INR|Rs\.?|₹|EUR|USD|GBP)\s*({NUMBER})\s*(crores?|lakhs?|million|mn|billion|bn)"
    else:
        value_pattern = r"(\d+(?:\.\d+)?)\s*%"
    value_line = re.compile(rf"{label}\s*[:=]?\s*{value_pattern}{suffix}", re.I)
    declaration = re.compile(rf"{label}\s*:\s*(not published|not disclosed)", re.I)
    dates, shared_dates, values, locators, declarations = set(), set(), set(), [], []
    for index, raw in enumerate(section.splitlines()):
        if len(raw) > 4096:
            raise ValueError("DOCUMENT_LINE_LAYOUT_UNSUPPORTED")
        line = raw.strip()
        match = shared.fullmatch(line)
        if match:
            shared_dates.add(date.fromisoformat(match[1]))
            locators.append(index + 1)
            continue
        match = date_line.fullmatch(line)
        if match:
            dates.add(date.fromisoformat(match[1]))
            locators.append(index + 1)
            continue
        match = value_line.fullmatch(line)
        if match:
            if metric == EtfMetric.AUM:
                currency, number, scale, effective = match.groups()
                currency = "INR" if currency.upper() in {"INR", "RS", "RS.", "₹"} else currency.upper()
                if currency != "INR" and scale.lower() in {"crore", "crores", "lakh", "lakhs"}:
                    raise ValueError("AMBIGUOUS_AUM_UNIT")
                with localcontext() as context:
                    context.prec = 40
                    value, unit = metric_decimal(number) * SCALES[scale.lower()], currency
            else:
                number, effective = match.groups()
                value, unit = metric_decimal(number), "percent"
                # Mathematical percent bounds; scoring/business thresholds are unchanged.
                if not 0 <= value <= 100:
                    raise ValueError("INVALID_TER_PERCENT")
            if effective:
                dates.add(date.fromisoformat(effective))
            values.add((value, unit))
            locators.append(index + 1)
        elif declaration.fullmatch(line):
            declarations.append(line)
            locators.append(index + 1)
        elif re.search(rf"\b{label}\b", line, re.I):
            raise ValueError("AMBIGUOUS_OR_UNPARSEABLE_METRIC")
    if source.as_of_date is not None and shared_dates != {source.as_of_date}:
        raise ValueError("REGISTERED_REPORTING_DATE_NOT_VERIFIED")
    dates |= shared_dates
    if len(dates) > 1 or len(values) > 1 or (declarations and values):
        raise ValueError("CONFLICTING_METRIC_VALUES_OR_DATES")
    as_of = next(iter(dates), None)
    if as_of and (as_of > now.date() or as_of > retrieved_at.date()):
        raise ValueError("FUTURE_METRIC_REPORTING_DATE")
    if declarations:
        return None, None, as_of, locators, "SOURCE_DECLARATION"
    if not values:
        return None, None, as_of, locators, "VALID_DOCUMENT_FIELD_ABSENT"
    value, unit = next(iter(values))
    return value, unit, as_of, locators, "EXPLICIT_SOURCE_EVIDENCE"
