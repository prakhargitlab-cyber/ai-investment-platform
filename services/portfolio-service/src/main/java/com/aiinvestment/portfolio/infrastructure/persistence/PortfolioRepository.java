package com.aiinvestment.portfolio.infrastructure.persistence;

import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.UUID;

public interface PortfolioRepository extends JpaRepository<PortfolioEntity, UUID> {
    List<PortfolioEntity> findByUserId(UUID userId);
}
