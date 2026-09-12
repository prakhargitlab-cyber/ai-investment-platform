"""Normalization of explicitly labelled Indian shareholding filing values.

This deliberately accepts only source labels adjacent to a percentage; it does not
invent categories, residual public holdings, or a pledge basis.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from app.models import (
    DocumentStatus, ResearchDocument, ShareholdingCategory, ShareholdingSnapshot,
    ShareholdingSnapshotValue, SourceClassification, SourceMode,
)

_PERIOD = re.compile(r"(?:as\s+on|quarter\s+ended|ended)\s*[:\-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{1,2}\s+[A-Za-z]+\s+\d{4})", re.I)
_VALUE = re.compile(r"(?P<label>[A-Za-z/&() .-]{3,80})\s*[:\-]?\s*(?P<value>\d{1,3}(?:\.\d{1,4})?)\s*%", re.I)
_LABELS: tuple[tuple[ShareholdingCategory, tuple[str, ...]], ...] = (
    (ShareholdingCategory.PROMOTER, ("promoter and promoter group", "promoter holding", "promoter")),
    (ShareholdingCategory.FII_FPI, ("foreign institutional investors", "foreign portfolio investors", "fii/fpi", "fii", "fpi")),
    (ShareholdingCategory.DII, ("domestic institutional investors", "dii")),
    (ShareholdingCategory.MUTUAL_FUNDS, ("mutual funds",)),
    (ShareholdingCategory.INSURANCE, ("insurance companies", "insurance")),
    (ShareholdingCategory.GOVERNMENT, ("government",)),
    (ShareholdingCategory.PUBLIC_RETAIL, ("public shareholders", "public/retail", "public holding", "retail")),
    (ShareholdingCategory.OTHERS, ("others", "other non-promoter")),
)


def parse_nse_shareholding_xbrl(xml_text: str) -> list[ShareholdingSnapshotValue]:
    """Normalize only explicit NSE shareholding-taxonomy facts.

    The SHP taxonomy reports ownership ratios as fractions (for example,
    ``0.494`` for 49.4%).  Context members identify the source category; no
    aggregate public-shareholding fact is used as a retail proxy.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    contexts: dict[str, str] = {}
    for context in root.iter():
        if _xml_local_name(context.tag) != "context":
            continue
        member = next((node.text or "" for node in context.iter() if _xml_local_name(node.tag) == "explicitMember"), "")
        contexts[context.attrib.get("id", "")] = member.rsplit(":", 1)[-1]

    facts: dict[tuple[str, str], str] = {}
    for node in root.iter():
        context = node.attrib.get("contextRef")
        if context and node.text is not None:
            facts[(contexts.get(context, ""), _xml_local_name(node.tag))] = node.text.strip()

    values: list[ShareholdingSnapshotValue] = []
    direct = (
        ("ShareholdingOfPromoterAndPromoterGroupMember", "PROMOTER", ShareholdingCategory.PROMOTER),
        ("InstitutionsDomesticMember", "Institutions Domestic", ShareholdingCategory.DII),
        ("MutualFundsOrUTIMember", "Mutual Funds or UTI", ShareholdingCategory.MUTUAL_FUNDS),
        ("InsuranceCompaniesMember", "Insurance Companies", ShareholdingCategory.INSURANCE),
        ("GovernmentsMember", "Governments", ShareholdingCategory.GOVERNMENT),
        ("ResidentIndividualShareholdersHoldingNominalShareCapitalUpToRsTwoLakhMember", "Resident Individual Shareholders Holding Nominal Share Capital Up To Rs Two Lakh", ShareholdingCategory.PUBLIC_RETAIL),
        ("OtherNonInstitutionsMember", "Other Non-Institutions", ShareholdingCategory.OTHERS),
    )
    for member, label, category in direct:
        value = _xbrl_ratio_percentage(facts.get((member, "ShareholdingAsAPercentageOfTotalNumberOfShares")))
        if value is not None:
            values.append(_xbrl_value(category, label, member, "ShareholdingAsAPercentageOfTotalNumberOfShares", value))

    fpi_members = (
        "InstitutionsForeignPortfolioInvestorCategoryOneMember",
        "InstitutionsForeignPortfolioInvestorCategoryTwoMember",
    )
    fpi_values = [_xbrl_ratio_percentage(facts.get((member, "ShareholdingAsAPercentageOfTotalNumberOfShares"))) for member in fpi_members]
    if all(value is not None for value in fpi_values):
        values.append(ShareholdingSnapshotValue(
            category=ShareholdingCategory.FII_FPI, percentage=sum(fpi_values, Decimal("0")),
            metric_basis="DERIVED_SUM_OF_MUTUALLY_EXCLUSIVE_FPI_CATEGORIES",
            raw_source_label="Foreign Portfolio Investor Category I + Category II",
            source_locator="nse-xbrl:context=" + "+".join(fpi_members) + ";tag=ShareholdingAsAPercentageOfTotalNumberOfShares",
            evidence_text=f"NSE XBRL FPI Category I + II: {fpi_values[0]}% + {fpi_values[1]}%",
        ))

    pledge = _xbrl_ratio_percentage(facts.get(("ShareholdingOfPromoterAndPromoterGroupMember", "EncumberedShareUnderPledgedAsPercentageOfTotalNumberOfShares")))
    if pledge is not None:
        values.append(ShareholdingSnapshotValue(
            category=ShareholdingCategory.PROMOTER_PLEDGE, percentage=pledge,
            metric_basis="PERCENT_OF_PROMOTER_HOLDING",
            raw_source_label="Encumbered Share Under Pledged (Promoter and Promoter Group)",
            source_locator="nse-xbrl:context=ShareholdingOfPromoterAndPromoterGroupMember;tag=EncumberedShareUnderPledgedAsPercentageOfTotalNumberOfShares",
            evidence_text=f"NSE XBRL promoter-group pledged-share ratio: {pledge}% of promoter holding",
        ))
    return values


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xbrl_ratio_percentage(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        percentage = Decimal(value) * Decimal("100")
    except InvalidOperation:
        return None
    return percentage if Decimal("0") <= percentage <= Decimal("100") else None


def _xbrl_value(category: ShareholdingCategory, label: str, member: str, tag: str, percentage: Decimal) -> ShareholdingSnapshotValue:
    return ShareholdingSnapshotValue(
        category=category, percentage=percentage, raw_source_label=label,
        source_locator=f"nse-xbrl:context={member};tag={tag}",
        evidence_text=f"NSE XBRL {label}: {percentage}%",
    )


def parse_official_shareholding(document: ResearchDocument) -> ShareholdingSnapshot | None:
    if document.source_mode != SourceMode.REAL or document.source_classification not in {
        SourceClassification.EXCHANGE, SourceClassification.REGULATORY, SourceClassification.OFFICIAL_COMPANY
    } or document.status not in {DocumentStatus.PARSED, DocumentStatus.PROCESSED} or not document.instrument_id:
        return None
    text = document.normalized_text or document.raw_text or ""
    period_match = _PERIOD.search(text)
    if not period_match:
        return None
    period_end = _parse_period(period_match.group(1))
    if period_end is None:
        return None
    values: list[ShareholdingSnapshotValue] = []
    seen: set[ShareholdingCategory] = set()
    for match in _VALUE.finditer(text):
        label = " ".join(match.group("label").lower().split())
        category = _category(label)
        if category is None or category in seen:
            continue
        percentage = _percentage(match.group("value"))
        if percentage is None:
            continue
        values.append(ShareholdingSnapshotValue(category=category, percentage=percentage,
            raw_source_label=match.group("label").strip(), source_locator=f"text:{match.start()}",
            evidence_text=match.group(0).strip()))
        seen.add(category)
    pledge = _pledge_value(text)
    if pledge is not None:
        values.append(pledge)
    if not values:
        return None
    return ShareholdingSnapshot(
        instrument_id=document.instrument_id, period_end=period_end, filing_basis=_filing_basis(text),
        source_provider="NSE" if document.source_classification == SourceClassification.EXCHANGE else document.source_name,
        source_type=str(document.source_type), source_identity_key=str(document.document_id), source_url=document.canonical_url,
        research_document_id=document.document_id, published_at=document.published_at, retrieved_at=document.retrieved_at,
        confidence=Decimal("0.90"), reliability_level=document.reliability_level, source_mode=document.source_mode,
        values=values,
    )


def _category(label: str) -> ShareholdingCategory | None:
    for category, labels in _LABELS:
        if any(candidate in label for candidate in labels):
            return category
    return None


def _pledge_value(text: str) -> ShareholdingSnapshotValue | None:
    match = re.search(r"(?P<label>promoter(?:s)?[^.\n]{0,70}?(?:pledged|encumbered)[^.\n]{0,70})\s*[:\-]?\s*(?P<value>\d{1,3}(?:\.\d{1,4})?)\s*%", text, re.I)
    if not match:
        return None
    lowered = match.group("label").lower()
    basis = "PERCENT_OF_PROMOTER_HOLDING" if "promoter holding" in lowered else (
        "PERCENT_OF_TOTAL_SHARES" if "total shares" in lowered else None
    )
    percentage = _percentage(match.group("value"))
    if basis is None or percentage is None:
        return None
    return ShareholdingSnapshotValue(category=ShareholdingCategory.PROMOTER_PLEDGE, percentage=percentage,
        metric_basis=basis, raw_source_label=match.group("label").strip(), source_locator=f"text:{match.start()}",
        evidence_text=match.group(0).strip())


def _percentage(value: str) -> Decimal | None:
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    return parsed if Decimal("0") <= parsed <= Decimal("100") else None


def _parse_period(value: str) -> datetime | None:
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _filing_basis(text: str) -> str | None:
    lowered = text.lower()
    if "consolidated" in lowered:
        return "CONSOLIDATED"
    if "standalone" in lowered:
        return "STANDALONE"
    return None
