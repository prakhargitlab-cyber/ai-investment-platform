package com.aiinvestment.portfolio.infrastructure.persistence;

import org.springframework.data.jpa.repository.JpaRepository;

import java.time.Instant;
import java.util.List;
import java.util.UUID;

public interface PortfolioValuationSnapshotRepository extends JpaRepository<PortfolioValuationSnapshotEntity, UUID> {
    boolean existsByPortfolioIdAndSourceSyncId(UUID portfolioId, String sourceSyncId);
    List<PortfolioValuationSnapshotEntity> findByPortfolioIdAndUserIdOrderBySnapshotTimestampAsc(UUID portfolioId, UUID userId);
    List<PortfolioValuationSnapshotEntity> findByPortfolioIdAndUserIdAndSnapshotTimestampGreaterThanEqualOrderBySnapshotTimestampAsc(
            UUID portfolioId, UUID userId, Instant from);
}
