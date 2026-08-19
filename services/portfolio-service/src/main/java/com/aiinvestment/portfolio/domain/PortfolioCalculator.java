package com.aiinvestment.portfolio.domain;

import com.aiinvestment.shared.domain.Money;
import com.aiinvestment.shared.domain.broker.BrokerCashBalance;
import com.aiinvestment.shared.domain.fx.FxRateProvider;
import org.springframework.stereotype.Component;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Function;

@Component
public class PortfolioCalculator {
    private final FxRateProvider fxRateProvider;

    public PortfolioCalculator(FxRateProvider fxRateProvider) {
        this.fxRateProvider = fxRateProvider;
    }

    public Money convert(Money money, String targetCurrency) {
        BigDecimal rate = fxRateProvider.getRate(money.currency(), targetCurrency);
        return new Money(money.amount().multiply(rate), targetCurrency);
    }

    public PortfolioSummary summarize(Portfolio portfolio, List<PortfolioPosition> positions, List<BrokerCashBalance> cashBalances) {
        Money marketValue = Money.zero(portfolio.baseCurrency());
        Money costBasis = Money.zero(portfolio.baseCurrency());
        for (PortfolioPosition position : positions) {
            marketValue = marketValue.add(convert(position.marketValue(), portfolio.baseCurrency()));
            costBasis = costBasis.add(convert(position.costBasis(), portfolio.baseCurrency()));
        }
        Money cash = Money.zero(portfolio.baseCurrency());
        for (BrokerCashBalance balance : cashBalances) {
            cash = cash.add(convert(balance.cash(), portfolio.baseCurrency()));
        }
        Money profitLoss = marketValue.subtract(costBasis);
        BigDecimal profitLossPercent = BigDecimal.ZERO;
        if (costBasis.amount().compareTo(BigDecimal.ZERO) != 0) {
            profitLossPercent = profitLoss.amount()
                    .divide(costBasis.amount(), 8, RoundingMode.HALF_UP)
                    .multiply(BigDecimal.valueOf(100))
                    .setScale(4, RoundingMode.HALF_UP);
        }
        return new PortfolioSummary(portfolio.portfolioId(), portfolio.baseCurrency(), marketValue, costBasis,
                profitLoss, profitLossPercent, cash, positions.size(), allocations(positions, portfolio.baseCurrency()));
    }

    private AllocationBreakdown allocations(List<PortfolioPosition> positions, String baseCurrency) {
        return new AllocationBreakdown(
                allocation(positions, p -> nullToUnknown(p.instrument().country()), baseCurrency),
                allocation(positions, p -> p.instrument().tradingCurrency(), baseCurrency),
                allocation(positions, p -> nullToUnknown(p.instrument().sector()), baseCurrency),
                allocation(positions, p -> p.instrument().assetType().name(), baseCurrency),
                allocation(positions, PortfolioPosition::brokerAccountId, baseCurrency)
        );
    }

    private Map<String, BigDecimal> allocation(List<PortfolioPosition> positions, Function<PortfolioPosition, String> classifier, String baseCurrency) {
        Money total = Money.zero(baseCurrency);
        Map<String, Money> values = new LinkedHashMap<>();
        for (PortfolioPosition position : positions) {
            Money converted = convert(position.marketValue(), baseCurrency);
            total = total.add(converted);
            String key = classifier.apply(position);
            values.merge(key, converted, Money::add);
        }
        Map<String, BigDecimal> percentages = new LinkedHashMap<>();
        for (Map.Entry<String, Money> entry : values.entrySet()) {
            BigDecimal pct = BigDecimal.ZERO;
            if (total.amount().compareTo(BigDecimal.ZERO) != 0) {
                pct = entry.getValue().amount().divide(total.amount(), 8, RoundingMode.HALF_UP)
                        .multiply(BigDecimal.valueOf(100)).setScale(4, RoundingMode.HALF_UP);
            }
            percentages.put(entry.getKey(), pct);
        }
        return percentages;
    }

    private static String nullToUnknown(String value) {
        return value == null || value.isBlank() ? "UNKNOWN" : value;
    }
}
