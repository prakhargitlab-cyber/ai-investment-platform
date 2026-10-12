package com.aiinvestment.auth;

import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.UUID;

public interface AppUserRoleAuditRepository extends JpaRepository<AppUserRoleAuditEntity, UUID> {
    org.springframework.data.domain.Page<AppUserRoleAuditEntity> findByUserId(UUID userId, org.springframework.data.domain.Pageable pageable);
    List<AppUserRoleAuditEntity> findByUserIdOrderByCreatedAtDesc(UUID userId);
}
