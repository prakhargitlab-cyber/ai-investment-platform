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
    private String provider;
    @Column(name = "provider_instrument_id")
    private String providerInstrumentId;
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
    @Column(name = "broker_symbol")
    private String brokerSymbol;
    @Column(name = "broker_description")
    private String brokerDescription;
    @Column(name = "broker_exchange")
    private String brokerExchange;
    @Column(name = "canonical_symbol")
    private String canonicalSymbol;
    @Column(name = "canonical_name")
    private String canonicalName;
    @Column(name = "canonical_exchange")
    private String canonicalExchange;
    @Column(name = "canonical_mic")
    private String canonicalMic;
    @Column(name = "security_type")
    private String securityType;
    @Column(name = "master_instrument_id")
    private UUID masterInstrumentId;

    protected InstrumentEntity() {
    }

    public InstrumentEntity(UUID instrumentId, String isin, String ticker, String exchange, String mic, String companyName,
                            AssetType assetType, String country, String tradingCurrency, String sector, String industry) {
        this(instrumentId, null, null, isin, ticker, exchange, mic, companyName, assetType, country, tradingCurrency, sector, industry);
    }

    public InstrumentEntity(UUID instrumentId, String provider, String providerInstrumentId, String isin, String ticker,
                            String exchange, String mic, String companyName, AssetType assetType, String country,
                            String tradingCurrency, String sector, String industry) {
        this(instrumentId, provider, providerInstrumentId, isin, ticker, exchange, mic, companyName, assetType, country,
                tradingCurrency, sector, industry, ticker, companyName, exchange, ticker, companyName, exchange, mic,
                assetType == null ? null : assetType.name());
    }

    public InstrumentEntity(UUID instrumentId, String provider, String providerInstrumentId, String isin, String ticker,
                            String exchange, String mic, String companyName, AssetType assetType, String country,
                            String tradingCurrency, String sector, String industry, String brokerSymbol,
                            String brokerDescription, String brokerExchange, String canonicalSymbol,
                            String canonicalName, String canonicalExchange, String canonicalMic, String securityType) {
        this.instrumentId = instrumentId;
        this.provider = provider;
        this.providerInstrumentId = providerInstrumentId;
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
        this.brokerSymbol = brokerSymbol;
        this.brokerDescription = brokerDescription;
        this.brokerExchange = brokerExchange;
        this.canonicalSymbol = canonicalSymbol;
        this.canonicalName = canonicalName;
        this.canonicalExchange = canonicalExchange;
        this.canonicalMic = canonicalMic;
        this.securityType = securityType;
    }

    public UUID getInstrumentId() { return instrumentId; }
    public String getProvider() { return provider; }
    public String getProviderInstrumentId() { return providerInstrumentId; }
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
    public String getBrokerSymbol() { return brokerSymbol; }
    public String getBrokerDescription() { return brokerDescription; }
    public String getBrokerExchange() { return brokerExchange; }
    public String getCanonicalSymbol() { return canonicalSymbol; }
    public String getCanonicalName() { return canonicalName; }
    public String getCanonicalExchange() { return canonicalExchange; }
    public String getCanonicalMic() { return canonicalMic; }
    public String getSecurityType() { return securityType; }
    public UUID getMasterInstrumentId() { return masterInstrumentId; }
    public void attachMaster(UUID value) { this.masterInstrumentId = value; }

    public void refreshBrokerMetadata(InstrumentEntity source) {
        this.isin = source.isin;
        this.ticker = source.ticker;
        this.exchange = source.exchange;
        this.mic = source.mic;
        this.companyName = source.companyName;
        this.assetType = source.assetType;
        this.country = source.country;
        this.tradingCurrency = source.tradingCurrency;
        this.sector = source.sector;
        this.industry = source.industry;
        this.brokerSymbol = source.brokerSymbol;
        this.brokerDescription = source.brokerDescription;
        this.brokerExchange = source.brokerExchange;
        this.canonicalSymbol = source.canonicalSymbol;
        this.canonicalName = source.canonicalName;
        this.canonicalExchange = source.canonicalExchange;
        this.canonicalMic = source.canonicalMic;
        this.securityType = source.securityType;
    }
}
