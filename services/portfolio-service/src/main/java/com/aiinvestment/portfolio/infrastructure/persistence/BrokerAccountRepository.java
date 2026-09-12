package com.aiinvestment.portfolio.infrastructure.persistence;

import com.aiinvestment.shared.domain.broker.BrokerType;
import jakarta.persistence.LockModeType;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;

import java.util.List;
import java.util.Optional;
import java.util.UUID;

public interface BrokerAccountRepository extends JpaRepository<BrokerAccountEntity, String> {
    Optional<BrokerAccountEntity> findByUserIdAndConnectionIdAndBrokerTypeAndSourceBrokerAccountId(
            UUID userId, UUID connectionId, BrokerType brokerType, String sourceBrokerAccountId);

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    List<BrokerAccountEntity> findByUserIdAndConnectionIdIsNullAndBrokerTypeAndSourceBrokerAccountId(
            UUID userId, BrokerType brokerType, String sourceBrokerAccountId);
}
