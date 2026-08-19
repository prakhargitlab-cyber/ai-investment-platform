from __future__ import annotations

import hashlib
import re
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from app.models import DocumentType, NormalizedNumber


TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid"}


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = f":{parts.port}" if parts.port and parts.port not in {80, 443} else ""
    path = re.sub(r"/+", "/", parts.path or "/")
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in TRACKING_PARAMS))
    return urlunsplit((scheme, host + port, path, query, ""))


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).lower().encode("utf-8")).hexdigest()


def detect_document_type(content_type: str, url: str = "") -> DocumentType:
    lower = content_type.lower()
    if "pdf" in lower or url.lower().endswith(".pdf"):
        return DocumentType.PDF_REFERENCE
    if "html" in lower:
        return DocumentType.HTML
    if "xml" in lower or "rss" in lower:
        return DocumentType.RSS_XML
    if "text/plain" in lower:
        return DocumentType.TEXT
    return DocumentType.UNKNOWN


def extract_text(content: str, content_type: str) -> tuple[str | None, str | None]:
    doc_type = detect_document_type(content_type)
    if doc_type == DocumentType.PDF_REFERENCE:
        return None, None
    if doc_type == DocumentType.HTML:
        soup = BeautifulSoup(content, "html.parser")
        title = normalize_text(soup.title.get_text(" ")) if soup.title else None
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return title, normalize_text(soup.get_text(" "))
    return None, normalize_text(content)


_MONEY_PATTERN = re.compile(
    r"(?P<prefix>₹|€|\$|INR|EUR|USD)?\s*(?P<number>[+-]?\d+(?:,\d{2,3})*(?:\.\d+)?)\s*(?P<scale>crore|lakh|million|billion|mn|bn)?",
    re.IGNORECASE,
)
_CAPACITY_PATTERN = re.compile(r"(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>MW|GW|units?)", re.IGNORECASE)
_PERCENT_PATTERN = re.compile(r"(?P<number>[+-]?\d+(?:\.\d+)?)\s*%")


def normalize_numbers(text: str) -> list[NormalizedNumber]:
    values: list[NormalizedNumber] = []
    for match in _MONEY_PATTERN.finditer(text):
        original = match.group(0).strip()
        prefix = (match.group("prefix") or "").upper()
        scale = (match.group("scale") or "").lower()
        if not prefix and scale not in {"crore", "lakh", "million", "billion", "mn", "bn"}:
            continue
        number = Decimal(match.group("number").replace(",", ""))
        multiplier = {
            "lakh": Decimal("100000"),
            "crore": Decimal("10000000"),
            "million": Decimal("1000000"),
            "mn": Decimal("1000000"),
            "billion": Decimal("1000000000"),
            "bn": Decimal("1000000000"),
            "": Decimal("1"),
        }[scale]
        currency = {"₹": "INR", "€": "EUR", "$": "USD"}.get(prefix, prefix or None)
        values.append(NormalizedNumber(original=original, value=number * multiplier, currency=currency))
    for match in _CAPACITY_PATTERN.finditer(text):
        values.append(NormalizedNumber(original=match.group(0), value=Decimal(match.group("number")), unit=match.group("unit").upper()))
    for match in _PERCENT_PATTERN.finditer(text):
        values.append(NormalizedNumber(original=match.group(0), value=Decimal(match.group("number")), unit="PERCENT"))
    return values
