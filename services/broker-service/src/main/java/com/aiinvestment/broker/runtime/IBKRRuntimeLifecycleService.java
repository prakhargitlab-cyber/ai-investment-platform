package com.aiinvestment.broker.runtime;

import com.aiinvestment.broker.application.BrokerConnectionNotFoundException;
import com.aiinvestment.broker.persistence.BrokerConnectorInstanceEntity;
import com.aiinvestment.broker.persistence.BrokerConnectorInstanceRepository;
import com.aiinvestment.shared.domain.broker.BrokerType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.time.Clock;
import java.time.Instant;
import java.util.UUID;

@Service
public class IBKRRuntimeLifecycleService {
    private static final Logger logger = LoggerFactory.getLogger(IBKRRuntimeLifecycleService.class);
    private final BrokerConnectorInstanceRepository repository;
    private final IBKRRuntimeOrchestrator orchestrator;
    private final Clock clock;

    public IBKRRuntimeLifecycleService(BrokerConnectorInstanceRepository repository, IBKRRuntimeOrchestrator orchestrator,
                                       Clock runtimeClock) {
        this.repository = repository;
        this.orchestrator = orchestrator;
        this.clock = runtimeClock;
    }

    public IBKRRuntimeDescriptor allocate(UUID connectorId, UUID trustedUserId) {
        BrokerConnectorInstanceEntity connector = owned(connectorId, trustedUserId);
        IBKRRuntimeAllocationOutcome outcome;
        try {
            outcome = orchestrator.allocate(connectorId, trustedUserId);
        } catch (RuntimeException exception) {
            throw new IBKRRuntimeLifecycleException("RUNTIME_ALLOCATION_FAILED", exception);
        }
        IBKRRuntimeDescriptor descriptor = outcome.descriptor();
        if (!connectorId.equals(descriptor.connectorId())) {
            orchestrator.compensate(outcome.allocationResult());
            throw new IBKRRuntimeLifecycleException("RUNTIME_RECONCILIATION_FAILED");
        }
        try {
            owned(connectorId, trustedUserId).markRuntimeAllocated(descriptor.runtimeEndpoint(), descriptor.runtimeIdentity(),
                    descriptor.runtimeProvider(), descriptor.runtimeCreatedAt());
            repository.flush();
            return descriptor;
        } catch (RuntimeException exception) {
            logger.warn("ibkr_runtime_allocation_failure stage=LIFECYCLE_PERSIST connector={} exception={}",
                    connectorId, exception.getClass().getSimpleName());
            try {
                orchestrator.compensate(outcome.allocationResult());
            } catch (RuntimeException cleanupFailure) {
                logger.warn("ibkr_runtime_allocation_failure stage=COMPENSATION connector={} exception={}",
                        connectorId, cleanupFailure.getClass().getSimpleName());
                exception.addSuppressed(cleanupFailure);
            }
            throw new IBKRRuntimeLifecycleException("RUNTIME_PERSISTENCE_FAILED", exception);
        }
    }

    public IBKRRuntimeDescriptor status(UUID connectorId, UUID trustedUserId) {
        owned(connectorId, trustedUserId);
        return orchestrator.status(connectorId)
                .orElseThrow(() -> new IBKRRuntimeLifecycleException("RUNTIME_RECONCILIATION_FAILED"));
    }

    /**
     * Verifies that the owned runtime is technically ready before its persisted endpoint is used.
     */
    public boolean isReady(UUID connectorId, UUID trustedUserId) {
        owned(connectorId, trustedUserId);
        return orchestrator.status(connectorId).isPresent();
    }

    public void stop(UUID connectorId, UUID trustedUserId) {
        BrokerConnectorInstanceEntity connector = owned(connectorId, trustedUserId);
        try {
            orchestrator.stop(connectorId);
        } catch (RuntimeException exception) {
            throw new IBKRRuntimeLifecycleException("RUNTIME_STOP_FAILED");
        }
        connector.markRuntimeStopped(Instant.now(clock));
        repository.flush();
    }

    private BrokerConnectorInstanceEntity owned(UUID connectorId, UUID trustedUserId) {
        BrokerConnectorInstanceEntity connector = repository.findByConnectorIdAndUserId(connectorId, trustedUserId)
                .orElseThrow(() -> new BrokerConnectionNotFoundException(connectorId));
        if (connector.getBrokerType() != BrokerType.IBKR) throw new BrokerConnectionNotFoundException(connectorId);
        return connector;
    }

}
