package com.aiinvestment.broker.partner;

import com.aiinvestment.shared.domain.broker.BrokerConnectionStatus;
import com.aiinvestment.shared.domain.broker.BrokerType;

import java.util.UUID;

/**
 * Boundary for a broker-contracted, multi-customer delegated authorization flow.
 * Implementations may provision or hold platform credentials only when the broker's
 * written partner contract explicitly permits it. Retail/BYO connectors do not implement this boundary.
 */
public interface PartnerAuthProvider {
    BrokerType brokerType();

    PartnerAuthorization beginAuthorization(UUID userId, UUID connectionId);

    BrokerConnectionStatus completeCallback(UUID userId, UUID connectionId,
                                            String authorizationCode, String state);
}
