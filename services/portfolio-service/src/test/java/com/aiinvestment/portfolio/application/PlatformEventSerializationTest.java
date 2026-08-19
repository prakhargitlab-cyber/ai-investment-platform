package com.aiinvestment.portfolio.application;

import com.aiinvestment.shared.domain.broker.BrokerConnectionState;
import com.aiinvestment.shared.domain.broker.BrokerType;
import com.aiinvestment.shared.domain.event.*;
import com.aiinvestment.shared.domain.market.MarketDataFreshness;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;

class PlatformEventSerializationTest {
    private final ObjectMapper objectMapper = new ObjectMapper().findAndRegisterModules();

    @Test
    void brokerAndPortfolioEventsSerializeWithoutAuthPayloads() throws Exception {
        UUID id = UUID.randomUUID();
        String brokerConnection = objectMapper.writeValueAsString(new BrokerConnectionChangedEvent(
                UUID.randomUUID(), "corr", Instant.now(), id, UUID.randomUUID(), BrokerType.MOCK, BrokerConnectionState.CONNECTED));
        String sync = objectMapper.writeValueAsString(BrokerSyncEvent.completed(id, UUID.randomUUID(), "corr"));
        String portfolio = objectMapper.writeValueAsString(new PortfolioUpdatedEvent(UUID.randomUUID(), "corr", Instant.now(), UUID.randomUUID(), UUID.randomUUID()));
        String quote = objectMapper.writeValueAsString(new MarketQuoteUpdatedEvent(UUID.randomUUID(), "corr", Instant.now(), UUID.randomUUID(), MarketDataFreshness.DELAYED));
        String researchDocument = objectMapper.writeValueAsString(ResearchDocumentEvent.processed(UUID.randomUUID(), UUID.randomUUID(), "INVESTOR_RELATIONS", "corr"));
        String researchEvent = objectMapper.writeValueAsString(ResearchExtractedEvent.extracted(UUID.randomUUID(), UUID.randomUUID(), UUID.randomUUID(), "NEW_ORDER", "POSITIVE", 0.91, "corr"));
        String researchCompany = objectMapper.writeValueAsString(new ResearchCompanyUpdatedEvent(UUID.randomUUID(), "corr", Instant.now(), UUID.randomUUID(), UUID.randomUUID()));

        String combined = brokerConnection + sync + portfolio + quote + researchDocument + researchEvent + researchCompany;
        assertThat(combined).contains("broker.connection.changed", "broker.sync.completed", "portfolio.updated", "market.quote.updated",
                "research.document.processed", "research.event.extracted", "research.company.updated");
        assertThat(combined).contains("\"version\":1");
        assertThat(combined.toLowerCase()).doesNotContain("password").doesNotContain("token").doesNotContain("otp").doesNotContain("mfa");
    }
}
