package com.aiinvestment.broker.runtime;

import java.time.Instant;
import java.util.UUID;

public record IBKRRuntimeDescriptor(UUID connectorId, String runtimeEndpoint, String runtimeIdentity,
                                    String runtimeProvider, Instant runtimeCreatedAt) {
}
