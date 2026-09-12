package com.aiinvestment.broker.provider.icici;

import com.aiinvestment.broker.config.ICICIDirectProviderProperties;
import com.aiinvestment.broker.connector.BrokerConnectorState;
import com.aiinvestment.broker.connector.BrokerConnectorStatus;
import com.aiinvestment.shared.domain.broker.*;
import org.junit.jupiter.api.Test;

import java.util.EnumSet;
import java.util.List;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.*;

class ICICIDirectBrokerProviderTest {
    @Test
    void disabledIsNotConfigured() {
        var provider = provider(properties(false, true, "base", "client", "auth"), mock(ICICIDirectConnector.class));

        assertThat(provider.connectionStatus().providerStatus()).isEqualTo(BrokerProviderStatus.NOT_CONFIGURED);
    }

    @Test
    void unverifiedDocumentationIsRequired() {
        var provider = provider(properties(true, false, "base", "client", "auth"), mock(ICICIDirectConnector.class));

        assertThat(provider.connectionStatus().providerStatus()).isEqualTo(BrokerProviderStatus.DOCUMENTATION_REQUIRED);
    }

    @Test
    void incompleteConfigurationIsNotConfigured() {
        var provider = provider(properties(true, true, "", "client", "auth"), mock(ICICIDirectConnector.class));

        assertThat(provider.connectionStatus().providerStatus()).isEqualTo(BrokerProviderStatus.NOT_CONFIGURED);
    }

    @Test
    void configuredButUnauthenticatedRequiresAuthentication() {
        ICICIDirectConnector connector = mock(ICICIDirectConnector.class);
        when(connector.status(any(), any())).thenReturn(status(BrokerConnectorState.AUTHENTICATION_REQUIRED));
        var provider = provider(configuredProperties(), connector);

        assertThat(provider.connectionStatus().providerStatus()).isEqualTo(BrokerProviderStatus.AUTHENTICATION_REQUIRED);
    }

    @Test
    void connectedConnectorIsConnected() {
        ICICIDirectConnector connector = mock(ICICIDirectConnector.class);
        when(connector.status(any(), any())).thenReturn(status(BrokerConnectorState.CONNECTED));
        var provider = provider(configuredProperties(), connector);

        assertThat(provider.connectionStatus().providerStatus()).isEqualTo(BrokerProviderStatus.CONNECTED);
    }

    @Test
    void sessionExpiryAndProviderFailureMapToNeutralStatuses() {
        ICICIDirectConnector connector = mock(ICICIDirectConnector.class);
        UUID userId = UUID.randomUUID();
        UUID connectionId = UUID.randomUUID();
        var provider = provider(configuredProperties(), connector);
        when(connector.status(userId, connectionId)).thenReturn(status(BrokerConnectorState.SESSION_EXPIRED));

        assertThat(provider.connectionStatus(userId, connectionId).state()).isEqualTo(BrokerConnectionState.SESSION_EXPIRED);
        assertThat(provider.connectionStatus(userId, connectionId).providerStatus())
                .isEqualTo(BrokerProviderStatus.AUTHENTICATION_REQUIRED);

        when(connector.status(userId, connectionId)).thenReturn(status(BrokerConnectorState.ERROR));
        assertThat(provider.connectionStatus(userId, connectionId).providerStatus()).isEqualTo(BrokerProviderStatus.UNAVAILABLE);
    }

    @Test
    void capabilitiesAreReadOnlyAndAdvertisedOnlyWhenReadyAndSupported() {
        ICICIDirectConnector connector = mock(ICICIDirectConnector.class);
        when(connector.supportedCapabilities()).thenReturn(readOnlyCapabilities());
        UUID userId = UUID.randomUUID();
        UUID connectionId = UUID.randomUUID();
        when(connector.status(userId, connectionId)).thenReturn(status(BrokerConnectorState.CONNECTED));
        var provider = provider(configuredProperties(), connector);

        assertThat(provider.connectionCapabilities().capabilities()).isEmpty();
        assertThat(provider.connectionCapabilities(userId, connectionId).capabilities()).containsExactlyInAnyOrder(
                BrokerCapability.ACCOUNTS_READ, BrokerCapability.ACCOUNT_METADATA_READ,
                BrokerCapability.POSITIONS_READ, BrokerCapability.CASH_READ, BrokerCapability.PORTFOLIO_READ);
        assertThat(provider.connectionCapabilities(userId, connectionId).capabilities())
                .doesNotContain(BrokerCapability.ORDER_EXECUTION);

        var unverified = provider(properties(true, false, "base", "client", "auth"), connector);
        assertThat(unverified.connectionCapabilities().capabilities()).isEmpty();
    }

    @Test
    void readOperationsDelegateWithUserAndConnectionScope() {
        ICICIDirectConnector connector = mock(ICICIDirectConnector.class);
        var provider = provider(configuredProperties(), connector);
        UUID userId = UUID.randomUUID();
        UUID connectionId = UUID.randomUUID();
        BrokerAccount account = account(userId, "ICICI_ACCOUNT_1");
        BrokerPosition position = mock(BrokerPosition.class);
        BrokerCashBalance cash = mock(BrokerCashBalance.class);
        when(connector.fetchAccounts(userId, connectionId)).thenReturn(List.of(account));
        when(connector.fetchPositions(userId, connectionId, account)).thenReturn(List.of(position));
        when(connector.fetchCashBalances(userId, connectionId, account)).thenReturn(List.of(cash));

        assertThat(provider.fetchAccounts(userId, connectionId)).containsExactly(account);
        assertThat(provider.fetchPositions(userId, connectionId, account)).containsExactly(position);
        assertThat(provider.fetchCashBalances(userId, connectionId, account)).containsExactly(cash);
        provider.disconnect(userId, connectionId);

        verify(connector).fetchAccounts(userId, connectionId);
        verify(connector).fetchPositions(userId, connectionId, account);
        verify(connector).fetchCashBalances(userId, connectionId, account);
        verify(connector).disconnect(userId, connectionId);
    }

    @Test
    void separateUsersAndConnectionsRemainSeparateConnectorKeys() {
        ICICIDirectConnector connector = mock(ICICIDirectConnector.class);
        var provider = provider(configuredProperties(), connector);
        UUID userOne = UUID.randomUUID();
        UUID userTwo = UUID.randomUUID();
        UUID connectionOne = UUID.randomUUID();
        UUID connectionTwo = UUID.randomUUID();
        when(connector.fetchAccounts(userOne, connectionOne)).thenReturn(List.of(account(userOne, "ONE")));
        when(connector.fetchAccounts(userTwo, connectionTwo)).thenReturn(List.of(account(userTwo, "TWO")));

        assertThat(provider.fetchAccounts(userOne, connectionOne)).extracting(BrokerAccount::brokerAccountId).containsExactly("ONE");
        assertThat(provider.fetchAccounts(userTwo, connectionTwo)).extracting(BrokerAccount::brokerAccountId).containsExactly("TWO");
        verify(connector).fetchAccounts(userOne, connectionOne);
        verify(connector).fetchAccounts(userTwo, connectionTwo);
        verifyNoMoreInteractions(connector);
    }

    private static ICICIDirectBrokerProvider provider(ICICIDirectProviderProperties properties,
                                                       ICICIDirectConnector connector) {
        return new ICICIDirectBrokerProvider(properties, connector);
    }

    private static ICICIDirectProviderProperties configuredProperties() {
        return properties(true, true, "https://api.icicidirect.com/breezeapi/api/v1/", "app-key",
                BreezeICICIDirectConnector.AUTH_METHOD);
    }

    private static ICICIDirectProviderProperties properties(boolean enabled, boolean verified, String baseUrl,
                                                              String clientId, String authMethod) {
        return new ICICIDirectProviderProperties(enabled, baseUrl,
                "https://api.icicidirect.com/apiuser/login", clientId,
                "https://application.example.test/icici/callback", authMethod, "ICICI_TEST_SECRET", verified);
    }

    private static BrokerConnectorStatus status(BrokerConnectorState state) {
        return new BrokerConnectorStatus(state, state, null, null, state.name(), state.name());
    }

    private static BrokerConnectionCapabilities readOnlyCapabilities() {
        return new BrokerConnectionCapabilities(EnumSet.of(BrokerCapability.ACCOUNTS_READ,
                BrokerCapability.ACCOUNT_METADATA_READ, BrokerCapability.POSITIONS_READ,
                BrokerCapability.CASH_READ, BrokerCapability.PORTFOLIO_READ));
    }

    private static BrokerAccount account(UUID userId, String id) {
        return new BrokerAccount(id, userId, BrokerType.ICICI_DIRECT, id, id, "INR", BrokerAccountStatus.ACTIVE);
    }
}
