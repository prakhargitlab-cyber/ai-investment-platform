package com.aiinvestment.portfolio.infrastructure.persistence;

import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.Optional;
import java.util.UUID;

public interface BrokerAccountCashBalanceEntryRepository extends JpaRepository<BrokerAccountCashBalanceEntryEntity, UUID> {
    List<BrokerAccountCashBalanceEntryEntity> findByUserId(UUID userId);

    Optional<BrokerAccountCashBalanceEntryEntity> findByUserIdAndConnectionIdAndBrokerAccountIdAndCashCurrency(
            UUID userId, UUID connectionId, String brokerAccountId, String cashCurrency);
}
