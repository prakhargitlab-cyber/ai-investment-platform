package com.aiinvestment.portfolio.importing;

import com.aiinvestment.shared.domain.broker.BrokerType;

public interface PortfolioImportParser {
    BrokerType brokerType();
    boolean supported();
    PortfolioImportParseResult parse(String sanitizedFilename, byte[] content, String requestedPortfolioName);
}
