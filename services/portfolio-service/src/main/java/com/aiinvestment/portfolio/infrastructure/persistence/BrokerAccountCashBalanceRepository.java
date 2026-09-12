package com.aiinvestment.portfolio.infrastructure.persistence;

import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.UUID;

public interface BrokerAccountCashBalanceRepository extends JpaRepository<BrokerAccountCashBalanceEntity, String> {
    List<BrokerAccountCashBalanceEntity> findByUserId(UUID userId);
}
