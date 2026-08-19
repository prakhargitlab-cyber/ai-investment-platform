package com.aiinvestment.broker.provider.mock;

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
        return new BrokerConnectionStatus(BrokerType.MOCK, BrokerConnectionState.CONNECTED, BrokerProviderStatus.CONNECTED, "MOCK_CONNECTED", "Mock provider returns static fake DEV data.");
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
                    position(account, "BESI", "NL0012866412", "XAMS", "EUR", "12", "110.00", "132.50", observedAt),
                    position(account, "AIXA", "DE000A0WMPJ6", "XETR", "EUR", "40", "18.25", "24.10", observedAt),
                    position(account, "NVDA", "US67066G1040", "XNAS", "USD", "8", "500.00", "920.00", observedAt)
            );
        }
        if ("MOCK_INDIA".equals(account.brokerAccountId())) {
            return List.of(
                    position(account, "RELIANCE", "INE002A01018", "XNSE", "INR", "20", "2400.00", "2850.00", observedAt),
                    position(account, "ZENTEC", "INE251B01027", "XNSE", "INR", "35", "650.00", "1025.00", observedAt)
            );
        }
        return List.of();
    }

    @Override
    public List<BrokerCashBalance> fetchCashBalances(BrokerAccount account) {
        return List.of(new BrokerCashBalance(account.brokerAccountId(),
                new Money(new BigDecimal("MOCK_INDIA".equals(account.brokerAccountId()) ? "150000.00" : "1250.00"), account.baseCurrency())));
    }

    @Override
    public void disconnect(UUID connectionId) {
        // No remote state exists for the mock provider.
    }

    private static BrokerPosition position(BrokerAccount account, String ticker, String isin, String exchange, String currency,
                                           String quantity, String averageCost, String currentPrice, Instant observedAt) {
        Instrument instrument = new Instrument(UUID.nameUUIDFromBytes((isin + exchange + ticker).getBytes()), isin, ticker, exchange,
                exchange, ticker + " Mock Company", AssetType.EQUITY, currency.equals("INR") ? "IN" : currency.equals("USD") ? "US" : "NL",
                currency, "Mock Sector", "Mock Industry");
        return new BrokerPosition(account.brokerAccountId(), instrument, new BigDecimal(quantity),
                new Money(new BigDecimal(averageCost), currency), new Money(new BigDecimal(currentPrice), currency), observedAt);
    }
}
