package com.aiinvestment.portfolio.infrastructure.persistence;
import org.springframework.data.jpa.repository.JpaRepository;
import java.util.*;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
public interface InstrumentMasterRepository extends JpaRepository<InstrumentMasterEntity, UUID> {
    Optional<InstrumentMasterEntity> findByNormalizedIsin(String normalizedIsin);
    List<InstrumentMasterEntity> findByPrimaryExchangeIgnoreCaseAndPrimarySymbolIgnoreCase(String exchange,String symbol);
    Page<InstrumentMasterEntity> findByStatusIgnoreCaseAndAssetType(String status, com.aiinvestment.shared.domain.AssetType assetType, Pageable pageable);
}
