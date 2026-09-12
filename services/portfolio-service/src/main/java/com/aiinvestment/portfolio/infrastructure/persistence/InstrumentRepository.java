package com.aiinvestment.portfolio.infrastructure.persistence;

import org.springframework.data.jpa.repository.JpaRepository;

import java.util.Optional;
import java.util.UUID;

public interface InstrumentRepository extends JpaRepository<InstrumentEntity, UUID> {
    Optional<InstrumentEntity> findByProviderAndProviderInstrumentId(String provider, String providerInstrumentId);
}
