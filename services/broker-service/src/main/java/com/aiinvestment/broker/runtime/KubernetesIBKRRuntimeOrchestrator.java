package com.aiinvestment.broker.runtime;

import com.aiinvestment.broker.application.BrokerProviderException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.time.Instant;
import java.util.Optional;
import java.util.UUID;

final class KubernetesIBKRRuntimeOrchestrator implements IBKRRuntimeOrchestrator {
    private static final Logger logger = LoggerFactory.getLogger(KubernetesIBKRRuntimeOrchestrator.class);
    private static final long READINESS_POLL_MILLIS = 250L;
    private final IBKRRuntimeProperties properties;
    private final IBKRKubernetesRuntimeSpecProperties runtimeSpec;
    private final KubernetesRuntimeResourceClient client;
    private final Clock clock;
    private final RuntimeWaitStrategy waitStrategy;

    KubernetesIBKRRuntimeOrchestrator(IBKRRuntimeProperties properties, IBKRKubernetesRuntimeSpecProperties runtimeSpec,
                                      KubernetesRuntimeResourceClient client, Clock clock) {
        this(properties, runtimeSpec, client, clock, duration -> Thread.sleep(duration.toMillis()));
    }

    KubernetesIBKRRuntimeOrchestrator(IBKRRuntimeProperties properties, IBKRKubernetesRuntimeSpecProperties runtimeSpec,
                                      KubernetesRuntimeResourceClient client, Clock clock, RuntimeWaitStrategy waitStrategy) {
        this.properties = properties;
        this.runtimeSpec = runtimeSpec;
        this.client = client;
        this.clock = clock;
        this.waitStrategy = waitStrategy;
    }

    @Override
    public IBKRRuntimeAllocationOutcome allocate(UUID connectorId, UUID userId) {
        KubernetesRuntimeResource resource = resource(connectorId);
        try {
            RuntimeAllocationResult result = client.reconcileOrCreate(properties.namespace(), resource);
            if (!result.podPresent() || !result.servicePresent()) throw BrokerProviderException.unavailable();
            awaitReady(connectorId);
            return new IBKRRuntimeAllocationOutcome(new IBKRRuntimeDescriptor(result.connectorId(), result.endpoint(), result.runtimeIdentity(), "KUBERNETES", Instant.now(clock)), result);
        } catch (RuntimeException exception) {
            try {
                client.compensate(new RuntimeAllocationResult(connectorId, resource.identity(), resource.endpoint(), true, true, false, false, false, false));
            } catch (RuntimeException cleanupFailure) {
                logger.warn("ibkr_runtime_allocation_failure stage=COMPENSATION connector={} resource_kind=Runtime resource_name={} exception={}",
                        connectorId, resource.name(), cleanupFailure.getClass().getSimpleName());
            }
            BrokerProviderException unavailable = BrokerProviderException.unavailable();
            unavailable.initCause(exception);
            throw unavailable;
        }
    }

    private void awaitReady(UUID connectorId) {
        Instant deadline = Instant.now(clock).plusSeconds(properties.startupTimeoutSeconds());
        int attempts = 0;
        while (!Instant.now(clock).isAfter(deadline)) {
            RuntimeTechnicalStatus technicalStatus = client.inspect(properties.namespace(), connectorId);
            if (technicalStatus == RuntimeTechnicalStatus.READY) return;
            if (technicalStatus == RuntimeTechnicalStatus.FAILED) {
                logger.warn("ibkr_runtime_startup_failed connector={} technical_state={} attempts={}", connectorId, technicalStatus, attempts);
                throw BrokerProviderException.unavailable();
            }
            attempts++;
            logger.info("ibkr_runtime_startup_wait connector={} technical_state={} attempts={}", connectorId, technicalStatus, attempts);
            try { waitStrategy.await(java.time.Duration.ofMillis(READINESS_POLL_MILLIS)); }
            catch (InterruptedException interrupted) { Thread.currentThread().interrupt(); throw BrokerProviderException.unavailable(); }
        }
        logger.warn("ibkr_runtime_startup_timeout connector={} attempts={}", connectorId, attempts);
        throw BrokerProviderException.unavailable();
    }

    @Override
    public Optional<IBKRRuntimeDescriptor> status(UUID connectorId) {
        RuntimeTechnicalStatus status = client.inspect(properties.namespace(), connectorId);
        return status == RuntimeTechnicalStatus.READY ? Optional.of(descriptor(resource(connectorId))) : Optional.empty();
    }

    @Override
    public void stop(UUID connectorId) {
        RuntimeStopResult result = client.stop(properties.namespace(), connectorId);
        if (!result.confirmedStopped()) throw BrokerProviderException.unavailable();
    }

    @Override public void compensate(RuntimeAllocationResult allocationResult) { client.compensate(allocationResult); }

    private KubernetesRuntimeResource resource(UUID connectorId) {
        return new KubernetesRuntimeResource(connectorId, name(connectorId), properties.namespace(), properties.image(),
                properties.imagePullPolicy(), properties.serviceAccountName(), properties.port(), runtimeSpec);
    }

    private IBKRRuntimeDescriptor descriptor(KubernetesRuntimeResource resource) {
        return new IBKRRuntimeDescriptor(resource.connectorId(), resource.endpoint(), resource.identity(),
                "KUBERNETES", Instant.now(clock));
    }

    static String name(UUID connectorId) {
        return "ibkr-runtime-" + connectorId.toString().replace("-", "").substring(0, 12);
    }
}
