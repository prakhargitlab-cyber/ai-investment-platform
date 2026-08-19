package com.aiinvestment.broker.persistence;

import java.util.List;
import java.util.UUID;

import org.springframework.data.jpa.repository.JpaRepository;

public interface BrokerConnectionRepository extends JpaRepository<BrokerConnectionEntity, UUID> {
    List<BrokerConnectionEntity> findByUserId(UUID userId);
}
