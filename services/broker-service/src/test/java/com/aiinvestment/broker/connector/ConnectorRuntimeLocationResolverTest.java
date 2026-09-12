package com.aiinvestment.broker.connector;

import com.aiinvestment.broker.application.BrokerConnectionNotFoundException;
import com.aiinvestment.broker.application.BrokerProviderException;
import com.aiinvestment.broker.config.IBKRProviderProperties;
import com.aiinvestment.broker.persistence.BrokerConnectorInstanceEntity;
import com.aiinvestment.broker.persistence.BrokerConnectorInstanceRepository;
import com.aiinvestment.shared.domain.broker.BrokerType;
import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.util.Optional;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

class ConnectorRuntimeLocationResolverTest {
    private static final String STATIC_ENDPOINT = "http://ibkr-connector";

    @Test
    void usesPersistedTrustedEndpointWhenPresent() {
        UUID userId = UUID.randomUUID();
        UUID connectorId = UUID.randomUUID();
        BrokerConnectorInstanceEntity entity = entity(connectorId, userId);
        entity.markRuntimeAllocated("http://ibkr-connector-" + connectorId, "service/ibkr-connector-" + connectorId,
                "KUBERNETES", Instant.parse("2026-09-04T10:00:00Z"));
        BrokerConnectorInstanceRepository repository = mock(BrokerConnectorInstanceRepository.class);
        when(repository.findById(connectorId)).thenReturn(Optional.of(entity));

        ConnectorRuntimeLocation location = resolver(repository).resolve(userId, connectorId);

        assertThat(location.runtimeEndpoint().toString()).isEqualTo("http://ibkr-connector-" + connectorId);
        assertThat(location.runtimeIdentity()).isEqualTo("service/ibkr-connector-" + connectorId);
        assertThat(location.runtimeProvider()).isEqualTo("KUBERNETES");
    }

    @Test
    void fallsBackToLegacyStaticEndpointForHistoricalRowWithoutRuntimeMetadata() {
        UUID userId = UUID.randomUUID();
        UUID connectorId = UUID.randomUUID();
        BrokerConnectorInstanceRepository repository = mock(BrokerConnectorInstanceRepository.class);
        when(repository.findById(connectorId)).thenReturn(Optional.of(entity(connectorId, userId)));

        ConnectorRuntimeLocation location = resolver(repository).resolve(userId, connectorId);

        assertThat(location.runtimeEndpoint().toString()).isEqualTo(STATIC_ENDPOINT);
        assertThat(location.runtimeProvider()).isEqualTo("STATIC_DEV");
    }

    @Test
    void rejectsMalformedOrUnsafePersistedEndpoint() {
        UUID userId = UUID.randomUUID();
        for (String endpoint : new String[]{"ftp://connector", "http://user@connector", "http://connector?target=x", "http://connector#fragment"}) {
            UUID connectorId = UUID.randomUUID();
            BrokerConnectorInstanceEntity entity = entity(connectorId, userId);
            entity.markRuntimeAllocated(endpoint, "runtime", "KUBERNETES", Instant.now());
            BrokerConnectorInstanceRepository repository = mock(BrokerConnectorInstanceRepository.class);
            when(repository.findById(connectorId)).thenReturn(Optional.of(entity));

            assertThatThrownBy(() -> resolver(repository).resolve(userId, connectorId))
                    .isInstanceOf(BrokerProviderException.class)
                    .satisfies(exception -> assertThat(((BrokerProviderException) exception).code())
                            .isEqualTo("BROKER_UNAVAILABLE"));
        }
    }

    @Test
    void doesNotResolveAnotherUsersRuntimeMetadata() {
        UUID ownerId = UUID.randomUUID();
        UUID requestingUserId = UUID.randomUUID();
        UUID connectorId = UUID.randomUUID();
        BrokerConnectorInstanceEntity entity = entity(connectorId, ownerId);
        entity.markRuntimeAllocated("http://ibkr-connector-owner", "runtime-owner", "KUBERNETES", Instant.now());
        BrokerConnectorInstanceRepository repository = mock(BrokerConnectorInstanceRepository.class);
        when(repository.findById(connectorId)).thenReturn(Optional.of(entity));

        assertThatThrownBy(() -> resolver(repository).resolve(requestingUserId, connectorId))
                .isInstanceOf(BrokerConnectionNotFoundException.class);
    }

    private static ConnectorRuntimeLocationResolver resolver(BrokerConnectorInstanceRepository repository) {
        return new ConnectorRuntimeLocationResolver(repository, properties());
    }

    private static BrokerConnectorInstanceEntity entity(UUID connectorId, UUID userId) {
        Instant now = Instant.parse("2026-09-04T09:00:00Z");
        return new BrokerConnectorInstanceEntity(connectorId, userId, BrokerType.IBKR, ConnectorRuntimeMode.LOCAL_AGENT,
                BrokerConnectorState.AUTHENTICATION_REQUIRED, BrokerConnectorState.AUTHENTICATION_REQUIRED,
                null, 1800, 86400, now, now);
    }

    private static IBKRProviderProperties properties() {
        return new IBKRProviderProperties(true, "", "", "", "", "", "", "client-portal-gateway",
                true, false, false, STATIC_ENDPOINT, "LOCAL_AGENT", "", "internal", 1800, 86400, "", "");
    }
}
