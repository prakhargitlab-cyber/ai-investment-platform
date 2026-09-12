package com.aiinvestment.portfolio.api;

import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterEntity;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterRepository;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentProviderMappingEntity;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentProviderMappingRepository;
import com.aiinvestment.portfolio.application.PortfolioImportService;
import com.aiinvestment.shared.domain.AssetType;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.mock.web.MockMultipartFile;
import java.nio.charset.StandardCharsets;
import java.math.BigDecimal;
import java.time.Instant;
import java.util.UUID;

import static org.hamcrest.Matchers.*;
import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class PortfolioControllerTest {
    private static final String USER_A = "10000000-0000-0000-0000-000000000001";
    private static final String USER_B = "20000000-0000-0000-0000-000000000002";

    @Autowired
    private MockMvc mockMvc;
    @Autowired
    private InstrumentMasterRepository instrumentMasters;
    @Autowired
    private InstrumentProviderMappingRepository instrumentMappings;
    @Autowired
    private PortfolioImportService portfolioImports;

    @Test
    void globalInstrumentReadReturnsOnlyMasterMetadataAndProviderMappings() throws Exception {
        UUID globalId = UUID.randomUUID();
        UUID localPortfolioInstrumentId = UUID.randomUUID();
        instrumentMasters.saveAndFlush(new InstrumentMasterEntity(globalId, "INE000A01010", "Example Components Limited",
                AssetType.EQUITY, "INR", "IN", "NSE", "VERIFIED_NSE", "ACTIVE", Instant.now()));
        instrumentMappings.save(new InstrumentProviderMappingEntity(UUID.randomUUID(), globalId, "NSE", "VERIFIED_NSE", null,
                "NSE", "INR", "VERIFIED", "OFFICIAL_NSE", new BigDecimal("0.99"), Instant.now()));
        instrumentMappings.save(new InstrumentProviderMappingEntity(UUID.randomUUID(), globalId, "YAHOO_FINANCE", "VERIFIED.NS", null,
                "NSE", "INR", "VERIFIED", "YAHOO_RESOLUTION", new BigDecimal("0.95"), Instant.now()));
        instrumentMappings.save(new InstrumentProviderMappingEntity(UUID.randomUUID(), globalId, "NSE", "INVALID_ALIAS", null,
                "NSE", "INR", "INVALID", "REJECTED", new BigDecimal("0.10"), Instant.now()));

        mockMvc.perform(get("/api/v1/instruments/{globalInstrumentId}", globalId)
                        .header("X-AIP-User-Id", USER_B).header("X-AIP-User-Issuer", "test").header("X-AIP-User-Subject", "user-b"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.globalInstrumentId").value(globalId.toString()))
                .andExpect(jsonPath("$.canonicalName").value("Example Components Limited"))
                .andExpect(jsonPath("$.providerMappings", hasSize(3)))
                .andExpect(jsonPath("$.providerMappings[?(@.provider=='NSE' && @.providerSymbol=='VERIFIED_NSE')].status").value(hasItem("VERIFIED")))
                .andExpect(jsonPath("$.providerMappings[?(@.provider=='YAHOO_FINANCE')].status").value(hasItem("VERIFIED")))
                .andExpect(jsonPath("$.providerMappings[?(@.providerSymbol=='INVALID_ALIAS')].status").value(hasItem("INVALID")))
                .andExpect(jsonPath("$.providerMappings[?(@.providerSymbol=='VERIFIED_NSE')].resolutionSource").value(hasItem("OFFICIAL_NSE")))
                .andExpect(jsonPath("$.portfolioId").doesNotExist())
                .andExpect(jsonPath("$.positionId").doesNotExist())
                .andExpect(jsonPath("$.quantity").doesNotExist());

        mockMvc.perform(get("/api/v1/instruments/{globalInstrumentId}", localPortfolioInstrumentId)
                        .header("X-AIP-User-Id", USER_A).header("X-AIP-User-Issuer", "test").header("X-AIP-User-Subject", "user-a"))
                .andExpect(status().isNotFound());
    }

    @Test
    void unknownGlobalInstrumentReturnsNotFound() throws Exception {
        mockMvc.perform(get("/api/v1/instruments/{globalInstrumentId}", UUID.randomUUID())
                        .header("X-AIP-User-Id", USER_A).header("X-AIP-User-Issuer", "test").header("X-AIP-User-Subject", "user-a"))
                .andExpect(status().isNotFound());
    }

    @Test
    void masterLinkedImportedPositionProjectsCanonicalIdentityWithoutChangingLocalProvenance() throws Exception {
        UUID masterId = UUID.randomUUID();
        instrumentMasters.saveAndFlush(new InstrumentMasterEntity(masterId, "INE171A01029", "FEDERAL BANK LTD",
                AssetType.EQUITY, "INR", "IN", "NSE", "FEDBAN", "ACTIVE", Instant.now()));
        instrumentMappings.saveAndFlush(new InstrumentProviderMappingEntity(UUID.randomUUID(), masterId, "NSE", "FEDBAN", null,
                "NSE", "INR", "VERIFIED", "OFFICIAL_NSE", new BigDecimal("0.99"), Instant.now()));

        mockMvc.perform(get("/api/v1/portfolios")
                        .header("X-AIP-User-Id", USER_A).header("X-AIP-User-Issuer", "test").header("X-AIP-User-Subject", "user-a"))
                .andExpect(status().isOk());
        var imported = portfolioImports.confirm(UUID.fromString(USER_A), com.aiinvestment.shared.domain.broker.BrokerType.ICICI_DIRECT,
                new MockMultipartFile("file", "8510744020_PortFolioEqtSummary.csv", "text/csv", ("Stock Symbol,Company Name,ISIN Code,Qty,Average Cost Price,Current Market Price,% Change over prev close,Value At Cost,Value At Market Price,Realized Profit / Loss,Unrealized Profit/Loss,Unrealized Profit/Loss %\n"
                        + "FEDBAN,Federal Bank import name,INE171A01029,10,200,250,1,2000,2500,0,500,25\n").getBytes(StandardCharsets.UTF_8)), null);

        mockMvc.perform(get("/api/v1/portfolios/{portfolioId}/positions", imported.portfolioId())
                        .header("X-AIP-User-Id", USER_A).header("X-AIP-User-Issuer", "test").header("X-AIP-User-Subject", "user-a"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$[0].instrument.globalInstrumentId").value(masterId.toString()))
                .andExpect(jsonPath("$[0].instrument.canonicalSymbol").value("FEDBAN"))
                .andExpect(jsonPath("$[0].instrument.canonicalExchange").value("NSE"))
                .andExpect(jsonPath("$[0].instrument.canonicalName").value("FEDERAL BANK LTD"))
                .andExpect(jsonPath("$[0].instrument.companyName").value("FEDERAL BANK LTD"))
                .andExpect(jsonPath("$[0].instrument.isin").value("INE171A01029"))
                .andExpect(jsonPath("$[0].instrument.provider").value("ICICI_DIRECT"))
                .andExpect(jsonPath("$[0].instrument.providerInstrumentId").value("ISIN:INE171A01029"))
                .andExpect(jsonPath("$[0].instrument.brokerSymbol").value("FEDBAN"))
                .andExpect(jsonPath("$[0].instrument.exchange").value("UNKNOWN"))
                .andExpect(jsonPath("$[0].instrument.providerMappings[?(@.provider=='NSE' && @.providerSymbol=='FEDBAN')].status").value(hasItem("VERIFIED")))
                .andExpect(jsonPath("$[0].instrument.providerMappings[?(@.provider=='NSE' && @.providerSymbol=='FEDBAN')].resolutionSource")
                        .value(hasItem("OFFICIAL_NSE")));

        mockMvc.perform(get("/api/v1/portfolios/{portfolioId}/positions", imported.portfolioId())
                        .header("X-AIP-User-Id", USER_B).header("X-AIP-User-Issuer", "test").header("X-AIP-User-Subject", "user-b"))
                .andExpect(status().isNotFound());
    }

    @Test
    void globalInstrumentReconcileIsAuthenticatedIdempotentAndReturnsAuthoritativeMappings() throws Exception {
        UUID globalId = UUID.randomUUID();
        instrumentMasters.saveAndFlush(new InstrumentMasterEntity(globalId, "INE999Z01011", "Reconciliation Example Limited",
                AssetType.EQUITY, "INR", "IN", "NSE", "OFFICIAL", "ACTIVE", Instant.now()));
        instrumentMappings.saveAndFlush(new InstrumentProviderMappingEntity(UUID.randomUUID(), globalId, "NSE", "OFFICIAL", null,
                "NSE", "INR", "VERIFIED", "NSE_OFFICIAL_ISIN_BOOTSTRAP", new BigDecimal("0.99"), Instant.now()));

        for (String user : new String[] {UUID.randomUUID().toString(), UUID.randomUUID().toString()}) {
            mockMvc.perform(post("/api/v1/instruments/{globalInstrumentId}/reconcile", globalId)
                            .header("X-AIP-User-Id", user).header("X-AIP-User-Issuer", "test").header("X-AIP-User-Subject", user))
                    .andExpect(status().isOk())
                    .andExpect(jsonPath("$.globalInstrumentId").value(globalId.toString()))
                    .andExpect(jsonPath("$.providerMappings", hasSize(1)))
                    .andExpect(jsonPath("$.providerMappings[0].providerSymbol").value("OFFICIAL"))
                    .andExpect(jsonPath("$.providerMappings[0].resolutionSource").value("NSE_OFFICIAL_ISIN_BOOTSTRAP"));
        }
        assertThat(instrumentMappings.findByInstrumentId(globalId)).hasSize(1);
    }

    @Test
    void globalInstrumentReconcileReturnsNotFoundForUnknownMasterId() throws Exception {
        mockMvc.perform(post("/api/v1/instruments/{globalInstrumentId}/reconcile", UUID.randomUUID())
                        .header("X-AIP-User-Id", USER_A).header("X-AIP-User-Issuer", "test").header("X-AIP-User-Subject", "user-a"))
                .andExpect(status().isNotFound());
    }

    @Test
    void createGetSyncPositionsAndSummaryFlow() throws Exception {
        String body = mockMvc.perform(post("/api/v1/portfolios")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"name\":\"My Global Portfolio\",\"baseCurrency\":\"EUR\"}")
                        .header("X-AIP-User-Id", USER_A)
                        .header("X-AIP-User-Issuer", "test")
                        .header("X-AIP-User-Subject", "user-a")
                        .header("X-Correlation-Id", "phase-1-test"))
                .andExpect(status().isCreated())
                .andExpect(header().string("X-Correlation-Id", "phase-1-test"))
                .andExpect(jsonPath("$.portfolioId", notNullValue()))
                .andReturn().getResponse().getContentAsString();
        String portfolioId = body.replaceAll(".*\"portfolioId\":\"([^\"]+)\".*", "$1");

        mockMvc.perform(get("/api/v1/portfolios/{portfolioId}", portfolioId)
                        .header("X-AIP-User-Id", USER_A)
                        .header("X-AIP-User-Issuer", "test")
                        .header("X-AIP-User-Subject", "user-a"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.name").value("My Global Portfolio"));

        mockMvc.perform(post("/api/v1/portfolios/{portfolioId}/sync", portfolioId)
                        .header("X-AIP-User-Id", USER_A)
                        .header("X-AIP-User-Issuer", "test")
                        .header("X-AIP-User-Subject", "user-a"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.positions").value(5))
                .andExpect(jsonPath("$.allocation.currency.EUR", notNullValue()))
                .andExpect(jsonPath("$.allocation.currency.USD", notNullValue()))
                .andExpect(jsonPath("$.allocation.currency.INR", notNullValue()));

        mockMvc.perform(get("/api/v1/portfolios")
                        .header("X-AIP-User-Id", USER_A)
                        .header("X-AIP-User-Issuer", "test")
                        .header("X-AIP-User-Subject", "user-a"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$[*].portfolioId", hasItem(portfolioId)))
                .andExpect(jsonPath("$[0].totalMarketValue.currency", notNullValue()));

        mockMvc.perform(get("/api/v1/portfolios/{portfolioId}/positions", portfolioId)
                        .header("X-AIP-User-Id", USER_A)
                        .header("X-AIP-User-Issuer", "test")
                        .header("X-AIP-User-Subject", "user-a"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$", hasSize(5)))
                .andExpect(jsonPath("$[0].quote.freshness").value("MOCK"))
                .andExpect(jsonPath("$[0].quote.source").value("MockMarketDataProvider"));

        mockMvc.perform(get("/api/v1/portfolios/{portfolioId}/summary", portfolioId)
                        .header("X-AIP-User-Id", USER_A)
                        .header("X-AIP-User-Issuer", "test")
                        .header("X-AIP-User-Subject", "user-a"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.baseCurrency").value("EUR"))
                .andExpect(jsonPath("$.totalMarketValue.currency").value("EUR"));
    }

    @Test
    void invalidRequestReturnsConsistentError() throws Exception {
        mockMvc.perform(post("/api/v1/portfolios")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"name\":\"\",\"baseCurrency\":\"EURO\"}")
                        .header("X-AIP-User-Id", USER_A)
                        .header("X-AIP-User-Issuer", "test")
                        .header("X-AIP-User-Subject", "user-a")
                        .header("X-Correlation-Id", "bad-request"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_PORTFOLIO_REQUEST"))
                .andExpect(jsonPath("$.correlationId").value("bad-request"));
    }

    @Test
    void unknownPortfolioReturnsNotFound() throws Exception {
        mockMvc.perform(get("/api/v1/portfolios/00000000-0000-0000-0000-000000000999")
                        .header("X-AIP-User-Id", USER_A)
                        .header("X-AIP-User-Issuer", "test")
                        .header("X-AIP-User-Subject", "user-a"))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.code").value("PORTFOLIO_NOT_FOUND"));
    }

    @Test
    void userCannotReadAnotherUsersPortfolio() throws Exception {
        String body = mockMvc.perform(post("/api/v1/portfolios")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"name\":\"User A Portfolio\",\"baseCurrency\":\"EUR\"}")
                        .header("X-AIP-User-Id", USER_A)
                        .header("X-AIP-User-Issuer", "test")
                        .header("X-AIP-User-Subject", "user-a"))
                .andExpect(status().isCreated())
                .andReturn().getResponse().getContentAsString();
        String portfolioId = body.replaceAll(".*\"portfolioId\":\"([^\"]+)\".*", "$1");

        mockMvc.perform(get("/api/v1/portfolios/{portfolioId}", portfolioId)
                        .header("X-AIP-User-Id", USER_B)
                        .header("X-AIP-User-Issuer", "test")
                        .header("X-AIP-User-Subject", "user-b"))
                .andExpect(status().isNotFound());

        mockMvc.perform(post("/api/v1/portfolios/{portfolioId}/sync", portfolioId)
                        .header("X-AIP-User-Id", USER_B)
                        .header("X-AIP-User-Issuer", "test")
                        .header("X-AIP-User-Subject", "user-b"))
                .andExpect(status().isNotFound());
    }

    @Test
    void frontendIciciMultipartContractReachesPreviewAndDetectsAccountFromRealFilename() throws Exception {
        String csv = "Stock Symbol,Company Name,ISIN Code,Qty,Average Cost Price,Current Market Price,% Change over prev close,Value At Cost,Value At Market Price,Realized Profit / Loss,Unrealized Profit/Loss,Unrealized Profit/Loss %,\n"
                + "ABC,ABC Limited,INE000A01001,10,200.50,250,+1.2,2005,2500,(10),+495,24.69,\n";
        MockMultipartFile file = new MockMultipartFile("file",
                "8510744020_PortFolioEqtSummary (1).csv", "text/csv", csv.getBytes(StandardCharsets.UTF_8));

        mockMvc.perform(multipart("/api/v1/portfolios/imports/icici_direct/preview").file(file)
                        .header("X-AIP-User-Id", USER_A).header("X-AIP-User-Issuer", "test")
                        .header("X-AIP-User-Subject", "user-a"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.portfolioName").value("8510744020"))
                .andExpect(jsonPath("$.validHoldings").value(1))
                .andExpect(jsonPath("$.holdings[0].companyName").value("ABC Limited"))
                .andExpect(jsonPath("$.holdings[0].importedPrice").value(250));
    }

    @Test
    void frontendHdfcMultipartContractReachesPreviewAndReturnsStatementTime() throws Exception {
        String csv = "Trading account: 7779646, Equity Portfolio Details as on 29/08/2026 21:14:38\n\n"
                + "SYMBOL,QUANTITY,LONG TERM QUANTITY,AVG PRICE,LTP,INVESTMENT VALUE,CURRENT VALUE,UNREALISED P/L,REALISED P/L,TOTAL P/L,TODAY'S P/L\n"
                + "ABCXYZ,100,80,200,225,20000,22500,2500,0,2500,10\n";
        MockMultipartFile file = new MockMultipartFile("file",
                "Invest Right Equity Portfolio_7779646.csv", "text/csv", csv.getBytes(StandardCharsets.UTF_8));

        mockMvc.perform(multipart("/api/v1/portfolios/imports/hdfc_securities/preview").file(file)
                        .header("X-AIP-User-Id", USER_A).header("X-AIP-User-Issuer", "test")
                        .header("X-AIP-User-Subject", "user-a"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.portfolioName").value("7779646"))
                .andExpect(jsonPath("$.statementAt").value("2026-08-29T15:44:38Z"))
                .andExpect(jsonPath("$.holdings[0].companyName").value("ABCXYZ"));
    }

    @Test
    void invalidImportReturnsSafeStructuredValidationError() throws Exception {
        MockMultipartFile file = new MockMultipartFile("file", "not-an-icici-file.csv", "text/csv",
                "bad,columns\n1,2\n".getBytes(StandardCharsets.UTF_8));
        mockMvc.perform(multipart("/api/v1/portfolios/imports/icici_direct/preview").file(file)
                        .header("X-AIP-User-Id", USER_A).header("X-AIP-User-Issuer", "test")
                        .header("X-AIP-User-Subject", "user-a"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_PORTFOLIO_FILE"))
                .andExpect(jsonPath("$.message", not("The request could not be completed.")))
                .andExpect(jsonPath("$.details").isArray());
    }
}
