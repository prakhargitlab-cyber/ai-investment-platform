package com.aiinvestment.broker.application;

import com.aiinvestment.broker.persistence.BrokerConnectionEntity;
import com.aiinvestment.shared.domain.broker.BrokerConnectionState;
import com.aiinvestment.shared.domain.broker.BrokerProviderStatus;
import com.aiinvestment.shared.domain.broker.BrokerType;
import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.util.List;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;

class CanonicalBrokerConnectionResolverTest {
    private final CanonicalBrokerConnectionResolver resolver = new CanonicalBrokerConnectionResolver();

    @Test
    void olderConnectedLinkedAndSyncedConnectionBeatsNewerErrorDuplicate() {
        BrokerConnectionEntity canonical = connection(BrokerConnectionState.CONNECTED, "U1234567",
                Instant.parse("2026-08-01T00:00:00Z"), Instant.parse("2026-08-02T00:00:00Z"));
        BrokerConnectionEntity stale = connection(BrokerConnectionState.ERROR, null, null,
                Instant.parse("2026-08-29T00:00:00Z"));

        assertThat(resolver.resolve(List.of(canonical, stale), BrokerType.IBKR)).contains(canonical);
    }

    @Test
    void connectedBeatsNewerDisconnectedConnection() {
        BrokerConnectionEntity connected = connection(BrokerConnectionState.CONNECTED, null, null,
                Instant.parse("2026-08-01T00:00:00Z"));
        BrokerConnectionEntity disconnected = connection(BrokerConnectionState.DISCONNECTED, null, null,
                Instant.parse("2026-08-29T00:00:00Z"));

        assertThat(resolver.resolve(List.of(connected, disconnected), BrokerType.IBKR)).contains(connected);
    }

    @Test
    void successfulHistoricalConnectionBeatsNeverSuccessfulErrorRow() {
        BrokerConnectionEntity historical = connection(BrokerConnectionState.DISCONNECTED, "U1234567",
                Instant.parse("2026-07-01T00:00:00Z"), Instant.parse("2026-08-01T00:00:00Z"));
        BrokerConnectionEntity error = connection(BrokerConnectionState.ERROR, null, null,
                Instant.parse("2026-08-29T00:00:00Z"));

        assertThat(resolver.resolve(List.of(historical, error), BrokerType.IBKR)).contains(historical);
    }

    @Test
    void updatedAtBreaksTiesOnlyForEquivalentQuality() {
        BrokerConnectionEntity older = connection(BrokerConnectionState.ERROR, null, null,
                Instant.parse("2026-08-01T00:00:00Z"));
        BrokerConnectionEntity newer = connection(BrokerConnectionState.ERROR, null, null,
                Instant.parse("2026-08-29T00:00:00Z"));

        assertThat(resolver.resolve(List.of(older, newer), BrokerType.IBKR)).contains(newer);
    }

    private BrokerConnectionEntity connection(BrokerConnectionState state, String accountReference,
                                               Instant lastSuccessfulSyncAt, Instant updatedAt) {
        return new BrokerConnectionEntity(UUID.randomUUID(), UUID.randomUUID(), BrokerType.IBKR,
                accountReference, "Interactive Brokers", state, "USD", providerStatus(state),
                lastSuccessfulSyncAt == null ? "UNAVAILABLE" : "REAL_BROKER", "client-portal-gateway", "",
                state == BrokerConnectionState.CONNECTED ? updatedAt : null, lastSuccessfulSyncAt, updatedAt,
                state == BrokerConnectionState.ERROR ? "BROKER_UNAVAILABLE" : null,
                updatedAt.minusSeconds(60), updatedAt);
    }

    private String providerStatus(BrokerConnectionState state) {
        return switch (state) {
            case CONNECTED -> BrokerProviderStatus.CONNECTED.name();
            case AUTHENTICATION_REQUIRED -> BrokerProviderStatus.AUTHENTICATION_REQUIRED.name();
            case ERROR -> BrokerProviderStatus.ERROR.name();
            default -> BrokerProviderStatus.UNAVAILABLE.name();
        };
    }
}
