package com.aiinvestment.portfolio.infrastructure.persistence;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "portfolios")
public class PortfolioEntity {
    @Id
    @Column(name = "portfolio_id", nullable = false)
    private UUID portfolioId;
    @Column(name = "user_id", nullable = false)
    private UUID userId;
    @Column(nullable = false)
    private String name;
    @Column(name = "base_currency", nullable = false)
    private String baseCurrency;
    @Column(name = "created_at", nullable = false)
    private Instant createdAt;
    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    protected PortfolioEntity() {
    }

    public PortfolioEntity(UUID portfolioId, UUID userId, String name, String baseCurrency, Instant createdAt, Instant updatedAt) {
        this.portfolioId = portfolioId;
        this.userId = userId;
        this.name = name;
        this.baseCurrency = baseCurrency;
        this.createdAt = createdAt;
        this.updatedAt = updatedAt;
    }

    public UUID getPortfolioId() { return portfolioId; }
    public UUID getUserId() { return userId; }
    public String getName() { return name; }
    public String getBaseCurrency() { return baseCurrency; }
    public Instant getCreatedAt() { return createdAt; }
    public Instant getUpdatedAt() { return updatedAt; }
}
