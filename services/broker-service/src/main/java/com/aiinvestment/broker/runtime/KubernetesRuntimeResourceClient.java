package com.aiinvestment.broker.runtime;

import java.util.UUID;

interface KubernetesRuntimeResourceClient {
    RuntimeResources get(String namespace, UUID connectorId);
    RuntimeAllocationResult reconcileOrCreate(String namespace, KubernetesRuntimeResource resource);
    RuntimeTechnicalStatus inspect(String namespace, UUID connectorId);
    RuntimeStopResult stop(String namespace, UUID connectorId);
    void compensate(RuntimeAllocationResult result);
}
