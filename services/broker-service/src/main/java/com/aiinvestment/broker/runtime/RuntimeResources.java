package com.aiinvestment.broker.runtime;
import java.util.UUID;
public record RuntimeResources(UUID connectorId, String runtimeIdentity, boolean podPresent, boolean servicePresent,
                               boolean labelsValid, boolean selectorValid, String endpoint) {
    public RuntimeReconciliationState state() {
        if (!labelsValid || !selectorValid) return RuntimeReconciliationState.IDENTITY_MISMATCH;
        if (podPresent && servicePresent) return RuntimeReconciliationState.BOTH_MATCHING;
        if (podPresent) return RuntimeReconciliationState.POD_ONLY;
        if (servicePresent) return RuntimeReconciliationState.SERVICE_ONLY;
        return RuntimeReconciliationState.ABSENT_BOTH;
    }
}
