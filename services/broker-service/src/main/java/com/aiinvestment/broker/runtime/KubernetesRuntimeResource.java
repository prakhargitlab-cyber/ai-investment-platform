package com.aiinvestment.broker.runtime;

import java.util.UUID;

record KubernetesRuntimeResource(UUID connectorId, String name, String namespace, String image,
                                 String imagePullPolicy, String serviceAccountName, int port,
                                 IBKRKubernetesRuntimeSpecProperties spec) {
    String endpoint() { return "http://" + name + ":" + spec.getPorts().getServicePort(); }
    String identity() { return "service/" + name; }
}
