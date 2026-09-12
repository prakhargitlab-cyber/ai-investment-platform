package com.aiinvestment.broker.runtime;
import java.util.UUID;
public record RuntimeAllocationResult(UUID connectorId, String runtimeIdentity, String endpoint, boolean podCreated,
                                      boolean serviceCreated, boolean podPresent, boolean servicePresent, boolean ready,
                                      boolean reconciled) { }
