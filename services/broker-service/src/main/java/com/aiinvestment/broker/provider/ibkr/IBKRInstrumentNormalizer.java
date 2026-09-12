package com.aiinvestment.broker.provider.ibkr;

import com.aiinvestment.shared.domain.AssetType;
import com.aiinvestment.shared.domain.Instrument;
import com.aiinvestment.shared.domain.broker.BrokerInstrumentIdentity;
import com.aiinvestment.shared.domain.broker.BrokerInstrumentNormalizer;
import com.aiinvestment.shared.domain.broker.BrokerType;
import org.springframework.stereotype.Component;

import java.nio.charset.StandardCharsets;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;

@Component
public class IBKRInstrumentNormalizer implements BrokerInstrumentNormalizer {
    private static final Map<String, String> IBKR_EXCHANGE_TO_MIC = Map.of(
            "IBIS", "XETR",
            "IBIS2", "XETR",
            "AEB", "XAMS"
    );

    @Override
    public BrokerType supportedBroker() {
        return BrokerType.IBKR;
    }

    @Override
    public Instrument normalize(BrokerInstrumentIdentity identity) {
        requireIdentity(identity);
        String exchange = normalizeExchange(identity.exchange());
        String mic = normalizeMic(identity.mic(), exchange);
        String ticker = identity.ticker().toUpperCase(Locale.ROOT);
        String currency = identity.currency().toUpperCase(Locale.ROOT);
        String stableKey = String.join("|", BrokerType.IBKR.name(), safe(identity.brokerContractId()),
                safe(identity.isin()), ticker, exchange, currency);
        String companyName = companyName(identity);
        return new Instrument(UUID.nameUUIDFromBytes(stableKey.getBytes(StandardCharsets.UTF_8)),
                BrokerType.IBKR.name(), identity.brokerContractId(), blankToNull(identity.isin()),
                ticker, exchange, mic, companyName, assetType(identity),
                blankToNull(identity.country()), currency, null, null,
                blankToNull(identity.brokerSymbol()),
                blankToNull(identity.brokerDescription()),
                blankToNull(identity.brokerExchange()),
                ticker,
                companyName,
                exchange,
                mic,
                securityType(identity));
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

    private static String blankToNull(String value) {
        return isBlank(value) ? null : value;
    }

    private static String normalizeExchange(String value) {
        String upper = value.toUpperCase(Locale.ROOT);
        return IBKR_EXCHANGE_TO_MIC.getOrDefault(upper, upper);
    }

    private static String normalizeMic(String mic, String exchange) {
        if (isBlank(mic)) {
            return exchange;
        }
        return IBKR_EXCHANGE_TO_MIC.getOrDefault(mic.toUpperCase(Locale.ROOT), mic.toUpperCase(Locale.ROOT));
    }

    private static String companyName(BrokerInstrumentIdentity identity) {
        return isBlank(identity.companyName()) ? identity.ticker() : identity.companyName();
    }

    private static AssetType assetType(BrokerInstrumentIdentity identity) {
        return identity.assetType() == null ? AssetType.EQUITY : identity.assetType();
    }

    private static String securityType(BrokerInstrumentIdentity identity) {
        if (!isBlank(identity.securityType())) {
            return identity.securityType().toUpperCase(Locale.ROOT);
        }
        return assetType(identity).name();
    }
}
