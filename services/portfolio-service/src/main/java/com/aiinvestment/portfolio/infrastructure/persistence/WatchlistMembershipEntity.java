package com.aiinvestment.portfolio.infrastructure.persistence;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "watchlist_memberships")
public class WatchlistMembershipEntity {
    @Id
    @Column(name = "membership_id", nullable = false)
    private UUID membershipId;
    @Column(name = "watchlist_id", nullable = false)
    private UUID watchlistId;
    @Column(name = "global_instrument_id", nullable = false)
    private UUID globalInstrumentId;
    @Column(name = "source_period")
    private String sourcePeriod;
    @Column(name = "source_performance_pct", precision = 18, scale = 8)
    private BigDecimal sourcePerformancePct;
    @Column(name = "added_at", nullable = false)
    private Instant addedAt;
    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    protected WatchlistMembershipEntity() {}

    public WatchlistMembershipEntity(UUID membershipId, UUID watchlistId, UUID globalInstrumentId,
                                     String sourcePeriod, BigDecimal sourcePerformancePct, Instant addedAt) {
        this.membershipId = membershipId;
        this.watchlistId = watchlistId;
        this.globalInstrumentId = globalInstrumentId;
        this.sourcePeriod = sourcePeriod;
        this.sourcePerformancePct = sourcePerformancePct;
        this.addedAt = addedAt;
        this.updatedAt = addedAt;
    }

    public UUID getMembershipId() { return membershipId; }
    public UUID getWatchlistId() { return watchlistId; }
    public UUID getGlobalInstrumentId() { return globalInstrumentId; }
    public String getSourcePeriod() { return sourcePeriod; }
    public BigDecimal getSourcePerformancePct() { return sourcePerformancePct; }
    public Instant getAddedAt() { return addedAt; }
    public Instant getUpdatedAt() { return updatedAt; }

    public void updateSourceContext(String sourcePeriod, BigDecimal sourcePerformancePct, Instant at) {
        if (sourcePeriod != null) {
            this.sourcePeriod = sourcePeriod;
            this.sourcePerformancePct = sourcePerformancePct;
            this.updatedAt = at;
        }
    }
}
