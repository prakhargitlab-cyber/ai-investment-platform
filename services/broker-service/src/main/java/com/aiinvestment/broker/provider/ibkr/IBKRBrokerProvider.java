package com.aiinvestment.broker.provider.ibkr;

import com.aiinvestment.broker.config.IBKRProviderProperties;
import com.aiinvestment.shared.domain.broker.*;
import org.springframework.stereotype.Component;

import java.util.List;
import java.util.UUID;

@Component
public class IBKRBrokerProvider implements BrokerProvider {
    private final IBKRProviderProperties properties;

    public IBKRBrokerProvider(IBKRProviderProperties properties) {
        this.properties = properties;
    }

    public IBKRBrokerProvider() {
        this(new IBKRProviderProperties(false, "", "", "", "", false));
    }

    @Override
    public BrokerType supportedBroker() {
        return BrokerType.IBKR;
    }

    @Override
    public BrokerConnectionCapabilities connectionCapabilities() {
        return BrokerConnectionCapabilities.none();
    }

    @Override
    public BrokerConnectionStatus connectionStatus() {
        if (!properties.enabled()) {
            return BrokerConnectionStatus.notConfigured(BrokerType.IBKR);
        }
        if (!properties.officialDocumentationVerified()) {
            return BrokerConnectionStatus.documentationRequired(BrokerType.IBKR);
        }
        if (isBlank(properties.baseUrl()) || isBlank(properties.clientId()) || isBlank(properties.authMethod())) {
            return BrokerConnectionStatus.notConfigured(BrokerType.IBKR);
        }
        return BrokerConnectionStatus.authenticationRequired(BrokerType.IBKR);
    }

    @Override
    public List<BrokerAccount> fetchAccounts(UUID userId) {
        throw new UnsupportedOperationException("IBKR integration is intentionally unsupported until official API documentation is provided and verified.");
    }

    @Override
    public List<BrokerPosition> fetchPositions(BrokerAccount account) {
        throw new UnsupportedOperationException("IBKR positions are intentionally unsupported until official API documentation is provided and verified.");
    }

    @Override
    public List<BrokerCashBalance> fetchCashBalances(BrokerAccount account) {
        throw new UnsupportedOperationException("IBKR cash balances are intentionally unsupported until official API research is complete.");
    }

    @Override
    public void disconnect(UUID connectionId) {
        throw new UnsupportedOperationException("IBKR disconnect is unavailable because the provider is not configured.");
    }

    private static boolean isBlank(String value) {
        return value == null || value.isBlank();
    }
}
