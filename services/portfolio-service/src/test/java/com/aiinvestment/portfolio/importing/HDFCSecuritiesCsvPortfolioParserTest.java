package com.aiinvestment.portfolio.importing;

import org.junit.jupiter.api.Test;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import static org.assertj.core.api.Assertions.*;

class HDFCSecuritiesCsvPortfolioParserTest {
    private final HDFCSecuritiesCsvPortfolioParser parser = new HDFCSecuritiesCsvPortfolioParser();
    private static final String CSV = """
            Trading account: 7779646, Equity Portfolio Details as on 29/08/2026 21:14:38

            SYMBOL,QUANTITY,LONG TERM QUANTITY,AVG PRICE,LTP,INVESTMENT VALUE,CURRENT VALUE,UNREALISED P/L,REALISED P/L,TOTAL P/L,TODAY'S P/L
            abcxyz,+100,80,200.00,225.50,20000,22550,+2550,(25.50),2524.50,-100.25
            """;

    @Test
    void parsesRealInvestRightStructureWithSymbolIdentityAndSnapshotTimestamp() {
        var result = parse("Invest Right Equity Portfolio_7779646.csv", CSV);
        var h = result.holdings().get(0);
        assertThat(result.accountReference()).isEqualTo("7779646");
        assertThat(result.sourceSnapshotAt()).isEqualTo(Instant.parse("2026-08-29T15:44:38Z"));
        assertThat(h.symbol()).isEqualTo("ABCXYZ");
        assertThat(h.companyName()).isEqualTo("ABCXYZ");
        assertThat(h.isin()).isNull();
        assertThat(h.securityKey()).isEqualTo("SYMBOL:ABCXYZ");
        assertThat(h.quantity()).isEqualByComparingTo("100");
        assertThat(h.longTermQuantity()).isEqualByComparingTo("80");
        assertThat(h.averageCost()).isEqualByComparingTo("200");
        assertThat(h.importedCurrentPrice()).isEqualByComparingTo("225.50");
        assertThat(h.valueAtCost()).isEqualByComparingTo("20000");
        assertThat(h.importedMarketValue()).isEqualByComparingTo("22550");
        assertThat(h.unrealizedPnl()).isEqualByComparingTo("2550");
        assertThat(h.realizedPnl()).isEqualByComparingTo("-25.50");
        assertThat(h.totalPnl()).isEqualByComparingTo("2524.50");
        assertThat(h.todayPnl()).isEqualByComparingTo("-100.25");
    }

    @Test
    void browserSuffixDoesNotChangeAccountAndFilenameMetadataMismatchIsRejected() {
        assertThat(parse("Invest Right Equity Portfolio_7779646 (1).csv", CSV).accountReference()).isEqualTo("7779646");
        assertThatThrownBy(() -> parse("Invest Right Equity Portfolio_1111111.csv", CSV))
                .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("do not match");
    }

    private PortfolioImportParseResult parse(String filename, String csv) {
        return parser.parse(filename, csv.getBytes(StandardCharsets.UTF_8), null);
    }
}
