package com.aiinvestment.portfolio.infrastructure.persistence;
import org.springframework.data.jpa.repository.JpaRepository;
import java.util.UUID;
public interface ManualImportHoldingSnapshotRepository extends JpaRepository<ManualImportHoldingSnapshotEntity, UUID> {}
