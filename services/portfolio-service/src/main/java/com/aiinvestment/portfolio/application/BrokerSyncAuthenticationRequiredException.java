package com.aiinvestment.portfolio.application;

public class BrokerSyncAuthenticationRequiredException extends RuntimeException {
    private final String brokerStatus;
    private final String code;

    public BrokerSyncAuthenticationRequiredException(String brokerStatus, String code) {
        super("Broker authentication is required before synchronization");
        this.brokerStatus = brokerStatus;
        this.code = code == null ? "AUTHENTICATION_REQUIRED" : code;
    }

    public String brokerStatus() { return brokerStatus; }
    public String code() { return code; }
}
