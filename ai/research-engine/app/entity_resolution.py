from __future__ import annotations

import re
from urllib.parse import urlparse

from app.models import CompanyResearchProfile, EntityResolution


class EntityResolver:
    def __init__(self, profiles: list[CompanyResearchProfile]):
        self.profiles = profiles

    def resolve(self, title: str | None, text: str, url: str) -> EntityResolution:
        haystack = f"{title or ''} {text}".lower()
        host = (urlparse(url).hostname or "").lower()
        best = EntityResolution(instrument_id=None, company_id=None, confidence=0.0, matched_on=[])
        for profile in self.profiles:
            score = 0.0
            matched: list[str] = []
            if profile.isin and profile.isin.lower() in haystack:
                score += 0.45
                matched.append("isin")
            if _contains_identity(haystack, profile.company_name):
                score += 0.30
                matched.append("company_name")
            for alias in profile.aliases:
                if _contains_identity(haystack, alias):
                    score += 0.30 if len(alias.strip()) >= 4 else 0.18
                    matched.append("alias")
                    break
            if _contains_identity(haystack, profile.ticker) and _contains_identity(haystack, profile.exchange):
                score += 0.18
                matched.append("ticker_exchange")
            if _contains_identity(haystack, profile.country):
                score += 0.04
                matched.append("country")
            if any(host.endswith(domain.lower()) for domain in profile.known_domains):
                score += 0.35
                matched.append("known_domain")
            score = min(score, 1.0)
            if score > best.confidence:
                best = EntityResolution(
                    instrument_id=profile.instrument_id,
                    company_id=profile.company_id,
                    confidence=score,
                    matched_on=matched,
                )
        return best


def _contains_identity(haystack: str, value: str) -> bool:
    identity = value.strip().lower()
    if not identity:
        return False
    pattern = r"(?<![a-z0-9])" + re.escape(identity) + r"(?![a-z0-9])"
    return re.search(pattern, haystack) is not None
