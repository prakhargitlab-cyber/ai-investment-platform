package com.aiinvestment.broker.persistence;

import java.util.List;
import java.util.Optional;
import java.util.UUID;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import jakarta.persistence.LockModeType;

public interface BrokerConnectionRepository extends JpaRepository<BrokerConnectionEntity, UUID> {
    List<BrokerConnectionEntity> findByUserId(UUID userId);
    Optional<BrokerConnectionEntity> findByConnectionIdAndUserId(UUID connectionId, UUID userId);

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    Optional<BrokerConnectionEntity> findForUpdateByConnectionIdAndUserId(UUID connectionId, UUID userId);
}
