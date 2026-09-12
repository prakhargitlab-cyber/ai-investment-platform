package com.aiinvestment.portfolio.infrastructure.persistence;

import org.springframework.data.jpa.repository.JpaRepository;
import java.util.List;
import java.util.UUID;

public interface BrokerPositionSnapshotRepository extends JpaRepository<BrokerPositionSnapshotEntity, UUID> {
    List<BrokerPositionSnapshotEntity> findByUserIdAndPortfolioIdOrderByEffectiveAtAsc(UUID userId, UUID portfolioId);
}
