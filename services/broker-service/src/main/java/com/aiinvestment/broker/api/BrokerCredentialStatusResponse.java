package com.aiinvestment.broker.api;

import java.util.List;
import java.util.UUID;

public record BrokerCredentialStatusResponse(UUID connectionId, String provider, boolean configured,
                                             List<String> requiredFields, String message) {}
