package com.aiinvestment.portfolio.infrastructure.persistence;

import org.springframework.data.jpa.repository.JpaRepository;

public interface BrokerAccountRepository extends JpaRepository<BrokerAccountEntity, String> {
}
