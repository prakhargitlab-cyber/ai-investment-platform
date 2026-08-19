package com.aiinvestment.broker.security;

import com.aiinvestment.shared.domain.broker.BrokerType;
import com.aiinvestment.shared.domain.broker.auth.BrokerSession;
import com.aiinvestment.shared.domain.broker.auth.BrokerSessionState;
import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;

class BrokerTokenStoreTest {
    @Test
    void storesOnlyTokenReferencesAndSessionLifecycleState() {
        InMemoryBrokerTokenStore store = new InMemoryBrokerTokenStore();
        UUID connectionId = UUID.randomUUID();

        var reference = store.storeSessionToken(connectionId, "raw-secret-token");
        store.attachSession(new BrokerSession(UUID.randomUUID(), connectionId, BrokerType.MOCK,
                BrokerSessionState.CONNECTED, Instant.now().plusSeconds(60), true));

        assertThat(reference.keyRef()).contains(connectionId.toString());
        assertThat(reference.keyRef()).doesNotContain("raw-secret-token");
        assertThat(reference.toString()).doesNotContain("raw-secret-token");
        assertThat(store.getSession(connectionId)).hasValueSatisfying(session ->
                assertThat(session.state()).isEqualTo(BrokerSessionState.CONNECTED));

        store.revoke(connectionId);

        assertThat(store.getSessionTokenReference(connectionId)).isEmpty();
        assertThat(store.getSession(connectionId)).isEmpty();
    }
}
