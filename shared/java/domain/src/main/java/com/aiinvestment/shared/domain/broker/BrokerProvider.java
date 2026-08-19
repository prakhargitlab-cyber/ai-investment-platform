package com.aiinvestment.shared.domain.broker;

import java.util.List;
import java.util.UUID;

public interface BrokerProvider {
    BrokerType supportedBroker();

    BrokerConnectionCapabilities connectionCapabilities();

    BrokerConnectionStatus connectionStatus();

    List<BrokerAccount> fetchAccounts(UUID userId);

    List<BrokerPosition> fetchPositions(BrokerAccount account);

    List<BrokerCashBalance> fetchCashBalances(BrokerAccount account);

    void disconnect(UUID connectionId);
}
