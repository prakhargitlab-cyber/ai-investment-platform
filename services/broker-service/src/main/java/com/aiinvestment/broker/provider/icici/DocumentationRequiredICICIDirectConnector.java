package com.aiinvestment.broker.provider.icici;

import com.aiinvestment.broker.application.BrokerProviderException;
import com.aiinvestment.broker.connector.BrokerConnectorState;
import com.aiinvestment.broker.connector.BrokerConnectorStatus;
import com.aiinvestment.shared.domain.broker.*;

import java.util.List;
import java.util.UUID;

/**
 * Safe production default until the external contract has been verified and implemented.
 * Deliberately contains no ICICI endpoint, payload, authentication, or instrument-ID assumptions.
 */
public class DocumentationRequiredICICIDirectConnector implements ICICIDirectConnector {
    @Override
    public BrokerConnectionCapabilities supportedCapabilities() {
        return BrokerConnectionCapabilities.none();
    }

    @Override
    public BrokerConnectorStatus status(UUID userId, UUID connectionId) {
        return new BrokerConnectorStatus(BrokerConnectorState.AUTHENTICATION_REQUIRED,
                BrokerConnectorState.AUTHENTICATION_REQUIRED, null, null,
                "AUTHENTICATION_REQUIRED", "An officially documented ICICI Direct connector must be configured.");
    }

    @Override
    public ICICIDirectLogin login(UUID userId, UUID connectionId) {
        throw BrokerProviderException.authenticationRequired(BrokerType.ICICI_DIRECT);
    }

    @Override
    public BrokerConnectorStatus attachApiSession(UUID userId, UUID connectionId, String apiSession) {
        throw BrokerProviderException.authenticationRequired(BrokerType.ICICI_DIRECT);
    }

    @Override
    public List<BrokerAccount> fetchAccounts(UUID userId, UUID connectionId) {
        throw BrokerProviderException.authenticationRequired();
    }

    @Override
    public List<BrokerPosition> fetchPositions(UUID userId, UUID connectionId, BrokerAccount account) {
        throw BrokerProviderException.authenticationRequired();
    }

    @Override
    public List<BrokerCashBalance> fetchCashBalances(UUID userId, UUID connectionId, BrokerAccount account) {
        throw BrokerProviderException.authenticationRequired();
    }

    @Override
    public void disconnect(UUID userId, UUID connectionId) {
        // No external session exists in this documentation-required implementation.
    }
}
