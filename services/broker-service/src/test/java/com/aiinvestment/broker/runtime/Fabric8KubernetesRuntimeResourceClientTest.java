package com.aiinvestment.broker.runtime;

import io.fabric8.kubernetes.api.model.Pod;
import io.fabric8.kubernetes.api.model.Service;
import io.fabric8.kubernetes.api.model.PodBuilder;
import io.fabric8.kubernetes.api.model.PodConditionBuilder;
import io.fabric8.kubernetes.api.model.StatusBuilder;
import io.fabric8.kubernetes.api.model.StatusCauseBuilder;
import io.fabric8.kubernetes.api.model.StatusDetailsBuilder;
import io.fabric8.kubernetes.client.KubernetesClient;
import io.fabric8.kubernetes.client.KubernetesClientException;
import io.fabric8.kubernetes.client.server.mock.KubernetesServer;
import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;

import java.util.List;
import java.util.UUID;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.mock;
import org.slf4j.LoggerFactory;

class Fabric8KubernetesRuntimeResourceClientTest {
    private KubernetesServer server;

    @BeforeEach void startServer() { server = new KubernetesServer(true, true); server.before(); }
    @AfterEach void stopServer() { server.after(); }

    @Test
    void buildsAnIsolatedSecurePodFromTheTypedSpecWithoutASecretValue() {
        IBKRKubernetesRuntimeSpecProperties spec = spec();
        KubernetesRuntimeResource resource = resource(spec);
        Pod pod = new Fabric8KubernetesRuntimeResourceClient(mock(KubernetesClient.class)).pod(resource);

        assertThat(pod.getMetadata().getName()).isEqualTo(resource.name());
        assertThat(pod.getMetadata().getLabels()).containsEntry(Fabric8KubernetesRuntimeResourceClient.CONNECTOR_ID_LABEL, resource.connectorId().toString());
        assertThat(pod.getSpec().getContainers()).singleElement().satisfies(container -> {
            assertThat(container.getImage()).isEqualTo("acr.example.test/ibkr@sha256:abc");
            assertThat(container.getPorts()).singleElement().extracting(p -> p.getContainerPort()).isEqualTo(8080);
            assertThat(container.getEnv()).anySatisfy(env -> {
                assertThat(env.getName()).isEqualTo("AIP_INTERNAL_TOKEN");
                assertThat(env.getValue()).isNull();
                assertThat(env.getValueFrom().getSecretKeyRef().getName()).isEqualTo("runtime-token");
                assertThat(env.getValueFrom().getSecretKeyRef().getKey()).isEqualTo("AIP_INTERNAL_TOKEN");
            });
            assertThat(container.getSecurityContext().getRunAsNonRoot()).isTrue();
            assertThat(container.getSecurityContext().getRunAsUser()).isEqualTo(1000L);
            assertThat(container.getSecurityContext().getAllowPrivilegeEscalation()).isFalse();
            assertThat(container.getSecurityContext().getCapabilities().getDrop()).containsExactly("ALL");
            assertThat(container.getStartupProbe().getHttpGet().getPath()).isEqualTo("/actuator/health/readiness");
            assertThat(container.getLivenessProbe().getHttpGet().getPath()).isEqualTo("/actuator/health/liveness");
        });
        assertThat(pod.getSpec().getVolumes()).anySatisfy(volume -> assertThat(volume.getPersistentVolumeClaim()).isNotNull());
        assertThat(pod.getSpec().getVolumes()).anySatisfy(volume -> assertThat(volume.getEmptyDir()).isNotNull());
        assertThat(pod.getSpec().getInitContainers()).singleElement().satisfies(init -> {
            assertThat(init.getName()).isEqualTo("copy-gateway-package");
            assertThat(init.getImage()).isEqualTo("acr.example.test/ibkr@sha256:abc");
            assertThat(init.getCommand()).containsExactly("/bin/sh", "-c");
            assertThat(init.getArgs()).containsExactly("cp -a /opt/ibkr/clientportal.gw-source/. /opt/ibkr/runtime/ && chmod u+x /opt/ibkr/runtime/bin/run.sh && chown -R 1000:1000 /opt/ibkr/runtime");
            assertThat(init.getSecurityContext().getRunAsUser()).isZero();
            assertThat(Boolean.TRUE.equals(init.getSecurityContext().getPrivileged())).isFalse();
            assertThat(init.getSecurityContext().getAllowPrivilegeEscalation()).isFalse();
            assertThat(init.getSecurityContext().getCapabilities().getDrop()).containsExactly("ALL");
            assertThat(init.getSecurityContext().getCapabilities().getAdd()).containsExactly("CHOWN");
        });
        assertThat(pod.getSpec().getVolumes()).anySatisfy(volume -> {
            assertThat(volume.getName()).isEqualTo("gateway-package");
            assertThat(volume.getPersistentVolumeClaim().getClaimName()).isEqualTo("gateway-package");
        });
        assertThat(pod.getSpec().getInitContainers().get(0).getVolumeMounts()).anySatisfy(mount -> {
            assertThat(mount.getName()).isEqualTo("gateway-package");
            assertThat(mount.getMountPath()).isEqualTo("/opt/ibkr/clientportal.gw-source");
            assertThat(mount.getReadOnly()).isTrue();
        });
        assertThat(pod.getSpec().getInitContainers().get(0).getVolumeMounts()).anySatisfy(mount -> {
            assertThat(mount.getName()).isEqualTo("runtime-workspace");
            assertThat(mount.getMountPath()).isEqualTo("/opt/ibkr/runtime");
            assertThat(Boolean.TRUE.equals(mount.getReadOnly())).isFalse();
        });
        assertThat(pod.getSpec().getContainers().get(0).getVolumeMounts()).anySatisfy(mount -> {
            assertThat(mount.getName()).isEqualTo("runtime-workspace");
            assertThat(mount.getMountPath()).isEqualTo("/opt/ibkr/runtime");
            assertThat(Boolean.TRUE.equals(mount.getReadOnly())).isFalse();
        });
        assertThat(pod.getSpec().getNodeSelector()).isEmpty();
        assertThat(pod.getSpec().getAffinity()).isNull();
        assertThat(pod.getSpec().getHostNetwork()).isNull();
        assertThat(pod.getSpec().getContainers().get(0).getPorts().get(0).getHostPort()).isNull();
    }

    @Test
    void buildsAConnectorScopedClusterIpServiceWithMatchingMandatorySelector() {
        KubernetesRuntimeResource resource = resource(spec());
        Service service = new Fabric8KubernetesRuntimeResourceClient(mock(KubernetesClient.class)).service(resource);

        assertThat(service.getSpec().getType()).isEqualTo("ClusterIP");
        assertThat(service.getSpec().getPorts()).singleElement().satisfies(port -> {
            assertThat(port.getPort()).isEqualTo(80);
            assertThat(port.getTargetPort().getIntVal()).isEqualTo(8080);
            assertThat(port.getNodePort()).isNull();
        });
        assertThat(service.getSpec().getSelector()).containsEntry(Fabric8KubernetesRuntimeResourceClient.CONNECTOR_ID_LABEL, resource.connectorId().toString());
        assertThat(service.getSpec().getSelector()).isEqualTo(service.getMetadata().getLabels().entrySet().stream()
                .filter(entry -> entry.getKey().equals(Fabric8KubernetesRuntimeResourceClient.CONNECTOR_ID_LABEL)
                        || entry.getKey().equals("app.kubernetes.io/name") || entry.getKey().equals("app.kubernetes.io/managed-by") || entry.getKey().equals("ai-investment/broker"))
                .collect(java.util.stream.Collectors.toMap(java.util.Map.Entry::getKey, java.util.Map.Entry::getValue)));
    }

    @Test
    void reconcilesAbsentAndPartialResourcesAndNeverCreatesForMismatchedIdentity() {
        Fabric8KubernetesRuntimeResourceClient adapter = new Fabric8KubernetesRuntimeResourceClient(server.getClient());
        KubernetesRuntimeResource resource = resource(spec());
        RuntimeAllocationResult first = adapter.reconcileOrCreate("ai-investment", resource);
        assertThat(first.podCreated()).isTrue(); assertThat(first.serviceCreated()).isTrue();
        assertThat(server.getClient().pods().inNamespace("ai-investment").withName(resource.name()).get()).isNotNull();
        assertThat(server.getClient().services().inNamespace("ai-investment").withName(resource.name()).get()).isNotNull();

        RuntimeAllocationResult reused = adapter.reconcileOrCreate("ai-investment", resource);
        assertThat(reused.podCreated()).isFalse(); assertThat(reused.serviceCreated()).isFalse();

        server.getClient().services().inNamespace("ai-investment").withName(resource.name()).delete();
        RuntimeAllocationResult podOnly = adapter.reconcileOrCreate("ai-investment", resource);
        assertThat(podOnly.podCreated()).isFalse(); assertThat(podOnly.serviceCreated()).isTrue();

        server.getClient().pods().inNamespace("ai-investment").withName(resource.name()).delete();
        RuntimeAllocationResult serviceOnly = adapter.reconcileOrCreate("ai-investment", resource);
        assertThat(serviceOnly.podCreated()).isTrue(); assertThat(serviceOnly.serviceCreated()).isFalse();
    }

    @Test
    void compensationAndStopAreScopedToTheAllocationFlagsAndValidatedIdentity() {
        Fabric8KubernetesRuntimeResourceClient adapter = new Fabric8KubernetesRuntimeResourceClient(server.getClient());
        KubernetesRuntimeResource resource = resource(spec());
        RuntimeAllocationResult allocated = adapter.reconcileOrCreate("ai-investment", resource);
        adapter.compensate(new RuntimeAllocationResult(resource.connectorId(), allocated.runtimeIdentity(), allocated.endpoint(), true, false, true, true, false, false));
        assertThat(server.getClient().pods().inNamespace("ai-investment").withName(resource.name()).get()).isNull();
        assertThat(server.getClient().services().inNamespace("ai-investment").withName(resource.name()).get()).isNotNull();

        RuntimeStopResult stopped = adapter.stop("ai-investment", resource.connectorId());
        assertThat(stopped.podAlreadyAbsent()).isTrue(); assertThat(stopped.serviceDeleted()).isTrue(); assertThat(stopped.confirmedStopped()).isTrue();
        assertThat(adapter.inspect("ai-investment", resource.connectorId())).isEqualTo(RuntimeTechnicalStatus.ABSENT);
    }

    @Test
    void mapsTechnicalPodStatesAndStopsBothMatchingResources() {
        Fabric8KubernetesRuntimeResourceClient adapter = new Fabric8KubernetesRuntimeResourceClient(server.getClient());
        KubernetesRuntimeResource resource = resource(spec());
        adapter.reconcileOrCreate("ai-investment", resource);
        Pod pod = server.getClient().pods().inNamespace("ai-investment").withName(resource.name()).get();

        server.getClient().pods().inNamespace("ai-investment").resource(new PodBuilder(pod).editOrNewStatus().withPhase("Pending").endStatus().build()).replace();
        assertThat(adapter.inspect("ai-investment", resource.connectorId())).isEqualTo(RuntimeTechnicalStatus.STARTING);
        server.getClient().pods().inNamespace("ai-investment").resource(new PodBuilder(pod).editOrNewStatus().withPhase("Running").withConditions(new PodConditionBuilder().withType("Ready").withStatus("False").build()).endStatus().build()).replace();
        assertThat(adapter.inspect("ai-investment", resource.connectorId())).isEqualTo(RuntimeTechnicalStatus.NOT_READY);
        server.getClient().pods().inNamespace("ai-investment").resource(new PodBuilder(pod).editOrNewStatus().withPhase("Running").withConditions(new PodConditionBuilder().withType("Ready").withStatus("True").build()).endStatus().build()).replace();
        assertThat(adapter.inspect("ai-investment", resource.connectorId())).isEqualTo(RuntimeTechnicalStatus.READY);
        server.getClient().pods().inNamespace("ai-investment").resource(new PodBuilder(pod).editOrNewStatus().withPhase("Failed").endStatus().build()).replace();
        assertThat(adapter.inspect("ai-investment", resource.connectorId())).isEqualTo(RuntimeTechnicalStatus.FAILED);

        server.getClient().pods().inNamespace("ai-investment").resource(pod).replace();
        RuntimeStopResult stopped = adapter.stop("ai-investment", resource.connectorId());
        assertThat(stopped.podDeleted()).isTrue(); assertThat(stopped.serviceDeleted()).isTrue(); assertThat(stopped.confirmedStopped()).isTrue();
    }

    @Test
    void stopTreatsBothInitiallyAbsentResourcesAsConfirmedAndIdempotent() {
        UUID connectorId = resource(spec()).connectorId();
        Fabric8KubernetesRuntimeResourceClient adapter = new Fabric8KubernetesRuntimeResourceClient(new DelayedDeleteOperations(null, null, 0), new com.fasterxml.jackson.databind.ObjectMapper(), new MutableClock(), duration -> {});

        RuntimeStopResult stopped = adapter.stop("ai-investment", connectorId);

        assertThat(stopped.podAlreadyAbsent()).isTrue();
        assertThat(stopped.serviceAlreadyAbsent()).isTrue();
        assertThat(stopped.podDeleted()).isFalse();
        assertThat(stopped.serviceDeleted()).isFalse();
        assertThat(stopped.confirmedStopped()).isTrue();
    }

    @Test
    void stopReconcilesAsynchronousPodAndServiceDeletionUntilBothAreAbsent() {
        KubernetesRuntimeResource resource = resource(spec());
        Fabric8KubernetesRuntimeResourceClient template = new Fabric8KubernetesRuntimeResourceClient(mock(KubernetesClient.class));
        DelayedDeleteOperations operations = new DelayedDeleteOperations(template.pod(resource), template.service(resource), 2);
        MutableClock clock = new MutableClock();
        Fabric8KubernetesRuntimeResourceClient adapter = new Fabric8KubernetesRuntimeResourceClient(operations, new com.fasterxml.jackson.databind.ObjectMapper(), clock,
                duration -> clock.advance(duration));

        RuntimeStopResult stopped = adapter.stop("ai-investment", resource.connectorId());

        assertThat(stopped.podDeleted()).isTrue();
        assertThat(stopped.serviceDeleted()).isTrue();
        assertThat(stopped.confirmedStopped()).isTrue();
        assertThat(operations.podDeletes).isEqualTo(1);
        assertThat(operations.serviceDeletes).isEqualTo(1);
    }

    @Test
    void stopFailsWhenOwnedResourcesRemainPastTheBoundedReconciliationWindow() {
        KubernetesRuntimeResource resource = resource(spec());
        Fabric8KubernetesRuntimeResourceClient template = new Fabric8KubernetesRuntimeResourceClient(mock(KubernetesClient.class));
        MutableClock clock = new MutableClock();
        Fabric8KubernetesRuntimeResourceClient adapter = new Fabric8KubernetesRuntimeResourceClient(
                new DelayedDeleteOperations(template.pod(resource), template.service(resource), Integer.MAX_VALUE),
                new com.fasterxml.jackson.databind.ObjectMapper(), clock, duration -> clock.advance(Duration.ofSeconds(6)));

        RuntimeStopResult stopped = adapter.stop("ai-investment", resource.connectorId());

        assertThat(stopped.confirmedStopped()).isFalse();
    }

    @Test
    void failsClosedWhenTheDeterministicNameHasTheWrongConnectorLabel() {
        Fabric8KubernetesRuntimeResourceClient adapter = new Fabric8KubernetesRuntimeResourceClient(server.getClient());
        KubernetesRuntimeResource resource = resource(spec());
        Pod wrong = adapter.pod(resource);
        wrong.getMetadata().getLabels().put(Fabric8KubernetesRuntimeResourceClient.CONNECTOR_ID_LABEL, UUID.randomUUID().toString());
        server.getClient().pods().inNamespace("ai-investment").resource(wrong).create();
        assertThatThrownBy(() -> adapter.reconcileOrCreate("ai-investment", resource)).isInstanceOf(IllegalStateException.class);
        assertThat(adapter.stop("ai-investment", resource.connectorId()).confirmedStopped()).isFalse();
        assertThat(server.getClient().pods().inNamespace("ai-investment").withName(resource.name()).get()).isNotNull();
    }

    @Test
    void logsSafePodAndServiceReconciliationFailuresWithTheirStages() {
        Logger logger = (Logger) LoggerFactory.getLogger(Fabric8KubernetesRuntimeResourceClient.class);
        ListAppender<ILoggingEvent> appender = new ListAppender<>();
        appender.start();
        logger.addAppender(appender);
        try {
            KubernetesRuntimeResource resource = resource(spec());
            assertThatThrownBy(() -> new Fabric8KubernetesRuntimeResourceClient(new FailingOperations(true, false))
                    .reconcileOrCreate("ai-investment", resource)).isInstanceOf(KubernetesClientException.class);
            assertThatThrownBy(() -> new Fabric8KubernetesRuntimeResourceClient(new FailingOperations(false, true))
                    .reconcileOrCreate("ai-investment", resource)).isInstanceOf(KubernetesClientException.class);

            List<String> messages = appender.list.stream().map(ILoggingEvent::getFormattedMessage).toList();
            assertThat(messages).anyMatch(message -> message.contains("stage=POD_RECONCILE")
                    && message.contains("kubernetes_status=403") && message.contains("kubernetes_reason=Forbidden")
                    && message.contains("kubernetes_message=Pod is invalid")
                    && message.contains("validation_field=spec.containers[0].image")
                    && message.contains("validation_reason=FieldValueRequired")
                    && message.contains("validation_message=Required value"));
            assertThat(messages).anyMatch(message -> message.contains("stage=SERVICE_RECONCILE")
                    && message.contains("kubernetes_status=403") && message.contains("kubernetes_reason=Forbidden"));
            assertThat(messages).noneMatch(message -> message.contains("super-secret"));
        } finally {
            logger.detachAppender(appender);
        }
    }

    @Test
    void omitsBlankResourceQuantitiesButPreservesConfiguredValues() {
        Fabric8KubernetesRuntimeResourceClient adapter = new Fabric8KubernetesRuntimeResourceClient(mock(KubernetesClient.class));
        IBKRKubernetesRuntimeSpecProperties blank = spec();
        blank.getResources().getRequests().setCpu(""); blank.getResources().getRequests().setMemory(" ");
        blank.getResources().getLimits().setCpu(null); blank.getResources().getLimits().setMemory("");

        Pod blankPod = adapter.pod(resource(blank));
        assertThat(blankPod.getSpec().getContainers().get(0).getResources().getRequests()).isNullOrEmpty();
        assertThat(blankPod.getSpec().getContainers().get(0).getResources().getLimits()).isNullOrEmpty();

        Pod configuredPod = adapter.pod(resource(spec()));
        assertThat(configuredPod.getSpec().getContainers().get(0).getResources().getRequests())
                .containsEntry("cpu", new io.fabric8.kubernetes.api.model.Quantity("250m"))
                .containsEntry("memory", new io.fabric8.kubernetes.api.model.Quantity("512Mi"));
        assertThat(configuredPod.getSpec().getContainers().get(0).getResources().getLimits())
                .containsEntry("cpu", new io.fabric8.kubernetes.api.model.Quantity("1"))
                .containsEntry("memory", new io.fabric8.kubernetes.api.model.Quantity("1Gi"));
    }

    @Test
    void invalidNonblankQuantityFailsAtPodReconcileWithoutNamespaceCompensationFailure() {
        IBKRKubernetesRuntimeSpecProperties spec = spec();
        spec.getResources().getRequests().setCpu("1Mi2x");
        KubernetesRuntimeResource resource = resource(spec);
        Fabric8KubernetesRuntimeResourceClient adapter = new Fabric8KubernetesRuntimeResourceClient(new FailingOperations(false, false));

        assertThatThrownBy(() -> adapter.reconcileOrCreate("ai-investment", resource))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatCode(() -> adapter.compensate(new RuntimeAllocationResult(resource.connectorId(), resource.identity(),
                resource.endpoint(), true, true, false, false, false, false))).doesNotThrowAnyException();
    }

    private static KubernetesRuntimeResource resource(IBKRKubernetesRuntimeSpecProperties spec) {
        UUID id = UUID.fromString("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee");
        String name = KubernetesIBKRRuntimeOrchestrator.name(id);
        return new KubernetesRuntimeResource(id, name, "ai-investment", spec.getRuntimeImage(), spec.getImagePullPolicy(), spec.getServiceAccountName(), 8080, spec);
    }

    private static IBKRKubernetesRuntimeSpecProperties spec() {
        IBKRKubernetesRuntimeSpecProperties spec = new IBKRKubernetesRuntimeSpecProperties();
        spec.setRuntimeImage("acr.example.test/ibkr@sha256:abc");
        spec.setImagePullPolicy("IfNotPresent");
        spec.setServiceAccountName("ibkr-runtime");
        spec.setImagePullSecrets(List.of("registry-pull"));
        spec.getGatewayPackage().setClaimName("gateway-package");
        spec.getInternalToken().setSecretName("runtime-token");
        spec.getInternalToken().setSecretKey("AIP_INTERNAL_TOKEN");
        spec.getResources().getRequests().setCpu("250m"); spec.getResources().getRequests().setMemory("512Mi");
        spec.getResources().getLimits().setCpu("1"); spec.getResources().getLimits().setMemory("1Gi");
        spec.getInitContainer().setImage("acr.example.test/ibkr@sha256:abc");
        return spec;
    }

    private static final class FailingOperations implements Fabric8RuntimeOperations {
        private final boolean failPod;
        private final boolean failService;

        private FailingOperations(boolean failPod, boolean failService) { this.failPod = failPod; this.failService = failService; }
        @Override public Pod getPod(String namespace, String name) { return null; }
        @Override public Pod createPod(String namespace, Pod pod) {
            if (failPod) throw failure();
            return pod;
        }
        @Override public boolean deletePod(String namespace, String name) { return true; }
        @Override public Service getService(String namespace, String name) { return null; }
        @Override public Service createService(String namespace, Service service) {
            if (failService) throw failure();
            return service;
        }
        @Override public boolean deleteService(String namespace, String name) { return true; }
        private static KubernetesClientException failure() {
            return new KubernetesClientException("super-secret must never be logged", 403,
                    new StatusBuilder().withReason("Forbidden").withMessage("Pod is invalid")
                            .withDetails(new StatusDetailsBuilder().withCauses(new StatusCauseBuilder()
                                    .withField("spec.containers[0].image").withReason("FieldValueRequired")
                                    .withMessage("Required value").build()).build()).build());
        }
    }

    private static final class DelayedDeleteOperations implements Fabric8RuntimeOperations {
        private final Pod pod; private final Service service; private int visiblePolls;
        private boolean podDeleteRequested; private boolean serviceDeleteRequested;
        private int podDeletes; private int serviceDeletes;
        private DelayedDeleteOperations(Pod pod, Service service, int visiblePolls) { this.pod = pod; this.service = service; this.visiblePolls = visiblePolls; }
        @Override public Pod getPod(String namespace, String name) {
            if (pod == null) return null;
            return !podDeleteRequested || visiblePolls-- > 0 ? pod : null;
        }
        @Override public Pod createPod(String namespace, Pod value) { return value; }
        @Override public boolean deletePod(String namespace, String name) { podDeleteRequested = true; podDeletes++; return true; }
        @Override public Service getService(String namespace, String name) {
            if (service == null) return null;
            return !serviceDeleteRequested || visiblePolls-- > 0 ? service : null;
        }
        @Override public Service createService(String namespace, Service value) { return value; }
        @Override public boolean deleteService(String namespace, String name) { serviceDeleteRequested = true; serviceDeletes++; return true; }
    }

    private static final class MutableClock extends Clock {
        private Instant now = Instant.parse("2026-09-06T10:00:00Z");
        @Override public ZoneOffset getZone() { return ZoneOffset.UTC; }
        @Override public Clock withZone(java.time.ZoneId zone) { return this; }
        @Override public Instant instant() { return now; }
        void advance(Duration duration) { now = now.plus(duration); }
    }
}
