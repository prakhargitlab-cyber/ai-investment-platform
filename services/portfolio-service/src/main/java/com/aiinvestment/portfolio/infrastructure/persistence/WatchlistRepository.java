package com.aiinvestment.portfolio.infrastructure.persistence;

import com.aiinvestment.portfolio.application.MarketRegion;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.Optional;
import java.util.UUID;

public interface WatchlistRepository extends JpaRepository<WatchlistEntity, UUID> {
    List<WatchlistEntity> findByUserIdOrderByRegionAscNameAsc(UUID userId);
    Optional<WatchlistEntity> findByWatchlistIdAndUserId(UUID watchlistId, UUID userId);
    Optional<WatchlistEntity> findByUserIdAndRegionAndSystemDefaultTrue(UUID userId, MarketRegion region);
}
