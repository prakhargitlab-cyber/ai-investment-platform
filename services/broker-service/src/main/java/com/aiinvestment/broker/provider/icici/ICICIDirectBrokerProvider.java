package com.aiinvestment.broker.provider.icici;

import com.aiinvestment.broker.config.ICICIDirectProviderProperties;
import com.aiinvestment.broker.connector.BrokerConnectorState;
import com.aiinvestment.shared.domain.broker.*;
import org.springframework.stereotype.Component;
import org.springframework.beans.factory.annotation.Autowired;

import java.util.List;
import java.util.UUID;

@Component
public class ICICIDirectBrokerProvider implements BrokerProvider {
    private static final UUID UNSCOPED_CONNECTION_ID = new UUID(0L, 0L);
    private final ICICIDirectProviderProperties properties;
    private final ICICIDirectConnector connector;

    @Autowired
    public ICICIDirectBrokerProvider(ICICIDirectProviderProperties properties, ICICIDirectConnector connector) {
        this.properties = properties;
        this.connector = connector;
    }

    public ICICIDirectBrokerProvider() {
        this(new ICICIDirectProviderProperties(false, "", "https://api.icicidirect.com/apiuser/login",
                        "", "", "", "", false),
                new DocumentationRequiredICICIDirectConnector());
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
    public BrokerConnectionCapabilities connectionCapabilities(UUID userId, UUID connectionId) {
        return connectionStatus(userId, connectionId).state() == BrokerConnectionState.CONNECTED
                ? connector.supportedCapabilities() : BrokerConnectionCapabilities.none();
    }

    @Override
    public BrokerConnectionStatus connectionStatus() {
        if (!properties.enabled()) {
            return BrokerConnectionStatus.notConfigured(BrokerType.ICICI_DIRECT);
        }
        if (!properties.officialDocumentationVerified()) {
            return BrokerConnectionStatus.documentationRequired(BrokerType.ICICI_DIRECT);
        }
        if (!configurationComplete()) {
            return BrokerConnectionStatus.notConfigured(BrokerType.ICICI_DIRECT);
        }
        return connectionStatus(new UUID(0L, 0L), UNSCOPED_CONNECTION_ID);
    }

    @Override
    public BrokerConnectionStatus connectionStatus(UUID userId, UUID connectionId) {
        var status = connector.status(userId, connectionId);
        if (status.authStatus() == BrokerConnectorState.CONNECTED) {
            return new BrokerConnectionStatus(BrokerType.ICICI_DIRECT, BrokerConnectionState.CONNECTED,
                    BrokerProviderStatus.CONNECTED, "CONNECTED", status.message());
        }
        if (status.authStatus() == BrokerConnectorState.AUTHENTICATION_REQUIRED) {
            return BrokerConnectionStatus.authenticationRequired(BrokerType.ICICI_DIRECT);
        }
        if (status.authStatus() == BrokerConnectorState.SESSION_EXPIRED) {
            return new BrokerConnectionStatus(BrokerType.ICICI_DIRECT, BrokerConnectionState.SESSION_EXPIRED,
                    BrokerProviderStatus.AUTHENTICATION_REQUIRED, "SESSION_EXPIRED", status.message());
        }
        return new BrokerConnectionStatus(BrokerType.ICICI_DIRECT, BrokerConnectionState.ERROR,
                BrokerProviderStatus.UNAVAILABLE, status.code(), status.message());
    }

    public ICICIDirectLogin login(UUID userId, UUID connectionId) {
        return connector.login(userId, connectionId);
    }

    public BrokerConnectionStatus attachApiSession(UUID userId, UUID connectionId, String apiSession) {
        connector.attachApiSession(userId, connectionId, apiSession);
        return connectionStatus(userId, connectionId);
    }

    @Override
    public List<BrokerAccount> fetchAccounts(UUID userId) {
        return fetchAccounts(userId, UNSCOPED_CONNECTION_ID);
    }

    @Override
    public List<BrokerAccount> fetchAccounts(UUID userId, UUID connectionId) {
        return connector.fetchAccounts(userId, connectionId);
    }

    @Override
    public List<BrokerPosition> fetchPositions(BrokerAccount account) {
        return fetchPositions(account.userId(), UNSCOPED_CONNECTION_ID, account);
    }

    @Override
    public List<BrokerPosition> fetchPositions(UUID userId, UUID connectionId, BrokerAccount account) {
        return connector.fetchPositions(userId, connectionId, account);
    }

    @Override
    public List<BrokerCashBalance> fetchCashBalances(BrokerAccount account) {
        return fetchCashBalances(account.userId(), UNSCOPED_CONNECTION_ID, account);
    }

    @Override
    public List<BrokerCashBalance> fetchCashBalances(UUID userId, UUID connectionId, BrokerAccount account) {
        return connector.fetchCashBalances(userId, connectionId, account);
    }

    @Override
    public void disconnect(UUID connectionId) {
        connector.disconnect(new UUID(0L, 0L), connectionId);
    }

    @Override
    public void disconnect(UUID userId, UUID connectionId) {
        connector.disconnect(userId, connectionId);
    }

    private boolean readyForConnectorCalls() {
        return properties.enabled() && properties.officialDocumentationVerified()
                && configurationComplete() && !connector.supportedCapabilities().capabilities().isEmpty();
    }

    private boolean configurationComplete() {
        return !isBlank(properties.baseUrl()) && !isBlank(properties.loginUrl()) && !isBlank(properties.appKey())
                && !isBlank(properties.redirectUrl())
                && BreezeICICIDirectConnector.AUTH_METHOD.equalsIgnoreCase(properties.authMethod())
                && !isBlank(properties.secretKeyReference());
    }

    private static boolean isBlank(String value) {
        return value == null || value.isBlank();
    }
}
