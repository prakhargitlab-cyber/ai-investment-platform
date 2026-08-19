package com.aiinvestment.broker.provider.ibkr;

import com.aiinvestment.shared.domain.AssetType;
import com.aiinvestment.shared.domain.Instrument;
import com.aiinvestment.shared.domain.broker.BrokerInstrumentIdentity;
import com.aiinvestment.shared.domain.broker.BrokerInstrumentNormalizer;
import com.aiinvestment.shared.domain.broker.BrokerType;
import org.springframework.stereotype.Component;

import java.nio.charset.StandardCharsets;
import java.util.UUID;

@Component
public class IBKRInstrumentNormalizer implements BrokerInstrumentNormalizer {
    @Override
    public BrokerType supportedBroker() {
        return BrokerType.IBKR;
    }

    @Override
    public Instrument normalize(BrokerInstrumentIdentity identity) {
        requireIdentity(identity);
        String stableKey = String.join("|", BrokerType.IBKR.name(), safe(identity.brokerContractId()),
                safe(identity.isin()), identity.ticker(), identity.exchange(), identity.currency());
        return new Instrument(UUID.nameUUIDFromBytes(stableKey.getBytes(StandardCharsets.UTF_8)), identity.isin(),
                identity.ticker(), identity.exchange(), identity.mic(), identity.ticker(), assetType(identity),
                identity.country(), identity.currency(), null, null);
    }

    private static void requireIdentity(BrokerInstrumentIdentity identity) {
        if (identity == null || identity.brokerType() != BrokerType.IBKR || isBlank(identity.ticker())
                || isBlank(identity.exchange()) || isBlank(identity.currency())
                || (isBlank(identity.brokerContractId()) && isBlank(identity.isin()))) {
            throw new IllegalArgumentException("IBKR instrument identity requires broker type, ticker, exchange, currency, and contract ID or ISIN");
        }
    }

    private static boolean isBlank(String value) {
        return value == null || value.isBlank();
    }

    private static String safe(String value) {
        return value == null ? "" : value;
    }

    private static AssetType assetType(BrokerInstrumentIdentity identity) {
        return identity.assetType() == null ? AssetType.EQUITY : identity.assetType();
    }
}
