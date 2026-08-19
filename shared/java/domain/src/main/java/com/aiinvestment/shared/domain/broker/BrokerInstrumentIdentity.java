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
        String currency,
        String country,
        AssetType assetType
) {
}
