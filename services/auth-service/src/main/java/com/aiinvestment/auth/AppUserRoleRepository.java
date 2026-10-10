package com.aiinvestment.auth;

import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.UUID;

public interface AppUserRoleRepository extends JpaRepository<AppUserRoleEntity, AppUserRoleEntity.Key> {
    List<AppUserRoleEntity> findByUserId(UUID userId);
    boolean existsByUserIdAndRole(UUID userId, String role);
    void deleteByUserIdAndRole(UUID userId, String role);
    boolean existsByRole(String role);
}
