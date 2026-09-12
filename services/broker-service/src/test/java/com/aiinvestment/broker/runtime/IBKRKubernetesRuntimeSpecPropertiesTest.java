package com.aiinvestment.broker.runtime;

import com.aiinvestment.broker.config.BrokerProviderConfiguration;
import org.junit.jupiter.api.Test;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;

import java.lang.reflect.Field;

import static org.assertj.core.api.Assertions.assertThat;

class IBKRKubernetesRuntimeSpecPropertiesTest {
    private final ApplicationContextRunner context = new ApplicationContextRunner()
            .withUserConfiguration(BrokerProviderConfiguration.class)
            .withPropertyValues(
                    "broker.ibkr.runtime.mode=STATIC",
                    "broker.ibkr.runtime.kubernetes.spec.runtime-image=acr.example.test/ibkr-connector@sha256:abc",
                    "broker.ibkr.runtime.kubernetes.spec.image-pull-policy=IfNotPresent",
                    "broker.ibkr.runtime.kubernetes.spec.resources.requests.cpu=250m",
                    "broker.ibkr.runtime.kubernetes.spec.resources.requests.memory=512Mi",
                    "broker.ibkr.runtime.kubernetes.spec.resources.limits.cpu=1",
                    "broker.ibkr.runtime.kubernetes.spec.resources.limits.memory=1Gi",
                    "broker.ibkr.runtime.kubernetes.spec.init-container.name=copy-gateway-package",
                    "broker.ibkr.runtime.kubernetes.spec.init-container.image=acr.example.test/ibkr-connector@sha256:abc",
                    "broker.ibkr.runtime.kubernetes.spec.init-container.command[0]=/bin/sh",
                    "broker.ibkr.runtime.kubernetes.spec.init-container.command[1]=-c",
                    "broker.ibkr.runtime.kubernetes.spec.init-container.args[0]=cp -a /source/. /runtime/",
                    "broker.ibkr.runtime.kubernetes.spec.service-account-name=ibkr-runtime",
                    "broker.ibkr.runtime.kubernetes.spec.image-pull-secrets[0]=registry-pull",
                    "broker.ibkr.runtime.kubernetes.spec.pod-labels.environment=dev",
                    "broker.ibkr.runtime.kubernetes.spec.pod-annotations.example.com/key=value",
                    "broker.ibkr.runtime.kubernetes.spec.service-labels.component=connector",
                    "broker.ibkr.runtime.kubernetes.spec.service-annotations.example.com/key=value",
                    "broker.ibkr.runtime.kubernetes.spec.startup-probe.initial-delay-seconds=1",
                    "broker.ibkr.runtime.kubernetes.spec.readiness-probe.period-seconds=7",
                    "broker.ibkr.runtime.kubernetes.spec.liveness-probe.timeout-seconds=4",
                    "broker.ibkr.runtime.kubernetes.spec.ports.container-port=8080",
                    "broker.ibkr.runtime.kubernetes.spec.ports.service-port=80",
                    "broker.ibkr.runtime.kubernetes.spec.ports.service-target-port=8080",
                    "broker.ibkr.runtime.kubernetes.spec.ports.gateway-port=5000",
                    "broker.ibkr.runtime.kubernetes.spec.gateway-package.claim-name=ibkr-gateway-package-dev",
                    "broker.ibkr.runtime.kubernetes.spec.gateway-package.read-only=true",
                    "broker.ibkr.runtime.kubernetes.spec.workspace.mount-path=/opt/ibkr/runtime",
                    "broker.ibkr.runtime.kubernetes.spec.workspace.size-limit=1Gi",
                    "broker.ibkr.runtime.kubernetes.spec.internal-token.secret-name=ibkr-runtime-token",
                    "broker.ibkr.runtime.kubernetes.spec.internal-token.secret-key=AIP_INTERNAL_TOKEN",
                    "broker.ibkr.runtime.kubernetes.spec.node-selector.workload=connector",
                    "broker.ibkr.runtime.kubernetes.spec.tolerations[0].key=dedicated",
                    "broker.ibkr.runtime.kubernetes.spec.tolerations[0].operator=Equal",
                    "broker.ibkr.runtime.kubernetes.spec.tolerations[0].value=ibkr",
                    "broker.ibkr.runtime.kubernetes.spec.tolerations[0].effect=NoSchedule",
                    "broker.ibkr.runtime.kubernetes.spec.tolerations[0].toleration-seconds=60",
                    "broker.ibkr.runtime.kubernetes.spec.affinity.node-affinity.required=true",
                    "broker.ibkr.runtime.kubernetes.spec.topology-spread-constraints[0].topology-key=kubernetes.io/hostname"
            );

    @Test
    void bindsTheDedicatedKubernetesRuntimeSpecWithoutChangingLifecycleProperties() {
        context.run(ctx -> {
            IBKRKubernetesRuntimeSpecProperties spec = ctx.getBean(IBKRKubernetesRuntimeSpecProperties.class);
            IBKRRuntimeProperties lifecycle = ctx.getBean(IBKRRuntimeProperties.class);

            assertThat(lifecycle.mode()).isEqualTo("STATIC");
            assertThat(spec.getRuntimeImage()).contains("sha256:abc");
            assertThat(spec.getResources().getRequests().getCpu()).isEqualTo("250m");
            assertThat(spec.getResources().getLimits().getMemory()).isEqualTo("1Gi");
            assertThat(spec.getInitContainer().getCommand()).containsExactly("/bin/sh", "-c");
            assertThat(spec.getInitContainer().getArgs()).containsExactly("cp -a /source/. /runtime/");
            assertThat(spec.getImagePullSecrets()).containsExactly("registry-pull");
            assertThat(spec.getPodLabels()).containsEntry("environment", "dev");
            assertThat(spec.getServiceLabels()).containsEntry("component", "connector");
            assertThat(spec.getStartupProbe().getPath()).isEqualTo("/actuator/health/readiness");
            assertThat(spec.getReadinessProbe().getPeriodSeconds()).isEqualTo(7);
            assertThat(spec.getLivenessProbe().getPath()).isEqualTo("/actuator/health/liveness");
            assertThat(spec.getPorts().getContainerPort()).isEqualTo(8080);
            assertThat(spec.getPorts().getServicePort()).isEqualTo(80);
            assertThat(spec.getPorts().getServiceTargetPort()).isEqualTo(8080);
            assertThat(spec.getPorts().getGatewayPort()).isEqualTo(5000);
            assertThat(spec.getGatewayPackage().getClaimName()).isEqualTo("ibkr-gateway-package-dev");
            assertThat(spec.getGatewayPackage().isReadOnly()).isTrue();
            assertThat(spec.getWorkspace().getMountPath()).isEqualTo("/opt/ibkr/runtime");
            assertThat(spec.getInternalToken().getSecretName()).isEqualTo("ibkr-runtime-token");
            assertThat(spec.getInternalToken().getSecretKey()).isEqualTo("AIP_INTERNAL_TOKEN");
            assertThat(spec.getSecurity().isRunAsNonRoot()).isTrue();
            assertThat(spec.getSecurity().getDropCapabilities()).containsExactly("ALL");
            assertThat(spec.getNodeSelector()).containsEntry("workload", "connector");
            assertThat(spec.getTolerations()).singleElement().extracting(IBKRKubernetesRuntimeSpecProperties.TolerationSpec::getEffect).isEqualTo("NoSchedule");
            assertThat(spec.getAffinity().get("node-affinity")).isInstanceOf(java.util.Map.class);
            assertThat(spec.getTopologySpreadConstraints()).singleElement()
                    .extracting(value -> value.get("topology-key")).isEqualTo("kubernetes.io/hostname");
        });
    }

    @Test
    void carriesOnlyTheInternalTokenReferenceAndDocumentsReservedIdentityMetadata() {
        assertThat(IBKRKubernetesRuntimeSpecProperties.class.getDeclaredFields())
                .extracting(Field::getName)
                .doesNotContain("internalTokenValue", "tokenValue", "secretValue");
        assertThat(IBKRKubernetesRuntimeSpecProperties.InternalTokenSecret.class.getDeclaredFields())
                .extracting(Field::getName)
                .containsExactlyInAnyOrder("secretName", "secretKey");
        assertThat(IBKRKubernetesRuntimeSpecProperties.class.getDeclaredAnnotation(ConfigurationProperties.class)
                .prefix()).isEqualTo("broker.ibkr.runtime.kubernetes.spec");
    }
}
