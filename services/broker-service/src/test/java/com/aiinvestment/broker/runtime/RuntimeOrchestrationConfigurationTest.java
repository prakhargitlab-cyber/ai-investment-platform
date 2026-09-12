package com.aiinvestment.broker.runtime;

import com.aiinvestment.broker.config.IBKRProviderProperties;
import org.junit.jupiter.api.Test;

import java.time.Clock;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.mock;

class RuntimeOrchestrationConfigurationTest {
    private final RuntimeOrchestrationConfiguration configuration = new RuntimeOrchestrationConfiguration();

    @Test
    void kubernetesModeRemainsTrueAndSelectsTheKubernetesOrchestrator() {
        IBKRRuntimeProperties properties = properties("KUBERNETES");

        assertThat(properties.kubernetes()).isTrue();
        assertThat(configuration.ibkrRuntimeOrchestrator(properties, providerProperties(),
                completeSpec(), mock(KubernetesRuntimeResourceClient.class), Clock.systemUTC()))
                .isInstanceOf(KubernetesIBKRRuntimeOrchestrator.class);
    }

    @Test void kubernetesModeFailsFastForEachMissingRequiredRuntimeValueWithoutExposingIt() {
        for (String missing : java.util.List.of("runtime image", "init image", "PVC claim", "secret name", "secret key")) {
            IBKRKubernetesRuntimeSpecProperties spec = completeSpec();
            switch (missing) {
                case "runtime image" -> spec.setRuntimeImage("");
                case "init image" -> spec.getInitContainer().setImage("");
                case "PVC claim" -> spec.getGatewayPackage().setClaimName("");
                case "secret name" -> spec.getInternalToken().setSecretName("");
                default -> spec.getInternalToken().setSecretKey("");
            }
            assertThatThrownBy(() -> configuration.ibkrRuntimeOrchestrator(properties("KUBERNETES"), providerProperties(), spec,
                    mock(KubernetesRuntimeResourceClient.class), Clock.systemUTC()))
                    .isInstanceOf(IllegalStateException.class).hasMessage("IBKR Kubernetes runtime configuration is incomplete")
                    .hasMessageNotContaining("secret");
        }
    }

    @Test
    void staticModeRemainsFalseAndSelectsTheStaticOrchestrator() {
        IBKRRuntimeProperties properties = properties("STATIC");

        assertThat(properties.kubernetes()).isFalse();
        assertThat(configuration.ibkrRuntimeOrchestrator(properties, providerProperties(),
                new IBKRKubernetesRuntimeSpecProperties(), mock(KubernetesRuntimeResourceClient.class), Clock.systemUTC()))
                .isInstanceOf(StaticIBKRRuntimeOrchestrator.class);
    }

    private static IBKRKubernetesRuntimeSpecProperties completeSpec() {
        IBKRKubernetesRuntimeSpecProperties spec = new IBKRKubernetesRuntimeSpecProperties();
        spec.setRuntimeImage("registry/ibkr:tag"); spec.getInitContainer().setImage("registry/ibkr:tag");
        spec.getGatewayPackage().setClaimName("gateway-pvc");
        spec.getInternalToken().setSecretName("runtime-token"); spec.getInternalToken().setSecretKey("AIP_INTERNAL_TOKEN");
        return spec;
    }

    private static IBKRRuntimeProperties properties(String mode) {
        return new IBKRRuntimeProperties(mode, "namespace", "image", "IfNotPresent", "service-account", 8080,
                120, "", "", "", "/opt/ibkr/clientportal.gw-source", true,
                "/opt/ibkr/runtime", "", "", "", "", "https://127.0.0.1:5000/v1/api", false, "");
    }

    private static IBKRProviderProperties providerProperties() {
        return new IBKRProviderProperties(true, "", "", "", "", "", "", "client-portal-gateway",
                true, false, false, "http://ibkr-connector", "LOCAL_AGENT", "", "", 1800, 86400, "", "");
    }
}
