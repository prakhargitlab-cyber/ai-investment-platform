package com.aiinvestment.broker.runtime;

import com.aiinvestment.broker.application.BrokerProviderException;
import org.junit.jupiter.api.Test;

import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.Duration;
import java.util.ArrayDeque;
import java.util.HashMap;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class KubernetesIBKRRuntimeOrchestratorTest {
    @Test
    void allocatesDistinctDeterministicConnectorScopedResources() {
        Client client = new Client();
        KubernetesIBKRRuntimeOrchestrator orchestrator = orchestrator(client);
        UUID first = UUID.randomUUID();
        UUID second = UUID.randomUUID();

        IBKRRuntimeDescriptor one = orchestrator.allocate(first, UUID.randomUUID()).descriptor();
        IBKRRuntimeDescriptor two = orchestrator.allocate(second, UUID.randomUUID()).descriptor();

        assertThat(one.runtimeProvider()).isEqualTo("KUBERNETES");
        assertThat(one.runtimeEndpoint()).isEqualTo("http://" + KubernetesIBKRRuntimeOrchestrator.name(first) + ":80");
        assertThat(one.runtimeIdentity()).isEqualTo("service/" + KubernetesIBKRRuntimeOrchestrator.name(first));
        assertThat(two.runtimeEndpoint()).isNotEqualTo(one.runtimeEndpoint());
        assertThat(client.created).hasSize(2);
    }

    @Test
    void allocationIsIdempotentAndStopIsConnectorScoped() {
        Client client = new Client();
        KubernetesIBKRRuntimeOrchestrator orchestrator = orchestrator(client);
        UUID connectorId = UUID.randomUUID();
        orchestrator.allocate(connectorId, UUID.randomUUID());
        orchestrator.allocate(connectorId, UUID.randomUUID());
        orchestrator.stop(connectorId);

        assertThat(client.created).hasSize(1);
        assertThat(client.deleted).containsExactly(KubernetesIBKRRuntimeOrchestrator.name(connectorId));
    }

    @Test
    void allocationFailureCleansOnlyNewConnectorResource() {
        Client client = new Client();
        client.failCreate = true;
        UUID connectorId = UUID.randomUUID();

        assertThatThrownBy(() -> orchestrator(client).allocate(connectorId, UUID.randomUUID()))
                .isInstanceOf(BrokerProviderException.class);
        assertThat(client.deleted).containsExactly(KubernetesIBKRRuntimeOrchestrator.name(connectorId));
    }

    @Test void waitsThroughStartingAndNotReadyUntilReady() {
        Client client = new Client(RuntimeTechnicalStatus.STARTING, RuntimeTechnicalStatus.NOT_READY, RuntimeTechnicalStatus.READY);
        MutableClock clock = new MutableClock();
        KubernetesIBKRRuntimeOrchestrator orchestrator = orchestrator(client, clock, duration -> clock.advance(duration));
        orchestrator.allocate(UUID.randomUUID(), UUID.randomUUID());
        assertThat(client.inspectCalls).isEqualTo(3); assertThat(client.deleted).isEmpty();
    }

    @Test void configuredStartupWindowAllowsReadinessAfterThirtySecondsWithoutAReplacementSpecificTimeout() {
        java.util.List<RuntimeTechnicalStatus> startupStates = new java.util.ArrayList<>();
        for (int attempt = 0; attempt < 35; attempt++) {
            startupStates.add(attempt % 2 == 0 ? RuntimeTechnicalStatus.STARTING : RuntimeTechnicalStatus.NOT_READY);
        }
        startupStates.add(RuntimeTechnicalStatus.READY);
        Client client = new Client(startupStates.toArray(RuntimeTechnicalStatus[]::new));
        MutableClock clock = new MutableClock();
        KubernetesIBKRRuntimeOrchestrator orchestrator = orchestrator(client, clock,
                ignored -> clock.advance(Duration.ofSeconds(1)), 240);

        orchestrator.allocate(UUID.randomUUID(), UUID.randomUUID());

        assertThat(client.inspectCalls).isEqualTo(36);
        assertThat(client.deleted).isEmpty();
    }

    @Test void configuredStartupWindowBoundsFailuresAndCompensatesTheOwnedRuntime() {
        MutableClock clock = new MutableClock();
        Client client = new Client(RuntimeTechnicalStatus.NOT_READY);
        KubernetesIBKRRuntimeOrchestrator orchestrator = orchestrator(client, clock,
                ignored -> clock.advance(Duration.ofSeconds(1)), 2);
        UUID connectorId = UUID.randomUUID();

        assertThatThrownBy(() -> orchestrator.allocate(connectorId, UUID.randomUUID()))
                .isInstanceOf(BrokerProviderException.class);

        assertThat(client.inspectCalls).isGreaterThan(1);
        assertThat(client.deleted).containsExactly(KubernetesIBKRRuntimeOrchestrator.name(connectorId));
    }

    @Test void readyReturnsWithoutWaitingAndFailedTerminatesImmediately() {
        Client ready = new Client(RuntimeTechnicalStatus.READY);
        KubernetesIBKRRuntimeOrchestrator success = orchestrator(ready, new MutableClock(), duration -> { throw new AssertionError("wait"); });
        success.allocate(UUID.randomUUID(), UUID.randomUUID());
        assertThat(ready.inspectCalls).isOne();
        Client failed = new Client(RuntimeTechnicalStatus.FAILED);
        assertThatThrownBy(() -> orchestrator(failed, new MutableClock(), duration -> { throw new AssertionError("wait"); })
                .allocate(UUID.randomUUID(), UUID.randomUUID())).isInstanceOf(BrokerProviderException.class);
        assertThat(failed.inspectCalls).isOne();
    }

    @Test void absentAndUnknownAreTransientButPersistentStatesTimeoutAndInterruptRestoresFlag() {
        MutableClock clock = new MutableClock(); Client transientClient = new Client(RuntimeTechnicalStatus.ABSENT, RuntimeTechnicalStatus.UNKNOWN, RuntimeTechnicalStatus.READY);
        orchestrator(transientClient, clock, clock::advance).allocate(UUID.randomUUID(), UUID.randomUUID());
        assertThat(transientClient.inspectCalls).isEqualTo(3);
        for (RuntimeTechnicalStatus status : java.util.List.of(RuntimeTechnicalStatus.STARTING, RuntimeTechnicalStatus.NOT_READY, RuntimeTechnicalStatus.ABSENT, RuntimeTechnicalStatus.UNKNOWN)) {
            MutableClock timeoutClock = new MutableClock(); Client client = new Client(status);
            assertThatThrownBy(() -> orchestrator(client, timeoutClock, timeoutClock::advance, 1).allocate(UUID.randomUUID(), UUID.randomUUID()))
                    .isInstanceOf(BrokerProviderException.class);
            assertThat(client.inspectCalls).isGreaterThan(1);
        }
        Thread.interrupted();
        assertThatThrownBy(() -> orchestrator(new Client(RuntimeTechnicalStatus.STARTING), new MutableClock(), d -> { throw new InterruptedException(); })
                .allocate(UUID.randomUUID(), UUID.randomUUID())).isInstanceOf(BrokerProviderException.class);
        assertThat(Thread.currentThread().isInterrupted()).isTrue(); Thread.interrupted();
    }

    @Test void statusOnlyAcceptsTechnicallyReadyRuntime() {
        UUID connectorId = UUID.randomUUID();
        for (RuntimeTechnicalStatus state : java.util.List.of(RuntimeTechnicalStatus.ABSENT,
                RuntimeTechnicalStatus.STARTING, RuntimeTechnicalStatus.NOT_READY, RuntimeTechnicalStatus.FAILED,
                RuntimeTechnicalStatus.UNKNOWN)) {
            assertThat(orchestrator(new Client(state)).status(connectorId)).isEmpty();
        }
        assertThat(orchestrator(new Client(RuntimeTechnicalStatus.READY)).status(connectorId)).isPresent();
    }

    private static KubernetesIBKRRuntimeOrchestrator orchestrator(Client client) {
        return orchestrator(client, Clock.fixed(Instant.parse("2026-09-04T10:00:00Z"), ZoneOffset.UTC), d -> {}, 120);
    }
    private static KubernetesIBKRRuntimeOrchestrator orchestrator(Client client, Clock clock, RuntimeWaitStrategy wait) { return orchestrator(client, clock, wait, 120); }
    private static KubernetesIBKRRuntimeOrchestrator orchestrator(Client client, Clock clock, RuntimeWaitStrategy wait, int timeout) {
        return new KubernetesIBKRRuntimeOrchestrator(new IBKRRuntimeProperties("KUBERNETES", "ai-investment",
                "acr.example.test/ibkr-connector@sha256:abc", "IfNotPresent", "ibkr-runtime-orchestrator", 8080,
                timeout, "", "", "gateway-package", "/opt/ibkr/clientportal.gw-source", true,
                "/opt/ibkr/runtime", "", "", "ibkr-runtime", "AIP_INTERNAL_TOKEN",
                "https://127.0.0.1:5000/v1/api", false, "http://localhost:18080"),
                new IBKRKubernetesRuntimeSpecProperties(), client, clock, wait);
    }

    private static final class Client implements KubernetesRuntimeResourceClient {
        private final ArrayDeque<RuntimeTechnicalStatus> statuses = new ArrayDeque<>(); private int inspectCalls;
        private final Map<String, KubernetesRuntimeResource> resources = new HashMap<>();
        private final java.util.List<KubernetesRuntimeResource> created = new java.util.ArrayList<>();
        private final java.util.List<String> deleted = new java.util.ArrayList<>();
        private boolean failCreate;
        Client(RuntimeTechnicalStatus... statuses) { for (RuntimeTechnicalStatus status : statuses) this.statuses.add(status); }
        @Override public RuntimeResources get(String namespace, UUID id) { return new RuntimeResources(id, "", false, false, true, true, ""); }
        @Override public RuntimeAllocationResult reconcileOrCreate(String namespace, KubernetesRuntimeResource resource) {
            if (failCreate) throw new IllegalStateException("create failed");
            resources.putIfAbsent(resource.name(), resource);
            if (created.stream().noneMatch(item -> item.name().equals(resource.name()))) created.add(resource);
            return new RuntimeAllocationResult(resource.connectorId(), resource.identity(), resource.endpoint(), true, true, true, true, false, false);
        }
        @Override public RuntimeTechnicalStatus inspect(String namespace, UUID id) { inspectCalls++; return statuses.isEmpty() ? RuntimeTechnicalStatus.READY : statuses.size() == 1 ? statuses.peek() : statuses.remove(); }
        @Override public RuntimeStopResult stop(String namespace, UUID id) { String name=KubernetesIBKRRuntimeOrchestrator.name(id); resources.remove(name); deleted.add(name); return new RuntimeStopResult(id,"",true,true,false,false,true); }
        @Override public void compensate(RuntimeAllocationResult result) { deleted.add(KubernetesIBKRRuntimeOrchestrator.name(result.connectorId())); }
    }
    private static final class MutableClock extends Clock { private Instant instant=Instant.parse("2026-09-04T10:00:00Z"); void advance(Duration duration){instant=instant.plus(duration);} public ZoneOffset getZone(){return ZoneOffset.UTC;} public Clock withZone(java.time.ZoneId zone){return this;} public Instant instant(){return instant;} }
}
