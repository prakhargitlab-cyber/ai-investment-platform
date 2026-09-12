package com.aiinvestment.shared.domain.broker.auth;

/**
 * Describes how a customer authorizes a broker connection. It intentionally
 * does not imply any data or trading capability.
 */
public enum BrokerAuthenticationModel {
    LOCAL_GATEWAY,
    INDIVIDUAL_API_CREDENTIALS,
    PLATFORM_OAUTH,
    PARTNER_OAUTH,
    CONSENT_AGGREGATOR,
    UNSUPPORTED
}
