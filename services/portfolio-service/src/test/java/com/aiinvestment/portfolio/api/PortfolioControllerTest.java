package com.aiinvestment.portfolio.api;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;

import static org.hamcrest.Matchers.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class PortfolioControllerTest {
    @Autowired
    private MockMvc mockMvc;

    @Test
    void createGetSyncPositionsAndSummaryFlow() throws Exception {
        String body = mockMvc.perform(post("/api/v1/portfolios")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"name\":\"My Global Portfolio\",\"baseCurrency\":\"EUR\"}")
                        .header("X-Correlation-Id", "phase-1-test"))
                .andExpect(status().isCreated())
                .andExpect(header().string("X-Correlation-Id", "phase-1-test"))
                .andExpect(jsonPath("$.portfolioId", notNullValue()))
                .andReturn().getResponse().getContentAsString();
        String portfolioId = body.replaceAll(".*\"portfolioId\":\"([^\"]+)\".*", "$1");

        mockMvc.perform(get("/api/v1/portfolios/{portfolioId}", portfolioId))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.name").value("My Global Portfolio"));

        mockMvc.perform(post("/api/v1/portfolios/{portfolioId}/sync", portfolioId))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.positions").value(5))
                .andExpect(jsonPath("$.allocation.currency.EUR", notNullValue()))
                .andExpect(jsonPath("$.allocation.currency.USD", notNullValue()))
                .andExpect(jsonPath("$.allocation.currency.INR", notNullValue()));

        mockMvc.perform(get("/api/v1/portfolios"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$[*].portfolioId", hasItem(portfolioId)))
                .andExpect(jsonPath("$[0].totalMarketValue.currency", notNullValue()));

        mockMvc.perform(get("/api/v1/portfolios/{portfolioId}/positions", portfolioId))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$", hasSize(5)))
                .andExpect(jsonPath("$[0].quote.freshness").value("MOCK"))
                .andExpect(jsonPath("$[0].quote.source").value("MockMarketDataProvider"));

        mockMvc.perform(get("/api/v1/portfolios/{portfolioId}/summary", portfolioId))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.baseCurrency").value("EUR"))
                .andExpect(jsonPath("$.totalMarketValue.currency").value("EUR"));
    }

    @Test
    void invalidRequestReturnsConsistentError() throws Exception {
        mockMvc.perform(post("/api/v1/portfolios")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"name\":\"\",\"baseCurrency\":\"EURO\"}")
                        .header("X-Correlation-Id", "bad-request"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_PORTFOLIO_REQUEST"))
                .andExpect(jsonPath("$.correlationId").value("bad-request"));
    }

    @Test
    void unknownPortfolioReturnsNotFound() throws Exception {
        mockMvc.perform(get("/api/v1/portfolios/00000000-0000-0000-0000-000000000999"))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.code").value("PORTFOLIO_NOT_FOUND"));
    }
}
