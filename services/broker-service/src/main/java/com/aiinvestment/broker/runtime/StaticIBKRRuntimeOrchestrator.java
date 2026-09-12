package com.aiinvestment.broker.runtime;

import java.time.Clock;
import java.time.Instant;
import java.util.Optional;
import java.util.UUID;

final class StaticIBKRRuntimeOrchestrator implements IBKRRuntimeOrchestrator {
    private final String endpoint;
    private final Clock clock;
    StaticIBKRRuntimeOrchestrator(String endpoint, Clock clock) { this.endpoint = endpoint; this.clock = clock; }
    @Override public IBKRRuntimeAllocationOutcome allocate(UUID connectorId, UUID userId) {
        IBKRRuntimeDescriptor descriptor = new IBKRRuntimeDescriptor(connectorId, endpoint, "static-ibkr-connector", "STATIC", Instant.now(clock));
        return new IBKRRuntimeAllocationOutcome(descriptor, new RuntimeAllocationResult(connectorId, descriptor.runtimeIdentity(), endpoint, false, false, true, true, false, true));
    }
    @Override public Optional<IBKRRuntimeDescriptor> status(UUID connectorId) { return Optional.empty(); }
    @Override public void stop(UUID connectorId) { }
    @Override public void compensate(RuntimeAllocationResult allocationResult) { }
}
