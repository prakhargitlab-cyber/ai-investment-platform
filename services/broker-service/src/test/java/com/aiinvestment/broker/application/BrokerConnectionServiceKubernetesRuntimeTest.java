package com.aiinvestment.broker.application;

import com.aiinvestment.broker.audit.BrokerOperationAuditor;
import com.aiinvestment.broker.config.IBKRProviderProperties;
import com.aiinvestment.broker.connector.BrokerConnector;
import com.aiinvestment.broker.connector.BrokerConnectorState;
import com.aiinvestment.broker.connector.BrokerConnectorStatus;
import com.aiinvestment.broker.connector.ConnectorRuntimeMode;
import com.aiinvestment.broker.persistence.BrokerConnectionEntity;
import com.aiinvestment.broker.persistence.BrokerConnectionRepository;
import com.aiinvestment.broker.persistence.BrokerConnectorInstanceEntity;
import com.aiinvestment.broker.persistence.BrokerConnectorInstanceRepository;
import com.aiinvestment.broker.resilience.ProviderCircuitBreaker;
import com.aiinvestment.broker.resilience.ProviderRateLimiter;
import com.aiinvestment.broker.runtime.IBKRRuntimeLifecycleException;
import com.aiinvestment.broker.runtime.IBKRRuntimeLifecycleService;
import com.aiinvestment.broker.runtime.IBKRRuntimeProperties;
import com.aiinvestment.shared.domain.broker.BrokerProvider;
import com.aiinvestment.shared.domain.broker.BrokerConnectionCapabilities;
import com.aiinvestment.shared.domain.broker.BrokerConnectionState;
import com.aiinvestment.shared.domain.broker.BrokerProviderStatus;
import com.aiinvestment.shared.domain.broker.BrokerType;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.mockito.InOrder;

import java.util.List;
import java.util.Optional;
import java.util.UUID;
import java.time.Instant;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.*;

class BrokerConnectionServiceKubernetesRuntimeTest {
    @Test
    void kubernetesConnectAllocatesTheSavedConnectorBeforeStartingIt() {
        Fixture fixture = fixture(ConnectorRuntimeMode.KUBERNETES);

        fixture.service.initiateConnection(fixture.userId, BrokerType.IBKR);

        ArgumentCaptor<BrokerConnectorInstanceEntity> connector = ArgumentCaptor.forClass(BrokerConnectorInstanceEntity.class);
        verify(fixture.connectorRepository).save(connector.capture());
        UUID connectorId = connector.getValue().getConnectorId();
        InOrder order = inOrder(fixture.lifecycle, fixture.connector);
        order.verify(fixture.lifecycle).allocate(connectorId, fixture.userId);
        order.verify(fixture.connector).start(fixture.userId, connectorId);
        assertThat(connector.getValue().getUserId()).isEqualTo(fixture.userId);
    }

    @Test
    void allocationFailureDoesNotStartTheStaticOrDedicatedConnector() {
        Fixture fixture = fixture(ConnectorRuntimeMode.KUBERNETES);
        doThrow(new IBKRRuntimeLifecycleException("RUNTIME_ALLOCATION_FAILED"))
                .when(fixture.lifecycle).allocate(any(), eq(fixture.userId));

        assertThatThrownBy(() -> fixture.service.initiateConnection(fixture.userId, BrokerType.IBKR))
                .isInstanceOf(BrokerProviderException.class)
                .satisfies(error -> assertThat(((BrokerProviderException) error).code())
                        .isEqualTo("IBKR_RUNTIME_ALLOCATION_FAILED"));

        verify(fixture.connector, never()).start(any(), any());
    }

    @Test
    void localAgentConnectDoesNotAllocateAndPreservesStartBehavior() {
        Fixture fixture = fixture(ConnectorRuntimeMode.LOCAL_AGENT);

        fixture.service.initiateConnection(fixture.userId, BrokerType.IBKR);

        verifyNoInteractions(fixture.lifecycle);
        verify(fixture.connector).start(eq(fixture.userId), any());
    }

    @Test
    void startFailureStopsOnlyTheJustAllocatedConnectorRuntime() {
        Fixture fixture = fixture(ConnectorRuntimeMode.KUBERNETES);
        doThrow(BrokerProviderException.unavailable()).when(fixture.connector).start(eq(fixture.userId), any());

        assertThatThrownBy(() -> fixture.service.initiateConnection(fixture.userId, BrokerType.IBKR))
                .isInstanceOf(BrokerProviderException.class);

        ArgumentCaptor<BrokerConnectorInstanceEntity> connector = ArgumentCaptor.forClass(BrokerConnectorInstanceEntity.class);
        verify(fixture.connectorRepository).save(connector.capture());
        verify(fixture.lifecycle).stop(connector.getValue().getConnectorId(), fixture.userId);
    }

    @Test
    void kubernetesHistoricalConnectorAllocatesBeforeStatusAndStart() {
        ExistingFixture fixture = existingFixture(ConnectorRuntimeMode.KUBERNETES, ConnectorRuntimeMode.LOCAL_AGENT, false);

        fixture.service.authenticationAction(fixture.userId, fixture.connectionId);

        InOrder order = inOrder(fixture.lifecycle, fixture.connector);
        order.verify(fixture.lifecycle).allocate(fixture.connectorId, fixture.userId);
        order.verify(fixture.connector).status(fixture.userId, fixture.connectorId);
        order.verify(fixture.connector).start(fixture.userId, fixture.connectorId);
    }

    @Test
    void connectedHistoricalLocalAgentConnectorAllocatesBeforeAnyLegacyConnectorOperation() {
        ExistingFixture fixture = existingFixture(ConnectorRuntimeMode.KUBERNETES, ConnectorRuntimeMode.LOCAL_AGENT, false);
        fixture.connectorEntity.setRuntimeStatus(BrokerConnectorState.CONNECTED);
        fixture.connectorEntity.setAuthStatus(BrokerConnectorState.CONNECTED);

        var action = fixture.service.authenticationAction(fixture.userId, fixture.connectionId);

        InOrder order = inOrder(fixture.lifecycle, fixture.connector);
        order.verify(fixture.lifecycle).allocate(fixture.connectorId, fixture.userId);
        order.verify(fixture.connector).status(fixture.userId, fixture.connectorId);
        order.verify(fixture.connector).start(fixture.userId, fixture.connectorId);
        assertThat(action.action()).isEqualTo("REDIRECT_REQUIRED");
        assertThat(fixture.connectorEntity.getConnectorId()).isEqualTo(fixture.connectorId);
        assertThat(fixture.connectorEntity.getUserId()).isEqualTo(fixture.userId);
    }

    @Test
    void existingKubernetesAllocationFailureDoesNotStart() {
        ExistingFixture fixture = existingFixture(ConnectorRuntimeMode.KUBERNETES, ConnectorRuntimeMode.LOCAL_AGENT, false);
        doThrow(new IBKRRuntimeLifecycleException("RUNTIME_ALLOCATION_FAILED"))
                .when(fixture.lifecycle).allocate(fixture.connectorId, fixture.userId);

        var action = fixture.service.authenticationAction(fixture.userId, fixture.connectionId);

        assertThat(action.action()).isEqualTo("UNAVAILABLE");
        verify(fixture.connector, never()).start(any(), any());
    }

    @Test
    void existingValidKubernetesRuntimeIsNotReallocated() {
        ExistingFixture fixture = existingFixture(ConnectorRuntimeMode.KUBERNETES, true);
        when(fixture.lifecycle.isReady(fixture.connectorId, fixture.userId)).thenReturn(true);
        when(fixture.connector.status(fixture.userId, fixture.connectorId)).thenReturn(unauthenticated());

        fixture.service.authenticationAction(fixture.userId, fixture.connectionId);

        verify(fixture.lifecycle).isReady(fixture.connectorId, fixture.userId);
        verify(fixture.lifecycle, never()).allocate(any(), any());
        verify(fixture.connector, never()).start(any(), any());
    }

    @Test
    void explicitReauthenticationReplacesOnlyAPreviouslyAuthenticatedStaleRuntime() {
        ExistingFixture fixture = existingFixture(ConnectorRuntimeMode.KUBERNETES, true);
        fixture.connectorEntity.setLastAuthenticatedAt(Instant.parse("2026-09-06T09:00:00Z"));
        when(fixture.lifecycle.isReady(fixture.connectorId, fixture.userId)).thenReturn(true);
        when(fixture.connector.status(fixture.userId, fixture.connectorId)).thenReturn(unauthenticated());
        when(fixture.connector.start(fixture.userId, fixture.connectorId)).thenReturn(unauthenticated());

        var action = fixture.service.authenticationAction(fixture.userId, fixture.connectionId);

        assertThat(action.action()).isEqualTo("REDIRECT_REQUIRED");
        assertThat(action.authenticationUrl()).isEqualTo("https://ibkr.test/login");
        InOrder order = inOrder(fixture.lifecycle, fixture.connector);
        order.verify(fixture.lifecycle).isReady(fixture.connectorId, fixture.userId);
        order.verify(fixture.connector).status(fixture.userId, fixture.connectorId);
        order.verify(fixture.lifecycle).stop(fixture.connectorId, fixture.userId);
        order.verify(fixture.lifecycle).allocate(fixture.connectorId, fixture.userId);
        order.verify(fixture.connector).start(fixture.userId, fixture.connectorId);
        order.verify(fixture.connector).loginUrl(fixture.userId, fixture.connectorId);
        verify(fixture.lifecycle, never()).stop(argThat(id -> !fixture.connectorId.equals(id)), any());
    }

    @Test
    void backgroundAuthenticationRequiredTransitionDoesNotReplacePreviouslyConnectedRuntime() {
        ExistingFixture fixture = existingFixture(ConnectorRuntimeMode.KUBERNETES, true);
        fixture.connectorEntity.setLastAuthenticatedAt(Instant.parse("2026-09-06T09:00:00Z"));
        when(fixture.connector.status(fixture.userId, fixture.connectorId)).thenReturn(unauthenticated());

        fixture.service.status(fixture.userId, fixture.connectionId);

        verifyNoInteractions(fixture.lifecycle);
        verify(fixture.connector, never()).start(any(), any());
    }

    @Test
    void staleRuntimeStopFailureDoesNotReportAReplacementOrAllocateAnotherRuntime() {
        ExistingFixture fixture = existingFixture(ConnectorRuntimeMode.KUBERNETES, true);
        fixture.connectorEntity.setLastAuthenticatedAt(Instant.parse("2026-09-06T09:00:00Z"));
        when(fixture.lifecycle.isReady(fixture.connectorId, fixture.userId)).thenReturn(true);
        when(fixture.connector.status(fixture.userId, fixture.connectorId)).thenReturn(unauthenticated());
        doThrow(new IBKRRuntimeLifecycleException("RUNTIME_STOP_FAILED"))
                .when(fixture.lifecycle).stop(fixture.connectorId, fixture.userId);

        var action = fixture.service.authenticationAction(fixture.userId, fixture.connectionId);

        assertThat(action.action()).isEqualTo("UNAVAILABLE");
        verify(fixture.lifecycle).stop(fixture.connectorId, fixture.userId);
        verify(fixture.lifecycle, never()).allocate(fixture.connectorId, fixture.userId);
        verify(fixture.connector, never()).start(any(), any());
        verify(fixture.connector, never()).loginUrl(any(), any());
    }

    @Test
    void stalePersistedKubernetesRuntimeIsRecreatedBeforeAnyConnectorHttpOperation() {
        ExistingFixture fixture = existingFixture(ConnectorRuntimeMode.KUBERNETES, true);
        fixture.connectorEntity.setRuntimeStatus(BrokerConnectorState.ERROR);
        fixture.connectorEntity.setAuthStatus(BrokerConnectorState.CONNECTED);
        when(fixture.lifecycle.isReady(fixture.connectorId, fixture.userId)).thenReturn(false);

        fixture.service.authenticationAction(fixture.userId, fixture.connectionId);

        InOrder order = inOrder(fixture.lifecycle, fixture.connector);
        order.verify(fixture.lifecycle).isReady(fixture.connectorId, fixture.userId);
        order.verify(fixture.lifecycle).allocate(fixture.connectorId, fixture.userId);
        order.verify(fixture.connector).status(fixture.userId, fixture.connectorId);
        order.verify(fixture.connector).start(fixture.userId, fixture.connectorId);
        verify(fixture.lifecycle, never()).stop(any(), any());
        assertThat(fixture.connectorEntity.getConnectorId()).isEqualTo(fixture.connectorId);
        assertThat(fixture.connectorEntity.getUserId()).isEqualTo(fixture.userId);
    }

    @Test
    void existingLocalAgentAuthenticationDoesNotAllocate() {
        ExistingFixture fixture = existingFixture(ConnectorRuntimeMode.LOCAL_AGENT, false);

        fixture.service.authenticationAction(fixture.userId, fixture.connectionId);

        verifyNoInteractions(fixture.lifecycle);
    }

    @Test
    void ordinaryExistingConnectorStatusDoesNotAllocate() {
        ExistingFixture fixture = existingFixture(ConnectorRuntimeMode.KUBERNETES, ConnectorRuntimeMode.LOCAL_AGENT, false);

        fixture.service.status(fixture.userId, fixture.connectionId);

        verifyNoInteractions(fixture.lifecycle);
    }

    @Test
    void existingKubernetesStartFailureStopsTheSameAllocatedRuntime() {
        ExistingFixture fixture = existingFixture(ConnectorRuntimeMode.KUBERNETES, ConnectorRuntimeMode.LOCAL_AGENT, false);
        doThrow(BrokerProviderException.unavailable()).when(fixture.connector)
                .start(fixture.userId, fixture.connectorId);

        fixture.service.authenticationAction(fixture.userId, fixture.connectionId);

        verify(fixture.lifecycle).stop(fixture.connectorId, fixture.userId);
    }

    @Test
    void initialKubernetesConnectorInitializationRetriesTransientReachabilityWithinSharedStartupBudget() {
        Fixture fixture = fixture(ConnectorRuntimeMode.KUBERNETES);
        when(fixture.runtimeProperties.startupTimeoutSeconds()).thenReturn(2);
        when(fixture.connector.start(eq(fixture.userId), any()))
                .thenThrow(new BrokerProviderException("IBKR_CONNECTOR_STARTING", org.springframework.http.HttpStatus.BAD_GATEWAY, "starting"))
                .thenThrow(new BrokerProviderException("IBKR_CONNECTOR_STARTING", org.springframework.http.HttpStatus.BAD_GATEWAY, "starting"))
                .thenReturn(unauthenticated());

        fixture.service.initiateConnection(fixture.userId, BrokerType.IBKR);

        verify(fixture.connector, times(3)).start(eq(fixture.userId), any());
        verify(fixture.lifecycle, never()).stop(any(), any());
    }

    @Test
    void connectorInitializationPastSharedStartupBudgetCompensatesOwnedRuntime() {
        Fixture fixture = fixture(ConnectorRuntimeMode.KUBERNETES);
        when(fixture.runtimeProperties.startupTimeoutSeconds()).thenReturn(0);
        doThrow(new BrokerProviderException("IBKR_CONNECTOR_STARTING", org.springframework.http.HttpStatus.BAD_GATEWAY, "starting"))
                .when(fixture.connector).start(eq(fixture.userId), any());

        assertThatThrownBy(() -> fixture.service.initiateConnection(fixture.userId, BrokerType.IBKR))
                .isInstanceOf(BrokerProviderException.class);

        verify(fixture.lifecycle).stop(any(), eq(fixture.userId));
        verify(fixture.connector, times(1)).start(eq(fixture.userId), any());
    }

    @Test
    void staleRuntimeReplacementUsesTheSameConnectorStartupInitializationPolicy() {
        ExistingFixture fixture = existingFixture(ConnectorRuntimeMode.KUBERNETES, true);
        fixture.connectorEntity.setLastAuthenticatedAt(Instant.parse("2026-09-06T09:00:00Z"));
        when(fixture.runtimeProperties.startupTimeoutSeconds()).thenReturn(2);
        when(fixture.lifecycle.isReady(fixture.connectorId, fixture.userId)).thenReturn(true);
        when(fixture.connector.status(fixture.userId, fixture.connectorId)).thenReturn(unauthenticated());
        when(fixture.connector.start(fixture.userId, fixture.connectorId))
                .thenThrow(new BrokerProviderException("IBKR_CONNECTOR_STARTING", org.springframework.http.HttpStatus.BAD_GATEWAY, "starting"))
                .thenReturn(unauthenticated());

        var action = fixture.service.authenticationAction(fixture.userId, fixture.connectionId);

        assertThat(action.action()).isEqualTo("REDIRECT_REQUIRED");
        verify(fixture.connector, times(2)).start(fixture.userId, fixture.connectorId);
        verify(fixture.lifecycle).stop(fixture.connectorId, fixture.userId);
        verify(fixture.lifecycle).allocate(fixture.connectorId, fixture.userId);
    }

    @Test
    void disconnectStopsOnlyTheOwnedKubernetesRuntimeAndMarksConnectionDisconnected() {
        ExistingFixture fixture = existingFixture(ConnectorRuntimeMode.KUBERNETES, true);

        fixture.service.disconnect(fixture.userId, fixture.connectionId);

        verify(fixture.lifecycle).stop(fixture.connectorId, fixture.userId);
        verify(fixture.lifecycle, never()).stop(argThat(id -> !fixture.connectorId.equals(id)), any());
        assertThat(fixture.connectorEntity.getRuntimeStatus()).isEqualTo(BrokerConnectorState.STOPPED);
        assertThat(fixture.connectorEntity.getAuthStatus()).isEqualTo(BrokerConnectorState.STOPPED);
    }

    private static Fixture fixture(ConnectorRuntimeMode runtimeMode) {
        UUID userId = UUID.randomUUID();
        BrokerConnectionRepository connectionRepository = mock(BrokerConnectionRepository.class);
        BrokerConnectorInstanceRepository connectorRepository = mock(BrokerConnectorInstanceRepository.class);
        BrokerConnector connector = mock(BrokerConnector.class);
        BrokerProvider provider = mock(BrokerProvider.class);
        IBKRRuntimeLifecycleService lifecycle = mock(IBKRRuntimeLifecycleService.class);
        when(connectionRepository.findByUserId(userId)).thenReturn(List.of());
        when(connectorRepository.save(any(BrokerConnectorInstanceEntity.class))).thenAnswer(invocation -> invocation.getArgument(0));
        when(connectionRepository.save(any(BrokerConnectionEntity.class))).thenAnswer(invocation -> invocation.getArgument(0));
        when(connector.brokerType()).thenReturn(BrokerType.IBKR);
        when(connector.runtimeMode()).thenReturn(runtimeMode);
        when(provider.supportedBroker()).thenReturn(BrokerType.IBKR);
        when(provider.connectionCapabilities()).thenReturn(BrokerConnectionCapabilities.none());
        when(connector.start(eq(userId), any())).thenReturn(unauthenticated());
        when(connector.status(eq(userId), any())).thenReturn(unauthenticated());

        IBKRRuntimeProperties runtimeProperties = runtimeProperties(runtimeMode);
        BrokerConnectionService service = new BrokerConnectionService(connectionRepository, List.of(provider),
                mock(ProviderRateLimiter.class), mock(ProviderCircuitBreaker.class), mock(BrokerOperationAuditor.class),
                connectorRepository, List.of(connector), properties(runtimeMode), List.of(),
                new CanonicalBrokerConnectionResolver(), lifecycle, runtimeProperties);
        return new Fixture(userId, connectorRepository, connector, lifecycle, runtimeProperties, service);
    }

    private static ExistingFixture existingFixture(ConnectorRuntimeMode runtimeMode, boolean validKubernetesRuntime) {
        return existingFixture(runtimeMode, runtimeMode, validKubernetesRuntime);
    }

    private static ExistingFixture existingFixture(ConnectorRuntimeMode configuredRuntimeMode,
                                                   ConnectorRuntimeMode persistedRuntimeMode,
                                                   boolean validKubernetesRuntime) {
        UUID userId = UUID.randomUUID();
        UUID connectionId = UUID.randomUUID();
        UUID connectorId = UUID.randomUUID();
        Instant now = Instant.parse("2026-09-05T10:00:00Z");
        BrokerConnectionRepository connectionRepository = mock(BrokerConnectionRepository.class);
        BrokerConnectorInstanceRepository connectorRepository = mock(BrokerConnectorInstanceRepository.class);
        BrokerConnector connector = mock(BrokerConnector.class);
        BrokerProvider provider = mock(BrokerProvider.class);
        IBKRRuntimeLifecycleService lifecycle = mock(IBKRRuntimeLifecycleService.class);
        BrokerConnectionEntity connection = new BrokerConnectionEntity(connectionId, userId, BrokerType.IBKR,
                connectorId, null, "Interactive Brokers", BrokerConnectionState.AUTHENTICATION_REQUIRED,
                null, BrokerProviderStatus.AUTHENTICATION_REQUIRED.name(), "UNAVAILABLE", null,
                null, null, null, null, null, now, now);
        BrokerConnectorInstanceEntity connectorEntity = new BrokerConnectorInstanceEntity(connectorId, userId,
                BrokerType.IBKR, persistedRuntimeMode, BrokerConnectorState.NOT_CONFIGURED,
                BrokerConnectorState.NOT_CONFIGURED, null, 1800, 86400, now, now);
        if (validKubernetesRuntime) {
            connectorEntity.markRuntimeAllocated("http://ibkr-" + connectorId, "service/ibkr-" + connectorId,
                    "KUBERNETES", now);
        }
        when(connectionRepository.findByConnectionIdAndUserId(connectionId, userId)).thenReturn(Optional.of(connection));
        when(connectorRepository.findByConnectorIdAndUserId(connectorId, userId)).thenReturn(Optional.of(connectorEntity));
        when(connector.brokerType()).thenReturn(BrokerType.IBKR);
        when(provider.supportedBroker()).thenReturn(BrokerType.IBKR);
        when(connector.status(userId, connectorId)).thenReturn(notConfigured(), unauthenticated());
        when(connector.start(userId, connectorId)).thenReturn(unauthenticated());
        when(connector.loginUrl(userId, connectorId)).thenReturn("https://ibkr.test/login");
        IBKRRuntimeProperties runtimeProperties = runtimeProperties(configuredRuntimeMode);
        BrokerConnectionService service = new BrokerConnectionService(connectionRepository, List.of(provider),
                mock(ProviderRateLimiter.class), mock(ProviderCircuitBreaker.class), mock(BrokerOperationAuditor.class),
                connectorRepository, List.of(connector), properties(configuredRuntimeMode), List.of(),
                new CanonicalBrokerConnectionResolver(), lifecycle, runtimeProperties);
        return new ExistingFixture(userId, connectionId, connectorId, connectorEntity, connector, lifecycle,
                runtimeProperties, service);
    }

    private static BrokerConnectorStatus unauthenticated() {
        return new BrokerConnectorStatus(BrokerConnectorState.AUTHENTICATION_REQUIRED,
                BrokerConnectorState.AUTHENTICATION_REQUIRED, null, null, null,
                "AUTHENTICATION_REQUIRED", "Authentication required");
    }

    private static BrokerConnectorStatus notConfigured() {
        return new BrokerConnectorStatus(BrokerConnectorState.NOT_CONFIGURED,
                BrokerConnectorState.NOT_CONFIGURED, null, null, null,
                "NOT_CONFIGURED", "Runtime absent");
    }

    private static IBKRProviderProperties properties(ConnectorRuntimeMode runtimeMode) {
        return new IBKRProviderProperties(true, "", "", "", "", "", "", "client-portal-gateway",
                true, false, false, "http://historical-static-connector", runtimeMode.name(), "", "internal", 1800, 86400, "", "");
    }

    private static IBKRRuntimeProperties runtimeProperties(ConnectorRuntimeMode runtimeMode) {
        IBKRRuntimeProperties properties = mock(IBKRRuntimeProperties.class);
        when(properties.kubernetes()).thenReturn(runtimeMode == ConnectorRuntimeMode.KUBERNETES);
        when(properties.startupTimeoutSeconds()).thenReturn(0);
        return properties;
    }

    private record Fixture(UUID userId, BrokerConnectorInstanceRepository connectorRepository, BrokerConnector connector,
                           IBKRRuntimeLifecycleService lifecycle, IBKRRuntimeProperties runtimeProperties,
                           BrokerConnectionService service) { }
    private record ExistingFixture(UUID userId, UUID connectionId, UUID connectorId, BrokerConnectorInstanceEntity connectorEntity, BrokerConnector connector,
                                   IBKRRuntimeLifecycleService lifecycle, IBKRRuntimeProperties runtimeProperties,
                                   BrokerConnectionService service) { }
}
