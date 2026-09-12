package com.aiinvestment.broker.provider.ibkr;

import com.aiinvestment.broker.application.BrokerProviderException;
import com.aiinvestment.broker.connector.*;
import com.aiinvestment.shared.domain.broker.*;

import java.util.List;
import java.util.UUID;

class UnconfiguredIBKRConnector implements BrokerConnector {
    @Override
    public BrokerType brokerType() {
        return BrokerType.IBKR;
    }

    @Override
    public ConnectorRuntimeMode runtimeMode() {
        return ConnectorRuntimeMode.LOCAL_AGENT;
    }

    @Override
    public BrokerConnectorStatus start(UUID userId, UUID connectorId) {
        return status(userId, connectorId);
    }

    @Override
    public BrokerConnectorStatus status(UUID userId, UUID connectorId) {
        return new BrokerConnectorStatus(BrokerConnectorState.NOT_CONFIGURED, BrokerConnectorState.NOT_CONFIGURED,
                null, null, "NOT_CONFIGURED", "IBKR connector is not configured.");
    }

    @Override
    public String loginUrl(UUID userId, UUID connectorId) {
        return null;
    }

    @Override
    public List<BrokerAccount> fetchAccounts(UUID userId, UUID connectorId) {
        throw BrokerProviderException.authenticationRequired();
    }

    @Override
    public List<BrokerPosition> fetchPositions(UUID userId, UUID connectorId, BrokerAccount account) {
        throw BrokerProviderException.authenticationRequired();
    }

    @Override
    public List<BrokerCashBalance> fetchCashBalances(UUID userId, UUID connectorId, BrokerAccount account) {
        throw BrokerProviderException.authenticationRequired();
    }
}
