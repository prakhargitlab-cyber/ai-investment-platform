package com.aiinvestment.portfolio.infrastructure.persistence;

import com.aiinvestment.shared.domain.AssetType;
import jakarta.persistence.*;
import java.time.Instant;
import java.util.Locale;
import java.util.UUID;

@Entity
@Table(name = "instrument_master")
public class InstrumentMasterEntity {
    @Id @Column(name = "instrument_id") private UUID instrumentId;
    private String isin;
    @Column(name = "normalized_isin") private String normalizedIsin;
    @Column(name = "canonical_name", nullable = false) private String canonicalName;
    @Enumerated(EnumType.STRING) @Column(name = "asset_type", nullable = false) private AssetType assetType;
    @Column(nullable = false) private String currency;
    private String country;
    @Column(name = "primary_exchange") private String primaryExchange;
    @Column(name = "primary_symbol") private String primarySymbol;
    @Column(nullable = false) private String status;
    @Column(name = "created_at", nullable = false) private Instant createdAt;
    @Column(name = "updated_at", nullable = false) private Instant updatedAt;

    protected InstrumentMasterEntity() {}
    public InstrumentMasterEntity(UUID id, String isin, String name, AssetType type, String currency, String country,
                                  String exchange, String symbol, String status, Instant now) {
        this.instrumentId=id; this.isin=normalizeIsin(isin); this.normalizedIsin=this.isin; this.canonicalName=name;
        this.assetType=type; this.currency=currency; this.country=country; this.primaryExchange=exchange;
        this.primarySymbol=symbol; this.status=status; this.createdAt=now; this.updatedAt=now;
    }
    public static String normalizeIsin(String value) {
        if (value == null || value.isBlank()) return null;
        String normalized=value.replaceAll("\\s+", "").toUpperCase(Locale.ROOT);
        return normalized.matches("[A-Z]{2}[A-Z0-9]{9}[0-9]") ? normalized : null;
    }
    public UUID getInstrumentId(){return instrumentId;} public String getIsin(){return isin;}
    public String getCanonicalName(){return canonicalName;} public AssetType getAssetType(){return assetType;}
    public String getCurrency(){return currency;} public String getCountry(){return country;}
    public String getPrimaryExchange(){return primaryExchange;} public String getPrimarySymbol(){return primarySymbol;}
    public String getStatus(){return status;}
    public void applyVerifiedPrimaryListing(String exchange, String symbol, String officialName) {
        if (exchange == null || exchange.isBlank() || symbol == null || symbol.isBlank())
            throw new IllegalArgumentException("VERIFIED_PRIMARY_LISTING_REQUIRED");
        this.primaryExchange = exchange.trim().toUpperCase(Locale.ROOT);
        this.primarySymbol = symbol.trim().toUpperCase(Locale.ROOT);
        if (officialName != null && !officialName.isBlank()) this.canonicalName = officialName.trim();
        this.updatedAt = Instant.now();
    }
    public void applyValidatedAssetType(AssetType validatedType) {
        if (validatedType != null && validatedType != this.assetType) {
            this.assetType = validatedType;
            this.updatedAt = Instant.now();
        }
    }
}
