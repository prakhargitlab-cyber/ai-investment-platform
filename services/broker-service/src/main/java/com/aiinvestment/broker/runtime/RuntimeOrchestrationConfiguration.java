package com.aiinvestment.broker.runtime;

import com.aiinvestment.broker.config.IBKRProviderProperties;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import io.fabric8.kubernetes.client.KubernetesClientBuilder;

import java.time.Clock;
import java.util.Optional;

@Configuration
class RuntimeOrchestrationConfiguration {
    private static final Logger logger = LoggerFactory.getLogger(RuntimeOrchestrationConfiguration.class);
    @Bean
    Clock runtimeClock() { return Clock.systemUTC(); }

    @Bean
    IBKRRuntimeOrchestrator ibkrRuntimeOrchestrator(IBKRRuntimeProperties properties,
                                                    IBKRProviderProperties providerProperties,
                                                    IBKRKubernetesRuntimeSpecProperties runtimeSpec,
                                                    KubernetesRuntimeResourceClient client,
                                                    Clock runtimeClock) {
        logger.info("IBKR runtime configuration: mode={}, kubernetes={}", properties.mode(), properties.kubernetes());
        if (properties.kubernetes()) validateRequiredSpec(runtimeSpec);
        if (properties.kubernetes()) return new KubernetesIBKRRuntimeOrchestrator(properties, runtimeSpec, client, runtimeClock);
        return new StaticIBKRRuntimeOrchestrator(providerProperties.connectorBaseUrl(), runtimeClock);
    }

    private static void validateRequiredSpec(IBKRKubernetesRuntimeSpecProperties spec) {
        if (blank(spec.getRuntimeImage()) || blank(spec.getInitContainer().getImage())
                || blank(spec.getGatewayPackage().getClaimName()) || blank(spec.getInternalToken().getSecretName())
                || blank(spec.getInternalToken().getSecretKey())) {
            throw new IllegalStateException("IBKR Kubernetes runtime configuration is incomplete");
        }
    }
    private static boolean blank(String value) { return value == null || value.isBlank(); }

    @Bean
    KubernetesRuntimeResourceClient kubernetesRuntimeResourceClient(IBKRRuntimeProperties properties) {
        if (properties.kubernetes()) return new Fabric8KubernetesRuntimeResourceClient(new KubernetesClientBuilder().build());
        return new KubernetesRuntimeResourceClient() {
            @Override public RuntimeResources get(String ns, java.util.UUID id) { return new RuntimeResources(id, "", false, false, true, true, ""); }
            @Override public RuntimeAllocationResult reconcileOrCreate(String ns, KubernetesRuntimeResource r) { throw new UnsupportedOperationException("Kubernetes runtime API client is not configured"); }
            @Override public RuntimeTechnicalStatus inspect(String ns, java.util.UUID id) { return RuntimeTechnicalStatus.ABSENT; }
            @Override public RuntimeStopResult stop(String ns, java.util.UUID id) { return new RuntimeStopResult(id, "", false, false, true, true, true); }
            @Override public void compensate(RuntimeAllocationResult result) { }
        };
    }
}
