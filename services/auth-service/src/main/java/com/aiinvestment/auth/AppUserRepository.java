package com.aiinvestment.auth;

import org.springframework.data.jpa.repository.JpaRepository;

import java.util.Optional;
import java.util.UUID;

public interface AppUserRepository extends JpaRepository<AppUserEntity, UUID> {
    org.springframework.data.domain.Page<AppUserEntity> findByEmailContainingIgnoreCaseOrDisplayNameContainingIgnoreCase(
            String email, String displayName, org.springframework.data.domain.Pageable pageable);
    Optional<AppUserEntity> findByIssuerAndExternalSubject(String issuer, String externalSubject);
    Optional<AppUserEntity> findByNormalizedEmail(String normalizedEmail);
}
