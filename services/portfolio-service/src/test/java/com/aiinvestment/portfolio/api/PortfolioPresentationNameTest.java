package com.aiinvestment.portfolio.api;

import com.aiinvestment.portfolio.domain.Portfolio;
import com.aiinvestment.shared.domain.broker.BrokerType;
import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;

class PortfolioPresentationNameTest {
    @Test
    void brokerLinkedInternalPhaseNameUsesFriendlyProviderName() {
        assertThat(PortfolioPresentationName.forPortfolio(portfolio("Phase 5B IBKR Validation", BrokerType.IBKR)))
                .isEqualTo("Interactive Brokers");
        assertThat(PortfolioPresentationName.forPortfolio(portfolio("Phase 5B Isolation A", BrokerType.ICICI_DIRECT)))
                .isEqualTo("ICICI Direct");
    }

    @Test
    void userDefinedNameHasPriorityEvenForBrokerPortfolio() {
        assertThat(PortfolioPresentationName.forPortfolio(portfolio("Retirement account", BrokerType.IBKR)))
                .isEqualTo("Retirement account");
    }

    @Test
    void unlinkedPortfolioNameIsNeverRewritten() {
        assertThat(PortfolioPresentationName.forPortfolio(portfolio("Phase 5B is my chosen name", null)))
                .isEqualTo("Phase 5B is my chosen name");
    }

    @Test
    void compactTechnicalPhasePortfolioIsHiddenFromManagedNavigation() {
        assertThat(PortfolioPresentationName.visibleInManagedNavigation(
                portfolio("phase5b3-isolation-check", null))).isFalse();
    }

    private static Portfolio portfolio(String name, BrokerType provider) {
        Instant now = Instant.parse("2026-08-30T00:00:00Z");
        return new Portfolio(UUID.randomUUID(), UUID.randomUUID(), name, "EUR", now, now,
                provider == null ? null : UUID.randomUUID(), provider == null ? null : "account", provider,
                null, null, null, null, null, null, null);
    }
}
