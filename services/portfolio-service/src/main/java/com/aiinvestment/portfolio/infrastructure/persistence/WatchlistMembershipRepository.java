package com.aiinvestment.portfolio.infrastructure.persistence;

import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.Optional;
import java.util.UUID;

public interface WatchlistMembershipRepository extends JpaRepository<WatchlistMembershipEntity, UUID> {
    List<WatchlistMembershipEntity> findByWatchlistIdOrderByAddedAtAscGlobalInstrumentIdAsc(UUID watchlistId);
    Optional<WatchlistMembershipEntity> findByWatchlistIdAndGlobalInstrumentId(UUID watchlistId, UUID globalInstrumentId);
    long deleteByWatchlistIdAndGlobalInstrumentId(UUID watchlistId, UUID globalInstrumentId);
    long countByWatchlistId(UUID watchlistId);
}
