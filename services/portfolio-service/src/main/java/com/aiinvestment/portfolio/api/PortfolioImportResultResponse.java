package com.aiinvestment.portfolio.api;
import java.time.Instant;
import java.util.UUID;
public record PortfolioImportResultResponse(UUID portfolioId, String portfolioName, String broker, String sourceType,
                                            Instant importedAt, int rowCount, int acceptedCount, int rejectedCount,
                                            boolean updatedExistingPortfolio) {}
