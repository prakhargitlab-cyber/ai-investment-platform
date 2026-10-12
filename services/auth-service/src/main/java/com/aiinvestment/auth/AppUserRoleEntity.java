package com.aiinvestment.auth;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.IdClass;
import jakarta.persistence.Table;

import java.io.Serializable;
import java.time.Instant;
import java.util.Objects;
import java.util.UUID;

@Entity
@Table(name = "app_user_roles")
@IdClass(AppUserRoleEntity.Key.class)
public class AppUserRoleEntity {
    @Id
    @Column(name = "user_id")
    private UUID userId;
    @Id
    @Column(name = "role")
    private String role;
    @Column(name = "granted_at", nullable = false)
    private Instant grantedAt;

    protected AppUserRoleEntity() {
    }

    public AppUserRoleEntity(UUID userId, String role, Instant grantedAt) {
        this.userId = userId;
        this.role = role;
        this.grantedAt = grantedAt;
    }

    public UUID getUserId() { return userId; }
    public String getRole() { return role; }
    public Instant getGrantedAt() { return grantedAt; }

    public static class Key implements Serializable {
        private UUID userId;
        private String role;

        public Key() {
        }

        public Key(UUID userId, String role) {
            this.userId = userId;
            this.role = role;
        }

        @Override
        public boolean equals(Object other) {
            if (this == other) {
                return true;
            }
            if (!(other instanceof Key)) {
                return false;
            }
            Key key = (Key) other;
            return Objects.equals(userId, key.userId) && Objects.equals(role, key.role);
        }

        @Override
        public int hashCode() {
            return Objects.hash(userId, role);
        }
    }
}
