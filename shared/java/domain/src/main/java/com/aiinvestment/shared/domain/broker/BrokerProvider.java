package com.aiinvestment.shared.domain.broker;

import java.util.List;
import java.util.UUID;

public interface BrokerProvider {
    BrokerType supportedBroker();

    BrokerConnectionCapabilities connectionCapabilities();

    default BrokerConnectionCapabilities connectionCapabilities(UUID userId, UUID connectionId) {
        return connectionCapabilities();
    }

    BrokerConnectionStatus connectionStatus();

    default BrokerConnectionStatus connectionStatus(UUID userId, UUID connectionId) {
        return connectionStatus();
    }

    List<BrokerAccount> fetchAccounts(UUID userId);

    default List<BrokerAccount> fetchAccounts(UUID userId, UUID connectionId) {
        return fetchAccounts(userId);
    }

    List<BrokerPosition> fetchPositions(BrokerAccount account);

    default List<BrokerPosition> fetchPositions(UUID userId, UUID connectionId, BrokerAccount account) {
        return fetchPositions(account);
    }

    List<BrokerCashBalance> fetchCashBalances(BrokerAccount account);

    default List<BrokerCashBalance> fetchCashBalances(UUID userId, UUID connectionId, BrokerAccount account) {
        return fetchCashBalances(account);
    }

    void disconnect(UUID connectionId);

    default void disconnect(UUID userId, UUID connectionId) {
        disconnect(connectionId);
    }
}
