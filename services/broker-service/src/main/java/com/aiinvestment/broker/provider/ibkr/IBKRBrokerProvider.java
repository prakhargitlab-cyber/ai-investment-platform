package com.aiinvestment.broker.provider.ibkr;

import com.aiinvestment.broker.config.IBKRProviderProperties;
import com.aiinvestment.broker.connector.BrokerConnector;
import com.aiinvestment.broker.connector.BrokerConnectorState;
import com.aiinvestment.shared.domain.broker.*;
import org.springframework.stereotype.Component;

import java.util.List;
import java.util.UUID;

@Component
public class IBKRBrokerProvider implements BrokerProvider {
    public static final String INDIVIDUAL_AUTH_METHOD = "client-portal-gateway";
    public static final String OAUTH2_AUTH_METHOD = "oauth2-private-key-jwt";

    private final IBKRProviderProperties properties;
    private final BrokerConnector connector;

    public IBKRBrokerProvider(IBKRProviderProperties properties, BrokerConnector connector) {
        this.properties = properties;
        this.connector = connector;
    }

    public IBKRBrokerProvider() {
        this(new IBKRProviderProperties(false, "", "", "", "", "", "", INDIVIDUAL_AUTH_METHOD,
                        false, false, false, "", "LOCAL_AGENT", "", "", 1800, 86400, "", ""),
                new UnconfiguredIBKRConnector());
    }

    @Override
    public BrokerType supportedBroker() {
        return BrokerType.IBKR;
    }

    @Override
    public BrokerConnectionCapabilities connectionCapabilities() {
        if (!readyForReadOnlyCalls()) {
            return BrokerConnectionCapabilities.none();
        }
        return BrokerConnectionCapabilities.ibkrReadOnly(properties.marketDataEnabled());
    }

    @Override
    public BrokerConnectionStatus connectionStatus() {
        if (!properties.enabled()) {
            return BrokerConnectionStatus.notConfigured(BrokerType.IBKR);
        }
        if (!properties.officialDocumentationVerified()) {
            return BrokerConnectionStatus.documentationRequired(BrokerType.IBKR);
        }
        if (OAUTH2_AUTH_METHOD.equalsIgnoreCase(properties.authMethod())) {
            return BrokerConnectionStatus.documentationRequired(BrokerType.IBKR);
        }
        if (!readyForReadOnlyCalls()) {
            return BrokerConnectionStatus.notConfigured(BrokerType.IBKR);
        }
        var status = connector.status(UUID.fromString("00000000-0000-0000-0000-000000000000"),
                UUID.fromString("00000000-0000-0000-0000-000000000000"));
        if (status.authStatus() == BrokerConnectorState.CONNECTED) {
            return new BrokerConnectionStatus(BrokerType.IBKR, BrokerConnectionState.CONNECTED,
                    BrokerProviderStatus.CONNECTED, "CONNECTED", status.message());
        }
        if (status.authStatus() == BrokerConnectorState.AUTHENTICATION_REQUIRED) {
            return BrokerConnectionStatus.authenticationRequired(BrokerType.IBKR);
        }
        return new BrokerConnectionStatus(BrokerType.IBKR, BrokerConnectionState.ERROR,
                BrokerProviderStatus.UNAVAILABLE, status.code(), status.message());
    }

    @Override
    public List<BrokerAccount> fetchAccounts(UUID userId) {
        return connector.fetchAccounts(userId, UUID.fromString("00000000-0000-0000-0000-000000000000"));
    }

    @Override
    public List<BrokerPosition> fetchPositions(BrokerAccount account) {
        return connector.fetchPositions(account.userId(), UUID.fromString("00000000-0000-0000-0000-000000000000"), account);
    }

    @Override
    public List<BrokerCashBalance> fetchCashBalances(BrokerAccount account) {
        return connector.fetchCashBalances(account.userId(), UUID.fromString("00000000-0000-0000-0000-000000000000"), account);
    }

    @Override
    public void disconnect(UUID connectionId) {
        // Connector runtime lifecycle is tracked separately from broker metadata.
    }

    private boolean readyForReadOnlyCalls() {
        return properties.enabled()
                && properties.officialDocumentationVerified()
                && INDIVIDUAL_AUTH_METHOD.equalsIgnoreCase(properties.authMethod())
                && !isBlank(properties.connectorBaseUrl());
    }

    private static boolean isBlank(String value) {
        return value == null || value.isBlank();
    }
}
