package com.aiinvestment.portfolio.infrastructure.persistence;

import com.aiinvestment.shared.domain.AssetType;
import jakarta.persistence.*;

import java.util.UUID;

@Entity
@Table(name = "instruments")
public class InstrumentEntity {
    @Id
    @Column(name = "instrument_id", nullable = false)
    private UUID instrumentId;
    private String isin;
    @Column(nullable = false)
    private String ticker;
    @Column(nullable = false)
    private String exchange;
    private String mic;
    @Column(name = "company_name", nullable = false)
    private String companyName;
    @Enumerated(EnumType.STRING)
    @Column(name = "asset_type", nullable = false)
    private AssetType assetType;
    private String country;
    @Column(name = "trading_currency", nullable = false)
    private String tradingCurrency;
    private String sector;
    private String industry;

    protected InstrumentEntity() {
    }

    public InstrumentEntity(UUID instrumentId, String isin, String ticker, String exchange, String mic, String companyName,
                            AssetType assetType, String country, String tradingCurrency, String sector, String industry) {
        this.instrumentId = instrumentId;
        this.isin = isin;
        this.ticker = ticker;
        this.exchange = exchange;
        this.mic = mic;
        this.companyName = companyName;
        this.assetType = assetType;
        this.country = country;
        this.tradingCurrency = tradingCurrency;
        this.sector = sector;
        this.industry = industry;
    }

    public UUID getInstrumentId() { return instrumentId; }
    public String getIsin() { return isin; }
    public String getTicker() { return ticker; }
    public String getExchange() { return exchange; }
    public String getMic() { return mic; }
    public String getCompanyName() { return companyName; }
    public AssetType getAssetType() { return assetType; }
    public String getCountry() { return country; }
    public String getTradingCurrency() { return tradingCurrency; }
    public String getSector() { return sector; }
    public String getIndustry() { return industry; }
}
