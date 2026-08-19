package com.aiinvestment.broker.provider;

import com.aiinvestment.broker.provider.ibkr.IBKRBrokerProvider;
import com.aiinvestment.broker.provider.icici.ICICIDirectBrokerProvider;
import com.aiinvestment.broker.provider.mock.MockBrokerProvider;
import com.aiinvestment.broker.config.IBKRProviderProperties;
import com.aiinvestment.shared.domain.broker.BrokerType;
import com.aiinvestment.shared.domain.broker.BrokerConnectionState;
import com.aiinvestment.shared.domain.broker.BrokerProviderStatus;
import org.junit.jupiter.api.Test;

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
        assertThatThrownBy(() -> ibkr.fetchAccounts(UUID.randomUUID())).isInstanceOf(UnsupportedOperationException.class);
        assertThatThrownBy(() -> icici.fetchAccounts(UUID.randomUUID())).isInstanceOf(UnsupportedOperationException.class);
    }

    @Test
    void enabledRealProviderStillRequiresVerifiedLocalOfficialDocumentation() {
        IBKRBrokerProvider ibkr = new IBKRBrokerProvider(
                new IBKRProviderProperties(true, "https://localhost:5000", "client", "https://localhost/callback", "gateway", false));

        assertThat(ibkr.connectionStatus().providerStatus()).isEqualTo(BrokerProviderStatus.DOCUMENTATION_REQUIRED);
        assertThat(ibkr.connectionCapabilities().capabilities()).isEmpty();
    }
}
