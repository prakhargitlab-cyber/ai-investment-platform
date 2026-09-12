package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.api.CombinedPortfolioHoldingResponse;
import com.aiinvestment.portfolio.infrastructure.persistence.*;
import com.aiinvestment.shared.domain.broker.BrokerType;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.mock.web.MockMultipartFile;
import org.springframework.test.context.ActiveProfiles;
import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.*;
import static org.assertj.core.api.Assertions.*;

@SpringBootTest
@ActiveProfiles("test")
class PortfolioImportServiceIntegrationTest {
    private static final UUID USER = UUID.fromString("30000000-0000-0000-0000-000000000001");
    private static final UUID OTHER = UUID.fromString("30000000-0000-0000-0000-000000000002");
    @Autowired PortfolioImportService imports;
    @Autowired PortfolioService portfolios;
    @Autowired PortfolioRepository portfolioRepository;
    @Autowired PortfolioPositionRepository positionRepository;
    @Autowired PortfolioImportHistoryRepository historyRepository;
    @Autowired ManualImportHoldingSnapshotRepository snapshotRepository;
    @Autowired JdbcTemplate jdbc;

    @BeforeEach
    void users() { seed(USER, "import-user"); seed(OTHER, "other-import-user"); }

    @Test
    void previewIsWriteFreeAndConfirmPersistsImportedSnapshotProvenance() {
        long portfoliosBefore = portfolioRepository.count(), historyBefore = historyRepository.count(), snapshotsBefore = snapshotRepository.count();
        var preview = imports.preview(USER, BrokerType.ICICI_DIRECT, file("900001_PortFolioEqtSummary.csv", icici("ABC", "ABC Limited", "INE000A01001", "10", "200")), null);
        assertThat(preview.portfolioName()).isEqualTo("900001");
        assertThat(portfolioRepository.count()).isEqualTo(portfoliosBefore);
        assertThat(historyRepository.count()).isEqualTo(historyBefore);
        assertThat(snapshotRepository.count()).isEqualTo(snapshotsBefore);
        assertThatThrownBy(() -> imports.confirm(USER, BrokerType.HDFC_SECURITIES,
                file("Invest Right Equity Portfolio_999999.csv", hdfc("888888", "BAD", "1", "1")), null))
                .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("do not match");
        assertThat(portfolioRepository.count()).isEqualTo(portfoliosBefore);

        var result = imports.confirm(USER, BrokerType.ICICI_DIRECT, file("900001_PortFolioEqtSummary.csv", icici("ABC", "ABC Limited", "INE000A01001", "10", "200")), null);
        var position = portfolios.getPositions(USER, result.portfolioId()).get(0);
        assertThat(position.dataFreshness()).isEqualTo("IMPORTED_SNAPSHOT");
        assertThat(position.instrument().companyName()).isEqualTo("ABC Limited");
        assertThat(position.instrument().isin()).isEqualTo("INE000A01001");
        assertThat(position.instrument().brokerSymbol()).isEqualTo("ABC");
        assertThat(position.currentPrice()).isNull();
        assertThat(position.importedPrice().amount()).isEqualByComparingTo("250");
        assertThat(historyRepository.count()).isEqualTo(historyBefore + 1);
        assertThat(snapshotRepository.count()).isEqualTo(snapshotsBefore + 1);
    }

    @Test
    void missingImportedPricePersistsAsNullAndCannotCreateDerivedLiveValuation() {
        String csv = "Stock Symbol,Company Name,ISIN Code,Qty,Average Cost Price,Current Market Price,% Change over prev close,Value At Cost,Value At Market Price,Realized Profit / Loss,Unrealized Profit/Loss,Unrealized Profit/Loss %\n"
                + "NOP,Noprice Limited,INE000N01001,10,200,,1,2000,,,,\n";

        var result = imports.confirm(USER, BrokerType.ICICI_DIRECT,
                file("900004_PortFolioEqtSummary.csv", csv), null);
        var position = portfolios.getPositions(USER, result.portfolioId()).get(0);
        var summary = portfolios.getSummary(USER, result.portfolioId());

        assertThat(position.currentPrice()).isNull();
        assertThat(position.importedPrice()).isNull();
        assertThat(position.marketValue()).isNull();
        assertThat(position.unrealizedProfitLoss()).isNull();
        assertThat(position.unrealizedProfitLossPercent()).isNull();
        assertThat(summary.totalMarketValue()).isNull();
        assertThat(summary.unrealizedProfitLoss()).isNull();
        assertThat(jdbc.queryForObject(
                "SELECT current_price_amount FROM portfolio.portfolio_positions WHERE position_id=?",
                BigDecimal.class, position.positionId())).isNull();
    }

    @Test
    void authoritativeReimportIsIdempotentReconcilesRemovedRowsAndFailureKeepsLastGoodState() {
        String account = "900002";
        String two = iciciRows("A,A Limited,INE000A01001,10,200", "B,B Limited,INE000B01001,5,300");
        var first = imports.confirm(USER, BrokerType.ICICI_DIRECT, file(account + "_PortFolioEqtSummary.csv", two), null);
        UUID portfolioId = first.portfolioId();
        UUID retainedId = portfolios.getPositions(USER, portfolioId).stream().filter(p -> p.instrument().ticker().equals("A")).findFirst().orElseThrow().positionId();
        imports.confirm(USER, BrokerType.ICICI_DIRECT, file(account + "_PortFolioEqtSummary (1).csv", icici("A", "A Updated", "INE000A01001", "12", "210")), null);
        var positions = portfolios.getPositions(USER, portfolioId);
        assertThat(positions).hasSize(2);
        assertThat(positions.stream().filter(p -> p.active()).toList()).singleElement()
                .satisfies(p -> { assertThat(p.positionId()).isEqualTo(retainedId); assertThat(p.quantity()).isEqualByComparingTo("12"); });
        assertThat(positions.stream().filter(p -> !p.active()).toList()).singleElement()
                .satisfies(p -> assertThat(p.instrument().ticker()).isEqualTo("B"));

        assertThatThrownBy(() -> imports.confirm(USER, BrokerType.ICICI_DIRECT,
                file(account + "_PortFolioEqtSummary.csv", "bad,columns\n1,2"), null)).isInstanceOf(IllegalArgumentException.class);
        assertThat(portfolios.getPositions(USER, portfolioId).stream().filter(p -> p.active()).findFirst().orElseThrow().quantity())
                .isEqualByComparingTo("12");
    }

    @Test
    void exactManualReimportIsIdempotentWithoutDuplicatingCurrentOrHistory() {
        String csv = icici("KPIT", "KPIT Technologies", "INE000K01001", "80", "1250");
        var first = imports.confirm(USER, BrokerType.ICICI_DIRECT,
                file("900003_PortFolioEqtSummary.csv", csv), null);
        long historyCount = historyRepository.count();
        long snapshotCount = snapshotRepository.count();

        var second = imports.confirm(USER, BrokerType.ICICI_DIRECT,
                file("900003_PortFolioEqtSummary.csv", csv), null);

        assertThat(second.portfolioId()).isEqualTo(first.portfolioId());
        assertThat(historyRepository.count()).isEqualTo(historyCount);
        assertThat(snapshotRepository.count()).isEqualTo(snapshotCount);
        assertThat(portfolios.getPositions(USER, first.portfolioId()).stream().filter(p -> p.active()).toList())
                .singleElement().satisfies(position -> {
                    assertThat(position.quantity()).isEqualByComparingTo("80");
                    assertThat(position.averageCost().amount()).isEqualByComparingTo("1250");
                });
    }

    @Test
    void multipleBrokerAccountsStayDistinctAndOwnershipIsolated() {
        var i1 = imports.confirm(USER, BrokerType.ICICI_DIRECT, file("910001_PortFolioEqtSummary.csv", icici("A", "A", "INE000A01001", "1", "1")), null);
        var i2 = imports.confirm(USER, BrokerType.ICICI_DIRECT, file("910002_PortFolioEqtSummary.csv", icici("A", "A", "INE000A01001", "1", "1")), null);
        var h1 = imports.confirm(USER, BrokerType.HDFC_SECURITIES, file("Invest Right Equity Portfolio_920001.csv", hdfc("920001", "XYZ", "1", "1")), null);
        var h2 = imports.confirm(USER, BrokerType.HDFC_SECURITIES, file("Invest Right Equity Portfolio_920002.csv", hdfc("920002", "XYZ", "1", "1")), null);
        assertThat(Set.of(i1.portfolioId(), i2.portfolioId(), h1.portfolioId(), h2.portfolioId())).hasSize(4);
        assertThatThrownBy(() -> portfolios.getPositions(OTHER, i1.portfolioId())).isInstanceOf(PortfolioNotFoundException.class);
    }

    @Test
    void hdfcOverrideSurvivesReimportAndRenameValidationPreservesAllFinancialAndProviderFields() {
        var result = imports.confirm(USER, BrokerType.HDFC_SECURITIES,
                file("Invest Right Equity Portfolio_930001.csv", hdfc("930001", "abcxyz", "100", "200")), null);
        var before = portfolios.getPositions(USER, result.portfolioId()).get(0);
        var renamed = portfolios.updateCustomDisplayName(USER, result.portfolioId(), before.positionId(), "  ABC XYZ Limited  ");
        assertThat(renamed.displayName()).isEqualTo("ABC XYZ Limited");
        assertUnchanged(before, renamed);
        imports.confirm(USER, BrokerType.HDFC_SECURITIES,
                file("Invest Right Equity Portfolio_930001 (1).csv", hdfc("930001", "ABCXYZ", "100", "200")), null);
        assertThat(portfolios.getPositions(USER, result.portfolioId()).get(0).displayName()).isEqualTo("ABC XYZ Limited");
        assertThatThrownBy(() -> portfolios.updateCustomDisplayName(OTHER, result.portfolioId(), before.positionId(), "X"))
                .isInstanceOf(PortfolioNotFoundException.class);
        assertThatThrownBy(() -> portfolios.updateCustomDisplayName(USER, result.portfolioId(), before.positionId(), "x".repeat(161)))
                .isInstanceOf(IllegalArgumentException.class);
        var reset = portfolios.updateCustomDisplayName(USER, result.portfolioId(), before.positionId(), "   ");
        assertThat(reset.customDisplayName()).isNull(); assertThat(reset.displayName()).isEqualTo("ABCXYZ");
        jdbc.update("UPDATE portfolio.portfolio_positions SET source_type='BROKER' WHERE position_id=?", before.positionId());
        assertThatThrownBy(() -> portfolios.updateCustomDisplayName(USER, result.portfolioId(), before.positionId(), "No"))
                .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("manually imported");
    }

    @Test
    void weightedAggregationUsesCanonicalIdentityCurrencyAndNeverCrossMergesHdfcSymbolWithIciciIsin() {
        var a = imports.confirm(USER, BrokerType.ICICI_DIRECT, file("940001_PortFolioEqtSummary.csv", icici("ABC", "ABC Ltd", "INE000A01001", "100", "200")), null);
        var b = imports.confirm(USER, BrokerType.ICICI_DIRECT, file("940002_PortFolioEqtSummary.csv", icici("ABC", "ABC Ltd", "INE000A01001", "50", "260")), null);
        var h = imports.confirm(USER, BrokerType.HDFC_SECURITIES, file("Invest Right Equity Portfolio_940003.csv", hdfc("940003", "ABC", "10", "999")), null);
        var all = new ArrayList<com.aiinvestment.portfolio.domain.PortfolioPosition>();
        all.addAll(portfolios.getPositions(USER, a.portfolioId())); all.addAll(portfolios.getPositions(USER, b.portfolioId())); all.addAll(portfolios.getPositions(USER, h.portfolioId()));
        var combined = CombinedPortfolioHoldingResponse.aggregate(all);
        assertThat(combined).hasSize(2);
        assertThat(combined).filteredOn(x -> x.securityKey().equals("ISIN:INE000A01001")).singleElement().satisfies(x -> {
            assertThat(x.quantity()).isEqualByComparingTo("150"); assertThat(x.averageCost().amount()).isEqualByComparingTo("220");
        });
        assertThat(combined).filteredOn(x -> x.securityKey().equals("HDFC_SECURITIES:SYMBOL:ABC")).singleElement();
        portfolios.updateCustomDisplayName(USER, a.portfolioId(), all.get(0).positionId(), "Custom ABC");
        var renamed = new ArrayList<com.aiinvestment.portfolio.domain.PortfolioPosition>();
        renamed.addAll(portfolios.getPositions(USER, a.portfolioId())); renamed.addAll(portfolios.getPositions(USER, b.portfolioId()));
        assertThat(CombinedPortfolioHoldingResponse.aggregate(renamed).get(0).securityKey()).isEqualTo("ISIN:INE000A01001");
        assertThat(CombinedPortfolioHoldingResponse.aggregate(renamed).get(0).companyName()).isEqualTo("Custom ABC");
    }

    @Test
    void manualImportAndBrokerApiPositionIdentitiesRemainSeparateAndCombinedCurrenciesDoNotMerge() {
        var imported = imports.confirm(USER, BrokerType.ICICI_DIRECT,
                file("950001_PortFolioEqtSummary.csv", icici("SEP", "Separate", "INE000S01001", "2", "10")), null);
        var manual = portfolios.getPositions(USER, imported.portfolioId()).get(0);
        assertThat(manual.sourceType()).isEqualTo("MANUAL_CSV_IMPORT");
        jdbc.update("UPDATE portfolio.portfolio_positions SET average_cost_currency='EUR', current_price_currency='EUR', market_value_currency='EUR', unrealized_profit_loss_currency='EUR' WHERE position_id=?", manual.positionId());
        var eur = portfolios.getPositions(USER, imported.portfolioId()).get(0);
        var combined = CombinedPortfolioHoldingResponse.aggregate(List.of(manual, eur));
        assertThat(combined).hasSize(2);
        assertThat(combined).extracting(x -> x.averageCost().currency()).containsExactlyInAnyOrder("INR", "EUR");
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM portfolio.portfolio_positions WHERE portfolio_id=? AND source_type='BROKER'",
                Integer.class, imported.portfolioId())).isZero();
    }

    private void assertUnchanged(com.aiinvestment.portfolio.domain.PortfolioPosition a, com.aiinvestment.portfolio.domain.PortfolioPosition b) {
        assertThat(b.instrument().provider()).isEqualTo(a.instrument().provider()); assertThat(b.instrument().providerInstrumentId()).isEqualTo(a.instrument().providerInstrumentId());
        assertThat(b.instrument().isin()).isEqualTo(a.instrument().isin()); assertThat(b.quantity()).isEqualByComparingTo(a.quantity());
        assertThat(b.averageCost()).isEqualTo(a.averageCost()); assertThat(b.marketValue()).isEqualTo(a.marketValue());
    }
    private MockMultipartFile file(String name, String body) { return new MockMultipartFile("file", name, "text/csv", body.getBytes(StandardCharsets.UTF_8)); }
    private String icici(String s, String n, String isin, String q, String avg) { return iciciRows(s + "," + n + "," + isin + "," + q + "," + avg); }
    private String iciciRows(String... rows) {
        String header = "Stock Symbol,Company Name,ISIN Code,Qty,Average Cost Price,Current Market Price,% Change over prev close,Value At Cost,Value At Market Price,Realized Profit / Loss,Unrealized Profit/Loss,Unrealized Profit/Loss %\n";
        return header + Arrays.stream(rows).map(r -> r + ",250,1,20000,25000,0,5000,25").reduce("", (x,y) -> x + y + "\n");
    }
    private String hdfc(String account, String symbol, String qty, String avg) {
        return "Trading account: " + account + ", Equity Portfolio Details as on 29/08/2026 21:14:38\n\n" +
                "SYMBOL,QUANTITY,LONG TERM QUANTITY,AVG PRICE,LTP,INVESTMENT VALUE,CURRENT VALUE,UNREALISED P/L,REALISED P/L,TOTAL P/L,TODAY'S P/L\n" +
                symbol + "," + qty + "," + qty + "," + avg + ",225," + qty + ",22500,2500,0,2500,10\n";
    }
    private void seed(UUID id, String subject) {
        Integer count = jdbc.queryForObject("SELECT COUNT(*) FROM portfolio.app_users WHERE id=?", Integer.class, id);
        if (count != null && count > 0) return;
        Instant now = Instant.now(); jdbc.update("INSERT INTO portfolio.app_users(id,issuer,external_subject,email,display_name,created_at,updated_at) VALUES(?, 'test', ?, ?, ?, ?, ?)", id, subject, subject + "@test.invalid", subject, Timestamp.from(now), Timestamp.from(now));
    }
}
