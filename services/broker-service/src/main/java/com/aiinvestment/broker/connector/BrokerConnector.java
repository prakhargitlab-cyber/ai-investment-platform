package com.aiinvestment.broker.connector;

import com.aiinvestment.shared.domain.broker.BrokerAccount;
import com.aiinvestment.shared.domain.broker.BrokerCashBalance;
import com.aiinvestment.shared.domain.broker.BrokerPosition;
import com.aiinvestment.shared.domain.broker.BrokerType;

import java.util.List;
import java.util.UUID;

public interface BrokerConnector {
    BrokerType brokerType();
    ConnectorRuntimeMode runtimeMode();
    BrokerConnectorStatus start(UUID userId, UUID connectorId);
    BrokerConnectorStatus status(UUID userId, UUID connectorId);
    String loginUrl(UUID userId, UUID connectorId);
    List<BrokerAccount> fetchAccounts(UUID userId, UUID connectorId);
    List<BrokerPosition> fetchPositions(UUID userId, UUID connectorId, BrokerAccount account);
    List<BrokerCashBalance> fetchCashBalances(UUID userId, UUID connectorId, BrokerAccount account);
}
