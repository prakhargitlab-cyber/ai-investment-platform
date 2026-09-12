package com.aiinvestment.broker.persistence;

import com.aiinvestment.shared.domain.broker.BrokerType;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.Optional;
import java.util.UUID;

public interface BrokerConnectorInstanceRepository extends JpaRepository<BrokerConnectorInstanceEntity, UUID> {
    Optional<BrokerConnectorInstanceEntity> findByConnectorIdAndUserId(UUID connectorId, UUID userId);
    List<BrokerConnectorInstanceEntity> findByUserIdAndBrokerType(UUID userId, BrokerType brokerType);
}
