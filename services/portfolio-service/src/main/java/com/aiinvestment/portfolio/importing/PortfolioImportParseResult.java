package com.aiinvestment.portfolio.importing;

import com.aiinvestment.shared.domain.broker.BrokerType;
import java.util.List;
import java.time.Instant;

public record PortfolioImportParseResult(BrokerType broker, String parserType, String accountReference,
                                         int rowCount, List<ImportedHolding> holdings,
                                         List<String> issues, List<String> mappedColumns,
                                         Instant sourceSnapshotAt) {
    public int acceptedCount() { return holdings.size(); }
    public int rejectedCount() { return issues.size(); }
}
