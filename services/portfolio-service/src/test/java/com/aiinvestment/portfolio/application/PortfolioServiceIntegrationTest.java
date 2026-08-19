package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.domain.Portfolio;
import com.aiinvestment.portfolio.domain.PortfolioSummary;
import com.aiinvestment.portfolio.infrastructure.persistence.PortfolioRepository;
import com.aiinvestment.shared.domain.event.BrokerSyncEvent;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;

import static org.assertj.core.api.Assertions.assertThat;

@SpringBootTest
@ActiveProfiles("test")
class PortfolioServiceIntegrationTest {
    @Autowired
    private PortfolioService portfolioService;
    @Autowired
    private PortfolioRepository portfolioRepository;
    @Autowired
    private ObjectMapper objectMapper;

    @Test
    void migrationsCreateRepositoryBackedSchema() {
        Portfolio portfolio = portfolioService.createPortfolio("Migration Check", "EUR");

        assertThat(portfolioRepository.findById(portfolio.portfolioId())).isPresent();
    }

    @Test
    void mockBrokerSyncCreatesPositionsAndSummary() {
        Portfolio portfolio = portfolioService.createPortfolio("My Global Portfolio", "EUR");

        PortfolioSummary summary = portfolioService.sync(portfolio.portfolioId());

        assertThat(summary.numberOfPositions()).isEqualTo(5);
        assertThat(summary.baseCurrency()).isEqualTo("EUR");
        assertThat(summary.totalMarketValue().amount()).isPositive();
        assertThat(summary.cash().amount()).isPositive();
        assertThat(summary.allocation().currency()).containsKeys("EUR", "USD", "INR");
        assertThat(summary.allocation().broker()).containsKeys("MOCK_EU", "MOCK_INDIA");
    }

    @Test
    void repeatedMockBrokerSyncIsIdempotent() {
        Portfolio portfolio = portfolioService.createPortfolio("Idempotent Portfolio", "EUR");

        portfolioService.sync(portfolio.portfolioId());
        portfolioService.sync(portfolio.portfolioId());

        assertThat(portfolioService.getPositions(portfolio.portfolioId())).hasSize(5);
    }

    @Test
    void portfolioListReturnsSummariesAndEventsAreSerializable() throws Exception {
        Portfolio portfolio = portfolioService.createPortfolio("Serializable Events", "EUR");
        portfolioService.sync(portfolio.portfolioId());

        assertThat(portfolioService.listPortfolioSummaries()).anySatisfy(summary ->
                assertThat(summary.portfolioId()).isEqualTo(portfolio.portfolioId()));

        String json = objectMapper.writeValueAsString(BrokerSyncEvent.started(null, portfolio.portfolioId(), "test-correlation"));
        assertThat(json).contains("broker.sync.started").contains("test-correlation");
    }
}
