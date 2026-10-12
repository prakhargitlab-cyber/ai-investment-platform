package com.aiinvestment.auth;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "app_user_role_audit")
public class AppUserRoleAuditEntity {
    @Id
    private UUID id;
    @Column(name = "user_id", nullable = false)
    private UUID userId;
    @Column(name = "target_email", nullable = false)
    private String targetEmail;
    @Column(nullable = false)
    private String role;
    @Column(nullable = false)
    private String action;
    @Column(nullable = false)
    private String operator;
    private String reason;
    @Column(name = "created_at", nullable = false)
    private Instant createdAt;

    protected AppUserRoleAuditEntity() {
    }

    public AppUserRoleAuditEntity(UUID id, UUID userId, String targetEmail, String role, String action, String operator, String reason, Instant createdAt) {
        this.id = id;
        this.userId = userId;
        this.targetEmail = targetEmail;
        this.role = role;
        this.action = action;
        this.operator = operator;
        this.reason = reason;
        this.createdAt = createdAt;
    }

    public UUID getId() { return id; }
    public UUID getUserId() { return userId; }
    public String getTargetEmail() { return targetEmail; }
    public String getRole() { return role; }
    public String getAction() { return action; }
    public String getOperator() { return operator; }
    public String getReason() { return reason; }
    public Instant getCreatedAt() { return createdAt; }
}
