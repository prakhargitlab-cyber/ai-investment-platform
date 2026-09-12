package com.aiinvestment.broker.runtime;
import java.util.UUID;
public record RuntimeStopResult(UUID connectorId, String runtimeIdentity, boolean podDeleted, boolean serviceDeleted,
                                boolean podAlreadyAbsent, boolean serviceAlreadyAbsent, boolean confirmedStopped) { }
