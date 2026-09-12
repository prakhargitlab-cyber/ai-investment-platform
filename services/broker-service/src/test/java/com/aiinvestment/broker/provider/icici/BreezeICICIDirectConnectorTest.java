package com.aiinvestment.broker.provider.icici;

import com.aiinvestment.broker.application.BrokerProviderException;
import com.aiinvestment.broker.config.ICICIDirectProviderProperties;
import com.aiinvestment.broker.connector.BrokerConnectorState;
import com.aiinvestment.broker.security.SecretProvider;
import org.junit.jupiter.api.Test;

import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.math.BigDecimal;
import java.util.List;
import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class BreezeICICIDirectConnectorTest {
    private static final Instant NOW = Instant.parse("2026-08-29T10:15:30Z");

    @Test
    void sessionsAreIsolatedByUserAndConnection() {
        InMemoryBreezeSessionStore store = new InMemoryBreezeSessionStore();
        BreezeICICIDirectConnector connector = connector(store, reference -> Optional.of("dummy-secret"),
                (appKey, apiSession) -> new BreezeCustomerDetails("dummy-session-token", "USER-A", "User A"));
        UUID userA = UUID.randomUUID();
        UUID userB = UUID.randomUUID();
        UUID connectionA = UUID.randomUUID();
        UUID connectionB = UUID.randomUUID();

        connector.login(userA, connectionA);
        connector.attachApiSession(userA, connectionA, "dummy-api-session");

        assertThat(connector.status(userA, connectionA).authStatus()).isEqualTo(BrokerConnectorState.CONNECTED);
        assertThat(connector.status(userB, connectionA).authStatus())
                .isEqualTo(BrokerConnectorState.AUTHENTICATION_REQUIRED);
        assertThat(connector.status(userA, connectionB).authStatus())
                .isEqualTo(BrokerConnectorState.AUTHENTICATION_REQUIRED);
    }

    @Test
    void apiSessionAttachmentRequiresLoginForExactUserAndConnection() {
        InMemoryBreezeSessionStore store = new InMemoryBreezeSessionStore();
        BreezeICICIDirectConnector connector = connector(store, reference -> Optional.of("dummy-secret"),
                (appKey, apiSession) -> new BreezeCustomerDetails("dummy-session-token", "USER-A", "User A"));
        UUID userA = UUID.randomUUID();
        UUID userB = UUID.randomUUID();
        UUID connectionA = UUID.randomUUID();
        UUID connectionB = UUID.randomUUID();
        connector.login(userA, connectionA);

        assertThatThrownBy(() -> connector.attachApiSession(userB, connectionA, "dummy-api-session"))
                .isInstanceOf(BrokerProviderException.class);
        assertThatThrownBy(() -> connector.attachApiSession(userA, connectionB, "dummy-api-session"))
                .isInstanceOf(BrokerProviderException.class);
        assertThat(connector.attachApiSession(userA, connectionA, "dummy-api-session").authenticated()).isTrue();
    }

    @Test
    void expiredSessionIsNeverReportedConnected() {
        UUID userId = UUID.randomUUID();
        UUID connectionId = UUID.randomUUID();
        InMemoryBreezeSessionStore store = new InMemoryBreezeSessionStore();
        store.store(new BreezeSession(userId, connectionId, "dummy-session-token", "USER-A", "User A",
                NOW.minusSeconds(3600), NOW));
        BreezeICICIDirectConnector connector = connector(store, reference -> Optional.of("dummy-secret"),
                (appKey, apiSession) -> new BreezeCustomerDetails("unused", "unused", "unused"));

        assertThat(connector.status(userId, connectionId).authStatus()).isEqualTo(BrokerConnectorState.SESSION_EXPIRED);
        assertThat(connector.status(userId, connectionId).authenticated()).isFalse();
    }

    @Test
    void loginIsScopedAndContainsOnlyOfficialLoginUrlAndEncodedAppKey() {
        InMemoryBreezeSessionStore store = new InMemoryBreezeSessionStore();
        BreezeICICIDirectConnector connector = connector(store, reference -> Optional.of("dummy-secret"),
                (appKey, apiSession) -> new BreezeCustomerDetails("unused", "unused", "unused"));
        UUID userId = UUID.randomUUID();
        UUID connectionId = UUID.randomUUID();

        ICICIDirectLogin login = connector.login(userId, connectionId);

        assertThat(login.connectionId()).isEqualTo(connectionId);
        assertThat(login.loginUrl()).isEqualTo("https://api.icicidirect.com/apiuser/login?api_key=dummy%2Bapp%3Dkey");
        assertThat(login.loginUrl()).doesNotContain("dummy-secret", "session");
        assertThat(store.consumeLoginInitiated(userId, connectionId)).isTrue();
        assertThat(store.consumeLoginInitiated(UUID.randomUUID(), connectionId)).isFalse();
    }

    @Test
    void secretIsResolvedByReferenceAndNeverAppearsInStatus() {
        AtomicReference<String> resolvedReference = new AtomicReference<>();
        SecretProvider secrets = reference -> {
            resolvedReference.set(reference);
            return Optional.of("dummy-secret");
        };
        BreezeICICIDirectConnector connector = connector(new InMemoryBreezeSessionStore(), secrets,
                (appKey, apiSession) -> new BreezeCustomerDetails("dummy-session-token", "USER-A", "User A"));
        UUID userId = UUID.randomUUID();
        UUID connectionId = UUID.randomUUID();
        connector.login(userId, connectionId);

        var status = connector.attachApiSession(userId, connectionId, "dummy-api-session");

        assertThat(resolvedReference.get()).isEqualTo("ICICI_DUMMY_SECRET_REFERENCE");
        assertThat(status.message()).doesNotContain("dummy-secret", "dummy-api-session", "dummy-session-token");
        assertThat(status.code()).doesNotContain("dummy-secret", "dummy-api-session", "dummy-session-token");
    }

    @Test
    void authenticationDoesNotAdvertiseReadOrOrderCapabilities() {
        BreezeICICIDirectConnector connector = connector(new InMemoryBreezeSessionStore(),
                reference -> Optional.of("dummy-secret"),
                (appKey, apiSession) -> new BreezeCustomerDetails("dummy-session-token", "USER-A", "User A"));

        assertThat(connector.supportedCapabilities().capabilities()).containsExactlyInAnyOrder(
                com.aiinvestment.shared.domain.broker.BrokerCapability.ACCOUNTS_READ,
                com.aiinvestment.shared.domain.broker.BrokerCapability.ACCOUNT_METADATA_READ,
                com.aiinvestment.shared.domain.broker.BrokerCapability.POSITIONS_READ,
                com.aiinvestment.shared.domain.broker.BrokerCapability.CASH_READ,
                com.aiinvestment.shared.domain.broker.BrokerCapability.PORTFOLIO_READ);
        assertThat(connector.supportedCapabilities().capabilities())
                .doesNotContain(com.aiinvestment.shared.domain.broker.BrokerCapability.ORDER_EXECUTION);
    }

    @Test
    void normalizesAccountDematHoldingsAndFundsWithoutFabricatingUnavailableData() {
        FixtureReadClient reads = new FixtureReadClient();
        reads.holdings = List.of(
                new BreezeDematHolding("RELIND", " in e002a01018 ", new BigDecimal("1.250")),
                new BreezeDematHolding("NOISIN", null, BigDecimal.TEN),
                new BreezeDematHolding("BADISIN", "ticker", BigDecimal.ONE),
                new BreezeDematHolding("TCS", "INE467B01029", new BigDecimal("2")));
        reads.funds = new BreezeFunds(new BigDecimal("1234.56"));
        UUID userId = UUID.randomUUID();
        UUID connectionId = UUID.randomUUID();
        BreezeICICIDirectConnector connector = authenticatedConnector(userId, connectionId, reads);

        var account = connector.fetchAccounts(userId, connectionId).get(0);
        var positions = connector.fetchPositions(userId, connectionId, account);
        var cash = connector.fetchCashBalances(userId, connectionId, account).get(0);

        assertThat(account.baseCurrency()).isNull();
        assertThat(account.externalAccountReference()).startsWith("***").doesNotContain("USER-A");
        assertThat(positions).hasSize(2);
        assertThat(positions).extracting(position -> position.instrument().providerInstrumentId())
                .containsExactly("ISIN:INE002A01018", "ISIN:INE467B01029");
        assertThat(positions.get(0).quantity()).isEqualByComparingTo("1.250");
        assertThat(positions.get(0).averageCost()).isNull();
        assertThat(positions.get(0).currentPrice()).isNull();
        assertThat(positions.get(0).instrument().exchange()).isNull();
        assertThat(cash.cash().amount()).isEqualByComparingTo("1234.56");
        assertThat(cash.cash().currency()).isEqualTo("INR");
        assertThat(cash.settledCash()).isNull();
        assertThat(cash.source()).isEqualTo("BREEZE_FUNDS_TOTAL_BANK_BALANCE");
    }

    @Test
    void wrongOrExpiredScopeCannotPerformRead() {
        FixtureReadClient reads = new FixtureReadClient();
        UUID userId = UUID.randomUUID();
        UUID connectionId = UUID.randomUUID();
        BreezeICICIDirectConnector connector = authenticatedConnector(userId, connectionId, reads);

        assertThatThrownBy(() -> connector.fetchAccounts(UUID.randomUUID(), connectionId))
                .isInstanceOf(BrokerProviderException.class);
        assertThatThrownBy(() -> connector.fetchAccounts(userId, UUID.randomUUID()))
                .isInstanceOf(BrokerProviderException.class);
        assertThat(reads.calls).isZero();
    }

    @Test
    void expiredSessionPreventsHoldingsHttpCall() {
        UUID userId = UUID.randomUUID();
        UUID connectionId = UUID.randomUUID();
        InMemoryBreezeSessionStore store = new InMemoryBreezeSessionStore();
        store.store(new BreezeSession(userId, connectionId, "dummy-session-token", "USER-A", "User A",
                NOW.minusSeconds(3600), NOW));
        FixtureReadClient reads = new FixtureReadClient();
        SecretProvider secrets = reference -> Optional.of("dummy-secret");
        BreezeICICIDirectConnector connector = connector(store, secrets, (appKey, apiSession) -> null, reads);
        var account = new com.aiinvestment.shared.domain.broker.BrokerAccount(
                "irrelevant", userId, com.aiinvestment.shared.domain.broker.BrokerType.ICICI_DIRECT,
                null, "User A", null, com.aiinvestment.shared.domain.broker.BrokerAccountStatus.ACTIVE);

        assertThatThrownBy(() -> connector.fetchPositions(userId, connectionId, account))
                .isInstanceOf(BrokerProviderException.class);
        assertThat(reads.calls).isZero();
    }

    private static BreezeICICIDirectConnector connector(InMemoryBreezeSessionStore store, SecretProvider secrets,
                                                         BreezeCustomerDetailsClient client) {
        return connector(store, secrets, client, new FixtureReadClient());
    }

    private static BreezeICICIDirectConnector connector(InMemoryBreezeSessionStore store, SecretProvider secrets,
                                                         BreezeCustomerDetailsClient client, BreezeReadClient reads) {
        Clock clock = Clock.fixed(NOW, ZoneOffset.UTC);
        return new BreezeICICIDirectConnector(properties(), secrets, store, client, reads,
                new BreezeRequestSigner(properties(), secrets, clock), clock);
    }

    private static BreezeICICIDirectConnector authenticatedConnector(UUID userId, UUID connectionId,
                                                                      FixtureReadClient reads) {
        InMemoryBreezeSessionStore store = new InMemoryBreezeSessionStore();
        SecretProvider secrets = reference -> Optional.of("dummy-secret");
        BreezeICICIDirectConnector connector = connector(store, secrets,
                (appKey, apiSession) -> new BreezeCustomerDetails("dummy-session-token", "USER-A", "User A"), reads);
        connector.login(userId, connectionId);
        connector.attachApiSession(userId, connectionId, "dummy-api-session");
        return connector;
    }

    private static final class FixtureReadClient implements BreezeReadClient {
        private List<BreezeDematHolding> holdings = List.of();
        private BreezeFunds funds = new BreezeFunds(BigDecimal.ZERO);
        private int calls;

        @Override
        public List<BreezeDematHolding> fetchDematHoldings(BreezeSignedHeaders headers) {
            calls++;
            return holdings;
        }

        @Override
        public BreezeFunds fetchFunds(BreezeSignedHeaders headers) {
            calls++;
            return funds;
        }
    }


    static ICICIDirectProviderProperties properties() {
        return new ICICIDirectProviderProperties(true,
                "https://api.icicidirect.com/breezeapi/api/v1/",
                "https://api.icicidirect.com/apiuser/login",
                "dummy+app=key", "https://application.example.test/icici/return",
                BreezeICICIDirectConnector.AUTH_METHOD, "ICICI_DUMMY_SECRET_REFERENCE", true);
    }
}
