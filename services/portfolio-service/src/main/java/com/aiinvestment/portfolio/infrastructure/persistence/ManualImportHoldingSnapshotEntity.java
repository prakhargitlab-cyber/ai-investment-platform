package com.aiinvestment.portfolio.infrastructure.persistence;

import com.aiinvestment.portfolio.importing.ImportedHolding;
import jakarta.persistence.*;
import java.math.BigDecimal;
import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name="manual_import_holding_snapshots")
public class ManualImportHoldingSnapshotEntity {
    @Id @Column(name="snapshot_holding_id") private UUID id;
    @Column(name="import_id",nullable=false) private UUID importId;
    @Column(name="user_id",nullable=false) private UUID userId;
    @Column(name="portfolio_id",nullable=false) private UUID portfolioId;
    @Column(name="security_key",nullable=false) private String securityKey;
    private String isin;
    private String symbol;
    @Column(name="company_name",nullable=false) private String companyName;
    @Column(nullable=false) private BigDecimal quantity;
    @Column(name="long_term_quantity") private BigDecimal longTermQuantity;
    @Column(name="average_cost") private BigDecimal averageCost;
    @Column(name="imported_current_price") private BigDecimal importedCurrentPrice;
    @Column(name="value_at_cost") private BigDecimal valueAtCost;
    @Column(name="imported_market_value") private BigDecimal importedMarketValue;
    @Column(name="realized_pnl") private BigDecimal realizedPnl;
    @Column(name="unrealized_pnl") private BigDecimal unrealizedPnl;
    @Column(name="unrealized_pnl_percent") private BigDecimal unrealizedPnlPercent;
    @Column(name="total_pnl") private BigDecimal totalPnl;
    @Column(name="today_pnl") private BigDecimal todayPnl;
    @Column(nullable=false) private String currency;
    @Column(name="observed_at",nullable=false) private Instant observedAt;
    protected ManualImportHoldingSnapshotEntity() {}
    public ManualImportHoldingSnapshotEntity(UUID id, UUID importId, UUID userId, UUID portfolioId,
                                             ImportedHolding h, Instant observedAt) {
        this.id=id; this.importId=importId; this.userId=userId; this.portfolioId=portfolioId;
        securityKey=h.securityKey(); isin=h.isin(); symbol=h.symbol(); companyName=h.companyName();
        quantity=h.quantity(); longTermQuantity=h.longTermQuantity(); averageCost=h.averageCost(); importedCurrentPrice=h.importedCurrentPrice();
        valueAtCost=h.valueAtCost(); importedMarketValue=h.importedMarketValue(); realizedPnl=h.realizedPnl();
        unrealizedPnl=h.unrealizedPnl(); unrealizedPnlPercent=h.unrealizedPnlPercent();
        totalPnl=h.totalPnl(); todayPnl=h.todayPnl(); currency=h.currency();
        this.observedAt=observedAt;
    }
}
