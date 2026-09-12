package com.aiinvestment.broker.api;

import com.aiinvestment.broker.persistence.BrokerConnectionEntity;
import com.aiinvestment.broker.persistence.BrokerConnectionRepository;
import com.aiinvestment.shared.domain.broker.BrokerConnectionState;
import com.aiinvestment.shared.domain.broker.BrokerProviderStatus;
import com.aiinvestment.shared.domain.broker.BrokerType;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;

import java.time.Instant;
import java.util.UUID;

import static org.hamcrest.Matchers.*;
import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

@SpringBootTest(properties = {
        "broker.advanced-individual-credentials-enabled=true",
        "broker.icici-direct.enabled=true",
        "broker.icici-direct.base-url=https://api.icicidirect.com/breezeapi/api/v1/",
        "broker.icici-direct.login-url=https://api.icicidirect.com/apiuser/login",
        "broker.icici-direct.app-key=dummy-test-app-key",
        "broker.icici-direct.redirect-url=https://application.example.test/icici/return",
        "broker.icici-direct.auth-method=breeze-interactive-session",
        "broker.icici-direct.secret-key-reference=ICICI_DUMMY_TEST_SECRET",
        "broker.icici-direct.official-documentation-verified=true"
})
@AutoConfigureMockMvc
@ActiveProfiles("test")
class BrokerControllerTest {
    private static final String USER_A = "10000000-0000-0000-0000-000000000001";
    private static final String USER_B = "20000000-0000-0000-0000-000000000002";

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private BrokerConnectionRepository brokerConnectionRepository;

    @Autowired
    private JdbcTemplate jdbcTemplate;

    @BeforeEach
    void cleanConnections() {
        brokerConnectionRepository.deleteAll();
    }

    @Test
    void listsProvidersWithCapabilitiesAndNotConfiguredStatus() throws Exception {
        mockMvc.perform(withUserA(get("/api/v1/brokers")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$[*].brokerType", containsInAnyOrder("IBKR", "ICICI_DIRECT", "HDFC_SECURITIES")))
                .andExpect(jsonPath("$[*].brokerType", not(hasItem("MOCK"))))
                .andExpect(jsonPath("$[*].authenticationModel").doesNotExist())
                .andExpect(jsonPath("$[*].capabilities").doesNotExist())
                .andExpect(jsonPath("$[?(@.brokerType == 'HDFC_SECURITIES')].connectable", contains(true)))
                .andExpect(jsonPath("$[?(@.brokerType == 'ICICI_DIRECT')].connectable", contains(true)))
                .andExpect(jsonPath("$[?(@.brokerType == 'IBKR')].consumerAuthMode", contains("BROKER_REDIRECT")))
                .andExpect(jsonPath("$[?(@.brokerType == 'HDFC_SECURITIES')].consumerAuthMode", contains("INDIVIDUAL_API_CREDENTIALS")))
                .andExpect(jsonPath("$[?(@.brokerType == 'ICICI_DIRECT')].consumerAuthMode", contains("INDIVIDUAL_API_CREDENTIALS")))
                .andExpect(jsonPath("$[?(@.brokerType == 'HDFC_SECURITIES')].individualApiSupported", contains(true)))
                .andExpect(jsonPath("$[?(@.brokerType == 'HDFC_SECURITIES')].advancedIndividualMode", contains(true)))
                .andExpect(jsonPath("$[?(@.brokerType == 'IBKR')].code", contains("NOT_CONFIGURED")));
    }

    @Test
    void listsProvidersAfterTheAuthenticatedUserWasAlreadyProvisionedByUuid() throws Exception {
        UUID userId = UUID.fromString("30000000-0000-0000-0000-000000000003");
        jdbcTemplate.update("""
                        INSERT INTO broker.app_users (id, issuer, external_subject, email, display_name, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        """,
                userId, "legacy-dev", "legacy-user-a", "legacy@example.test", "Legacy user");

        mockMvc.perform(withUser(userId, "test", "existing-user", get("/api/v1/brokers")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$[*].brokerType", containsInAnyOrder("IBKR", "ICICI_DIRECT", "HDFC_SECURITIES")));
        mockMvc.perform(withUser(userId, "test", "existing-user", get("/api/v1/brokers")))
                .andExpect(status().isOk());

        assertThat(jdbcTemplate.queryForObject("SELECT COUNT(*) FROM broker.app_users WHERE id = ?", Integer.class, userId)).isEqualTo(1);
        assertThat(jdbcTemplate.queryForObject("SELECT issuer FROM broker.app_users WHERE id = ?", String.class, userId)).isEqualTo("test");
        assertThat(jdbcTemplate.queryForObject("SELECT external_subject FROM broker.app_users WHERE id = ?", String.class, userId)).isEqualTo("existing-user");
    }

    @Test
    void configuredIbkrProviderUsesExistingConnectionInsteadOfNotConfigured() throws Exception {
        Instant now = Instant.now();
        UUID connectionId = UUID.randomUUID();
        brokerConnectionRepository.save(new BrokerConnectionEntity(
                connectionId,
                UUID.fromString(USER_A),
                BrokerType.IBKR,
                "****0000",
                "Interactive Brokers",
                BrokerConnectionState.CONNECTED,
                "EUR",
                BrokerProviderStatus.CONNECTED.name(),
                "REAL_BROKER",
                "client-portal-gateway",
                "",
                now,
                now,
                now,
                null,
                now,
                now));

        mockMvc.perform(withUserA(get("/api/v1/brokers")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$[?(@.brokerType == 'IBKR')].providerStatus", contains("CONNECTED")))
                .andExpect(jsonPath("$[?(@.brokerType == 'IBKR')].code", not(contains("NOT_CONFIGURED"))))
                .andExpect(jsonPath("$[?(@.brokerType == 'IBKR')].readOnly", contains(true)));

        mockMvc.perform(withUserA(get("/api/v1/broker-connections")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$[0].capabilities", hasItem("POSITIONS_READ")))
                .andExpect(jsonPath("$[0].dataFreshness").value("REAL_BROKER"))
                .andExpect(jsonPath("$[0].lastSuccessfulSyncAt", notNullValue()));

        mockMvc.perform(withUserA(get("/api/v1/broker-connections/{id}/auth-status", connectionId)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.connectionId").value(connectionId.toString()))
                .andExpect(jsonPath("$.state").value("CONNECTED"))
                .andExpect(jsonPath("$.authenticated").value(true))
                .andExpect(jsonPath("$.authenticationUrl").doesNotExist());

        mockMvc.perform(withUserB(get("/api/v1/broker-connections/{id}/auth-status", connectionId)))
                .andExpect(status().isNotFound());
    }

    @Test
    void mockConnectionLifecycleDoesNotRequireSecrets() throws Exception {
        String body = mockMvc.perform(withUserA(post("/api/v1/broker-connections/mock")))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.brokerType").value("MOCK"))
                .andExpect(jsonPath("$.status").value("CONNECTED"))
                .andExpect(jsonPath("$.capabilities", hasItem("POSITIONS_READ")))
                .andReturn().getResponse().getContentAsString();

        String connectionId = body.replaceAll(".*\"connectionId\":\"([^\"]+)\".*", "$1");

        mockMvc.perform(withUserA(get("/api/v1/broker-connections")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$[*].connectionId", hasItem(connectionId)));

        mockMvc.perform(withUserA(post("/api/v1/broker-connections/{id}/sync", connectionId)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.lastSuccessfulSyncAt", notNullValue()));

        mockMvc.perform(withUserA(delete("/api/v1/broker-connections/{id}", connectionId)))
                .andExpect(status().isNoContent());
    }

    @Test
    void userCannotAccessAnotherUsersBrokerConnection() throws Exception {
        String body = mockMvc.perform(withUserA(post("/api/v1/broker-connections/mock")))
                .andExpect(status().isCreated())
                .andReturn().getResponse().getContentAsString();

        String connectionId = body.replaceAll(".*\"connectionId\":\"([^\"]+)\".*", "$1");

        mockMvc.perform(withUserB(get("/api/v1/broker-connections/{id}", connectionId)))
                .andExpect(status().isNotFound());

        mockMvc.perform(withUserB(post("/api/v1/broker-connections/{id}/sync", connectionId)))
                .andExpect(status().isNotFound());

        mockMvc.perform(withUserB(delete("/api/v1/broker-connections/{id}", connectionId)))
                .andExpect(status().isNotFound());
    }

    @Test
    void userCannotAccessAnotherUsersBrokerConnector() throws Exception {
        String body = mockMvc.perform(withUserA(post("/api/v1/broker-connections/ibkr/connect")))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.connectorId").doesNotExist())
                .andExpect(jsonPath("$.sessionReference").doesNotExist())
                .andExpect(jsonPath("$.userId").doesNotExist())
                .andReturn().getResponse().getContentAsString();

        UUID connectorId = brokerConnectionRepository.findByUserId(UUID.fromString(USER_A)).get(0).getConnectorId();

        mockMvc.perform(withUserA(post("/api/v1/broker-connections/{id}/sync",
                        brokerConnectionRepository.findByUserId(UUID.fromString(USER_A)).get(0).getConnectionId())))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("AUTHENTICATION_REQUIRED"));

        mockMvc.perform(withUserB(get("/api/v1/broker-connectors/{id}/status", connectorId)))
                .andExpect(status().isNotFound());

        mockMvc.perform(withUserB(get("/api/v1/broker-connectors/{id}/login", connectorId)))
                .andExpect(status().isNotFound());
    }

    @Test
    void unconfiguredRealProviderConnectCreatesUserOwnedConnectorAwaitingAuthentication() throws Exception {
        mockMvc.perform(withUserA(post("/api/v1/broker-connections/ibkr/connect"))
                        .header("X-Correlation-Id", "phase2b-test-correlation"))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.brokerType").value("IBKR"))
                .andExpect(jsonPath("$.connectorId").doesNotExist())
                .andExpect(jsonPath("$.sessionReference").doesNotExist())
                .andExpect(jsonPath("$.status").value("AUTHENTICATION_REQUIRED"))
                .andExpect(jsonPath("$.providerStatus").value("AUTHENTICATION_REQUIRED"))
                .andExpect(jsonPath("$.lastErrorCode").value("NOT_CONFIGURED"))
                .andExpect(jsonPath("$.externalAccountReference").doesNotExist())
                .andExpect(jsonPath("$.readOnly").value(true));
    }

    @Test
    void genericIbkrAuthenticationActionHidesTokensAndFailsSafelyWhenGatewayIsUnconfigured() throws Exception {
        String body = mockMvc.perform(withUserA(post("/api/v1/broker-connections/ibkr/connect")))
                .andExpect(status().isCreated()).andReturn().getResponse().getContentAsString();
        String connectionId = body.replaceAll(".*\"connectionId\":\"([^\"]+)\".*", "$1");

        mockMvc.perform(withUserA(get("/api/v1/broker-connections/{id}/authentication-action", connectionId)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.provider").value("IBKR"))
                .andExpect(jsonPath("$.authenticationModel").doesNotExist())
                .andExpect(jsonPath("$.action").value("UNAVAILABLE"))
                .andExpect(jsonPath("$.authenticationUrl").doesNotExist())
                .andExpect(jsonPath("$.capabilities").doesNotExist());

        mockMvc.perform(withUserB(get("/api/v1/broker-connections/{id}/authentication-action", connectionId)))
                .andExpect(status().isNotFound());
    }

    @Test
    void iciciLoginAndSessionAttachmentAreConnectionOwnerScoped() throws Exception {
        String body = mockMvc.perform(withUserA(post("/api/v1/broker-connections/icici-direct/connect")))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.brokerType").value("ICICI_DIRECT"))
                .andExpect(jsonPath("$.status").value("AUTHENTICATION_REQUIRED"))
                .andReturn().getResponse().getContentAsString();
        String connectionId = body.replaceAll(".*\"connectionId\":\"([^\"]+)\".*", "$1");

        mockMvc.perform(withUserA(put("/api/v1/broker-connections/{id}/credentials", connectionId)
                        .contentType("application/json")
                        .content("{\"clientKey\":\"dummy-test-app-key\",\"clientSecret\":\"dummy-test-secret\"}")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.configured").value(true));

        mockMvc.perform(withUserA(get("/api/v1/broker-connections/{id}/icici-login", connectionId)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.connectionId").value(connectionId))
                .andExpect(jsonPath("$.loginUrl").value(
                        "https://api.icicidirect.com/apiuser/login?api_key=dummy-test-app-key"))
                .andExpect(jsonPath("$.loginUrl", not(containsString("secret"))));

        mockMvc.perform(withUserB(get("/api/v1/broker-connections/{id}/icici-login", connectionId)))
                .andExpect(status().isNotFound());
        mockMvc.perform(withUserB(post("/api/v1/broker-connections/{id}/icici-session", connectionId)
                        .contentType("application/json")
                        .content("{\"apiSession\":\"dummy-api-session\"}")))
                .andExpect(status().isNotFound());
        mockMvc.perform(withUserB(get("/api/v1/broker-connections/{id}/accounts", connectionId)))
                .andExpect(status().isNotFound());
        mockMvc.perform(withUserB(get("/api/v1/broker-connections/{id}/demat-holdings", connectionId)))
                .andExpect(status().isNotFound());
        mockMvc.perform(withUserB(get("/api/v1/broker-connections/{id}/funds", connectionId)))
                .andExpect(status().isNotFound());
    }

    @Test
    void normalCustomerAuthenticationNeverRequestsRetailApiCredentials() throws Exception {
        String body = mockMvc.perform(withUserA(post("/api/v1/broker-connections/hdfc-securities/connect")))
                .andExpect(status().isCreated())
                .andReturn().getResponse().getContentAsString();
        String connectionId = body.replaceAll(".*\"connectionId\":\"([^\"]+)\".*", "$1");

        mockMvc.perform(withUserA(get("/api/v1/broker-connections/{id}/authentication-action", connectionId)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.action").value("PARTNER_AUTH_UNAVAILABLE"))
                .andExpect(jsonPath("$.authenticationUrl").doesNotExist())
                .andExpect(jsonPath("$.message", not(containsString("API key"))))
                .andExpect(jsonPath("$.message", not(containsString("secret"))));
    }

    @Test
    void reconnectReusesCanonicalIbkrConnection() throws Exception {
        String first = mockMvc.perform(withUserA(post("/api/v1/broker-connections/ibkr/connect")))
                .andExpect(status().isCreated()).andReturn().getResponse().getContentAsString();
        String second = mockMvc.perform(withUserA(post("/api/v1/broker-connections/ibkr/connect")))
                .andExpect(status().isCreated()).andReturn().getResponse().getContentAsString();

        String firstId = first.replaceAll(".*\"connectionId\":\"([^\"]+)\".*", "$1");
        String secondId = second.replaceAll(".*\"connectionId\":\"([^\"]+)\".*", "$1");
        assertThat(secondId).isEqualTo(firstId);
        assertThat(brokerConnectionRepository.findByUserId(UUID.fromString(USER_A))).hasSize(1);
    }

    @Test
    void legacyDuplicateListAndReconnectUseOlderSuccessfullySyncedIbkrConnection() throws Exception {
        Instant older = Instant.parse("2026-08-01T00:00:00Z");
        Instant newer = Instant.parse("2026-08-29T00:00:00Z");
        BrokerConnectionEntity canonical = brokerConnectionRepository.save(new BrokerConnectionEntity(
                UUID.randomUUID(), UUID.fromString(USER_A), BrokerType.IBKR, "U1234567", "Real IBKR account",
                BrokerConnectionState.CONNECTED, "USD", BrokerProviderStatus.CONNECTED.name(), "REAL_BROKER",
                "client-portal-gateway", "", older, older, older, null, older, older));
        brokerConnectionRepository.save(new BrokerConnectionEntity(
                UUID.randomUUID(), UUID.fromString(USER_A), BrokerType.IBKR, null, "Stale IBKR attempt",
                BrokerConnectionState.ERROR, null, BrokerProviderStatus.ERROR.name(), "UNAVAILABLE",
                null, "", null, null, newer, "BROKER_UNAVAILABLE", newer, newer));

        mockMvc.perform(withUserA(get("/api/v1/broker-connections")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$", hasSize(1)))
                .andExpect(jsonPath("$[0].connectionId").value(canonical.getConnectionId().toString()))
                .andExpect(jsonPath("$[0].status").value("CONNECTED"));
        mockMvc.perform(withUserA(get("/api/v1/brokers")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$[?(@.brokerType == 'IBKR')].status", contains("CONNECTED")));
        mockMvc.perform(withUserA(post("/api/v1/broker-connections/ibkr/connect")))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.connectionId").value(canonical.getConnectionId().toString()));
        assertThat(brokerConnectionRepository.findByUserId(UUID.fromString(USER_A))).hasSize(2);
    }

    private static org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder withUserA(
            org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder request) {
        return request.header("X-AIP-User-Id", USER_A)
                .header("X-AIP-User-Issuer", "test")
                .header("X-AIP-User-Subject", "user-a");
    }

    private static org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder withUserB(
            org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder request) {
        return request.header("X-AIP-User-Id", USER_B)
                .header("X-AIP-User-Issuer", "test")
                .header("X-AIP-User-Subject", "user-b");
    }

    private static org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder withUser(
            UUID userId, String issuer, String subject,
            org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder request) {
        return request.header("X-AIP-User-Id", userId.toString())
                .header("X-AIP-User-Issuer", issuer)
                .header("X-AIP-User-Subject", subject);
    }
}
