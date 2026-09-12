package com.aiinvestment.broker.api;

import com.aiinvestment.broker.application.BrokerProviderException;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

class GlobalExceptionHandlerTest {
    @Test
    void connectorCapacityConflictIsReturnedAsSanitizedPublicConflict() {
        var response = new GlobalExceptionHandler().brokerProvider(BrokerProviderException.connectorCapacityUnavailable());

        assertThat(response.getStatusCode().value()).isEqualTo(409);
        assertThat(response.getBody().code()).isEqualTo("IBKR_CONNECTOR_CAPACITY_UNAVAILABLE");
        assertThat(response.getBody().message())
                .doesNotContain("connector", "account", "session", "user")
                .contains("capacity is currently in use");
    }
}
