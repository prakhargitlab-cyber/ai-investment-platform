package com.aiinvestment.broker.application;

import com.aiinvestment.broker.audit.BrokerOperationAuditor;
import com.aiinvestment.broker.config.IBKRProviderProperties;
import com.aiinvestment.broker.connector.*;
import com.aiinvestment.broker.persistence.*;
import com.aiinvestment.broker.partner.PartnerAuthProvider;
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
import static org.mockito.Mockito.*;

class BrokerConnectionServiceStatusTest {
    @Test
    void consumerAuthMetadataSeparatesPartnerAndAdvancedIndividualAvailability() {
        Fixture fixture = fixture();
        assertThat(fixture.service.consumerAuthMode(BrokerType.HDFC_SECURITIES, false))
                .isEqualTo("PARTNER_UNAVAILABLE");
        assertThat(fixture.service.consumerAuthMode(BrokerType.ICICI_DIRECT, true))
                .isEqualTo("INDIVIDUAL_API_CREDENTIALS");

        PartnerAuthProvider partner = mock(PartnerAuthProvider.class);
        when(partner.brokerType()).thenReturn(BrokerType.HDFC_SECURITIES);
        BrokerConnectionService withPartner = new BrokerConnectionService(fixture.connectionRepository,
                List.of(mock(BrokerProvider.class)), mock(ProviderRateLimiter.class),
                mock(ProviderCircuitBreaker.class), mock(BrokerOperationAuditor.class), fixture.connectorRepository,
                List.of(fixture.connector), ibkrProperties(), List.of(partner),
                new CanonicalBrokerConnectionResolver());
        assertThat(withPartner.consumerAuthMode(BrokerType.HDFC_SECURITIES, false)).isEqualTo("PARTNER_OAUTH");
    }

    @Test
    void ibkrStatusUsesLiveConnectorStateBeforePersistedConnectedState() {
        UUID userId = UUID.randomUUID();
        UUID connectionId = UUID.randomUUID();
        UUID connectorId = UUID.randomUUID();
        Instant lastSuccessfulSyncAt = Instant.parse("2026-08-23T12:00:00Z");
        Instant now = Instant.parse("2026-08-23T11:00:00Z");
        BrokerConnectionEntity connection = new BrokerConnectionEntity(connectionId, userId, BrokerType.IBKR,
                connectorId, "****1234", "Interactive Brokers", BrokerConnectionState.CONNECTED,
                "EUR", BrokerProviderStatus.CONNECTED.name(), "REAL_BROKER", "client-portal-gateway",
                "ACCOUNTS_READ,CASH_READ,POSITIONS_READ,PORTFOLIO_READ,ACCOUNT_METADATA_READ",
                now, lastSuccessfulSyncAt, now, null, now, now);
        BrokerConnectorInstanceEntity connectorEntity = new BrokerConnectorInstanceEntity(connectorId, userId,
                BrokerType.IBKR, ConnectorRuntimeMode.LOCAL_AGENT, BrokerConnectorState.CONNECTED,
                BrokerConnectorState.CONNECTED, "https://login.example.test", 1800, 86400, now, now);
        BrokerConnectionRepository connectionRepository = mock(BrokerConnectionRepository.class);
        BrokerConnectorInstanceRepository connectorRepository = mock(BrokerConnectorInstanceRepository.class);
        BrokerConnector connector = mock(BrokerConnector.class);
        when(connectionRepository.findByConnectionIdAndUserId(connectionId, userId)).thenReturn(Optional.of(connection));
        when(connectorRepository.findByConnectorIdAndUserId(connectorId, userId)).thenReturn(Optional.of(connectorEntity));
        when(connector.brokerType()).thenReturn(BrokerType.IBKR);
        when(connector.status(userId, connectorId)).thenReturn(new BrokerConnectorStatus(
                BrokerConnectorState.AUTHENTICATION_REQUIRED,
                BrokerConnectorState.AUTHENTICATION_REQUIRED,
                null,
                null,
                "AUTHENTICATION_REQUIRED",
                "Interactive IBKR authentication is required."));
        BrokerConnectionService service = new BrokerConnectionService(connectionRepository, List.of(),
                mock(ProviderRateLimiter.class), mock(ProviderCircuitBreaker.class), mock(BrokerOperationAuditor.class),
                connectorRepository, List.of(connector), ibkrProperties(), List.of(),
                new CanonicalBrokerConnectionResolver());

        var status = service.status(userId, connectionId);

        assertThat(status.status()).isEqualTo(BrokerConnectionState.AUTHENTICATION_REQUIRED);
        assertThat(status.providerStatus()).isEqualTo(BrokerProviderStatus.AUTHENTICATION_REQUIRED.name());
        assertThat(status.lastErrorCode()).isEqualTo("AUTHENTICATION_REQUIRED");
        assertThat(status.lastSuccessfulSyncAt()).isEqualTo(lastSuccessfulSyncAt);
        assertThat(status.dataFreshness()).isEqualTo("REAL_BROKER");
        assertThat(status.sessionReference()).isEqualTo("client-portal-gateway");
        assertThat(connectorEntity.getAuthStatus()).isEqualTo(BrokerConnectorState.AUTHENTICATION_REQUIRED);
        verify(connector, never()).start(any(), any());
        verify(connector, never()).loginUrl(any(), any());
        verify(connector, never()).fetchAccounts(any(), any());
    }

    @Test
    void ibkrSafeStatusReconcilesExistingConnectionWithoutReplacingCanonicalLinkage() {
        Fixture fixture = fixture();
        Instant successfulSync = Instant.parse("2026-08-20T10:00:00Z");
        fixture.connection.setLastSuccessfulSyncAt(successfulSync);
        var authenticated = new BrokerConnectorStatus(BrokerConnectorState.CONNECTED, BrokerConnectorState.CONNECTED,
                Instant.now(), Instant.now(), null, "CONNECTED", "Connected");
        when(fixture.connector.status(fixture.userId, fixture.connectorId)).thenReturn(authenticated);

        var status = fixture.service.status(fixture.userId, fixture.connectionId);

        assertThat(status.connectionId()).isEqualTo(fixture.connectionId);
        assertThat(fixture.connection.getConnectorId()).isEqualTo(fixture.connectorId);
        assertThat(status.status()).isEqualTo(BrokerConnectionState.CONNECTED);
        assertThat(status.providerStatus()).isEqualTo(BrokerProviderStatus.CONNECTED.name());
        assertThat(status.lastSuccessfulSyncAt()).isEqualTo(successfulSync);
        verify(fixture.connector).status(fixture.userId, fixture.connectorId);
        verify(fixture.connector, never()).start(any(), any());
        verify(fixture.connector, never()).loginUrl(any(), any());
        verify(fixture.connector, never()).fetchAccounts(any(), any());
        verify(fixture.connectionRepository, never()).save(any());
        verify(fixture.connectorRepository, never()).save(any());
    }

    @Test
    void authenticationActionEnsuresMissingRuntimeWithSamePersistedConnectorAndReturnsRedirect() {
        Fixture fixture = fixture();
        // Fixture starts with ERROR state (line 225), so status() should return ERROR to trigger start()
        var error = new BrokerConnectorStatus(BrokerConnectorState.ERROR,
                BrokerConnectorState.ERROR, null, null,
                null, "CONNECTOR_ERROR", "Runtime error");
        var ensured = new BrokerConnectorStatus(BrokerConnectorState.AUTHENTICATION_REQUIRED,
                BrokerConnectorState.AUTHENTICATION_REQUIRED, null, null,
                "AUTHENTICATION_REQUIRED", "Authentication required");
        when(fixture.connector.status(fixture.userId, fixture.connectorId))
                .thenReturn(error)     // First probe: ERROR (triggers start())
                .thenReturn(ensured);  // After start() called
        when(fixture.connector.start(fixture.userId, fixture.connectorId)).thenReturn(ensured);
        when(fixture.connector.loginUrl(fixture.userId, fixture.connectorId))
                .thenReturn("https://localhost:5000/login?token=opaque");

        var action = fixture.service.authenticationAction(fixture.userId, fixture.connectionId);

        assertThat(action.action()).isEqualTo("REDIRECT_REQUIRED");
        assertThat(action.authenticationUrl()).isEqualTo("https://localhost:5000/login?token=opaque");
        assertThat(fixture.connection.getConnectorId()).isEqualTo(fixture.connectorId);
        assertThat(fixture.connectorEntity.getConnectorId()).isEqualTo(fixture.connectorId);
        assertThat(fixture.connectorEntity.getRuntimeStatus()).isEqualTo(BrokerConnectorState.AUTHENTICATION_REQUIRED);
        assertThat(fixture.connectorEntity.getAuthStatus()).isEqualTo(BrokerConnectorState.AUTHENTICATION_REQUIRED);
        verify(fixture.connector).start(fixture.userId, fixture.connectorId);
        verify(fixture.connector, times(2)).status(fixture.userId, fixture.connectorId);
        verify(fixture.connectionRepository, never()).save(any());
        verify(fixture.connectorRepository, never()).save(any());
    }

    @Test
    void authenticationActionDoesNotRedirectWhenEnsuredRuntimeIsAlreadyAuthenticated() {
        Fixture fixture = fixture();
        var authenticated = new BrokerConnectorStatus(BrokerConnectorState.CONNECTED, BrokerConnectorState.CONNECTED,
                Instant.now(), Instant.now(), "https://unused.example.test", "CONNECTED", "Connected");
        when(fixture.connector.start(fixture.userId, fixture.connectorId)).thenReturn(authenticated);
        when(fixture.connector.status(fixture.userId, fixture.connectorId)).thenReturn(authenticated);

        var action = fixture.service.authenticationAction(fixture.userId, fixture.connectionId);

        assertThat(action.action()).isEqualTo("NONE");
        assertThat(action.authenticationUrl()).isNull();
        assertThat(fixture.connection.getStatus()).isEqualTo(BrokerConnectionState.CONNECTED);
        verify(fixture.connector, never()).loginUrl(any(), any());
    }

    @Test
    void persistedConnectedWithSuccessfulSyncStillRedirectsWhenLiveRuntimeIsUnauthenticated() {
        Fixture fixture = fixture();
        Instant successfulSync = Instant.parse("2026-08-20T10:00:00Z");
        fixture.connection.setStatus(BrokerConnectionState.CONNECTED);
        fixture.connection.setProviderStatus(BrokerProviderStatus.CONNECTED.name());
        fixture.connection.setLastSuccessfulSyncAt(successfulSync);
        // Live connector is unauthenticated but NOT in ERROR/NOT_CONFIGURED state
        var live = new BrokerConnectorStatus(BrokerConnectorState.AUTHENTICATION_REQUIRED,
                BrokerConnectorState.AUTHENTICATION_REQUIRED, Instant.now(), null,
                "https://localhost:5000/login?token=opaque", "AUTHENTICATION_REQUIRED", "Authentication required");
        when(fixture.connector.status(fixture.userId, fixture.connectorId))
                .thenReturn(live);  // Live status shows AUTHENTICATION_REQUIRED
        when(fixture.connector.loginUrl(fixture.userId, fixture.connectorId))
                .thenReturn("https://localhost:5000/login?token=opaque");

        var action = fixture.service.authenticationAction(fixture.userId, fixture.connectionId);

        assertThat(action.status()).isEqualTo(BrokerConnectionState.AUTHENTICATION_REQUIRED.name());
        assertThat(action.action()).isEqualTo("REDIRECT_REQUIRED");
        assertThat(action.authenticationUrl()).isEqualTo("https://localhost:5000/login?token=opaque");
        assertThat(fixture.connection.getConnectionId()).isEqualTo(fixture.connectionId);
        assertThat(fixture.connection.getConnectorId()).isEqualTo(fixture.connectorId);
        assertThat(fixture.connection.getLastSuccessfulSyncAt()).isEqualTo(successfulSync);
        // With persisted CONNECTED but live AUTHENTICATION_REQUIRED, start() should NOT be called
        // because status is not ERROR or NOT_CONFIGURED
        verify(fixture.connector, never()).start(any(), any());
        verify(fixture.connectionRepository, never()).save(any());
    }

    @Test
    void authenticationActionReturnsSafeNormalizedFailureWhenRuntimeEnsureFails() {
        Fixture fixture = fixture();
        when(fixture.connector.start(fixture.userId, fixture.connectorId))
                .thenThrow(BrokerProviderException.unavailable());

        var action = fixture.service.authenticationAction(fixture.userId, fixture.connectionId);

        assertThat(action.action()).isEqualTo("UNAVAILABLE");
        assertThat(action.authenticationUrl()).isNull();
        assertThat(action.message()).contains("saved portfolio is unchanged");
        assertThat(fixture.connection.getStatus()).isEqualTo(BrokerConnectionState.ERROR);
        assertThat(fixture.connection.getConnectorId()).isEqualTo(fixture.connectorId);
        verify(fixture.connectionRepository, never()).save(any());
        verify(fixture.connectorRepository, never()).save(any());
    }

    @Test
    void authenticationActionCannotEnsureAnotherUsersRuntime() {
        Fixture fixture = fixture();
        UUID otherUserId = UUID.randomUUID();
        when(fixture.connectionRepository.findByConnectionIdAndUserId(fixture.connectionId, otherUserId))
                .thenReturn(Optional.empty());

        org.assertj.core.api.Assertions.assertThatThrownBy(
                        () -> fixture.service.authenticationAction(otherUserId, fixture.connectionId))
                .isInstanceOf(BrokerConnectionNotFoundException.class);

        verify(fixture.connector, never()).start(any(), any());
    }

    private static Fixture fixture() {
        UUID userId = UUID.randomUUID();
        UUID connectionId = UUID.randomUUID();
        UUID connectorId = UUID.randomUUID();
        Instant now = Instant.parse("2026-08-23T11:00:00Z");
        BrokerConnectionEntity connection = new BrokerConnectionEntity(connectionId, userId, BrokerType.IBKR,
                connectorId, "****1234", "Interactive Brokers", BrokerConnectionState.AUTHENTICATION_REQUIRED,
                "EUR", BrokerProviderStatus.AUTHENTICATION_REQUIRED.name(), "REAL_BROKER", "client-portal-gateway",
                "ACCOUNTS_READ,CASH_READ,POSITIONS_READ,PORTFOLIO_READ,ACCOUNT_METADATA_READ",
                now, null, now, "AUTHENTICATION_REQUIRED", now, now);
        BrokerConnectorInstanceEntity connectorEntity = new BrokerConnectorInstanceEntity(connectorId, userId,
                BrokerType.IBKR, ConnectorRuntimeMode.LOCAL_AGENT, BrokerConnectorState.ERROR,
                BrokerConnectorState.SESSION_EXPIRED, null, 1800, 86400, now, now);
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

    private record Fixture(UUID userId, UUID connectionId, UUID connectorId,
                           BrokerConnectionEntity connection, BrokerConnectorInstanceEntity connectorEntity,
                           BrokerConnectionRepository connectionRepository,
                           BrokerConnectorInstanceRepository connectorRepository,
                           BrokerConnector connector, BrokerConnectionService service) {}

    private static IBKRProviderProperties ibkrProperties() {
        return new IBKRProviderProperties(true, "", "", "", "", "", "", "client-portal-gateway",
                true, false, false, "https://ibkr-connector.test", "LOCAL_AGENT", "",
                "internal-token", 1800, 86400, "", "");
    }
}
