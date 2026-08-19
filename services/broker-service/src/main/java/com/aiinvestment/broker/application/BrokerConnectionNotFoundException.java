package com.aiinvestment.broker.application;

import java.util.UUID;

public class BrokerConnectionNotFoundException extends RuntimeException {
    public BrokerConnectionNotFoundException(UUID connectionId) {
        super("Broker connection not found: " + connectionId);
    }
}
