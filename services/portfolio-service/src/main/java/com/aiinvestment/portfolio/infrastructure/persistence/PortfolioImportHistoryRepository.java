package com.aiinvestment.portfolio.infrastructure.persistence;
import org.springframework.data.jpa.repository.JpaRepository;
import java.util.UUID;
public interface PortfolioImportHistoryRepository extends JpaRepository<PortfolioImportHistoryEntity, UUID> {
    boolean existsByUserIdAndPortfolioIdAndBrokerProviderAndSourceAccountReferenceAndFileSha256(
            UUID userId, UUID portfolioId, String brokerProvider, String sourceAccountReference, String fileSha256);
}
