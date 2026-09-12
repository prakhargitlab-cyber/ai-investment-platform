from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID
from urllib.parse import urlparse

from app.models import DocumentSubtype, ReliabilityLevel, SourceClassification, SourceType


@dataclass(frozen=True)
class RegisteredResearchSource:
    source_id: str
    instrument_id: UUID
    url: str
    source_type: SourceType
    source_name: str
    publisher: str
    reliability_level: ReliabilityLevel
    source_classification: SourceClassification = SourceClassification.OTHER
    domain: str = ""
    company_id: UUID | None = None
    allowed: bool = True
    discovery_method: str = "REGISTERED"
    priority: int = 1
    categories: tuple[str, ...] = ()
    # Optional metadata enrichment from NSE announcement discovery.  It is not
    # part of the source URL or document identity.
    document_subtype: DocumentSubtype | None = None
    # Set exclusively by OfficialFilingDiscovery after NSE's corporate
    # announcements API was queried with this profile's verified NSE symbol.
    # It is deliberately not derived from a URL, publisher, or source name.
    official_nse_profile_symbol: str | None = None

    @property
    def host(self) -> str:
        return (self.domain or urlparse(self.url).hostname or "").lower()


AIXTRON_INSTRUMENT_ID = UUID("11111111-1111-1111-1111-111111111111")


def registered_sources_for(instrument_id: UUID) -> list[RegisteredResearchSource]:
    if instrument_id != AIXTRON_INSTRUMENT_ID:
        return []
    return [
        RegisteredResearchSource(
            source_id="aixtron-official-h1-2026-optoelectronics",
            instrument_id=AIXTRON_INSTRUMENT_ID,
            url="https://www.aixtron.com/en/press/press-releases/Strong%20momentum%20in%20optoelectronics%20continues_n14146",
            source_type=SourceType.INVESTOR_RELATIONS,
            source_classification=SourceClassification.OFFICIAL_COMPANY,
            source_name="AIXTRON official press release",
            publisher="AIXTRON SE",
            reliability_level=ReliabilityLevel.LEVEL_B,
            domain="www.aixtron.com",
            company_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"),
            allowed=True,
            discovery_method="REGISTERED_OFFICIAL",
            priority=1,
            categories=("Orders & Backlog", "CAPEX & Capacity", "Guidance", "Growth", "Customers"),
        )
    ]


def approved_sources_for_categories(instrument_id: UUID, categories: set[str]) -> list[RegisteredResearchSource]:
    sources = [
        source
        for source in registered_sources_for(instrument_id)
        if source.allowed and (not categories or bool(set(source.categories) & categories))
    ]
    return sorted(sources, key=lambda source: (source.priority, source.source_id))
