package com.aiinvestment.broker.provider.icici;

import com.aiinvestment.broker.config.ICICIDirectProviderProperties;
import com.aiinvestment.shared.domain.broker.*;
import org.springframework.stereotype.Component;

import java.util.List;
import java.util.UUID;

@Component
public class ICICIDirectBrokerProvider implements BrokerProvider {
    private final ICICIDirectProviderProperties properties;

    public ICICIDirectBrokerProvider(ICICIDirectProviderProperties properties) {
        this.properties = properties;
    }

    public ICICIDirectBrokerProvider() {
        this(new ICICIDirectProviderProperties(false, "", "", "", "", false));
    }

    @Override
    public BrokerType supportedBroker() {
        return BrokerType.ICICI_DIRECT;
    }

    @Override
    public BrokerConnectionCapabilities connectionCapabilities() {
        return BrokerConnectionCapabilities.none();
    }

    @Override
    public BrokerConnectionStatus connectionStatus() {
        if (!properties.enabled()) {
            return BrokerConnectionStatus.notConfigured(BrokerType.ICICI_DIRECT);
        }
        if (!properties.officialDocumentationVerified()) {
            return BrokerConnectionStatus.documentationRequired(BrokerType.ICICI_DIRECT);
        }
        if (isBlank(properties.baseUrl()) || isBlank(properties.clientId()) || isBlank(properties.authMethod())) {
            return BrokerConnectionStatus.notConfigured(BrokerType.ICICI_DIRECT);
        }
        return BrokerConnectionStatus.authenticationRequired(BrokerType.ICICI_DIRECT);
    }

    @Override
    public List<BrokerAccount> fetchAccounts(UUID userId) {
        throw new UnsupportedOperationException("ICICI Direct integration is intentionally unsupported until official API documentation is provided and verified.");
    }

    @Override
    public List<BrokerPosition> fetchPositions(BrokerAccount account) {
        throw new UnsupportedOperationException("ICICI Direct positions are intentionally unsupported until official API documentation is provided and verified.");
    }

    @Override
    public List<BrokerCashBalance> fetchCashBalances(BrokerAccount account) {
        throw new UnsupportedOperationException("ICICI Direct cash balances are intentionally unsupported until official API research is complete.");
    }

    @Override
    public void disconnect(UUID connectionId) {
        throw new UnsupportedOperationException("ICICI Direct disconnect is unavailable because the provider is not configured.");
    }

    private static boolean isBlank(String value) {
        return value == null || value.isBlank();
    }
}
