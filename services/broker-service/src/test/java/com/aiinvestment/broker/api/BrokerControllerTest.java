package com.aiinvestment.broker.api;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;

import static org.hamcrest.Matchers.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class BrokerControllerTest {
    @Autowired
    private MockMvc mockMvc;

    @Test
    void listsProvidersWithCapabilitiesAndNotConfiguredStatus() throws Exception {
        mockMvc.perform(get("/api/v1/brokers"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$[*].brokerType", containsInAnyOrder("MOCK", "IBKR", "ICICI_DIRECT")))
                .andExpect(jsonPath("$[?(@.brokerType == 'MOCK')].capabilities[0]", notNullValue()))
                .andExpect(jsonPath("$[?(@.brokerType == 'IBKR')].code", contains("NOT_CONFIGURED")));
    }

    @Test
    void mockConnectionLifecycleDoesNotRequireSecrets() throws Exception {
        String body = mockMvc.perform(post("/api/v1/broker-connections/mock"))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.brokerType").value("MOCK"))
                .andExpect(jsonPath("$.status").value("CONNECTED"))
                .andExpect(jsonPath("$.capabilities", hasItem("POSITIONS_READ")))
                .andReturn().getResponse().getContentAsString();

        String connectionId = body.replaceAll(".*\"connectionId\":\"([^\"]+)\".*", "$1");

        mockMvc.perform(get("/api/v1/broker-connections"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$[*].connectionId", hasItem(connectionId)));

        mockMvc.perform(post("/api/v1/broker-connections/{id}/sync", connectionId))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.lastSuccessfulSyncAt", notNullValue()));

        mockMvc.perform(delete("/api/v1/broker-connections/{id}", connectionId))
                .andExpect(status().isNoContent());
    }

    @Test
    void unconfiguredRealProviderConnectReturnsSafeError() throws Exception {
        mockMvc.perform(post("/api/v1/broker-connections/ibkr/connect")
                        .header("X-Correlation-Id", "phase2b-test-correlation"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("BROKER_PROVIDER_UNAVAILABLE"))
                .andExpect(jsonPath("$.message", containsString("NOT_CONFIGURED")))
                .andExpect(jsonPath("$.correlationId").value("phase2b-test-correlation"))
                .andExpect(jsonPath("$.message", not(containsString("password"))))
                .andExpect(jsonPath("$.message", not(containsString("token"))));
    }
}
