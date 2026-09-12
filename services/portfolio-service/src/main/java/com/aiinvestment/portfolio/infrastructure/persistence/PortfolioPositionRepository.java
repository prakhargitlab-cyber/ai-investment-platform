package com.aiinvestment.portfolio.infrastructure.persistence;

import jakarta.persistence.LockModeType;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;

import java.util.List;
import java.util.Optional;
import java.util.UUID;

public interface PortfolioPositionRepository extends JpaRepository<PortfolioPositionEntity, UUID> {
    List<PortfolioPositionEntity> findByPortfolioPortfolioId(UUID portfolioId);
    Optional<PortfolioPositionEntity> findByPositionIdAndPortfolioPortfolioIdAndPortfolioUserId(
            UUID positionId, UUID portfolioId, UUID userId);

    void deleteByPortfolioPortfolioId(UUID portfolioId);

    Optional<PortfolioPositionEntity> findByPortfolioPortfolioIdAndPortfolioUserIdAndSourceTypeAndSourceConnectionIdAndSourceBrokerAccountIdAndExternalInstrumentProviderAndExternalInstrumentId(
            UUID portfolioId, UUID userId, String sourceType, UUID sourceConnectionId, String sourceBrokerAccountId,
            String externalInstrumentProvider, String externalInstrumentId);

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    List<PortfolioPositionEntity> findByPortfolioPortfolioIdAndPortfolioUserIdAndSourceTypeAndSourceConnectionIdIsNullAndSourceBrokerAccountIdAndExternalInstrumentProviderAndExternalInstrumentId(
            UUID portfolioId, UUID userId, String sourceType, String sourceBrokerAccountId,
            String externalInstrumentProvider, String externalInstrumentId);

    List<PortfolioPositionEntity> findByPortfolioPortfolioIdAndPortfolioUserIdAndSourceTypeAndSourceConnectionIdAndSourceBrokerAccountIdAndActiveTrue(
            UUID portfolioId, UUID userId, String sourceType, UUID sourceConnectionId, String sourceBrokerAccountId);
    List<PortfolioPositionEntity> findByPortfolioPortfolioIdAndPortfolioUserIdAndSourceTypeAndSourceConnectionIdIsNullAndSourceBrokerAccountIdAndActiveTrue(
            UUID portfolioId, UUID userId, String sourceType, String sourceBrokerAccountId);
}
