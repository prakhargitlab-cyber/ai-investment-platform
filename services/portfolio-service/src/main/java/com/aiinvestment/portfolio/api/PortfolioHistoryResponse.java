package com.aiinvestment.portfolio.api;

import com.aiinvestment.portfolio.domain.PortfolioHistory;
import com.aiinvestment.portfolio.domain.PortfolioValuationPoint;

import java.time.Instant;
import java.util.List;
import java.util.UUID;

public record PortfolioHistoryResponse(
        UUID portfolioId,
        String baseCurrency,
        String range,
        Instant from,
        Instant to,
        String investedCapitalStatus,
        boolean backfillAvailable,
        List<PortfolioHistoryPointResponse> points
) {
    public static PortfolioHistoryResponse from(PortfolioHistory history) {
        return new PortfolioHistoryResponse(
                history.portfolioId(),
                history.baseCurrency(),
                history.range().code(),
                history.from(),
                history.to(),
                history.investedCapitalStatus(),
                history.backfillAvailable(),
                history.points().stream().map(PortfolioHistoryPointResponse::from).toList());
    }

    public record PortfolioHistoryPointResponse(
            Instant timestamp,
            MoneyResponse marketValue,
            MoneyResponse investedCapital,
            String investedCapitalStatus,
            MoneyResponse cash,
            MoneyResponse positionsMarketValue,
            MoneyResponse unrealizedPnl,
            MoneyResponse realizedPnl,
            String broker,
            String source,
            String dataFreshness
    ) {
        public static PortfolioHistoryPointResponse from(PortfolioValuationPoint point) {
            return new PortfolioHistoryPointResponse(
                    point.timestamp(),
                    MoneyResponse.from(point.portfolioMarketValue()),
                    point.investedCapital() == null ? null : MoneyResponse.from(point.investedCapital()),
                    point.investedCapitalStatus(),
                    MoneyResponse.from(point.cash()),
                    MoneyResponse.from(point.positionsMarketValue()),
                    MoneyResponse.from(point.unrealizedPnl()),
                    point.realizedPnl() == null ? null : MoneyResponse.from(point.realizedPnl()),
                    point.broker(),
                    point.source(),
                    point.dataFreshness());
        }
    }
}
