package com.aiinvestment.portfolio.infrastructure.persistence;
import org.springframework.data.jpa.repository.JpaRepository; import org.springframework.data.domain.*; import java.util.*;
public interface Nifty500UniverseRepository extends JpaRepository<Nifty500UniverseEntity,UUID>{ Page<Nifty500UniverseEntity> findAllByOrderBySymbolAscInstrumentIdAsc(Pageable pageable); }
