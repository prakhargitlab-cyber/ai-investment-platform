package com.aiinvestment.broker.runtime;

import java.util.Optional;
import java.util.UUID;

public interface IBKRRuntimeOrchestrator {
    IBKRRuntimeAllocationOutcome allocate(UUID connectorId, UUID userId);
    Optional<IBKRRuntimeDescriptor> status(UUID connectorId);
    void stop(UUID connectorId);
    void compensate(RuntimeAllocationResult allocationResult);
}
