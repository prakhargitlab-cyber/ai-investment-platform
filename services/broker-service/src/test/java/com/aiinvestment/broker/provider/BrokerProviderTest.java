package com.aiinvestment.broker.provider;

import com.aiinvestment.broker.provider.ibkr.IBKRBrokerProvider;
import com.aiinvestment.broker.provider.ibkr.IBKRInstrumentNormalizer;
import com.aiinvestment.broker.connector.ibkr.IBKRIndividualConnector;
import com.aiinvestment.broker.provider.icici.ICICIDirectBrokerProvider;
import com.aiinvestment.broker.provider.mock.MockBrokerProvider;
import com.aiinvestment.broker.application.BrokerProviderException;
import com.aiinvestment.broker.config.IBKRProviderProperties;
import com.aiinvestment.shared.domain.broker.BrokerCapability;
import com.aiinvestment.shared.domain.broker.BrokerType;
import com.aiinvestment.shared.domain.broker.BrokerConnectionState;
import com.aiinvestment.shared.domain.broker.BrokerProviderStatus;
import org.junit.jupiter.api.Test;
import org.springframework.web.client.RestClient;

import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class BrokerProviderTest {
    @Test
    void mockProviderReturnsTwoAccountsAndPositions() {
        MockBrokerProvider provider = new MockBrokerProvider();

        var accounts = provider.fetchAccounts(UUID.randomUUID());

        assertThat(accounts).hasSize(2);
        assertThat(accounts).extracting("brokerAccountId").containsExactly("MOCK_EU", "MOCK_INDIA");
        assertThat(provider.fetchPositions(accounts.get(0))).isNotEmpty();
        assertThat(provider.fetchCashBalances(accounts.get(1))).isNotEmpty();
    }

    @Test
    void realProviderPlaceholdersAreDisabledByDefault() {
        IBKRBrokerProvider ibkr = new IBKRBrokerProvider();
        ICICIDirectBrokerProvider icici = new ICICIDirectBrokerProvider();

        assertThat(ibkr.supportedBroker()).isEqualTo(BrokerType.IBKR);
        assertThat(icici.supportedBroker()).isEqualTo(BrokerType.ICICI_DIRECT);
        assertThat(ibkr.connectionStatus().state()).isEqualTo(BrokerConnectionState.DISCONNECTED);
        assertThat(icici.connectionStatus().code()).isEqualTo("NOT_CONFIGURED");
        assertThat(ibkr.connectionCapabilities().capabilities()).isEmpty();
        assertThat(icici.connectionCapabilities().capabilities()).isEmpty();
        assertThatThrownBy(() -> ibkr.fetchAccounts(UUID.randomUUID())).isInstanceOf(BrokerProviderException.class)
                .hasMessageContaining("authentication is required");
        assertThatThrownBy(() -> icici.fetchAccounts(UUID.randomUUID())).isInstanceOf(BrokerProviderException.class)
                .hasMessageContaining("authentication is required");
    }

    @Test
    void enabledRealProviderStillRequiresVerifiedLocalOfficialDocumentation() {
        IBKRBrokerProvider ibkr = new IBKRBrokerProvider(
                new IBKRProviderProperties(true, "https://api.ibkr.com/v1/api", "https://api.ibkr.com/oauth2",
                        "https://api.ibkr.com", "client", "key-id", "https://localhost/callback",
                        "client-portal-gateway", false, false, false,
                        "https://api.ibkr.com/v1/api", "LOCAL_AGENT", "", "", 1800, 86400,
                        "k8s-secret:ibkr-private-key", "IBKR_SESSION_TOKEN"),
                new IBKRIndividualConnector(new IBKRProviderProperties(true, "https://api.ibkr.com/v1/api", "https://api.ibkr.com/oauth2",
                        "https://api.ibkr.com", "client", "key-id", "https://localhost/callback",
                        "client-portal-gateway", false, false, false,
                        "https://api.ibkr.com/v1/api", "LOCAL_AGENT", "", "", 1800, 86400,
                        "k8s-secret:ibkr-private-key", "IBKR_SESSION_TOKEN"),
                        new IBKRInstrumentNormalizer(), RestClient.builder().baseUrl("https://api.ibkr.com/v1/api").build()));

        assertThat(ibkr.connectionStatus().providerStatus()).isEqualTo(BrokerProviderStatus.DOCUMENTATION_REQUIRED);
        assertThat(ibkr.connectionCapabilities().capabilities()).isEmpty();
    }

    @Test
    void orderExecutionCapabilityIsRejected() {
        assertThatThrownBy(() -> new com.aiinvestment.shared.domain.broker.BrokerConnectionCapabilities(
                java.util.Set.of(BrokerCapability.ORDER_EXECUTION)))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("ORDER_EXECUTION");
    }
}
