package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.domain.Portfolio;
import com.aiinvestment.portfolio.domain.PortfolioHistory;
import com.aiinvestment.portfolio.domain.PortfolioSummary;
import com.aiinvestment.portfolio.api.CombinedPortfolioHoldingResponse;
import com.aiinvestment.portfolio.infrastructure.persistence.PortfolioRepository;
import com.aiinvestment.portfolio.infrastructure.persistence.PortfolioValuationSnapshotEntity;
import com.aiinvestment.portfolio.infrastructure.persistence.PortfolioValuationSnapshotRepository;
import com.aiinvestment.portfolio.infrastructure.persistence.BrokerPositionSnapshotRepository;
import com.aiinvestment.shared.domain.event.BrokerSyncEvent;
import com.aiinvestment.shared.domain.broker.BrokerType;
import com.aiinvestment.shared.web.auth.AuthenticatedUser;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.context.ActiveProfiles;

import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.List;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

@SpringBootTest
@ActiveProfiles("test")
class PortfolioServiceIntegrationTest {
    private static final UUID USER_A = UUID.fromString("10000000-0000-0000-0000-000000000001");
    private static final UUID USER_B = UUID.fromString("10000000-0000-0000-0000-000000000002");
    private static final HttpServer BROKER_SERVER = startBrokerServer();
    private static volatile String brokerSnapshotResponse = "{}";
    @Autowired
    private PortfolioService portfolioService;
    @Autowired
    private PortfolioRepository portfolioRepository;
    @Autowired
    private PortfolioValuationSnapshotRepository valuationSnapshotRepository;
    @Autowired
    private BrokerPositionSnapshotRepository brokerPositionSnapshotRepository;
    @Autowired
    private ObjectMapper objectMapper;
    @Autowired
    private JdbcTemplate jdbcTemplate;

    @DynamicPropertySource
    static void brokerServiceProperties(DynamicPropertyRegistry registry) {
        registry.add("broker.service.base-url", () -> "http://127.0.0.1:" + BROKER_SERVER.getAddress().getPort());
    }

    @AfterAll
    static void stopBrokerServer() {
        BROKER_SERVER.stop(0);
    }

    @BeforeEach
    void seedAppUsers() {
        jdbcTemplate.update("UPDATE portfolio.instruments SET master_instrument_id = NULL");
        jdbcTemplate.update("DELETE FROM portfolio.instrument_provider_mappings");
        jdbcTemplate.update("DELETE FROM portfolio.instrument_master");
        seedAppUser(USER_A, "user-a", "user.a@example.invalid", "User A");
        seedAppUser(USER_B, "service-user-b", "user.b@example.invalid", "User B");
    }

    private void seedAppUser(UUID userId, String subject, String email, String displayName) {
        Instant now = Instant.now();
        int updated = jdbcTemplate.update("""
                        UPDATE portfolio.app_users
                        SET email = ?, display_name = ?, updated_at = ?
                        WHERE id = ?
                        """,
                email,
                displayName,
                Timestamp.from(now),
                userId);
        if (updated > 0) {
            return;
        }
        jdbcTemplate.update("""
                        INSERT INTO portfolio.app_users (id, issuer, external_subject, email, display_name, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                userId,
                "test",
                subject,
                email,
                displayName,
                Timestamp.from(now),
                Timestamp.from(now));
    }

    @Test
    void migrationsCreateRepositoryBackedSchema() {
        Portfolio portfolio = portfolioService.createPortfolio(USER_A, "Migration Check", "EUR");

        assertThat(portfolioRepository.findById(portfolio.portfolioId())).isPresent();
    }

    @Test
    void manualImportDisplayNameOverridePreservesIdentityAcrossReimportAndCanBeReset() {
        Portfolio portfolio = portfolioService.createPortfolio(USER_A, "HDFC import", "INR");
        UUID positionId = insertImportedHdfcPosition(portfolio.portfolioId(), USER_A, "ABCXYZ");

        var renamed = portfolioService.updateCustomDisplayName(
                USER_A, portfolio.portfolioId(), positionId, "  ABC XYZ Limited  ");

        assertThat(renamed.displayName()).isEqualTo("ABC XYZ Limited");
        assertThat(renamed.instrument().providerInstrumentId()).isEqualTo("SYMBOL:ABCXYZ");
        assertThat(renamed.instrument().isin()).isNull();
        assertThat(renamed.instrument().brokerSymbol()).isEqualTo("ABCXYZ");
        assertThat(renamed.quantity()).isEqualByComparingTo("7");

        jdbcTemplate.update("""
                UPDATE portfolio.portfolio_positions
                SET quantity = 9, current_price_amount = 125, last_updated = ?
                WHERE position_id = ?
                """, Timestamp.from(Instant.now()), positionId);
        var afterReimport = portfolioService.getPositions(USER_A, portfolio.portfolioId()).get(0);
        assertThat(afterReimport.displayName()).isEqualTo("ABC XYZ Limited");
        assertThat(afterReimport.quantity()).isEqualByComparingTo("9");
        assertThat(CombinedPortfolioHoldingResponse.aggregate(List.of(afterReimport)).get(0))
                .satisfies(combined -> {
                    assertThat(combined.securityKey()).isEqualTo("HDFC_SECURITIES:SYMBOL:ABCXYZ");
                    assertThat(combined.companyName()).isEqualTo("ABC XYZ Limited");
                });

        assertThatThrownBy(() -> portfolioService.updateCustomDisplayName(
                USER_B, portfolio.portfolioId(), positionId, "Not allowed"))
                .isInstanceOf(PortfolioNotFoundException.class);

        var reset = portfolioService.updateCustomDisplayName(USER_A, portfolio.portfolioId(), positionId, "   ");
        assertThat(reset.customDisplayName()).isNull();
        assertThat(reset.displayName()).isEqualTo("ABCXYZ");
        assertThat(reset.instrument().providerInstrumentId()).isEqualTo("SYMBOL:ABCXYZ");
    }

    @Test
    void mockBrokerSyncCreatesPositionsAndSummary() {
        Portfolio portfolio = portfolioService.createPortfolio(USER_A, "My Global Portfolio", "EUR");

        PortfolioSummary summary = portfolioService.sync(USER_A, portfolio.portfolioId());

        assertThat(summary.numberOfPositions()).isEqualTo(5);
        assertThat(summary.baseCurrency()).isEqualTo("EUR");
        assertThat(summary.totalMarketValue().amount()).isPositive();
        assertThat(summary.cash().amount()).isPositive();
        assertThat(summary.allocation().currency()).containsKeys("EUR", "USD", "INR");
        assertThat(summary.allocation().broker().keySet()).anyMatch(key -> key.startsWith("MOCK_EU_"));
        assertThat(summary.allocation().broker().keySet()).anyMatch(key -> key.startsWith("MOCK_INDIA_"));
    }

    @Test
    void realBrokerImportPersistsEurCashBaseCurrencyAndProvenanceAcrossReload() {
        Portfolio portfolio = portfolioService.createPortfolio(USER_A, "IBKR EUR Portfolio", "USD");
        UUID connectionId = UUID.randomUUID();
        brokerSnapshotResponse = """
                {
                  "accounts": [{
                    "brokerAccountId": "IBKR_U1234567",
                    "brokerType": "IBKR",
                    "externalAccountReference": "****4567",
                    "displayName": "Main IBKR",
                    "baseCurrency": "USD",
                    "status": "ACTIVE"
                  }],
                  "positions": [{
                    "brokerAccountId": "IBKR_U1234567",
                    "instrument": {
                      "instrumentId": "20000000-0000-0000-0000-000000000001",
                      "provider": "IBKR",
                      "providerInstrumentId": "123456",
                      "isin": "DE000A0WMPJ6",
                      "ticker": "AIXA",
                      "exchange": "XETR",
                      "mic": null,
                      "companyName": "AIXA AG",
                      "assetType": "EQUITY",
                      "country": "DE",
                      "tradingCurrency": "EUR",
                      "sector": null,
                      "industry": null,
                      "brokerSymbol": "AIXA",
                      "brokerDescription": "AIXA",
                      "brokerExchange": "AEB",
                      "canonicalSymbol": "AIXA",
                      "canonicalName": "AIXTRON SE",
                      "canonicalExchange": "XETR",
                      "canonicalMic": "XETR",
                      "securityType": "STK"
                    },
                    "quantity": 10,
                    "averageCost": {"amount": 20.00, "currency": "EUR"},
                    "currentPrice": {"amount": 25.00, "currency": "EUR"},
                    "marketValue": {"amount": 250.00, "currency": "EUR"},
                    "unrealizedProfitLoss": {"amount": 50.00, "currency": "EUR"},
                    "dataFreshness": "REAL_BROKER",
                    "observedAt": "2026-08-24T12:00:00Z"
                  }],
                  "cashBalances": [{
                    "brokerAccountId": "IBKR_U1234567",
                    "cash": {"amount": 123.45, "currency": "EUR"},
                    "settledCash": {"amount": 120.00, "currency": "EUR"},
                    "netLiquidationValue": {"amount": 456.78, "currency": "EUR"},
                    "stockMarketValue": {"amount": 333.33, "currency": "EUR"},
                    "unrealizedPnl": {"amount": 12.34, "currency": "EUR"},
                    "realizedPnl": {"amount": 5.67, "currency": "EUR"},
                    "source": "REAL_BROKER"
                  }]
                }
                """;

        PortfolioSummary imported = portfolioService.importBrokerConnection(userA(), portfolio.portfolioId(), connectionId);
        PortfolioSummary reloaded = portfolioService.getSummary(USER_A, portfolio.portfolioId());

        assertThat(imported.baseCurrency()).isEqualTo("EUR");
        assertThat(imported.cash().currency()).isEqualTo("EUR");
        assertThat(imported.cash().amount()).isEqualByComparingTo("123.45");
        assertThat(reloaded.baseCurrency()).isEqualTo("EUR");
        assertThat(reloaded.cash().currency()).isEqualTo("EUR");
        assertThat(reloaded.cash().amount()).isEqualByComparingTo("123.45");
        assertThat(portfolioService.getPortfolio(USER_A, portfolio.portfolioId()).baseCurrency()).isEqualTo("EUR");
        assertThat(portfolioService.getPositions(USER_A, portfolio.portfolioId()))
                .singleElement()
                .satisfies(position -> {
                    assertThat(position.dataFreshness()).isEqualTo("REAL_BROKER");
                    assertThat(position.brokerType()).isEqualTo("IBKR");
                    assertThat(position.marketValue().currency()).isEqualTo("EUR");
                    assertThat(position.instrument().provider()).isEqualTo("IBKR");
                    assertThat(position.instrument().providerInstrumentId()).isEqualTo("123456");
                    assertThat(position.instrument().brokerSymbol()).isEqualTo("AIXA");
                    assertThat(position.instrument().brokerDescription()).isEqualTo("AIXA");
                    assertThat(position.instrument().brokerExchange()).isEqualTo("AEB");
                    assertThat(position.instrument().canonicalSymbol()).isEqualTo("AIXA");
                    assertThat(position.instrument().canonicalName()).isEqualTo("AIXTRON SE");
                    assertThat(position.instrument().canonicalExchange()).isEqualTo("XETR");
                    assertThat(position.instrument().canonicalMic()).isEqualTo("XETR");
                    assertThat(position.instrument().securityType()).isEqualTo("STK");
                });
        assertThatThrownBy(() -> portfolioService.getPositions(USER_B, portfolio.portfolioId()))
                .isInstanceOf(PortfolioNotFoundException.class);
    }

    @Test
    void realBrokerImportCreatesIdempotentEurValuationSnapshotWithoutInventedInvestedCapital() {
        Portfolio portfolio = portfolioService.createPortfolio(USER_A, "IBKR History", "USD");
        UUID connectionId = UUID.randomUUID();
        brokerSnapshotResponse = realBrokerSnapshotJson("2026-08-24T12:00:00Z", "250.00", "123.45");

        portfolioService.importBrokerConnection(userA(), portfolio.portfolioId(), connectionId);
        portfolioService.importBrokerConnection(userA(), portfolio.portfolioId(), connectionId);

        PortfolioHistory history = portfolioService.getHistory(USER_A, portfolio.portfolioId(), "MAX");

        assertThat(history.baseCurrency()).isEqualTo("EUR");
        assertThat(history.investedCapitalStatus()).isEqualTo("INVESTED_CAPITAL_HISTORY_UNAVAILABLE");
        assertThat(history.backfillAvailable()).isFalse();
        assertThat(history.points()).singleElement().satisfies(point -> {
            assertThat(point.source()).isEqualTo("REAL_BROKER");
            assertThat(point.dataFreshness()).isEqualTo("REAL_BROKER");
            assertThat(point.broker()).isEqualTo("IBKR");
            assertThat(point.portfolioMarketValue().currency()).isEqualTo("EUR");
            assertThat(point.portfolioMarketValue().amount()).isEqualByComparingTo("373.45");
            assertThat(point.positionsMarketValue().amount()).isEqualByComparingTo("250.00");
            assertThat(point.cash().amount()).isEqualByComparingTo("123.45");
            assertThat(point.investedCapital()).isNull();
            assertThat(point.investedCapitalStatus()).isEqualTo("INVESTED_CAPITAL_HISTORY_UNAVAILABLE");
        });
        assertThat(valuationSnapshotRepository.findByPortfolioIdAndUserIdOrderBySnapshotTimestampAsc(portfolio.portfolioId(), USER_A)).hasSize(1);
    }

    @Test
    void realBrokerImportWithNewObservedTimestampCreatesNewValuationSnapshot() {
        Portfolio portfolio = portfolioService.createPortfolio(USER_A, "IBKR History Growth", "USD");
        UUID connectionId = UUID.randomUUID();
        brokerSnapshotResponse = realBrokerSnapshotJson("2026-08-24T12:00:00Z", "250.00", "123.45");

        portfolioService.importBrokerConnection(userA(), portfolio.portfolioId(), connectionId);
        brokerSnapshotResponse = realBrokerSnapshotJson("2026-08-24T12:05:00Z", "251.00", "123.45");
        portfolioService.importBrokerConnection(userA(), portfolio.portfolioId(), connectionId);

        PortfolioHistory history = portfolioService.getHistory(USER_A, portfolio.portfolioId(), "MAX");

        assertThat(history.points()).hasSize(2);
        assertThat(history.points()).extracting(point -> point.timestamp().toString())
                .containsExactly("2026-08-24T12:00:00Z", "2026-08-24T12:05:00Z");
        assertThat(history.points().get(1).portfolioMarketValue().amount()).isEqualByComparingTo("374.45");
    }

    @Test
    void portfolioHistorySupportsAllRangesChronologicalOrderingAndOwnershipIsolation() {
        Portfolio portfolio = portfolioService.createPortfolio(USER_A, "Range History", "EUR");
        Instant now = Instant.now();
        valuationSnapshotRepository.save(snapshot(portfolio.portfolioId(), USER_A, now.minusSeconds(7200), "100.00"));
        valuationSnapshotRepository.save(snapshot(portfolio.portfolioId(), USER_A, now.minusSeconds(3600), "125.00"));
        valuationSnapshotRepository.save(snapshot(portfolio.portfolioId(), USER_A, now, "150.00"));

        for (String range : List.of("1D", "5D", "1W", "1M", "1Y", "2Y", "3Y", "4Y", "5Y", "MAX")) {
            PortfolioHistory history = portfolioService.getHistory(USER_A, portfolio.portfolioId(), range);
            assertThat(history.range().code()).isEqualTo(range);
            assertThat(history.points()).hasSize(3);
            assertThat(history.points()).isSortedAccordingTo(java.util.Comparator.comparing(point -> point.timestamp()));
        }
        assertThatThrownBy(() -> portfolioService.getHistory(USER_B, portfolio.portfolioId(), "MAX"))
                .isInstanceOf(PortfolioNotFoundException.class);
    }

    @Test
    void newPortfolioHistoryWithOneSnapshotDoesNotFabricateEarlierValues() {
        Portfolio portfolio = portfolioService.createPortfolio(USER_A, "One Point", "EUR");
        valuationSnapshotRepository.save(snapshot(portfolio.portfolioId(), USER_A, Instant.now(), "100.00"));

        PortfolioHistory history = portfolioService.getHistory(USER_A, portfolio.portfolioId(), "1M");

        assertThat(history.points()).hasSize(1);
        assertThat(history.points().get(0).portfolioMarketValue().amount()).isEqualByComparingTo("100.00");
    }

    @Test
    void repeatedMockBrokerSyncIsIdempotent() {
        Portfolio portfolio = portfolioService.createPortfolio(USER_A, "Idempotent Portfolio", "EUR");

        portfolioService.sync(USER_A, portfolio.portfolioId());
        portfolioService.sync(USER_A, portfolio.portfolioId());

        assertThat(portfolioService.getPositions(USER_A, portfolio.portfolioId())).hasSize(5);
    }

    @Test
    void repeatedSameBrokerSnapshotCreatesNoDuplicatePositions() {
        Portfolio portfolio = portfolioService.createPortfolio(USER_A, "Repeated Import", "EUR");
        UUID connectionId = UUID.randomUUID();
        brokerSnapshotResponse = realBrokerSnapshotJson("2026-08-24T12:00:00Z", "250.00", "123.45");

        portfolioService.importBrokerConnection(userA(), portfolio.portfolioId(), connectionId);
        portfolioService.importBrokerConnection(userA(), portfolio.portfolioId(), connectionId);

        assertThat(portfolioService.getPositions(USER_A, portfolio.portfolioId())).hasSize(1);
    }

    @Test
    void genericBrokerSyncCreatesAndReusesAccountPortfolioIdempotently() {
        UUID connectionId = UUID.randomUUID();
        brokerSnapshotResponse = realBrokerSnapshotJson("2026-08-24T12:00:00Z", "250.00", "123.45");

        List<PortfolioSummary> first = portfolioService.syncBrokerConnection(userA(), connectionId);
        List<PortfolioSummary> second = portfolioService.syncBrokerConnection(userA(), connectionId);

        assertThat(first).hasSize(1);
        assertThat(second).hasSize(1);
        assertThat(second.get(0).portfolioId()).isEqualTo(first.get(0).portfolioId());
        assertThat(portfolioRepository.findByUserId(USER_A)).filteredOn(p -> connectionId.equals(p.getBrokerConnectionId())).hasSize(1);
        assertThat(portfolioService.getPositions(USER_A, first.get(0).portfolioId())).hasSize(1);
        assertThat(portfolioService.getPortfolio(USER_A, first.get(0).portfolioId()).lastSuccessfulBrokerSyncAt()).isNotNull();
    }

    @Test
    void successiveBrokerSyncsReplaceCurrentAndRetainNormalizedPositionHistory() {
        UUID connectionId = UUID.randomUUID();
        brokerSnapshotResponse = singlePositionSnapshot("IBKR_U1234567", "123456", "BESI", "400.00", "4",
                "2026-08-24T12:00:00Z", "EUR");
        UUID portfolioId = portfolioService.syncBrokerConnection(userA(), connectionId).get(0).portfolioId();

        brokerSnapshotResponse = singlePositionSnapshot("IBKR_U1234567", "123456", "BESI", "500.00", "5",
                "2026-08-24T12:05:00Z", "EUR");
        portfolioService.syncBrokerConnection(userA(), connectionId);

        assertThat(portfolioService.getPositions(USER_A, portfolioId)).singleElement()
                .satisfies(position -> assertThat(position.quantity()).isEqualByComparingTo("5"));
        assertThat(brokerPositionSnapshotRepository.findByUserIdAndPortfolioIdOrderByEffectiveAtAsc(USER_A, portfolioId))
                .satisfiesExactly(
                        snapshot -> assertThat(snapshot.getQuantity()).isEqualByComparingTo("4"),
                        snapshot -> assertThat(snapshot.getQuantity()).isEqualByComparingTo("5"));

        long historyCount = brokerPositionSnapshotRepository.count();
        Instant successfulAt = portfolioService.getPortfolio(USER_A, portfolioId).lastSuccessfulBrokerSyncAt();
        brokerSnapshotResponse = "{}";
        assertThatThrownBy(() -> portfolioService.syncBrokerConnection(userA(), connectionId))
                .isInstanceOf(IllegalStateException.class);
        assertThat(brokerPositionSnapshotRepository.count()).isEqualTo(historyCount);
        assertThat(portfolioService.getPortfolio(USER_A, portfolioId).lastSuccessfulBrokerSyncAt()).isEqualTo(successfulAt);
        assertThat(portfolioService.getPositions(USER_A, portfolioId)).singleElement()
                .satisfies(position -> assertThat(position.quantity()).isEqualByComparingTo("5"));
    }

    @Test
    void genericBrokerSyncCreatesSeparatePortfoliosForMultipleAccounts() {
        UUID connectionId = UUID.randomUUID();
        brokerSnapshotResponse = multiAccountSnapshot(true, true);

        List<PortfolioSummary> summaries = portfolioService.syncBrokerConnection(userA(), connectionId);

        assertThat(summaries).hasSize(2);
        assertThat(summaries).extracting(PortfolioSummary::portfolioId).doesNotHaveDuplicates();
        assertThat(portfolioRepository.findByUserId(USER_A)).filteredOn(p -> connectionId.equals(p.getBrokerConnectionId())).hasSize(2);
    }

    @Test
    void failedGenericSnapshotPreservesLastKnownGoodHoldings() {
        UUID connectionId = UUID.randomUUID();
        brokerSnapshotResponse = realBrokerSnapshotJson("2026-08-24T12:00:00Z", "250.00", "123.45");
        UUID portfolioId = portfolioService.syncBrokerConnection(userA(), connectionId).get(0).portfolioId();
        brokerSnapshotResponse = "{}";

        assertThatThrownBy(() -> portfolioService.syncBrokerConnection(userA(), connectionId))
                .isInstanceOf(IllegalStateException.class);
        assertThat(portfolioService.getPositions(USER_A, portfolioId)).singleElement()
                .satisfies(position -> {
                    assertThat(position.active()).isTrue();
                    assertThat(position.marketValue().amount()).isEqualByComparingTo("250.00");
                });
    }

    @Test
    void currencyTotalsAggregateSameCurrencyAndNeverCombineDifferentCurrencies() {
        var before = portfolioService.currencyTotals(USER_A);
        Portfolio eurOne = portfolioService.createPortfolio(USER_A, "EUR One", "EUR");
        Portfolio eurTwo = portfolioService.createPortfolio(USER_A, "EUR Two", "EUR");
        Portfolio inr = portfolioService.createPortfolio(USER_A, "INR One", "INR");
        portfolioService.sync(USER_A, eurOne.portfolioId());
        portfolioService.sync(USER_A, eurTwo.portfolioId());
        portfolioService.sync(USER_A, inr.portfolioId());

        var totals = portfolioService.currencyTotals(USER_A);

        assertThat(totals).containsKeys("EUR", "INR");
        assertThat(totals.get("EUR").subtract(before.getOrDefault("EUR", java.math.BigDecimal.ZERO))).isEqualByComparingTo(
                portfolioService.getSummary(USER_A, eurOne.portfolioId()).totalMarketValue().amount().multiply(java.math.BigDecimal.valueOf(2)));
        assertThat(totals.get("INR").subtract(before.getOrDefault("INR", java.math.BigDecimal.ZERO))).isEqualByComparingTo(
                portfolioService.getSummary(USER_A, inr.portfolioId()).totalMarketValue().amount());
    }

    @Test
    void updatedBrokerSnapshotUpdatesExistingPosition() {
        Portfolio portfolio = portfolioService.createPortfolio(USER_A, "Updated Import", "EUR");
        UUID connectionId = UUID.randomUUID();
        brokerSnapshotResponse = realBrokerSnapshotJson("2026-08-24T12:00:00Z", "250.00", "123.45");
        portfolioService.importBrokerConnection(userA(), portfolio.portfolioId(), connectionId);

        brokerSnapshotResponse = realBrokerSnapshotJson("2026-08-24T12:05:00Z", "275.00", "123.45");
        portfolioService.importBrokerConnection(userA(), portfolio.portfolioId(), connectionId);

        assertThat(portfolioService.getPositions(USER_A, portfolio.portfolioId()))
                .singleElement()
                .satisfies(position -> assertThat(position.marketValue().amount()).isEqualByComparingTo("275.00"));
    }

    @Test
    void brokerImportDoesNotDeleteManualPosition() {
        Portfolio portfolio = portfolioService.createPortfolio(USER_A, "Manual Safety", "EUR");
        insertManualPosition(portfolio.portfolioId(), USER_A, "MANUAL_A", "MANUAL-INST-A", "MANUAL");
        UUID connectionId = UUID.randomUUID();
        brokerSnapshotResponse = realBrokerSnapshotJson("2026-08-24T12:00:00Z", "250.00", "123.45");

        portfolioService.importBrokerConnection(userA(), portfolio.portfolioId(), connectionId);

        assertThat(portfolioService.getPositions(USER_A, portfolio.portfolioId()))
                .extracting("sourceType")
                .contains("MANUAL", "BROKER");
    }

    @Test
    void oneBrokerConnectionDoesNotModifyAnotherConnection() {
        Portfolio portfolio = portfolioService.createPortfolio(USER_A, "Connection Isolation", "EUR");
        UUID firstConnection = UUID.randomUUID();
        UUID secondConnection = UUID.randomUUID();
        brokerSnapshotResponse = realBrokerSnapshotJson("2026-08-24T12:00:00Z", "250.00", "123.45");
        portfolioService.importBrokerConnection(userA(), portfolio.portfolioId(), firstConnection);

        brokerSnapshotResponse = realBrokerSnapshotJson("2026-08-24T12:05:00Z", "275.00", "123.45");
        portfolioService.importBrokerConnection(userA(), portfolio.portfolioId(), secondConnection);

        assertThat(portfolioService.getPositions(USER_A, portfolio.portfolioId()))
                .hasSize(2)
                .extracting(position -> position.marketValue().amount())
                .containsExactlyInAnyOrder(new java.math.BigDecimal("250.0000"), new java.math.BigDecimal("275.0000"));
    }

    @Test
    void oneBrokerAccountDoesNotModifyAnotherAccount() {
        Portfolio portfolio = portfolioService.createPortfolio(USER_A, "Account Isolation", "EUR");
        UUID connectionId = UUID.randomUUID();
        brokerSnapshotResponse = multiAccountSnapshot(true, true);
        portfolioService.importBrokerConnection(userA(), portfolio.portfolioId(), connectionId);

        brokerSnapshotResponse = multiAccountSnapshot(false, true);
        portfolioService.importBrokerConnection(userA(), portfolio.portfolioId(), connectionId);

        assertThat(portfolioService.getPositions(USER_A, portfolio.portfolioId()))
                .filteredOn(position -> position.brokerAccountId().equals("IBKR_U2222222"))
                .singleElement()
                .satisfies(position -> assertThat(position.active()).isTrue());
        assertThat(portfolioService.getPositions(USER_A, portfolio.portfolioId()))
                .filteredOn(position -> position.brokerAccountId().equals("IBKR_U1111111"))
                .singleElement()
                .satisfies(position -> {
                    assertThat(position.active()).isFalse();
                    assertThat(position.dataFreshness()).isEqualTo("STALE_BROKER");
                });
    }

    @Test
    void userACannotModifyUserBPositions() {
        Portfolio userBPortfolio = portfolioService.createPortfolio(USER_B, "User B Portfolio", "EUR");
        insertManualPosition(userBPortfolio.portfolioId(), USER_B, "MANUAL_B", "MANUAL-INST-B", "MANUAL_B");
        UUID connectionId = UUID.randomUUID();
        brokerSnapshotResponse = realBrokerSnapshotJson("2026-08-24T12:00:00Z", "250.00", "123.45");

        assertThatThrownBy(() -> portfolioService.importBrokerConnection(userA(), userBPortfolio.portfolioId(), connectionId))
                .isInstanceOf(PortfolioNotFoundException.class);

        assertThat(portfolioService.getPositions(USER_B, userBPortfolio.portfolioId()))
                .singleElement()
                .satisfies(position -> assertThat(position.sourceType()).isEqualTo("MANUAL"));
    }

    @Test
    void sameTickerWithDifferentProviderNativeInstrumentIdDoesNotCollide() {
        Portfolio portfolio = portfolioService.createPortfolio(USER_A, "Instrument Identity", "EUR");
        UUID firstConnection = UUID.randomUUID();
        brokerSnapshotResponse = singlePositionSnapshot("IBKR_U1234567", "123456", "AIXA", "250.00", "10", "2026-08-24T12:00:00Z", "EUR");
        portfolioService.importBrokerConnection(userA(), portfolio.portfolioId(), firstConnection);

        brokerSnapshotResponse = singlePositionSnapshot("IBKR_U1234567", "789012", "AIXA", "300.00", "10", "2026-08-24T12:05:00Z", "EUR");
        portfolioService.importBrokerConnection(userA(), portfolio.portfolioId(), firstConnection);

        assertThat(portfolioService.getPositions(USER_A, portfolio.portfolioId()))
                .hasSize(2)
                .extracting(position -> position.instrument().providerInstrumentId())
                .containsExactlyInAnyOrder("123456", "789012");
    }

    @Test
    void fractionalQuantitiesSurvivePersistence() {
        Portfolio portfolio = portfolioService.createPortfolio(USER_A, "Fractions", "EUR");
        UUID connectionId = UUID.randomUUID();
        brokerSnapshotResponse = singlePositionSnapshot("IBKR_U1234567", "123456", "AIXA", "78.55", "31.4195", "2026-08-24T12:00:00Z", "EUR");

        portfolioService.importBrokerConnection(userA(), portfolio.portfolioId(), connectionId);

        assertThat(portfolioService.getPositions(USER_A, portfolio.portfolioId()))
                .singleElement()
                .satisfies(position -> assertThat(position.quantity()).isEqualByComparingTo("31.4195"));
    }

    @Test
    void multipleCashCurrenciesForOneAccountCoexist() {
        Portfolio portfolio = portfolioService.createPortfolio(USER_A, "Multi Cash", "EUR");
        UUID connectionId = UUID.randomUUID();
        brokerSnapshotResponse = multiCurrencyCashSnapshot();

        PortfolioSummary summary = portfolioService.importBrokerConnection(userA(), portfolio.portfolioId(), connectionId);

        assertThat(summary.cash().currency()).isEqualTo("EUR");
        assertThat(jdbcTemplate.queryForObject("""
                SELECT COUNT(*) FROM portfolio.broker_account_cash_balance_entries
                WHERE user_id = ? AND connection_id = ? AND broker_account_id = ?
                """, Integer.class, USER_A, connectionId, "IBKR_U1234567")).isEqualTo(3);
    }

    @Test
    void missingBrokerPositionBecomesStaleOnlyWithinOwnSourceScope() {
        Portfolio portfolio = portfolioService.createPortfolio(USER_A, "Stale Scope", "EUR");
        UUID firstConnection = UUID.randomUUID();
        UUID secondConnection = UUID.randomUUID();
        brokerSnapshotResponse = twoPositionSnapshot("IBKR_U1234567");
        portfolioService.importBrokerConnection(userA(), portfolio.portfolioId(), firstConnection);
        brokerSnapshotResponse = singlePositionSnapshot("IBKR_U1234567", "123456", "AIXA", "250.00", "10", "2026-08-24T12:05:00Z", "EUR");
        portfolioService.importBrokerConnection(userA(), portfolio.portfolioId(), secondConnection);

        brokerSnapshotResponse = singlePositionSnapshot("IBKR_U1234567", "123456", "AIXA", "250.00", "10", "2026-08-24T12:10:00Z", "EUR");
        portfolioService.importBrokerConnection(userA(), portfolio.portfolioId(), firstConnection);

        assertThat(portfolioService.getPositions(USER_A, portfolio.portfolioId()))
                .filteredOn(position -> position.instrument().providerInstrumentId().equals("654321"))
                .singleElement()
                .satisfies(position -> assertThat(position.active()).isFalse());
        assertThat(portfolioService.getPositions(USER_A, portfolio.portfolioId()))
                .filteredOn(position -> position.active() && position.instrument().providerInstrumentId().equals("123456"))
                .hasSize(2);
    }

    @Test
    void firstRealBrokerSyncAdoptsLegacyNullConnectionPositionWithoutDuplicate() {
        Portfolio portfolio = portfolioService.createPortfolio(USER_A, "Legacy Adopt", "EUR");
        UUID legacyPositionId = UUID.randomUUID();
        UUID connectionId = UUID.randomUUID();
        insertLegacyBrokerPosition(legacyPositionId, portfolio.portfolioId(), USER_A, "LEGACY_ACCOUNT_A",
                "IBKR_LEGACY_A", "IBKR", "LEGACY-001", "7.00000000", "140.0000", null);
        brokerSnapshotResponse = singlePositionSnapshot("IBKR_LEGACY_A", "LEGACY-001", "AIXA",
                "250.00", "10", "2026-08-24T12:00:00Z", "EUR");

        portfolioService.importBrokerConnection(userA(), portfolio.portfolioId(), connectionId);

        assertThat(portfolioService.getPositions(USER_A, portfolio.portfolioId()))
                .singleElement()
                .satisfies(position -> {
                    assertThat(position.positionId()).isEqualTo(legacyPositionId);
                    assertThat(position.quantity()).isEqualByComparingTo("10");
                    assertThat(position.marketValue().amount()).isEqualByComparingTo("250.00");
                });
        assertThat(positionCount(portfolio.portfolioId())).isEqualTo(1);
        assertThat(positionConnectionId(legacyPositionId)).isEqualTo(connectionId);
    }

    @Test
    void secondSyncAfterLegacyAdoptionUpdatesSamePosition() {
        Portfolio portfolio = portfolioService.createPortfolio(USER_A, "Legacy Idempotent", "EUR");
        UUID legacyPositionId = UUID.randomUUID();
        UUID connectionId = UUID.randomUUID();
        insertLegacyBrokerPosition(legacyPositionId, portfolio.portfolioId(), USER_A, "LEGACY_ACCOUNT_B",
                "IBKR_LEGACY_B", "IBKR", "LEGACY-002", "7.00000000", "140.0000", null);
        brokerSnapshotResponse = singlePositionSnapshot("IBKR_LEGACY_B", "LEGACY-002", "AIXA",
                "250.00", "10", "2026-08-24T12:00:00Z", "EUR");

        portfolioService.importBrokerConnection(userA(), portfolio.portfolioId(), connectionId);
        portfolioService.importBrokerConnection(userA(), portfolio.portfolioId(), connectionId);

        assertThat(positionCount(portfolio.portfolioId())).isEqualTo(1);
        assertThat(portfolioService.getPositions(USER_A, portfolio.portfolioId()))
                .singleElement()
                .satisfies(position -> assertThat(position.positionId()).isEqualTo(legacyPositionId));
    }

    @Test
    void differentConnectionCannotStealAlreadyScopedPosition() {
        Portfolio portfolio = portfolioService.createPortfolio(USER_A, "Scoped Existing", "EUR");
        UUID existingConnection = UUID.randomUUID();
        UUID incomingConnection = UUID.randomUUID();
        UUID existingPositionId = UUID.randomUUID();
        insertLegacyBrokerPosition(existingPositionId, portfolio.portfolioId(), USER_A, "SCOPED_ACCOUNT_A",
                "IBKR_SCOPED_A", "IBKR", "SCOPED-001", "7.00000000", "140.0000", existingConnection);
        brokerSnapshotResponse = singlePositionSnapshot("IBKR_SCOPED_A", "SCOPED-001", "AIXA",
                "250.00", "10", "2026-08-24T12:00:00Z", "EUR");

        portfolioService.importBrokerConnection(userA(), portfolio.portfolioId(), incomingConnection);

        assertThat(positionCount(portfolio.portfolioId())).isEqualTo(2);
        assertThat(positionConnectionId(existingPositionId)).isEqualTo(existingConnection);
    }

    @Test
    void anotherUsersLegacyPositionCannotBeAdopted() {
        Portfolio userAPortfolio = portfolioService.createPortfolio(USER_A, "User A Import", "EUR");
        Portfolio userBPortfolio = portfolioService.createPortfolio(USER_B, "User B Legacy", "EUR");
        UUID userBLegacyPositionId = UUID.randomUUID();
        UUID connectionId = UUID.randomUUID();
        insertLegacyBrokerPosition(userBLegacyPositionId, userBPortfolio.portfolioId(), USER_B, "USER_B_LEGACY_ACCOUNT",
                "IBKR_USER_SHARED", "IBKR", "USER-SHARED-001", "7.00000000", "140.0000", null);
        brokerSnapshotResponse = singlePositionSnapshot("IBKR_USER_SHARED", "USER-SHARED-001", "AIXA",
                "250.00", "10", "2026-08-24T12:00:00Z", "EUR");

        portfolioService.importBrokerConnection(userA(), userAPortfolio.portfolioId(), connectionId);

        assertThat(positionCount(userAPortfolio.portfolioId())).isEqualTo(1);
        assertThat(positionConnectionId(userBLegacyPositionId)).isNull();
    }

    @Test
    void anotherPortfolioLegacyPositionCannotBeAdopted() {
        Portfolio importPortfolio = portfolioService.createPortfolio(USER_A, "Import Portfolio", "EUR");
        Portfolio legacyPortfolio = portfolioService.createPortfolio(USER_A, "Other Legacy Portfolio", "EUR");
        UUID legacyPositionId = UUID.randomUUID();
        UUID connectionId = UUID.randomUUID();
        insertLegacyBrokerPosition(legacyPositionId, legacyPortfolio.portfolioId(), USER_A, "OTHER_PORTFOLIO_ACCOUNT",
                "IBKR_PORTFOLIO_SHARED", "IBKR", "PORTFOLIO-SHARED-001", "7.00000000", "140.0000", null);
        brokerSnapshotResponse = singlePositionSnapshot("IBKR_PORTFOLIO_SHARED", "PORTFOLIO-SHARED-001", "AIXA",
                "250.00", "10", "2026-08-24T12:00:00Z", "EUR");

        portfolioService.importBrokerConnection(userA(), importPortfolio.portfolioId(), connectionId);

        assertThat(positionCount(importPortfolio.portfolioId())).isEqualTo(1);
        assertThat(positionConnectionId(legacyPositionId)).isNull();
    }

    @Test
    void differentProviderNativeInstrumentIdDoesNotAdoptLegacyPosition() {
        Portfolio portfolio = portfolioService.createPortfolio(USER_A, "Different Native Id", "EUR");
        UUID legacyPositionId = UUID.randomUUID();
        UUID connectionId = UUID.randomUUID();
        insertLegacyBrokerPosition(legacyPositionId, portfolio.portfolioId(), USER_A, "LEGACY_ACCOUNT_C",
                "IBKR_LEGACY_C", "IBKR", "LEGACY-003", "7.00000000", "140.0000", null);
        brokerSnapshotResponse = singlePositionSnapshot("IBKR_LEGACY_C", "LEGACY-004", "AIXA",
                "250.00", "10", "2026-08-24T12:00:00Z", "EUR");

        portfolioService.importBrokerConnection(userA(), portfolio.portfolioId(), connectionId);

        assertThat(positionCount(portfolio.portfolioId())).isEqualTo(2);
        assertThat(positionConnectionId(legacyPositionId)).isNull();
    }

    @Test
    void fractionalQuantitySurvivesLegacyAdoption() {
        Portfolio portfolio = portfolioService.createPortfolio(USER_A, "Legacy Fractions", "EUR");
        UUID connectionId = UUID.randomUUID();
        insertLegacyBrokerPosition(UUID.randomUUID(), portfolio.portfolioId(), USER_A, "LEGACY_ACCOUNT_D",
                "IBKR_LEGACY_D", "IBKR", "LEGACY-005", "7.00000000", "140.0000", null);
        brokerSnapshotResponse = singlePositionSnapshot("IBKR_LEGACY_D", "LEGACY-005", "AIXA",
                "78.55", "31.4195", "2026-08-24T12:00:00Z", "EUR");

        portfolioService.importBrokerConnection(userA(), portfolio.portfolioId(), connectionId);

        assertThat(portfolioService.getPositions(USER_A, portfolio.portfolioId()))
                .singleElement()
                .satisfies(position -> assertThat(position.quantity()).isEqualByComparingTo("31.4195"));
    }

    @Test
    void ambiguousLegacyPositionFailsWithoutChoosingOne() {
        Portfolio portfolio = portfolioService.createPortfolio(USER_A, "Ambiguous Legacy", "EUR");
        UUID firstLegacyPositionId = UUID.randomUUID();
        UUID secondLegacyPositionId = UUID.randomUUID();
        UUID connectionId = UUID.randomUUID();
        insertLegacyBrokerPosition(firstLegacyPositionId, portfolio.portfolioId(), USER_A, "AMBIG_ACCOUNT",
                "IBKR_AMBIG", "IBKR", "AMBIG-001", "7.00000000", "140.0000", null);
        insertLegacyBrokerPosition(secondLegacyPositionId, portfolio.portfolioId(), USER_A, "AMBIG_ACCOUNT",
                "IBKR_AMBIG", "IBKR", "AMBIG-001", "8.00000000", "160.0000", null);
        brokerSnapshotResponse = singlePositionSnapshot("IBKR_AMBIG", "AMBIG-001", "AIXA",
                "250.00", "10", "2026-08-24T12:00:00Z", "EUR");

        assertThatThrownBy(() -> portfolioService.importBrokerConnection(userA(), portfolio.portfolioId(), connectionId))
                .isInstanceOf(IllegalStateException.class)
                .hasMessageContaining("Ambiguous legacy broker position");
        assertThat(positionConnectionId(firstLegacyPositionId)).isNull();
        assertThat(positionConnectionId(secondLegacyPositionId)).isNull();
    }

    @Test
    void legacyBrokerAccountConnectionAdoptionIsScopedByUserBrokerAndSourceAccount() {
        Portfolio portfolio = portfolioService.createPortfolio(USER_A, "Legacy Account Adopt", "EUR");
        UUID connectionId = UUID.randomUUID();
        insertLegacyBrokerAccount("USER_A_LEGACY_ACCOUNT_ADOPT", USER_A, "IBKR_ACCOUNT_ADOPT", "IBKR");
        insertLegacyBrokerAccount("USER_B_LEGACY_ACCOUNT_ADOPT", USER_B, "IBKR_ACCOUNT_ADOPT", "IBKR");
        brokerSnapshotResponse = singlePositionSnapshot("IBKR_ACCOUNT_ADOPT", "ACCOUNT-ADOPT-001", "AIXA",
                "250.00", "10", "2026-08-24T12:00:00Z", "EUR");

        portfolioService.importBrokerConnection(userA(), portfolio.portfolioId(), connectionId);

        assertThat(accountConnectionId("USER_A_LEGACY_ACCOUNT_ADOPT")).isEqualTo(connectionId);
        assertThat(accountConnectionId("USER_B_LEGACY_ACCOUNT_ADOPT")).isNull();
        assertThat(brokerAccountCount(USER_A, "IBKR_ACCOUNT_ADOPT")).isEqualTo(1);
    }

    @Test
    void persistedRealBrokerPortfolioIsAdoptedWithoutAuthenticationOrPositionDuplication() {
        Portfolio portfolio = portfolioService.createPortfolio(USER_A, "Phase 5B style import", "EUR");
        UUID connectionId = UUID.randomUUID();
        for (int index = 0; index < 7; index++) {
            insertLegacyBrokerPosition(UUID.randomUUID(), portfolio.portfolioId(), USER_A,
                    "ADOPT_ACCOUNT_ROW", "IBKR_ADOPT_ACCOUNT", "IBKR", "ADOPT-" + index,
                    "1.00000000", "25.0000", connectionId);
        }

        portfolioService.listPortfolioSummaries(USER_A);
        portfolioService.listPortfolioSummaries(USER_A);

        Portfolio adopted = portfolioService.getPortfolio(USER_A, portfolio.portfolioId());
        assertThat(adopted.portfolioId()).isEqualTo(portfolio.portfolioId());
        assertThat(adopted.brokerConnectionId()).isEqualTo(connectionId);
        assertThat(adopted.brokerProvider()).isEqualTo(BrokerType.IBKR);
        assertThat(positionCount(portfolio.portfolioId())).isEqualTo(7);
    }

    @Test
    void ambiguousPersistedBrokerSourcesAreNotAdopted() {
        Portfolio portfolio = portfolioService.createPortfolio(USER_A, "Ambiguous persisted sources", "EUR");
        insertLegacyBrokerPosition(UUID.randomUUID(), portfolio.portfolioId(), USER_A, "AMBIG_ROW_ONE",
                "IBKR_ONE", "IBKR", "AMBIG-SOURCE-ONE", "1", "25", UUID.randomUUID());
        insertLegacyBrokerPosition(UUID.randomUUID(), portfolio.portfolioId(), USER_A, "AMBIG_ROW_TWO",
                "IBKR_TWO", "IBKR", "AMBIG-SOURCE-TWO", "1", "25", UUID.randomUUID());

        portfolioService.listPortfolioSummaries(USER_A);

        assertThat(portfolioService.getPortfolio(USER_A, portfolio.portfolioId()).brokerConnectionId()).isNull();
        assertThat(positionCount(portfolio.portfolioId())).isEqualTo(2);
    }

    @Test
    void listingOneUserCannotAdoptAnotherUsersLegacyPortfolio() {
        Portfolio userBPortfolio = portfolioService.createPortfolio(USER_B, "User B persisted broker", "EUR");
        insertLegacyBrokerPosition(UUID.randomUUID(), userBPortfolio.portfolioId(), USER_B, "USER_B_ADOPT_ROW",
                "IBKR_USER_B", "IBKR", "USER-B-ADOPT", "1", "25", UUID.randomUUID());

        portfolioService.listPortfolioSummaries(USER_A);

        assertThat(portfolioService.getPortfolio(USER_B, userBPortfolio.portfolioId()).brokerConnectionId()).isNull();
        assertThat(positionCount(userBPortfolio.portfolioId())).isEqualTo(1);
    }

    @Test
    void portfolioListReturnsSummariesAndEventsAreSerializable() throws Exception {
        Portfolio portfolio = portfolioService.createPortfolio(USER_A, "Serializable Events", "EUR");
        portfolioService.sync(USER_A, portfolio.portfolioId());

        assertThat(portfolioService.listPortfolioSummaries(USER_A)).anySatisfy(summary ->
                assertThat(summary.portfolioId()).isEqualTo(portfolio.portfolioId()));

        String json = objectMapper.writeValueAsString(BrokerSyncEvent.started(null, portfolio.portfolioId(), "test-correlation"));
        assertThat(json).contains("broker.sync.started").contains("test-correlation");
    }

    private static AuthenticatedUser userA() {
        return new AuthenticatedUser(USER_A, "test", "user-a", "user.a@example.invalid", "User A", List.of("USER"));
    }

    private void insertManualPosition(UUID portfolioId, UUID userId, String accountId, String instrumentKey, String ticker) {
        Instant now = Instant.now();
        jdbcTemplate.update("""
                INSERT INTO portfolio.broker_accounts
                    (broker_account_id, user_id, connection_id, broker_type, source_broker_account_id,
                     external_account_reference, display_name, base_currency, status)
                VALUES (?, ?, NULL, 'MOCK', ?, ?, ?, 'EUR', 'ACTIVE')
                """, accountId, userId, accountId, accountId, "Manual Account");
        UUID instrumentId = UUID.nameUUIDFromBytes(instrumentKey.getBytes(java.nio.charset.StandardCharsets.UTF_8));
        jdbcTemplate.update("""
                INSERT INTO portfolio.instruments
                    (instrument_id, provider, provider_instrument_id, isin, ticker, exchange, mic, company_name,
                     asset_type, country, trading_currency, sector, industry)
                VALUES (?, NULL, NULL, NULL, ?, 'MANUAL', NULL, ?, 'EQUITY', 'DE', 'EUR', NULL, NULL)
                """, instrumentId, ticker, ticker + " Manual");
        jdbcTemplate.update("""
                INSERT INTO portfolio.portfolio_positions
                    (position_id, portfolio_id, instrument_id, quantity, average_cost_amount, average_cost_currency,
                     current_price_amount, current_price_currency, broker_account_id, last_updated,
                     data_freshness, source_type, source_broker_type, source_broker_account_id, observed_at, active)
                VALUES (?, ?, ?, 1.00000000, 10.00000000, 'EUR', 12.00000000, 'EUR', ?, ?, 'MANUAL',
                        'MANUAL', 'MANUAL', ?, ?, TRUE)
                """, UUID.randomUUID(), portfolioId, instrumentId, accountId, Timestamp.from(now), accountId, Timestamp.from(now));
    }

    private UUID insertImportedHdfcPosition(UUID portfolioId, UUID userId, String symbol) {
        String accountId = "hdfc-manual-" + UUID.randomUUID();
        jdbcTemplate.update("""
                INSERT INTO portfolio.broker_accounts
                    (broker_account_id, user_id, connection_id, broker_type, source_broker_account_id,
                     external_account_reference, display_name, base_currency, status)
                VALUES (?, ?, NULL, 'HDFC_SECURITIES', 'HDFC-ACCOUNT', 'HDFC-ACCOUNT', 'HDFC Account', 'INR', 'ACTIVE')
                """, accountId, userId);
        String providerInstrumentId = "SYMBOL:" + symbol;
        List<UUID> existingInstrumentIds = jdbcTemplate.query("""
                SELECT instrument_id FROM portfolio.instruments
                WHERE provider = 'HDFC_SECURITIES' AND provider_instrument_id = ?
                """, (resultSet, rowNumber) -> resultSet.getObject("instrument_id", UUID.class), providerInstrumentId);
        UUID instrumentId;
        if (existingInstrumentIds.isEmpty()) {
            instrumentId = UUID.randomUUID();
            jdbcTemplate.update("""
                    INSERT INTO portfolio.instruments
                        (instrument_id, provider, provider_instrument_id, isin, ticker, exchange, mic, company_name,
                         asset_type, country, trading_currency, sector, industry, broker_symbol)
                    VALUES (?, 'HDFC_SECURITIES', ?, NULL, ?, 'NSE', NULL, ?, 'EQUITY', 'IN', 'INR', NULL, NULL, ?)
                    """, instrumentId, providerInstrumentId, symbol, symbol, symbol);
        } else {
            instrumentId = existingInstrumentIds.get(0);
        }
        UUID positionId = UUID.randomUUID();
        Instant now = Instant.now();
        jdbcTemplate.update("""
                INSERT INTO portfolio.portfolio_positions
                    (position_id, portfolio_id, instrument_id, quantity, average_cost_amount, average_cost_currency,
                     current_price_amount, current_price_currency, broker_account_id, last_updated,
                     data_freshness, source_type, source_broker_type, source_broker_account_id,
                     external_instrument_provider, external_instrument_id, observed_at, active)
                VALUES (?, ?, ?, 7, 100, 'INR', 120, 'INR', ?, ?, 'IMPORTED_SNAPSHOT',
                        'MANUAL_CSV_IMPORT', 'HDFC_SECURITIES', 'HDFC-ACCOUNT', 'HDFC_SECURITIES', ?, ?, TRUE)
                """, positionId, portfolioId, instrumentId, accountId, Timestamp.from(now),
                providerInstrumentId, Timestamp.from(now));
        return positionId;
    }

    private void insertLegacyBrokerPosition(UUID positionId, UUID portfolioId, UUID userId, String accountRowId,
                                            String sourceBrokerAccountId, String provider,
                                            String externalInstrumentId, String quantity, String marketValue,
                                            UUID sourceConnectionId) {
        insertLegacyBrokerAccount(accountRowId, userId, sourceBrokerAccountId, provider);
        UUID instrumentId = UUID.nameUUIDFromBytes((provider + "|" + externalInstrumentId)
                .getBytes(java.nio.charset.StandardCharsets.UTF_8));
        Integer instrumentCount = jdbcTemplate.queryForObject("""
                SELECT COUNT(*) FROM portfolio.instruments
                WHERE provider = ? AND provider_instrument_id = ?
                """, Integer.class, provider, externalInstrumentId);
        if (instrumentCount == null || instrumentCount == 0) {
            jdbcTemplate.update("""
                    INSERT INTO portfolio.instruments
                        (instrument_id, provider, provider_instrument_id, isin, ticker, exchange, mic, company_name,
                         asset_type, country, trading_currency, sector, industry)
                    VALUES (?, ?, ?, NULL, ?, 'XETR', 'XETR', ?, 'EQUITY', 'DE', 'EUR', NULL, NULL)
                    """, instrumentId, provider, externalInstrumentId, "AIXA", "AIXA AG");
        }
        Instant now = Instant.now();
        jdbcTemplate.update("""
                INSERT INTO portfolio.portfolio_positions
                    (position_id, portfolio_id, instrument_id, quantity, average_cost_amount, average_cost_currency,
                     current_price_amount, current_price_currency, market_value_amount, market_value_currency,
                     unrealized_profit_loss_amount, unrealized_profit_loss_currency, broker_account_id, last_updated,
                     data_freshness, source_type, source_connection_id, source_broker_type, source_broker_account_id,
                     external_instrument_provider, external_instrument_id, observed_at, active)
                VALUES (?, ?, ?, ?, 20.00000000, 'EUR', 25.00000000, 'EUR', ?, 'EUR',
                        10.0000, 'EUR', ?, ?, 'REAL_BROKER', 'BROKER', ?, ?, ?, ?, ?, ?, TRUE)
                """, positionId, portfolioId, instrumentId, new java.math.BigDecimal(quantity),
                new java.math.BigDecimal(marketValue), accountRowId, Timestamp.from(now), sourceConnectionId,
                provider, sourceBrokerAccountId, provider, externalInstrumentId, Timestamp.from(now));
    }

    private void insertLegacyBrokerAccount(String accountRowId, UUID userId, String sourceBrokerAccountId,
                                           String provider) {
        Integer accountCount = jdbcTemplate.queryForObject("""
                SELECT COUNT(*) FROM portfolio.broker_accounts
                WHERE broker_account_id = ?
                """, Integer.class, accountRowId);
        if (accountCount != null && accountCount > 0) {
            return;
        }
        jdbcTemplate.update("""
                INSERT INTO portfolio.broker_accounts
                    (broker_account_id, user_id, connection_id, broker_type, source_broker_account_id,
                     external_account_reference, display_name, base_currency, status)
                VALUES (?, ?, NULL, ?, ?, ?, ?, 'EUR', 'ACTIVE')
                """, accountRowId, userId, provider, sourceBrokerAccountId, sourceBrokerAccountId,
                "Legacy " + sourceBrokerAccountId);
    }

    private int positionCount(UUID portfolioId) {
        return jdbcTemplate.queryForObject("""
                SELECT COUNT(*) FROM portfolio.portfolio_positions
                WHERE portfolio_id = ?
                """, Integer.class, portfolioId);
    }

    private UUID positionConnectionId(UUID positionId) {
        return jdbcTemplate.queryForObject("""
                SELECT source_connection_id FROM portfolio.portfolio_positions
                WHERE position_id = ?
                """, UUID.class, positionId);
    }

    private UUID accountConnectionId(String brokerAccountId) {
        return jdbcTemplate.queryForObject("""
                SELECT connection_id FROM portfolio.broker_accounts
                WHERE broker_account_id = ?
                """, UUID.class, brokerAccountId);
    }

    private int brokerAccountCount(UUID userId, String sourceBrokerAccountId) {
        return jdbcTemplate.queryForObject("""
                SELECT COUNT(*) FROM portfolio.broker_accounts
                WHERE user_id = ? AND broker_type = 'IBKR' AND source_broker_account_id = ?
                """, Integer.class, userId, sourceBrokerAccountId);
    }

    private static PortfolioValuationSnapshotEntity snapshot(UUID portfolioId, UUID userId, Instant timestamp, String value) {
        return new PortfolioValuationSnapshotEntity(
                UUID.randomUUID(),
                portfolioId,
                userId,
                timestamp,
                "EUR",
                null,
                null,
                "INVESTED_CAPITAL_HISTORY_UNAVAILABLE",
                new java.math.BigDecimal("10.00"),
                "EUR",
                new java.math.BigDecimal(value).subtract(new java.math.BigDecimal("10.00")),
                "EUR",
                new java.math.BigDecimal(value),
                "EUR",
                new java.math.BigDecimal("5.00"),
                "EUR",
                java.math.BigDecimal.ZERO,
                "EUR",
                "IBKR",
                "REAL_BROKER",
                "REAL_BROKER",
                "test|" + timestamp,
                Instant.now());
    }

    private static String realBrokerSnapshotJson(String observedAt, String marketValue, String cash) {
        return singlePositionSnapshot("IBKR_U1234567", "123456", "AIXA", marketValue, "10", observedAt, "EUR")
                .replace("\"cash\": {\"amount\": 123.45, \"currency\": \"EUR\"}",
                        "\"cash\": {\"amount\": %s, \"currency\": \"EUR\"}".formatted(cash));
    }

    private static String singlePositionSnapshot(String accountId, String conid, String ticker, String marketValue,
                                                 String quantity, String observedAt, String currency) {
        return """
                {
                  "accounts": [{
                    "brokerAccountId": "%s",
                    "brokerType": "IBKR",
                    "externalAccountReference": "****4567",
                    "displayName": "Main IBKR",
                    "baseCurrency": "%s",
                    "status": "ACTIVE"
                  }],
                  "positions": [%s],
                  "cashBalances": [{
                    "brokerAccountId": "%s",
                    "cash": {"amount": 123.45, "currency": "EUR"},
                    "settledCash": {"amount": 120.00, "currency": "EUR"},
                    "netLiquidationValue": {"amount": 456.78, "currency": "EUR"},
                    "stockMarketValue": {"amount": 333.33, "currency": "EUR"},
                    "unrealizedPnl": {"amount": 12.34, "currency": "EUR"},
                    "realizedPnl": {"amount": 5.67, "currency": "EUR"},
                    "source": "REAL_BROKER"
                  }]
                }
                """.formatted(accountId, currency,
                positionJson(accountId, conid, ticker, marketValue, quantity, observedAt, currency), accountId);
    }

    private static String twoPositionSnapshot(String accountId) {
        return """
                {
                  "accounts": [{
                    "brokerAccountId": "%s",
                    "brokerType": "IBKR",
                    "externalAccountReference": "****4567",
                    "displayName": "Main IBKR",
                    "baseCurrency": "EUR",
                    "status": "ACTIVE"
                  }],
                  "positions": [%s,%s],
                  "cashBalances": [{
                    "brokerAccountId": "%s",
                    "cash": {"amount": 123.45, "currency": "EUR"},
                    "source": "REAL_BROKER"
                  }]
                }
                """.formatted(accountId,
                positionJson(accountId, "123456", "AIXA", "250.00", "10", "2026-08-24T12:00:00Z", "EUR"),
                positionJson(accountId, "654321", "BESI", "120.00", "2", "2026-08-24T12:00:00Z", "EUR"),
                accountId);
    }

    private static String multiAccountSnapshot(boolean includeFirstAccountPosition, boolean includeSecondAccountPosition) {
        String first = includeFirstAccountPosition
                ? positionJson("IBKR_U1111111", "111111", "AIXA", "250.00", "10", "2026-08-24T12:00:00Z", "EUR")
                : "";
        String second = includeSecondAccountPosition
                ? positionJson("IBKR_U2222222", "222222", "BESI", "120.00", "2", "2026-08-24T12:00:00Z", "EUR")
                : "";
        String positions = java.util.stream.Stream.of(first, second)
                .filter(value -> !value.isBlank())
                .collect(java.util.stream.Collectors.joining(","));
        return """
                {
                  "accounts": [
                    {"brokerAccountId": "IBKR_U1111111", "brokerType": "IBKR", "externalAccountReference": "****1111", "displayName": "IBKR 1", "baseCurrency": "EUR", "status": "ACTIVE"},
                    {"brokerAccountId": "IBKR_U2222222", "brokerType": "IBKR", "externalAccountReference": "****2222", "displayName": "IBKR 2", "baseCurrency": "EUR", "status": "ACTIVE"}
                  ],
                  "positions": [%s],
                  "cashBalances": [
                    {"brokerAccountId": "IBKR_U1111111", "cash": {"amount": 10.00, "currency": "EUR"}, "source": "REAL_BROKER"},
                    {"brokerAccountId": "IBKR_U2222222", "cash": {"amount": 20.00, "currency": "EUR"}, "source": "REAL_BROKER"}
                  ]
                }
                """.formatted(positions);
    }

    private static String multiCurrencyCashSnapshot() {
        return """
                {
                  "accounts": [{
                    "brokerAccountId": "IBKR_U1234567",
                    "brokerType": "IBKR",
                    "externalAccountReference": "****4567",
                    "displayName": "Main IBKR",
                    "baseCurrency": "EUR",
                    "status": "ACTIVE"
                  }],
                  "positions": [%s],
                  "cashBalances": [
                    {"brokerAccountId": "IBKR_U1234567", "cash": {"amount": 100.00, "currency": "EUR"}, "source": "REAL_BROKER"},
                    {"brokerAccountId": "IBKR_U1234567", "cash": {"amount": 50.00, "currency": "USD"}, "source": "REAL_BROKER"},
                    {"brokerAccountId": "IBKR_U1234567", "cash": {"amount": 1000.00, "currency": "INR"}, "source": "REAL_BROKER"}
                  ]
                }
                """.formatted(positionJson("IBKR_U1234567", "123456", "AIXA", "250.00", "10", "2026-08-24T12:00:00Z", "EUR"));
    }

    private static String positionJson(String accountId, String conid, String ticker, String marketValue,
                                       String quantity, String observedAt, String currency) {
        return """
                {
                  "brokerAccountId": "%s",
                  "instrument": {
                    "instrumentId": "%s",
                    "provider": "IBKR",
                    "providerInstrumentId": "%s",
                    "isin": null,
                    "ticker": "%s",
                    "exchange": "XETR",
                    "mic": "XETR",
                    "companyName": "%s AG",
                    "assetType": "EQUITY",
                    "country": "DE",
                    "tradingCurrency": "%s",
                    "sector": null,
                    "industry": null,
                    "brokerSymbol": "%s",
                    "brokerDescription": "%s",
                    "brokerExchange": "AEB",
                    "canonicalSymbol": "%s",
                    "canonicalName": "AIXTRON SE",
                    "canonicalExchange": "XETR",
                    "canonicalMic": "XETR",
                    "securityType": "STK"
                  },
                  "quantity": %s,
                  "averageCost": {"amount": 20.00, "currency": "%s"},
                  "currentPrice": {"amount": 25.00, "currency": "%s"},
                  "marketValue": {"amount": %s, "currency": "%s"},
                  "unrealizedProfitLoss": {"amount": 50.00, "currency": "%s"},
                  "dataFreshness": "REAL_BROKER",
                  "observedAt": "%s"
                }
                """.formatted(accountId,
                UUID.nameUUIDFromBytes(("IBKR|" + conid).getBytes(java.nio.charset.StandardCharsets.UTF_8)),
                conid, ticker, ticker, currency, ticker, ticker, ticker, quantity, currency, currency, marketValue,
                currency, currency, observedAt);
    }

    private static HttpServer startBrokerServer() {
        try {
            HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
            server.createContext("/api/v1/broker-connections", exchange -> {
                String response = "POST".equals(exchange.getRequestMethod()) ? """
                        {"connectionId":"00000000-0000-0000-0000-000000000001","brokerType":"IBKR",
                         "displayName":"Interactive Brokers","status":"CONNECTED","providerStatus":"CONNECTED",
                         "capabilities":["ACCOUNTS_READ","ACCOUNT_METADATA_READ","POSITIONS_READ","CASH_READ","PORTFOLIO_READ"]}
                        """ : brokerSnapshotResponse;
                byte[] body = response.getBytes(java.nio.charset.StandardCharsets.UTF_8);
                exchange.getResponseHeaders().set("Content-Type", "application/json");
                exchange.sendResponseHeaders(200, body.length);
                try (OutputStream output = exchange.getResponseBody()) {
                    output.write(body);
                }
            });
            server.start();
            return server;
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to start test broker server", exception);
        }
    }
}
