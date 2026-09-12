package com.aiinvestment.portfolio.infrastructure.persistence;
import org.springframework.data.jpa.repository.JpaRepository;
import java.util.*;
public interface InstrumentProviderMappingRepository extends JpaRepository<InstrumentProviderMappingEntity, UUID> {
    Optional<InstrumentProviderMappingEntity> findByProviderAndProviderInstrumentId(String provider,String providerInstrumentId);
    Optional<InstrumentProviderMappingEntity> findByProviderAndExchangeIgnoreCaseAndProviderSymbolIgnoreCase(String provider,String exchange,String symbol);
    Optional<InstrumentProviderMappingEntity> findFirstByInstrumentIdAndProviderAndStatusIn(UUID instrumentId,String provider,Collection<String> statuses);
    List<InstrumentProviderMappingEntity> findByInstrumentId(UUID instrumentId);
    List<InstrumentProviderMappingEntity> findByInstrumentIdIn(Collection<UUID> instrumentIds);
}
