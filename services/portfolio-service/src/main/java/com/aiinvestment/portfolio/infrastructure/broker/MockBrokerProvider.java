package com.aiinvestment.portfolio.infrastructure.broker;

import com.aiinvestment.shared.domain.AssetType;
import com.aiinvestment.shared.domain.Instrument;
import com.aiinvestment.shared.domain.Money;
import com.aiinvestment.shared.domain.broker.*;
import org.springframework.stereotype.Component;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.List;
import java.util.UUID;

@Component
public class MockBrokerProvider implements BrokerProvider {
    @Override
    public BrokerType supportedBroker() {
        return BrokerType.MOCK;
    }

    @Override
    public BrokerConnectionCapabilities connectionCapabilities() {
        return BrokerConnectionCapabilities.mockReadOnly();
    }

    @Override
    public BrokerConnectionStatus connectionStatus() {
        return new BrokerConnectionStatus(BrokerType.MOCK, BrokerConnectionState.CONNECTED, BrokerProviderStatus.CONNECTED, "MOCK_CONNECTED", "Mock broker provider uses static fake DEV data.");
    }

    @Override
    public List<BrokerAccount> fetchAccounts(UUID userId) {
        return List.of(
                new BrokerAccount("MOCK_EU", userId, BrokerType.MOCK, "mock-eu-account", "Mock EU Account", "EUR", BrokerAccountStatus.ACTIVE),
                new BrokerAccount("MOCK_INDIA", userId, BrokerType.MOCK, "mock-india-account", "Mock India Account", "INR", BrokerAccountStatus.ACTIVE)
        );
    }

    @Override
    public List<BrokerPosition> fetchPositions(BrokerAccount account) {
        Instant observedAt = Instant.parse("2026-01-01T00:00:00Z");
        if ("MOCK_EU".equals(account.brokerAccountId())) {
            return List.of(
                    position(account, instrument("BESI", "NL0012866412", "XAMS", "XAMS", "BE Semiconductor Industries", "NL", "EUR", "Technology", "Semiconductor Equipment"), "12", "110.00", "132.50", observedAt),
                    position(account, instrument("AIXA", "DE000A0WMPJ6", "XETR", "XETR", "AIXTRON SE", "DE", "EUR", "Technology", "Semiconductor Equipment"), "40", "18.25", "24.10", observedAt),
                    position(account, instrument("NVDA", "US67066G1040", "XNAS", "XNAS", "NVIDIA Corporation", "US", "USD", "Technology", "Semiconductors"), "8", "500.00", "920.00", observedAt)
            );
        }
        if ("MOCK_INDIA".equals(account.brokerAccountId())) {
            return List.of(
                    position(account, instrument("RELIANCE", "INE002A01018", "XNSE", "XNSE", "Reliance Industries Limited", "IN", "INR", "Energy", "Oil Gas and Consumable Fuels"), "20", "2400.00", "2850.00", observedAt),
                    position(account, instrument("ZENTEC", "INE251B01027", "XNSE", "XNSE", "Zen Technologies Limited", "IN", "INR", "Industrials", "Aerospace and Defense"), "35", "650.00", "1025.00", observedAt)
            );
        }
        return List.of();
    }

    @Override
    public void disconnect(UUID connectionId) {
        // No remote state exists for the mock provider.
    }

    @Override
    public List<BrokerCashBalance> fetchCashBalances(BrokerAccount account) {
        if ("MOCK_EU".equals(account.brokerAccountId())) {
            return List.of(new BrokerCashBalance(account.brokerAccountId(), new Money(new BigDecimal("1250.00"), "EUR")));
        }
        if ("MOCK_INDIA".equals(account.brokerAccountId())) {
            return List.of(new BrokerCashBalance(account.brokerAccountId(), new Money(new BigDecimal("150000.00"), "INR")));
        }
        return List.of();
    }

    private static BrokerPosition position(BrokerAccount account, Instrument instrument, String quantity, String averageCost,
                                           String currentPrice, Instant observedAt) {
        return new BrokerPosition(account.brokerAccountId(), instrument, new BigDecimal(quantity),
                new Money(new BigDecimal(averageCost), instrument.tradingCurrency()),
                new Money(new BigDecimal(currentPrice), instrument.tradingCurrency()), observedAt);
    }

    private static Instrument instrument(String ticker, String isin, String exchange, String mic, String companyName,
                                         String country, String currency, String sector, String industry) {
        UUID instrumentId = UUID.nameUUIDFromBytes((isin + "|" + exchange + "|" + ticker).getBytes());
        return new Instrument(instrumentId, isin, ticker, exchange, mic, companyName, AssetType.EQUITY, country, currency, sector, industry);
    }
}
