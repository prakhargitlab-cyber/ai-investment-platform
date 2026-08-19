package com.aiinvestment.broker.provider.icici;

import com.aiinvestment.shared.domain.AssetType;
import com.aiinvestment.shared.domain.Instrument;
import com.aiinvestment.shared.domain.broker.BrokerInstrumentIdentity;
import com.aiinvestment.shared.domain.broker.BrokerInstrumentNormalizer;
import com.aiinvestment.shared.domain.broker.BrokerType;
import org.springframework.stereotype.Component;

import java.nio.charset.StandardCharsets;
import java.util.UUID;

@Component
public class ICICIDirectInstrumentNormalizer implements BrokerInstrumentNormalizer {
    @Override
    public BrokerType supportedBroker() {
        return BrokerType.ICICI_DIRECT;
    }

    @Override
    public Instrument normalize(BrokerInstrumentIdentity identity) {
        requireIdentity(identity);
        String stableKey = String.join("|", BrokerType.ICICI_DIRECT.name(), safe(identity.brokerSecurityId()),
                safe(identity.isin()), identity.ticker(), identity.exchange(), identity.currency());
        return new Instrument(UUID.nameUUIDFromBytes(stableKey.getBytes(StandardCharsets.UTF_8)), identity.isin(),
                identity.ticker(), identity.exchange(), identity.mic(), identity.ticker(), assetType(identity),
                "IN", identity.currency(), null, null);
    }

    private static void requireIdentity(BrokerInstrumentIdentity identity) {
        if (identity == null || identity.brokerType() != BrokerType.ICICI_DIRECT || isBlank(identity.ticker())
                || isBlank(identity.exchange()) || isBlank(identity.currency())
                || (!"INR".equals(identity.currency()))
                || (isBlank(identity.brokerSecurityId()) && isBlank(identity.isin()))) {
            throw new IllegalArgumentException("ICICI Direct instrument identity requires Indian exchange, INR currency, and broker security ID or ISIN");
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
