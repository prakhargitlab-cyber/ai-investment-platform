package com.aiinvestment.portfolio.infrastructure.persistence;

import com.aiinvestment.portfolio.domain.Portfolio;
import com.aiinvestment.portfolio.domain.PortfolioValuationPoint;
import com.aiinvestment.portfolio.domain.PortfolioPosition;
import com.aiinvestment.shared.domain.Instrument;
import com.aiinvestment.shared.domain.Money;
import com.aiinvestment.shared.domain.broker.BrokerAccount;
import com.aiinvestment.shared.domain.broker.BrokerCashBalance;

public final class PortfolioMapper {
    private PortfolioMapper() {
    }

    public static Portfolio toDomain(PortfolioEntity entity) {
        return new Portfolio(entity.getPortfolioId(), entity.getUserId(), entity.getName(), entity.getBaseCurrency(),
                entity.getCreatedAt(), entity.getUpdatedAt(), entity.getBrokerConnectionId(), entity.getBrokerAccountId(),
                entity.getBrokerProvider(), entity.getLastSuccessfulBrokerSyncAt(), entity.getLastBrokerSyncAttemptAt(),
                entity.getLastBrokerSyncErrorCode(), entity.getAcquisitionSource(), entity.getSourceAccountReference(),
                entity.getLastImportedAt(), entity.getLastImportedFilename());
    }

    public static Instrument toDomain(InstrumentEntity entity) {
        return new Instrument(entity.getMasterInstrumentId() == null ? entity.getInstrumentId() : entity.getMasterInstrumentId(),
                entity.getProvider(), entity.getProviderInstrumentId(),
                entity.getIsin(), entity.getTicker(), displayExchange(entity),
                entity.getMic(), entity.getCompanyName(), entity.getAssetType(), entity.getCountry(),
                entity.getTradingCurrency(), entity.getSector(), entity.getIndustry(), entity.getBrokerSymbol(),
                entity.getBrokerDescription(), entity.getBrokerExchange(), entity.getCanonicalSymbol(),
                entity.getCanonicalName(), entity.getCanonicalExchange(), entity.getCanonicalMic(),
                entity.getSecurityType());
    }

    public static BrokerAccount toDomain(BrokerAccountEntity entity) {
        String brokerAccountId = entity.getSourceBrokerAccountId() == null || entity.getSourceBrokerAccountId().isBlank()
                ? entity.getBrokerAccountId()
                : entity.getSourceBrokerAccountId();
        return new BrokerAccount(brokerAccountId, entity.getUserId(), entity.getBrokerType(),
                entity.getExternalAccountReference(), entity.getDisplayName(), entity.getBaseCurrency(), entity.getStatus());
    }

    public static BrokerCashBalance toDomain(BrokerAccountCashBalanceEntryEntity entity) {
        return new BrokerCashBalance(
                entity.getBrokerAccountId(),
                new Money(entity.getCashAmount(), entity.getCashCurrency()),
                money(entity.getSettledCashAmount(), entity.getSettledCashCurrency()),
                money(entity.getNetLiquidationValueAmount(), entity.getNetLiquidationValueCurrency()),
                money(entity.getStockMarketValueAmount(), entity.getStockMarketValueCurrency()),
                money(entity.getUnrealizedPnlAmount(), entity.getUnrealizedPnlCurrency()),
                money(entity.getRealizedPnlAmount(), entity.getRealizedPnlCurrency()),
                entity.getSource());
    }

    public static BrokerCashBalance toDomain(BrokerAccountCashBalanceEntity entity) {
        return new BrokerCashBalance(
                entity.getBrokerAccountId(),
                new Money(entity.getCashAmount(), entity.getCashCurrency()),
                money(entity.getSettledCashAmount(), entity.getSettledCashCurrency()),
                money(entity.getNetLiquidationValueAmount(), entity.getNetLiquidationValueCurrency()),
                money(entity.getStockMarketValueAmount(), entity.getStockMarketValueCurrency()),
                money(entity.getUnrealizedPnlAmount(), entity.getUnrealizedPnlCurrency()),
                money(entity.getRealizedPnlAmount(), entity.getRealizedPnlCurrency()),
                entity.getSource());
    }

    public static PortfolioPosition toDomain(PortfolioPositionEntity entity) {
        Money brokerMarketValue = entity.getMarketValueAmount() == null ? null : new Money(entity.getMarketValueAmount(), entity.getMarketValueCurrency());
        Money brokerUnrealizedProfitLoss = entity.getUnrealizedProfitLossAmount() == null ? null
                : new Money(entity.getUnrealizedProfitLossAmount(), entity.getUnrealizedProfitLossCurrency());
        String brokerAccountId = entity.getSourceBrokerAccountId() == null || entity.getSourceBrokerAccountId().isBlank()
                ? entity.getBrokerAccount().getBrokerAccountId()
                : entity.getSourceBrokerAccountId();
        String brokerType = entity.getSourceBrokerType() == null || entity.getSourceBrokerType().isBlank()
                ? entity.getBrokerAccount().getBrokerType().name()
                : entity.getSourceBrokerType();
        return PortfolioPosition.brokerReported(entity.getPositionId(), entity.getPortfolio().getPortfolioId(), toDomain(entity.getInstrument()),
                entity.getQuantity(), new Money(entity.getAverageCostAmount(), entity.getAverageCostCurrency()),
                money(entity.getCurrentPriceAmount(), entity.getCurrentPriceCurrency()),
                brokerMarketValue, brokerUnrealizedProfitLoss,
                brokerAccountId, brokerType, entity.getSourceType(), entity.isActive(),
                entity.getDataFreshness(), entity.getLastUpdated(), entity.getCustomDisplayName(),
                money(entity.getImportedPriceAmount(), entity.getImportedPriceCurrency()));
    }

    public static PortfolioValuationPoint toDomain(PortfolioValuationSnapshotEntity entity) {
        return new PortfolioValuationPoint(
                entity.getId(),
                entity.getPortfolioId(),
                entity.getUserId(),
                entity.getSnapshotTimestamp(),
                entity.getBaseCurrency(),
                money(entity.getInvestedCapitalAmount(), entity.getInvestedCapitalCurrency()),
                entity.getInvestedCapitalStatus(),
                money(entity.getCashAmount(), entity.getCashCurrency()),
                money(entity.getPositionsMarketValueAmount(), entity.getPositionsMarketValueCurrency()),
                money(entity.getPortfolioMarketValueAmount(), entity.getPortfolioMarketValueCurrency()),
                money(entity.getUnrealizedPnlAmount(), entity.getUnrealizedPnlCurrency()),
                money(entity.getRealizedPnlAmount(), entity.getRealizedPnlCurrency()),
                entity.getBroker(),
                entity.getSource(),
                entity.getDataFreshness(),
                entity.getCreatedAt());
    }

    public static InstrumentEntity toEntity(Instrument instrument) {
        return new InstrumentEntity(instrument.instrumentId(), instrument.provider(), instrument.providerInstrumentId(),
                instrument.isin(), instrument.ticker(), instrument.exchange(),
                instrument.mic(), instrument.companyName(), instrument.assetType(), instrument.country(),
                instrument.tradingCurrency(), instrument.sector(), instrument.industry(), instrument.brokerSymbol(),
                instrument.brokerDescription(), instrument.brokerExchange(), instrument.canonicalSymbol(),
                instrument.canonicalName(), instrument.canonicalExchange(), instrument.canonicalMic(),
                instrument.securityType());
    }

    public static BrokerAccountEntity toEntity(BrokerAccount account) {
        return new BrokerAccountEntity(account.brokerAccountId(), account.userId(), account.brokerType(),
                account.externalAccountReference(), account.displayName(), account.baseCurrency(), account.status());
    }

    private static Money money(java.math.BigDecimal amount, String currency) {
        return amount == null || currency == null ? null : new Money(amount, currency);
    }

    private static String displayExchange(InstrumentEntity entity) {
        if (entity.getExchange() != null && !entity.getExchange().isBlank()) return entity.getExchange();
        if (entity.getCanonicalExchange() != null && !entity.getCanonicalExchange().isBlank()) return entity.getCanonicalExchange();
        if (entity.getBrokerExchange() != null && !entity.getBrokerExchange().isBlank()) return entity.getBrokerExchange();
        return "IN".equals(entity.getCountry()) ? "NSE" : "UNKNOWN";
    }
}
