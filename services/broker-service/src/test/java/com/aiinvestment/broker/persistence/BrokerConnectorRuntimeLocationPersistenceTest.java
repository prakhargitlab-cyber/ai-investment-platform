package com.aiinvestment.broker.persistence;

import com.aiinvestment.broker.connector.BrokerConnectorState;
import com.aiinvestment.broker.connector.ConnectorRuntimeMode;
import com.aiinvestment.shared.domain.broker.BrokerType;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;

import java.time.Instant;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;

@SpringBootTest
@ActiveProfiles("test")
class BrokerConnectorRuntimeLocationPersistenceTest {
    @Autowired
    private BrokerConnectorInstanceRepository repository;
    @Autowired
    private JdbcTemplate jdbcTemplate;

    @Test
    void persistsRuntimeLocationFieldsWhileHistoricalNullFieldsRemainCompatible() {
        UUID userId = UUID.randomUUID();
        UUID historicalConnectorId = UUID.randomUUID();
        UUID allocatedConnectorId = UUID.randomUUID();
        Instant now = Instant.parse("2026-09-04T10:00:00Z");
        jdbcTemplate.update("INSERT INTO broker.app_users (id, issuer, external_subject, display_name, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                userId, "test", "runtime-location", "Runtime location", now, now);
        try {
            BrokerConnectorInstanceEntity historical = entity(historicalConnectorId, userId, now);
            BrokerConnectorInstanceEntity allocated = entity(allocatedConnectorId, userId, now);
            allocated.markRuntimeAllocated("http://ibkr-connector-" + allocatedConnectorId,
                    "service/ibkr-connector-" + allocatedConnectorId, "KUBERNETES", now);
            allocated.markAuthenticated(now.plusSeconds(5));
            repository.saveAndFlush(historical);
            repository.saveAndFlush(allocated);

            BrokerConnectorInstanceEntity persistedHistorical = repository.findById(historicalConnectorId).orElseThrow();
            BrokerConnectorInstanceEntity persistedAllocated = repository.findById(allocatedConnectorId).orElseThrow();

            assertThat(persistedHistorical.getRuntimeEndpoint()).isNull();
            assertThat(persistedHistorical.getRuntimeIdentity()).isNull();
            assertThat(persistedAllocated.getUserId()).isEqualTo(userId);
            assertThat(persistedAllocated.getRuntimeEndpoint()).isEqualTo("http://ibkr-connector-" + allocatedConnectorId);
            assertThat(persistedAllocated.getRuntimeIdentity()).isEqualTo("service/ibkr-connector-" + allocatedConnectorId);
            assertThat(persistedAllocated.getRuntimeProvider()).isEqualTo("KUBERNETES");
            assertThat(persistedAllocated.getRuntimeCreatedAt()).isEqualTo(now);
            assertThat(persistedAllocated.getLastAuthenticatedAt()).isEqualTo(now.plusSeconds(5));
        } finally {
            repository.deleteById(historicalConnectorId);
            repository.deleteById(allocatedConnectorId);
            jdbcTemplate.update("DELETE FROM broker.app_users WHERE id = ?", userId);
        }
    }

    private static BrokerConnectorInstanceEntity entity(UUID connectorId, UUID userId, Instant now) {
        return new BrokerConnectorInstanceEntity(connectorId, userId, BrokerType.IBKR, ConnectorRuntimeMode.LOCAL_AGENT,
                BrokerConnectorState.AUTHENTICATION_REQUIRED, BrokerConnectorState.AUTHENTICATION_REQUIRED,
                null, 1800, 86400, now, now);
    }
}
