from __future__ import annotations

from app.models import (
    FetchStrategy,
    ReliabilityLevel,
    ResearchSourceProvider,
    SourceAccessStatus,
    SourceRateLimitPolicy,
    SourceType,
)


def default_source_providers() -> list[ResearchSourceProvider]:
    return [
        ResearchSourceProvider(
            source_id="official-company-website",
            source_type=SourceType.COMPANY_WEBSITE,
            source_name="Company website",
            fetch_strategy=FetchStrategy.HTTP,
            reliability_level=ReliabilityLevel.LEVEL_B,
            automatic_access=SourceAccessStatus.MANUAL_ONLY,
            rate_limit_policy=SourceRateLimitPolicy(requests_per_minute=2, min_delay_seconds=30),
        ),
        ResearchSourceProvider(
            source_id="investor-relations",
            source_type=SourceType.INVESTOR_RELATIONS,
            source_name="Investor relations",
            fetch_strategy=FetchStrategy.HTTP,
            reliability_level=ReliabilityLevel.LEVEL_B,
            automatic_access=SourceAccessStatus.MANUAL_ONLY,
            rate_limit_policy=SourceRateLimitPolicy(requests_per_minute=3, min_delay_seconds=20),
        ),
        ResearchSourceProvider(
            source_id="exchange-announcements",
            source_type=SourceType.EXCHANGE_ANNOUNCEMENT,
            source_name="Exchange announcements",
            fetch_strategy=FetchStrategy.HTTP,
            reliability_level=ReliabilityLevel.LEVEL_A,
            automatic_access=SourceAccessStatus.MANUAL_ONLY,
            rate_limit_policy=SourceRateLimitPolicy(requests_per_minute=6, min_delay_seconds=10),
        ),
        ResearchSourceProvider(
            source_id="regulatory-filings",
            source_type=SourceType.REGULATORY_FILING,
            source_name="Regulatory filings",
            fetch_strategy=FetchStrategy.HTTP,
            reliability_level=ReliabilityLevel.LEVEL_A,
            automatic_access=SourceAccessStatus.MANUAL_ONLY,
        ),
        ResearchSourceProvider(
            source_id="government-procurement",
            source_type=SourceType.GOVERNMENT_PROCUREMENT,
            source_name="Government procurement",
            fetch_strategy=FetchStrategy.HTTP,
            reliability_level=ReliabilityLevel.LEVEL_A,
            automatic_access=SourceAccessStatus.MANUAL_ONLY,
        ),
        ResearchSourceProvider(
            source_id="rss",
            source_type=SourceType.RSS,
            source_name="Permitted RSS feed",
            fetch_strategy=FetchStrategy.HTTP,
            reliability_level=ReliabilityLevel.LEVEL_C,
            automatic_access=SourceAccessStatus.AVAILABLE,
        ),
        ResearchSourceProvider(
            source_id="news",
            source_type=SourceType.NEWS,
            source_name="Permitted financial news",
            fetch_strategy=FetchStrategy.HTTP,
            reliability_level=ReliabilityLevel.LEVEL_C,
            automatic_access=SourceAccessStatus.MANUAL_ONLY,
        ),
        ResearchSourceProvider(
            source_id="search-discovery-disabled",
            source_type=SourceType.SEARCH_DISCOVERY,
            source_name="Search discovery provider",
            fetch_strategy=FetchStrategy.MANUAL,
            reliability_level=ReliabilityLevel.LEVEL_E,
            automatic_access=SourceAccessStatus.UNAVAILABLE,
        ),
    ]
