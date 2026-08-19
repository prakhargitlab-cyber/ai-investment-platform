package com.aiinvestment.portfolio.infrastructure.persistence;

import com.aiinvestment.portfolio.domain.Portfolio;
import com.aiinvestment.portfolio.domain.PortfolioPosition;
import com.aiinvestment.shared.domain.Instrument;
import com.aiinvestment.shared.domain.Money;
import com.aiinvestment.shared.domain.broker.BrokerAccount;

public final class PortfolioMapper {
    private PortfolioMapper() {
    }

    public static Portfolio toDomain(PortfolioEntity entity) {
        return new Portfolio(entity.getPortfolioId(), entity.getUserId(), entity.getName(), entity.getBaseCurrency(),
                entity.getCreatedAt(), entity.getUpdatedAt());
    }

    public static Instrument toDomain(InstrumentEntity entity) {
        return new Instrument(entity.getInstrumentId(), entity.getIsin(), entity.getTicker(), entity.getExchange(),
                entity.getMic(), entity.getCompanyName(), entity.getAssetType(), entity.getCountry(),
                entity.getTradingCurrency(), entity.getSector(), entity.getIndustry());
    }

    public static BrokerAccount toDomain(BrokerAccountEntity entity) {
        return new BrokerAccount(entity.getBrokerAccountId(), entity.getUserId(), entity.getBrokerType(),
                entity.getExternalAccountReference(), entity.getDisplayName(), entity.getBaseCurrency(), entity.getStatus());
    }

    public static PortfolioPosition toDomain(PortfolioPositionEntity entity) {
        return PortfolioPosition.priced(entity.getPositionId(), entity.getPortfolio().getPortfolioId(), toDomain(entity.getInstrument()),
                entity.getQuantity(), new Money(entity.getAverageCostAmount(), entity.getAverageCostCurrency()),
                new Money(entity.getCurrentPriceAmount(), entity.getCurrentPriceCurrency()),
                entity.getBrokerAccount().getBrokerAccountId(), entity.getLastUpdated());
    }

    public static InstrumentEntity toEntity(Instrument instrument) {
        return new InstrumentEntity(instrument.instrumentId(), instrument.isin(), instrument.ticker(), instrument.exchange(),
                instrument.mic(), instrument.companyName(), instrument.assetType(), instrument.country(),
                instrument.tradingCurrency(), instrument.sector(), instrument.industry());
    }

    public static BrokerAccountEntity toEntity(BrokerAccount account) {
        return new BrokerAccountEntity(account.brokerAccountId(), account.userId(), account.brokerType(),
                account.externalAccountReference(), account.displayName(), account.baseCurrency(), account.status());
    }
}
