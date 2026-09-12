package com.aiinvestment.portfolio.api;

import com.aiinvestment.portfolio.importing.PortfolioImportParseResult;
import java.util.List;
import java.time.Instant;
import java.math.BigDecimal;
import com.aiinvestment.portfolio.importing.ImportedHolding;

public record PortfolioImportPreviewResponse(String portfolioName, String broker, String sourceType,
                                             int rowsDetected, int validHoldings, int rejectedRows,
                                             List<String> columnsMapped, List<String> issues,
                                             boolean updatesExistingPortfolio, boolean parserSupported,
                                             Instant statementAt, String currency, List<HoldingPreview> holdings) {
    public static PortfolioImportPreviewResponse from(PortfolioImportParseResult result, boolean exists) {
        return new PortfolioImportPreviewResponse(result.accountReference(), result.broker().name(),
                "MANUAL_CSV_IMPORT", result.rowCount(), result.acceptedCount(), result.rejectedCount(),
                result.mappedColumns(), result.issues(), exists, true, result.sourceSnapshotAt(), "INR",
                result.holdings().stream().map(HoldingPreview::from).toList());
    }

    public record HoldingPreview(String companyName, String symbol, BigDecimal quantity, BigDecimal averageCost,
                                 BigDecimal importedPrice, BigDecimal marketValue, BigDecimal unrealizedPnl) {
        static HoldingPreview from(ImportedHolding holding) {
            return new HoldingPreview(holding.companyName(), holding.symbol(), holding.quantity(),
                    holding.averageCost(), holding.importedCurrentPrice(), holding.importedMarketValue(),
                    holding.unrealizedPnl());
        }
    }
}
