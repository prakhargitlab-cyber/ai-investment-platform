package com.aiinvestment.broker.connector;

import java.net.URI;
import java.util.UUID;

/** Trusted infrastructure location for a connector runtime; never browser supplied. */
public record ConnectorRuntimeLocation(
        UUID connectorId,
        URI runtimeEndpoint,
        String runtimeIdentity,
        String runtimeProvider
) {
}
