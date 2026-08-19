package com.aiinvestment.portfolio.infrastructure.persistence;

import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.UUID;

public interface PortfolioPositionRepository extends JpaRepository<PortfolioPositionEntity, UUID> {
    List<PortfolioPositionEntity> findByPortfolioPortfolioId(UUID portfolioId);

    void deleteByPortfolioPortfolioId(UUID portfolioId);
}
