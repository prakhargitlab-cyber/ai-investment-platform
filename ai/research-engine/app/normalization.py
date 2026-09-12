from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
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
    text = text.replace("\x00", " ")
    text = repair_text_encoding(text)
    text = _normalize_typography(text)
    return re.sub(r"\s+", " ", text).strip()


def repair_text_encoding(text: str) -> str:
    text = _repair_known_mojibake(text)
    if any(marker in text for marker in ("â", "Ã", "�")):
        try:
            repaired = text.encode("latin1").decode("utf-8")
        except UnicodeError:
            repaired = text
        else:
            if repaired.count("�") <= text.count("�"):
                text = repaired
    return unicodedata.normalize("NFKC", text)


def _normalize_typography(text: str) -> str:
    return (
        text.replace("\u2010", "-")
        .replace("\u2011", "-")
        .replace("\u2012", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u00a0", " ")
    )


def _repair_known_mojibake(text: str) -> str:
    replacements = {
        "\u00c3\u00a2\u00c2\u0080\u00c2\u0091": "-",
        "\u00c3\u00a2\u00c2\u0080\u00c2\u0099": "'",
        "\u00c3\u00a2\u00c2\u0080\u00c2\u009c": '"',
        "\u00c3\u00a2\u00c2\u0080\u00c2\u009d": '"',
        "\u00c3\u00a2\u00c2\u0080\u00c2\u0093": "-",
        "\u00c3\u00a2\u00c2\u0080\u00c2\u0094": "-",
        "\u00e2\u201a\u00ac": "\u20ac",
        "\u00e2\u201a\u00b9": "\u20b9",
        "\u00c3\u00a2\u00e2\u20ac\u0161\u00c2\u00ac": "\u20ac",
        "\u00c3\u00a2\u00e2\u20ac\u0161\u00c2\u00b9": "\u20b9",
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    return text


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
        return None, normalize_text(content)
    if doc_type == DocumentType.HTML:
        soup = BeautifulSoup(content, "html.parser")
        title = normalize_text(soup.title.get_text(" ")) if soup.title else None
        for tag in soup(["script", "style", "noscript", "svg", "nav", "header", "footer", "form", "button"]):
            tag.decompose()
        for tag in soup.select("[role='navigation'], .navigation, .navbar, .breadcrumb, .language, .social"):
            tag.decompose()
        article = _best_article_node(soup)
        return title, clean_article_text(normalize_text(article.get_text(" ")))
    return None, normalize_text(content)


def clean_article_text(text: str) -> str:
    text = normalize_text(text)
    start_patterns = [
        r"\b[A-Z][A-Za-z.\- ]+,\s+(?:Germany,\s+)?(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+20\d{2}\b",
        r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+20\d{2}\b",
        r"\b\d{1,2}\.\d{1,2}\.20\d{2}\b",
    ]
    first = min((match.start() for pattern in start_patterns if (match := re.search(pattern, text, re.IGNORECASE))), default=None)
    if first is not None and first > 0:
        heading_start = text.rfind(". ", 0, first)
        start = heading_start + 2 if heading_start >= 0 else 0
        text = text[start:]
    boilerplate = [
        "Navigation",
        "Suche",
        "German English",
        "Facebook",
        "Instagram",
        "linkedIn",
        "Xing",
        "Close search",
        "Search / HOME",
    ]
    for phrase in boilerplate:
        text = text.replace(phrase, " ")
    return normalize_text(text)


def _best_article_node(soup: BeautifulSoup):
    candidates = soup.select("article, main, [class*='press'], [class*='news'], [class*='content']")
    if not candidates:
        return soup
    return max(candidates, key=lambda tag: len(tag.get_text(" ")))


def extract_published_at(text: str) -> datetime | None:
    european = re.search(r"\b(?P<day>\d{1,2})\.(?P<month>\d{1,2})\.(?P<year>20\d{2})\b", text)
    if european:
        return datetime(
            int(european.group("year")),
            int(european.group("month")),
            int(european.group("day")),
            tzinfo=timezone.utc,
        )
    named = re.search(
        r"\b(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)\s+"
        r"(?P<day>\d{1,2}),\s+(?P<year>20\d{2})\b",
        text,
        re.IGNORECASE,
    )
    if named:
        month = {
            "january": 1,
            "february": 2,
            "march": 3,
            "april": 4,
            "may": 5,
            "june": 6,
            "july": 7,
            "august": 8,
            "september": 9,
            "october": 10,
            "november": 11,
            "december": 12,
        }[named.group("month").lower()]
        return datetime(int(named.group("year")), month, int(named.group("day")), tzinfo=timezone.utc)
    return None


_MONEY_PATTERN = re.compile(
    r"(?P<prefix>₹|€|\$|INR|EUR|USD)?\s*(?P<number>[+-]?\d+(?:,\d{2,3})*(?:\.\d+)?)\s*(?P<scale>crore|lakh|million|billion|mn|bn)?",
    re.IGNORECASE,
)
_CAPACITY_PATTERN = re.compile(r"(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>MW|GW|units?)", re.IGNORECASE)
_PERCENT_PATTERN = re.compile(r"(?P<number>[+-]?\d+(?:\.\d+)?)\s*%")

_MONEY_PATTERN = re.compile(
    r"(?P<prefix>₹|€|â‚¹|â‚¬|\$|INR|EUR|USD)?\s*(?P<number>[+-]?\d+(?:,\d{2,3})*(?:\.\d+)?)\s*(?P<scale>crore|lakh|million|billion|mn|bn)?",
    re.IGNORECASE,
)


_MONEY_PATTERN = re.compile(
    r"(?P<prefix>\u20b9|\u20ac|\$|INR|EUR|USD)?\s*(?P<number>[+-]?\d+(?:,\d{2,3})*(?:\.\d+)?)\s*(?P<scale>crore|lakh|million|billion|mn|bn)?",
    re.IGNORECASE,
)


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
        if prefix in {"₹", "€", "$"}:
            currency = {"₹": "INR", "€": "EUR", "$": "USD"}[prefix]
        if prefix == "€":
            currency = "EUR"
        elif prefix == "₹":
            currency = "INR"
        values.append(NormalizedNumber(original=original, value=number * multiplier, currency=currency))
    for match in _CAPACITY_PATTERN.finditer(text):
        values.append(NormalizedNumber(original=match.group(0), value=Decimal(match.group("number")), unit=match.group("unit").upper()))
    for match in _PERCENT_PATTERN.finditer(text):
        values.append(NormalizedNumber(original=match.group(0), value=Decimal(match.group("number")), unit="PERCENT"))
    return values
