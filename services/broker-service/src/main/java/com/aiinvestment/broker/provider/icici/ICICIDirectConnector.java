package com.aiinvestment.broker.provider.icici;

import com.aiinvestment.broker.connector.BrokerConnectorStatus;
import com.aiinvestment.shared.domain.broker.BrokerAccount;
import com.aiinvestment.shared.domain.broker.BrokerCashBalance;
import com.aiinvestment.shared.domain.broker.BrokerConnectionCapabilities;
import com.aiinvestment.shared.domain.broker.BrokerPosition;

import java.util.List;
import java.util.UUID;

/**
 * Provider-facing boundary for a future, officially documented ICICI Direct adapter.
 * Every operation carries both ownership dimensions; implementations must not share
 * authentication or session state between users or broker connections.
 */
public interface ICICIDirectConnector {
    BrokerConnectionCapabilities supportedCapabilities();

    BrokerConnectorStatus status(UUID userId, UUID connectionId);

    ICICIDirectLogin login(UUID userId, UUID connectionId);

    BrokerConnectorStatus attachApiSession(UUID userId, UUID connectionId, String apiSession);

    List<BrokerAccount> fetchAccounts(UUID userId, UUID connectionId);

    List<BrokerPosition> fetchPositions(UUID userId, UUID connectionId, BrokerAccount account);

    List<BrokerCashBalance> fetchCashBalances(UUID userId, UUID connectionId, BrokerAccount account);

    void disconnect(UUID userId, UUID connectionId);
}
