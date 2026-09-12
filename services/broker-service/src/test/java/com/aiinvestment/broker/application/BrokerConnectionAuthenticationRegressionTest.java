package com.aiinvestment.broker.application;

import com.aiinvestment.broker.audit.BrokerOperationAuditor;
import com.aiinvestment.broker.config.IBKRProviderProperties;
import com.aiinvestment.broker.connector.*;
import com.aiinvestment.broker.persistence.*;
import com.aiinvestment.broker.resilience.ProviderCircuitBreaker;
import com.aiinvestment.broker.resilience.ProviderRateLimiter;
import com.aiinvestment.shared.domain.broker.BrokerConnectionState;
import com.aiinvestment.shared.domain.broker.BrokerProviderStatus;
import com.aiinvestment.shared.domain.broker.BrokerType;
import com.aiinvestment.shared.domain.broker.BrokerProvider;
import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.util.List;
import java.util.Optional;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

/**
 * REGRESSION TESTS for IBKR second-authentication lifecycle fix.
 * 
 * These tests verify that the fix to conditionally call connector.start()
 * does NOT break recovery from missing ephemeral runtimes while preserving
 * existing session state on re-authentication.
 * 
 * Critical scenarios:
 * 1. First authentication: missing runtime → start() → MFA → CONNECTED
 * 2. Re-authentication: connector still CONNECTED in persistence →
 *    skip start() → preserve session → MFA → same CONNECTED
 * 3. Pod restart scenario: persisted CONNECTED but live runtime missing →
 *    explicit Connect → start() called once → runtime recreated → CONNECTED
 * 4. Prevent duplicate connections/connectors/portfolios
 */
class BrokerConnectionAuthenticationRegressionTest {

    @Test
    void firstAuthenticationCreatesRuntimeAndReachesConnected() {
        Fixture fixture = fixtureWithMissingRuntime();
        
        var unauthenticated = new BrokerConnectorStatus(BrokerConnectorState.NOT_CONFIGURED,
                BrokerConnectorState.NOT_CONFIGURED, null, null,
                "https://localhost:5000/login?token=firstAuth", "LOGIN_URL_AVAILABLE", "Ready for login");
        var authenticating = new BrokerConnectorStatus(BrokerConnectorState.AUTHENTICATING,
                BrokerConnectorState.AUTHENTICATING, null, null,
                "https://localhost:5000/login?token=firstAuth", "WAITING_FOR_MFA", "Awaiting user MFA");
        var connected = new BrokerConnectorStatus(BrokerConnectorState.CONNECTED,
                BrokerConnectorState.CONNECTED, Instant.now(), Instant.now(),
                null, "CONNECTED", "Connected");

        when(fixture.connector.status(fixture.userId, fixture.connectorId))
                .thenReturn(unauthenticated)  // First probe: NOT_CONFIGURED
                .thenReturn(authenticating)   // After start() called: AUTHENTICATING
                .thenReturn(connected);       // Later polling: CONNECTED
        when(fixture.connector.start(fixture.userId, fixture.connectorId))
                .thenReturn(authenticating);  // start() returns intermediate state
        when(fixture.connector.loginUrl(fixture.userId, fixture.connectorId))
                .thenReturn("https://localhost:5000/login?token=firstAuth");

        // First call: should invoke start() because status is NOT_CONFIGURED
        var action1 = fixture.service.authenticationAction(fixture.userId, fixture.connectionId);
        assertThat(action1.action()).isEqualTo("REDIRECT_REQUIRED");
        assertThat(action1.authenticationUrl()).startsWith("https://localhost:5000/login");
        
        // Verify start() was called exactly once on first auth
        verify(fixture.connector, times(1)).start(fixture.userId, fixture.connectorId);
        verify(fixture.connector, times(1)).loginUrl(fixture.userId, fixture.connectorId);

        // Later polling: status probe shows CONNECTED, no further start() calls
        when(fixture.connector.status(fixture.userId, fixture.connectorId)).thenReturn(connected);
        var status = fixture.service.status(fixture.userId, fixture.connectionId);
        assertThat(status.status()).isEqualTo(BrokerConnectionState.CONNECTED);
        
        // Verify start() was NOT called again during status probe
        verify(fixture.connector, times(1)).start(fixture.userId, fixture.connectorId);
    }

    @Test
    void secondAuthenticationWhenLiveRuntimeBecomesUnauthenticated() {
        // Scenario: User clicks Re-authenticate. Persisted shows CONNECTED (from previous successful auth).
        // But live runtime has become AUTHENTICATION_REQUIRED (session expired).
        // Expected: start() is NOT called (because live status is not ERROR/NOT_CONFIGURED),
        // but redirect is returned based on live status.
        
        Fixture fixture = fixtureWithConnectedConnector();
        
        var liveUnauthenticated = new BrokerConnectorStatus(BrokerConnectorState.AUTHENTICATION_REQUIRED,
                BrokerConnectorState.AUTHENTICATION_REQUIRED, null, null,
                "https://localhost:5000/login?token=secondAuth", "AUTHENTICATION_REQUIRED", "Session expired");
        var connectedAgain = new BrokerConnectorStatus(BrokerConnectorState.CONNECTED,
                BrokerConnectorState.CONNECTED, Instant.now(), Instant.now(),
                null, "CONNECTED", "Re-authenticated");

        // On second auth: status() shows AUTHENTICATION_REQUIRED (live state)
        when(fixture.connector.status(fixture.userId, fixture.connectorId))
                .thenReturn(liveUnauthenticated);
        when(fixture.connector.loginUrl(fixture.userId, fixture.connectorId))
                .thenReturn("https://localhost:5000/login?token=secondAuth");

        // Invoke authenticationAction on second re-auth
        var action = fixture.service.authenticationAction(fixture.userId, fixture.connectionId);

        assertThat(action.action()).isEqualTo("REDIRECT_REQUIRED");
        assertThat(action.authenticationUrl()).contains("token=secondAuth");
        
        // CRITICAL: start() should NOT have been called because status was AUTHENTICATION_REQUIRED (not ERROR/NOT_CONFIGURED)
        verify(fixture.connector, never()).start(any(), any());
        
        // Verify same connector/connection remain unchanged
        assertThat(fixture.connection.getConnectorId()).isEqualTo(fixture.connectorId);
        assertThat(fixture.connectorEntity.getConnectorId()).isEqualTo(fixture.connectorId);

        // Later polling shows CONNECTED
        when(fixture.connector.status(fixture.userId, fixture.connectorId)).thenReturn(connectedAgain);
        var status = fixture.service.status(fixture.userId, fixture.connectionId);
        assertThat(status.status()).isEqualTo(BrokerConnectionState.CONNECTED);
    }

    @Test
    void podRestartRecreatesMissingRuntimeWithSamePersistedConnectorId() {
        // Scenario: Connector persisted as CONNECTED. Pod restarts, ephemeral IBKR runtime dies.
        // Live runtime is NOT_CONFIGURED (missing). User clicks Connect to restore.
        // Runtime should be recreated with SAME connector ID.
        
        Fixture fixture = fixtureWithConnectedConnector();
        
        var missingRuntime = new BrokerConnectorStatus(BrokerConnectorState.NOT_CONFIGURED,
                BrokerConnectorState.NOT_CONFIGURED, null, null,
                "https://localhost:5000/login?token=podRestart", "LOGIN_READY", "Runtime missing");
        var recovered = new BrokerConnectorStatus(BrokerConnectorState.AUTHENTICATION_REQUIRED,
                BrokerConnectorState.AUTHENTICATION_REQUIRED, null, null,
                "https://localhost:5000/login?token=podRestart", "LOGIN_READY", "Runtime recovered");

        // After pod restart: live status is NOT_CONFIGURED (runtime missing)
        when(fixture.connector.status(fixture.userId, fixture.connectorId))
                .thenReturn(missingRuntime)   // First probe: runtime is missing
                .thenReturn(recovered);       // After start() recreates it: recovered state
        when(fixture.connector.start(fixture.userId, fixture.connectorId))
                .thenReturn(recovered);
        when(fixture.connector.loginUrl(fixture.userId, fixture.connectorId))
                .thenReturn("https://localhost:5000/login?token=error");
        when(fixture.connector.loginUrl(fixture.userId, fixture.connectorId))
                .thenReturn("https://localhost:5000/login?token=podRestart");

        var action = fixture.service.authenticationAction(fixture.userId, fixture.connectionId);

        // After recovery, should show AUTHENTICATION_REQUIRED (login needed)
        assertThat(action.status()).isEqualTo(BrokerConnectionState.AUTHENTICATION_REQUIRED.name());
        assertThat(action.action()).isEqualTo("REDIRECT_REQUIRED");
        
        // start() MUST be called because status is NOT_CONFIGURED
        verify(fixture.connector).start(fixture.userId, fixture.connectorId);
        
        // Critical: Same connector ID persisted and used for recovery
        assertThat(fixture.connectorEntity.getConnectorId()).isEqualTo(fixture.connectorId);

        // Verify no duplicate connector created in database
        verify(fixture.connectorRepository, never()).save(any());
    }

    @Test
    void connectorErrorStateInvokesStartToRecover() {
        Fixture fixture = fixtureWithConnectedConnector();
        
        var errorState = new BrokerConnectorStatus(BrokerConnectorState.ERROR,
                BrokerConnectorState.ERROR, null, null,
                null, "CONNECTOR_ERROR", "Connector error");
        var recovered = new BrokerConnectorStatus(BrokerConnectorState.AUTHENTICATION_REQUIRED,
                BrokerConnectorState.AUTHENTICATION_REQUIRED, null, null,
                "https://localhost:5000/login?token=error", "LOGIN_READY", "Recovered");

        when(fixture.connector.status(fixture.userId, fixture.connectorId))
                .thenReturn(errorState)   // First probe: ERROR
                .thenReturn(recovered);   // After start(): recovered to AUTHENTICATION_REQUIRED
        when(fixture.connector.start(fixture.userId, fixture.connectorId))
                .thenReturn(recovered);
        when(fixture.connector.loginUrl(fixture.userId, fixture.connectorId))
                .thenReturn("https://localhost:5000/login?token=error");

        var action = fixture.service.authenticationAction(fixture.userId, fixture.connectionId);

        assertThat(action.action()).isEqualTo("REDIRECT_REQUIRED");
        
        // start() MUST be called because status is ERROR
        verify(fixture.connector).start(fixture.userId, fixture.connectorId);
    }

    @Test
    void noDuplicateConnectionCreatedOnSecondAuth() {
        Fixture fixture = fixtureWithConnectedConnector();
        UUID originalConnectionId = fixture.connectionId;
        UUID originalConnectorId = fixture.connectorId;
        
        var stillConnected = new BrokerConnectorStatus(BrokerConnectorState.CONNECTED,
                BrokerConnectorState.CONNECTED, Instant.now(), Instant.now(),
                null, "CONNECTED", "Connected");
        when(fixture.connector.status(fixture.userId, fixture.connectorId))
                .thenReturn(stillConnected);

        // First re-auth call
        fixture.service.authenticationAction(fixture.userId, fixture.connectionId);

        // Verify connection not duplicated
        assertThat(fixture.connection.getConnectionId()).isEqualTo(originalConnectionId);
        assertThat(fixture.connection.getConnectorId()).isEqualTo(originalConnectorId);
        
        verify(fixture.connectionRepository, never()).save(any());
    }

    @Test
    void noDuplicateConnectorCreatedAcrossMultipleAuthAttempts() {
        Fixture fixture = fixtureWithMissingRuntime();
        UUID originalConnectorId = fixture.connectorId;
        
        var notConfigured = new BrokerConnectorStatus(BrokerConnectorState.NOT_CONFIGURED,
                BrokerConnectorState.NOT_CONFIGURED, null, null,
                "https://localhost:5000/login?token=attempt1", "LOGIN_READY", "Ready");
        var authenticating = new BrokerConnectorStatus(BrokerConnectorState.AUTHENTICATING,
                BrokerConnectorState.AUTHENTICATING, null, null,
                "https://localhost:5000/login?token=attempt1", "WAITING_FOR_MFA", "Awaiting MFA");

        when(fixture.connector.status(fixture.userId, fixture.connectorId))
                .thenReturn(notConfigured)
                .thenReturn(authenticating);
        when(fixture.connector.start(fixture.userId, fixture.connectorId))
                .thenReturn(authenticating);

        // Multiple authentication attempts
        fixture.service.authenticationAction(fixture.userId, fixture.connectionId);
        fixture.service.authenticationAction(fixture.userId, fixture.connectionId);

        // Same connector ID must be used throughout
        assertThat(fixture.connectorEntity.getConnectorId()).isEqualTo(originalConnectorId);
        
        // No duplicate connector record created
        verify(fixture.connectorRepository, never()).save(any());
    }

    @Test
    void statusPollingIsAlwaysSideEffectFree() {
        Fixture fixture = fixtureWithConnectedConnector();
        
        var connected = new BrokerConnectorStatus(BrokerConnectorState.CONNECTED,
                BrokerConnectorState.CONNECTED, Instant.now(), Instant.now(),
                null, "CONNECTED", "Connected");
        when(fixture.connector.status(fixture.userId, fixture.connectorId))
                .thenReturn(connected);

        // Poll status 10 times (simulating 2-second frontend polling)
        for (int i = 0; i < 10; i++) {
            var status = fixture.service.status(fixture.userId, fixture.connectionId);
            assertThat(status.status()).isEqualTo(BrokerConnectionState.CONNECTED);
        }

        // Verify start() was NEVER called during polling
        verify(fixture.connector, never()).start(any(), any());
        
        // Verify loginUrl() was NEVER called during polling
        verify(fixture.connector, never()).loginUrl(any(), any());
        
        // Verify fetchAccounts() was NEVER called during polling
        verify(fixture.connector, never()).fetchAccounts(any(), any());
    }

    @Test
    void persistedConnectedButLiveUnauthenticatedRequiresReauthentication() {
        Fixture fixture = fixtureWithConnectedConnector();
        
        var unauthenticatedLive = new BrokerConnectorStatus(BrokerConnectorState.AUTHENTICATION_REQUIRED,
                BrokerConnectorState.AUTHENTICATION_REQUIRED, null, null,
                "https://localhost:5000/login?token=stale", "SESSION_EXPIRED", "Session expired");
        
        when(fixture.connector.status(fixture.userId, fixture.connectorId))
                .thenReturn(unauthenticatedLive);

        // Status check should reflect live unauthenticated state
        var status = fixture.service.status(fixture.userId, fixture.connectionId);
        
        assertThat(status.status()).isEqualTo(BrokerConnectionState.AUTHENTICATION_REQUIRED);
        assertThat(status.providerStatus()).isEqualTo(BrokerProviderStatus.AUTHENTICATION_REQUIRED.name());
        
        // No start() should be invoked during plain status check
        verify(fixture.connector, never()).start(any(), any());
    }

    @Test
    void duplicateConnectClicksDoNotTriggerMultipleStartCalls() {
        Fixture fixture = fixtureWithMissingRuntime();
        
        var notConfigured = new BrokerConnectorStatus(BrokerConnectorState.NOT_CONFIGURED,
                BrokerConnectorState.NOT_CONFIGURED, null, null,
                "https://localhost:5000/login?token=click1", "LOGIN_READY", "Ready");
        var authenticating = new BrokerConnectorStatus(BrokerConnectorState.AUTHENTICATING,
                BrokerConnectorState.AUTHENTICATING, null, null,
                "https://localhost:5000/login?token=click1", "WAITING_FOR_MFA", "Awaiting MFA");

        when(fixture.connector.status(fixture.userId, fixture.connectorId))
                .thenReturn(notConfigured)
                .thenReturn(authenticating)
                .thenReturn(authenticating);  // Subsequent clicks see same state
        when(fixture.connector.start(fixture.userId, fixture.connectorId))
                .thenReturn(authenticating);

        // User rapid-clicks Connect button
        fixture.service.authenticationAction(fixture.userId, fixture.connectionId);
        fixture.service.authenticationAction(fixture.userId, fixture.connectionId);
        fixture.service.authenticationAction(fixture.userId, fixture.connectionId);

        // start() should only be called once (on first click when NOT_CONFIGURED)
        // Subsequent clicks see AUTHENTICATING, so start() is skipped
        verify(fixture.connector, times(1)).start(fixture.userId, fixture.connectorId);
    }

    @Test
    void timeoutAndCancelPreserveConnectorState() {
        Fixture fixture = fixtureWithMissingRuntime();
        
        var notConfigured = new BrokerConnectorStatus(BrokerConnectorState.NOT_CONFIGURED,
                BrokerConnectorState.NOT_CONFIGURED, null, null,
                "https://localhost:5000/login?token=timeout", "LOGIN_READY", "Ready");
        
        when(fixture.connector.status(fixture.userId, fixture.connectorId))
                .thenReturn(notConfigured);
        when(fixture.connector.start(fixture.userId, fixture.connectorId))
                .thenReturn(notConfigured);
        when(fixture.connector.loginUrl(fixture.userId, fixture.connectorId))
                .thenReturn("https://localhost:5000/login?token=timeout");

        // First Connect attempt
        var action1 = fixture.service.authenticationAction(fixture.userId, fixture.connectionId);
        assertThat(action1.action()).isEqualTo("REDIRECT_REQUIRED");

        // User times out or cancels; connector state is preserved
        assertThat(fixture.connectorEntity.getConnectorId()).isEqualTo(fixture.connectorId);
        
        // No duplicate connector created during timeout
        verify(fixture.connectorRepository, never()).save(any());
    }

    private static Fixture fixtureWithMissingRuntime() {
        UUID userId = UUID.randomUUID();
        UUID connectionId = UUID.randomUUID();
        UUID connectorId = UUID.randomUUID();
        Instant now = Instant.parse("2026-08-30T05:00:00Z");
        
        // Connection persisted but runtime missing (pod restart scenario)
        BrokerConnectionEntity connection = new BrokerConnectionEntity(connectionId, userId, BrokerType.IBKR,
                connectorId, "****1234", "Interactive Brokers", BrokerConnectionState.AUTHENTICATION_REQUIRED,
                "EUR", BrokerProviderStatus.AUTHENTICATION_REQUIRED.name(), "REAL_BROKER", "client-portal-gateway",
                "ACCOUNTS_READ,CASH_READ,POSITIONS_READ,PORTFOLIO_READ,ACCOUNT_METADATA_READ",
                now, null, now, "AUTHENTICATION_REQUIRED", now, now);
        
        BrokerConnectorInstanceEntity connectorEntity = new BrokerConnectorInstanceEntity(connectorId, userId,
                BrokerType.IBKR, ConnectorRuntimeMode.LOCAL_AGENT, BrokerConnectorState.NOT_CONFIGURED,
                BrokerConnectorState.NOT_CONFIGURED, null, 1800, 86400, now, now);

        return createFixture(userId, connectionId, connectorId, connection, connectorEntity);
    }

    private static Fixture fixtureWithConnectedConnector() {
        UUID userId = UUID.randomUUID();
        UUID connectionId = UUID.randomUUID();
        UUID connectorId = UUID.randomUUID();
        Instant now = Instant.parse("2026-08-30T05:00:00Z");
        Instant connectedAt = Instant.parse("2026-08-29T12:00:00Z");
        
        // Connection already CONNECTED with successful sync
        BrokerConnectionEntity connection = new BrokerConnectionEntity(connectionId, userId, BrokerType.IBKR,
                connectorId, "****1234", "Interactive Brokers", BrokerConnectionState.CONNECTED,
                "EUR", BrokerProviderStatus.CONNECTED.name(), "REAL_BROKER", "client-portal-gateway",
                "ACCOUNTS_READ,CASH_READ,POSITIONS_READ,PORTFOLIO_READ,ACCOUNT_METADATA_READ",
                now, connectedAt, now, null, now, now);
        
        BrokerConnectorInstanceEntity connectorEntity = new BrokerConnectorInstanceEntity(connectorId, userId,
                BrokerType.IBKR, ConnectorRuntimeMode.LOCAL_AGENT, BrokerConnectorState.CONNECTED,
                BrokerConnectorState.CONNECTED, "https://cached.login.url", 1800, 86400, now, now);

        return createFixture(userId, connectionId, connectorId, connection, connectorEntity);
    }

    private static Fixture createFixture(UUID userId, UUID connectionId, UUID connectorId,
                                         BrokerConnectionEntity connection,
                                         BrokerConnectorInstanceEntity connectorEntity) {
        BrokerConnectionRepository connectionRepository = mock(BrokerConnectionRepository.class);
        BrokerConnectorInstanceRepository connectorRepository = mock(BrokerConnectorInstanceRepository.class);
        BrokerConnector connector = mock(BrokerConnector.class);
        BrokerProvider provider = mock(BrokerProvider.class);

        when(connectionRepository.findByConnectionIdAndUserId(connectionId, userId)).thenReturn(Optional.of(connection));
        when(connectorRepository.findByConnectorIdAndUserId(connectorId, userId)).thenReturn(Optional.of(connectorEntity));
        when(connector.brokerType()).thenReturn(BrokerType.IBKR);
        when(provider.supportedBroker()).thenReturn(BrokerType.IBKR);

        BrokerConnectionService service = new BrokerConnectionService(connectionRepository, List.of(provider),
                mock(ProviderRateLimiter.class), mock(ProviderCircuitBreaker.class), mock(BrokerOperationAuditor.class),
                connectorRepository, List.of(connector), ibkrProperties(), List.of(),
                new CanonicalBrokerConnectionResolver());

        return new Fixture(userId, connectionId, connectorId, connection, connectorEntity,
                connectionRepository, connectorRepository, connector, service);
    }

    private static IBKRProviderProperties ibkrProperties() {
        return new IBKRProviderProperties(true, "", "", "", "", "", "", "client-portal-gateway",
                true, false, false, "https://ibkr-connector.test", "LOCAL_AGENT", "",
                "internal-token", 1800, 86400, "", "");
    }

    private record Fixture(UUID userId, UUID connectionId, UUID connectorId,
                           BrokerConnectionEntity connection, BrokerConnectorInstanceEntity connectorEntity,
                           BrokerConnectionRepository connectionRepository,
                           BrokerConnectorInstanceRepository connectorRepository,
                           BrokerConnector connector, BrokerConnectionService service) {}
}
