package com.aiinvestment.portfolio.infrastructure.persistence;

import com.aiinvestment.portfolio.application.MarketRegion;
import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "watchlists")
public class WatchlistEntity {
    @Id
    @Column(name = "watchlist_id", nullable = false)
    private UUID watchlistId;
    @Column(name = "user_id", nullable = false)
    private UUID userId;
    @Column(nullable = false)
    private String name;
    @Enumerated(EnumType.STRING)
    @Column(nullable = false)
    private MarketRegion region;
    @Column(name = "system_default", nullable = false)
    private boolean systemDefault;
    @Column(name = "created_at", nullable = false)
    private Instant createdAt;
    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    protected WatchlistEntity() {}

    public WatchlistEntity(UUID watchlistId, UUID userId, String name, MarketRegion region,
                           boolean systemDefault, Instant createdAt) {
        this.watchlistId = watchlistId;
        this.userId = userId;
        this.name = name;
        this.region = region;
        this.systemDefault = systemDefault;
        this.createdAt = createdAt;
        this.updatedAt = createdAt;
    }

    public UUID getWatchlistId() { return watchlistId; }
    public UUID getUserId() { return userId; }
    public String getName() { return name; }
    public MarketRegion getRegion() { return region; }
    public boolean isSystemDefault() { return systemDefault; }
    public Instant getCreatedAt() { return createdAt; }
    public Instant getUpdatedAt() { return updatedAt; }

    public void touch(Instant at) { this.updatedAt = at; }
}
