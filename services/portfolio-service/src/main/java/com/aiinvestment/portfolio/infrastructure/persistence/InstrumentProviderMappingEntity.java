package com.aiinvestment.portfolio.infrastructure.persistence;

import jakarta.persistence.*;
import java.math.BigDecimal;
import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name="instrument_provider_mappings")
public class InstrumentProviderMappingEntity {
    @Id @Column(name="mapping_id") private UUID mappingId;
    @Column(name="instrument_id",nullable=false) private UUID instrumentId;
    @Column(nullable=false) private String provider;
    @Column(name="provider_symbol") private String providerSymbol;
    @Column(name="provider_instrument_id") private String providerInstrumentId;
    private String exchange; private String currency;
    @Column(nullable=false) private String status;
    @Column(name="resolution_source",nullable=false) private String resolutionSource;
    @Column(nullable=false) private BigDecimal confidence;
    @Column(name="resolved_at",nullable=false) private Instant resolvedAt;
    @Column(name="verified_at") private Instant verifiedAt;
    @Column(name="last_validation_at") private Instant lastValidationAt;
    @Column(name="failure_reason") private String failureReason;
    @Column(name="created_at",nullable=false) private Instant createdAt;
    @Column(name="updated_at",nullable=false) private Instant updatedAt;
    protected InstrumentProviderMappingEntity() {}
    public InstrumentProviderMappingEntity(UUID mappingId, UUID instrumentId, String provider, String symbol, String providerId,
            String exchange, String currency, String status, String source, BigDecimal confidence, Instant now) {
        this.mappingId=mappingId;this.instrumentId=instrumentId;this.provider=provider;this.providerSymbol=symbol;
        this.providerInstrumentId=providerId;this.exchange=exchange;this.currency=currency;this.status=status;
        this.resolutionSource=source;this.confidence=confidence;this.resolvedAt=now;this.verifiedAt="VERIFIED".equals(status)?now:null;
        this.lastValidationAt=now;this.createdAt=now;this.updatedAt=now;
    }
    public UUID getMappingId(){return mappingId;} public UUID getInstrumentId(){return instrumentId;}
    public String getProvider(){return provider;} public String getProviderSymbol(){return providerSymbol;}
    public String getProviderInstrumentId(){return providerInstrumentId;} public String getExchange(){return exchange;}
    public String getCurrency(){return currency;} public String getStatus(){return status;}
    public String getResolutionSource(){return resolutionSource;}
    public BigDecimal getConfidence(){return confidence;} public Instant getLastValidationAt(){return lastValidationAt;}
    public Instant getVerifiedAt(){return verifiedAt;} public String getFailureReason(){return failureReason;}
    public Instant getResolvedAt(){return resolvedAt;}
    public void markInvalid(String reason, Instant now){status="INVALID";failureReason=reason;lastValidationAt=now;updatedAt=now;}
    public void markVerified(String symbol, String providerId, String exchange, String currency, String source,
            BigDecimal confidence, Instant now) {
        this.providerSymbol = symbol;
        if (providerId != null && !providerId.isBlank()) this.providerInstrumentId = providerId;
        if (exchange != null && !exchange.isBlank()) this.exchange = exchange;
        if (currency != null && !currency.isBlank()) this.currency = currency;
        this.status = "VERIFIED";
        this.resolutionSource = source;
        this.confidence = confidence;
        this.resolvedAt = now;
        if (this.verifiedAt == null) this.verifiedAt = now;
        this.lastValidationAt = now;
        this.failureReason = null;
        this.updatedAt = now;
    }
}
