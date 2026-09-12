package com.aiinvestment.portfolio.infrastructure.persistence;

import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.Optional;
import java.util.UUID;

public interface PortfolioRepository extends JpaRepository<PortfolioEntity, UUID> {
    List<PortfolioEntity> findByUserId(UUID userId);
    Optional<PortfolioEntity> findByPortfolioIdAndUserId(UUID portfolioId, UUID userId);
    boolean existsByPortfolioIdAndUserId(UUID portfolioId, UUID userId);
    Optional<PortfolioEntity> findByUserIdAndBrokerConnectionIdAndBrokerAccountId(
            UUID userId, UUID brokerConnectionId, String brokerAccountId);
    Optional<PortfolioEntity> findByUserIdAndBrokerProviderAndAcquisitionSourceAndSourceAccountReference(
            UUID userId, com.aiinvestment.shared.domain.broker.BrokerType brokerProvider,
            String acquisitionSource, String sourceAccountReference);
}
