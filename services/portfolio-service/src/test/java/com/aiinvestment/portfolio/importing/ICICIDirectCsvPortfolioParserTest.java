package com.aiinvestment.portfolio.importing;

import org.junit.jupiter.api.Test;
import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import static org.assertj.core.api.Assertions.assertThat;

class ICICIDirectCsvPortfolioParserTest {
    private final ICICIDirectCsvPortfolioParser parser = new ICICIDirectCsvPortfolioParser();

    @Test
    void parsesRealPortfolioSummaryStructureAndBrowserSuffixWithoutChangingAccountIdentity() {
        String csv = """
                Stock Symbol,Company Name,ISIN Code,Qty,Average Cost Price,Current Market Price,% Change over prev close,Value At Cost,Value At Market Price,Realized Profit / Loss,Unrealized Profit/Loss,Unrealized Profit/Loss %,
                HDFCBANK,HDFC Bank Limited, in e040a01034 ,+10.50,1500.25,1600.75,+1.2,15752.625,16807.875,(125.50),+1055.25,+6.70,
                """;
        var result = parser.parse("8510744020_PortFolioEqtSummary (1).csv",
                csv.getBytes(StandardCharsets.UTF_8), null);
        var holding = result.holdings().get(0);
        assertThat(result.accountReference()).isEqualTo("8510744020");
        assertThat(holding.companyName()).isEqualTo("HDFC Bank Limited");
        assertThat(holding.isin()).isEqualTo("INE040A01034");
        assertThat(holding.securityKey()).isEqualTo("ISIN:INE040A01034");
        assertThat(holding.symbol()).isEqualTo("HDFCBANK");
        assertThat(holding.quantity()).isEqualByComparingTo(new BigDecimal("10.50"));
        assertThat(holding.averageCost()).isEqualByComparingTo("1500.25");
        assertThat(holding.realizedPnl()).isEqualByComparingTo("-125.50");
        assertThat(holding.unrealizedPnl()).isEqualByComparingTo("1055.25");
        assertThat(holding.importedCurrentPrice()).isEqualByComparingTo("1600.75");
        assertThat(result.sourceSnapshotAt()).isNull();
    }
}
