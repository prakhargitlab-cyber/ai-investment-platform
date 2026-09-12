package com.aiinvestment.portfolio.infrastructure.persistence;
import org.springframework.data.jpa.repository.JpaRepository;
import java.util.*;
public interface BrokerHoldingRepository extends JpaRepository<BrokerHoldingEntity,UUID> {
    Optional<BrokerHoldingEntity> findByUserIdAndConnectionIdAndBrokerAccountIdAndProviderAndProviderInstrumentId(
            UUID userId, UUID connectionId, String brokerAccountId, String provider, String providerInstrumentId);
    List<BrokerHoldingEntity> findByUserIdAndConnectionIdAndBrokerAccountIdAndActiveTrue(
            UUID userId, UUID connectionId, String brokerAccountId);
}
