package com.aiinvestment.broker.runtime;

import com.aiinvestment.broker.connector.BrokerConnectorState;
import com.aiinvestment.broker.connector.ConnectorRuntimeMode;
import com.aiinvestment.broker.application.BrokerConnectionNotFoundException;
import com.aiinvestment.broker.persistence.BrokerConnectorInstanceEntity;
import com.aiinvestment.broker.persistence.BrokerConnectorInstanceRepository;
import com.aiinvestment.shared.domain.broker.BrokerType;
import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import org.junit.jupiter.api.Test;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.Optional;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.*;

class IBKRRuntimeLifecycleServiceTest {
    @Test void ownerIsVerifiedBeforeAllocationAndSuccessPersistsRuntimeOnly() {
        Fixture f = fixture(true); when(f.orchestrator.allocate(f.id, f.user)).thenReturn(f.outcome());
        f.service.allocate(f.id, f.user);
        verify(f.orchestrator).allocate(f.id, f.user); verify(f.repository).flush();
        assertThat(f.entity.getRuntimeProvider()).isEqualTo("KUBERNETES"); assertThat(f.entity.getLastAuthenticatedAt()).isNull();
    }
    @Test void allocationTransitionsOwnedHistoricalLocalAgentConnectorToKubernetes() {
        Fixture f = fixture(true);
        f.entity = new BrokerConnectorInstanceEntity(f.id, f.user, BrokerType.IBKR, ConnectorRuntimeMode.LOCAL_AGENT,
                BrokerConnectorState.STARTING, BrokerConnectorState.AUTHENTICATION_REQUIRED, null, 1, 1, Instant.now(), Instant.now());
        when(f.repository.findByConnectorIdAndUserId(f.id, f.user)).thenReturn(Optional.of(f.entity));
        when(f.orchestrator.allocate(f.id, f.user)).thenReturn(f.outcome());

        f.service.allocate(f.id, f.user);

        assertThat(f.entity.getRuntimeMode()).isEqualTo(ConnectorRuntimeMode.KUBERNETES);
        assertThat(f.entity.getRuntimeEndpoint()).isEqualTo("http://runtime.ai-investment.svc:80");
    }
    @Test void missingOrWrongOwnerNeverCallsOrchestrator() {
        Fixture f = fixture(false);
        assertThatThrownBy(() -> f.service.allocate(f.id, f.user)).isInstanceOf(RuntimeException.class);
        verifyNoInteractions(f.orchestrator);
    }
    @Test void persistenceFailureTriggersScopedStop() {
        Fixture f = fixture(true); when(f.orchestrator.allocate(f.id, f.user)).thenReturn(f.outcome()); doThrow(new RuntimeException()).when(f.repository).flush();
        assertThatThrownBy(() -> f.service.allocate(f.id, f.user)).isInstanceOf(IBKRRuntimeLifecycleException.class);
        verify(f.orchestrator).compensate(f.outcome().allocationResult());
        verify(f.orchestrator, never()).stop(f.id);
    }
    @Test void persistenceAndCompensationFailuresAreDiagnosedWithoutReplacingThePrimaryFailure() {
        Fixture f = fixture(true); when(f.orchestrator.allocate(f.id, f.user)).thenReturn(f.outcome());
        doThrow(new IllegalStateException("super-secret")).when(f.repository).flush();
        doThrow(new IllegalArgumentException("super-secret")).when(f.orchestrator).compensate(f.outcome().allocationResult());
        Logger logger = (Logger) LoggerFactory.getLogger(IBKRRuntimeLifecycleService.class);
        ListAppender<ILoggingEvent> appender = new ListAppender<>();
        appender.start(); logger.addAppender(appender);
        try {
            assertThatThrownBy(() -> f.service.allocate(f.id, f.user))
                    .isInstanceOf(IBKRRuntimeLifecycleException.class)
                    .satisfies(error -> {
                        IBKRRuntimeLifecycleException lifecycle = (IBKRRuntimeLifecycleException) error;
                        assertThat(lifecycle.code()).isEqualTo("RUNTIME_PERSISTENCE_FAILED");
                        assertThat(lifecycle.getCause()).isInstanceOf(IllegalStateException.class);
                        assertThat(lifecycle.getCause().getSuppressed()).hasSize(1);
                    });
            var messages = appender.list.stream().map(ILoggingEvent::getFormattedMessage).toList();
            assertThat(messages).anyMatch(message -> message.contains("stage=LIFECYCLE_PERSIST"));
            assertThat(messages).anyMatch(message -> message.contains("stage=COMPENSATION"));
            assertThat(messages).noneMatch(message -> message.contains("super-secret"));
        } finally { logger.detachAppender(appender); }
    }
    @Test void stopPersistsOnlyAfterScopedTeardown() {
        Fixture f = fixture(true); f.service.stop(f.id, f.user);
        verify(f.orchestrator).stop(f.id); verify(f.repository).flush(); assertThat(f.entity.getRuntimeStoppedAt()).isNotNull();
    }
    @Test void readinessInspectionIsOwnedAndOnlyReadyRuntimeIsAccepted() {
        Fixture f = fixture(true);
        when(f.orchestrator.status(f.id)).thenReturn(Optional.of(f.descriptor()));
        assertThat(f.service.isReady(f.id, f.user)).isTrue();
        verify(f.orchestrator).status(f.id);

        Fixture missing = fixture(false);
        assertThatThrownBy(() -> missing.service.isReady(missing.id, missing.user))
                .isInstanceOf(BrokerConnectionNotFoundException.class);
        verifyNoInteractions(missing.orchestrator);
    }
    private static Fixture fixture(boolean owned) {
        UUID id=UUID.randomUUID(), user=UUID.randomUUID(); BrokerConnectorInstanceRepository repo=mock(BrokerConnectorInstanceRepository.class); IBKRRuntimeOrchestrator orch=mock(IBKRRuntimeOrchestrator.class);
        BrokerConnectorInstanceEntity e=new BrokerConnectorInstanceEntity(id,user,BrokerType.IBKR,ConnectorRuntimeMode.KUBERNETES,BrokerConnectorState.STARTING,BrokerConnectorState.AUTHENTICATION_REQUIRED,null,1,1,Instant.now(),Instant.now());
        when(repo.findByConnectorIdAndUserId(id,user)).thenReturn(owned?Optional.of(e):Optional.empty());
        return new Fixture(id,user,e,repo,orch,new IBKRRuntimeLifecycleService(repo,orch,Clock.fixed(Instant.parse("2026-09-04T10:00:00Z"), ZoneOffset.UTC)));
    }
    private static class Fixture {
        private final UUID id; private final UUID user; private BrokerConnectorInstanceEntity entity;
        private final BrokerConnectorInstanceRepository repository; private final IBKRRuntimeOrchestrator orchestrator;
        private final IBKRRuntimeLifecycleService service;
        private Fixture(UUID id, UUID user, BrokerConnectorInstanceEntity entity, BrokerConnectorInstanceRepository repository, IBKRRuntimeOrchestrator orchestrator, IBKRRuntimeLifecycleService service) { this.id=id; this.user=user; this.entity=entity; this.repository=repository; this.orchestrator=orchestrator; this.service=service; }
        IBKRRuntimeDescriptor descriptor(){ return new IBKRRuntimeDescriptor(id,"http://runtime.ai-investment.svc:80","service/runtime","KUBERNETES",Instant.now()); }
        IBKRRuntimeAllocationOutcome outcome(){ return new IBKRRuntimeAllocationOutcome(descriptor(), new RuntimeAllocationResult(id,"service/runtime","http://runtime.ai-investment.svc:80",true,true,true,true,false,false)); }
    }
}
