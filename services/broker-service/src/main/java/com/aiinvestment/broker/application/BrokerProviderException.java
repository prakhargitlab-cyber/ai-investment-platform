package com.aiinvestment.broker.application;

import org.springframework.http.HttpStatus;
import com.aiinvestment.shared.domain.broker.BrokerType;

public class BrokerProviderException extends RuntimeException {
    private final String code;
    private final HttpStatus status;

    public BrokerProviderException(String code, HttpStatus status, String message) {
        super(message);
        this.code = code;
        this.status = status;
    }

    public String code() {
        return code;
    }

    public HttpStatus status() {
        return status;
    }

    public static BrokerProviderException authenticationRequired() {
        return new BrokerProviderException("BROKER_AUTHENTICATION_REQUIRED", HttpStatus.CONFLICT,
                "Interactive Brokers authentication is required in the Client Portal Gateway.");
    }

    public static BrokerProviderException authenticationRequired(BrokerType brokerType) {
        return new BrokerProviderException("BROKER_AUTHENTICATION_REQUIRED", HttpStatus.CONFLICT,
                brokerType + " interactive authentication is required.");
    }

    public static BrokerProviderException documentationRequired(BrokerType brokerType) {
        return new BrokerProviderException("BROKER_DOCUMENTATION_REQUIRED", HttpStatus.CONFLICT,
                brokerType + " authentication cannot continue because the required official contract is unverified.");
    }

    public static BrokerProviderException secretUnavailable(BrokerType brokerType) {
        return new BrokerProviderException("BROKER_SECRET_UNAVAILABLE", HttpStatus.SERVICE_UNAVAILABLE,
                brokerType + " credential configuration is unavailable.");
    }

    public static BrokerProviderException sessionExpired() {
        return new BrokerProviderException("BROKER_SESSION_EXPIRED", HttpStatus.CONFLICT,
                "Interactive Brokers session is expired or not authenticated.");
    }

    public static BrokerProviderException unavailable() {
        return new BrokerProviderException("BROKER_UNAVAILABLE", HttpStatus.BAD_GATEWAY,
                "Interactive Brokers gateway is unavailable.");
    }

    public static BrokerProviderException connectorCapacityUnavailable() {
        return new BrokerProviderException("IBKR_CONNECTOR_CAPACITY_UNAVAILABLE", HttpStatus.CONFLICT,
                "Interactive Brokers connection capacity is currently in use in this DEV environment. Try again when capacity is available.");
    }

    public static BrokerProviderException connectorConflict() {
        return new BrokerProviderException("IBKR_CONNECTOR_CONFLICT", HttpStatus.CONFLICT,
                "Interactive Brokers connection is currently unavailable. Try again later.");
    }

    public static BrokerProviderException unavailable(BrokerType brokerType) {
        return new BrokerProviderException("BROKER_UNAVAILABLE", HttpStatus.BAD_GATEWAY,
                brokerType + " provider is unavailable.");
    }

    public static BrokerProviderException accountNotFound() {
        return new BrokerProviderException("BROKER_ACCOUNT_NOT_FOUND", HttpStatus.NOT_FOUND,
                "Interactive Brokers account was not found for this connection.");
    }

    public static BrokerProviderException permissionDenied() {
        return new BrokerProviderException("BROKER_PERMISSION_DENIED", HttpStatus.FORBIDDEN,
                "Interactive Brokers denied access to the requested resource.");
    }

    public static BrokerProviderException rateLimited() {
        return new BrokerProviderException("BROKER_RATE_LIMITED", HttpStatus.TOO_MANY_REQUESTS,
                "Interactive Brokers rate limit was reached.");
    }

    public static BrokerProviderException marketDataNotEntitled() {
        return new BrokerProviderException("BROKER_MARKET_DATA_NOT_ENTITLED", HttpStatus.CONFLICT,
                "Interactive Brokers market data entitlement is not available.");
    }

    public static BrokerProviderException malformedResponse() {
        return new BrokerProviderException("BROKER_MALFORMED_RESPONSE", HttpStatus.BAD_GATEWAY,
                "Interactive Brokers returned an unexpected response shape.");
    }
}
