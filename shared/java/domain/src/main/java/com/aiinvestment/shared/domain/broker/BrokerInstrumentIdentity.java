package com.aiinvestment.shared.domain.broker;

import com.aiinvestment.shared.domain.AssetType;

public record BrokerInstrumentIdentity(
        BrokerType brokerType,
        String brokerSecurityId,
        String brokerContractId,
        String isin,
        String ticker,
        String exchange,
        String mic,
        String companyName,
        String currency,
        String country,
        AssetType assetType,
        String brokerSymbol,
        String brokerDescription,
        String brokerExchange,
        String securityType
) {
    public BrokerInstrumentIdentity(BrokerType brokerType, String brokerSecurityId, String brokerContractId,
                                    String isin, String ticker, String exchange, String mic, String currency,
                                    String country, AssetType assetType) {
        this(brokerType, brokerSecurityId, brokerContractId, isin, ticker, exchange, mic, null, currency, country, assetType);
    }

    public BrokerInstrumentIdentity(BrokerType brokerType, String brokerSecurityId, String brokerContractId,
                                    String isin, String ticker, String exchange, String mic, String companyName,
                                    String currency, String country, AssetType assetType) {
        this(brokerType, brokerSecurityId, brokerContractId, isin, ticker, exchange, mic, companyName, currency,
                country, assetType, ticker, companyName, exchange, assetType == null ? null : assetType.name());
    }
}
