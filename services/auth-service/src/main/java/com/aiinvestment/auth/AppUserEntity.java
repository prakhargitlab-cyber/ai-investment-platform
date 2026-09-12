package com.aiinvestment.auth;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "app_users")
public class AppUserEntity {
    @Id
    private UUID id;
    @Column(nullable = false)
    private String issuer;
    @Column(name = "external_subject", nullable = false)
    private String externalSubject;
    private String email;
    @Column(name = "normalized_email")
    private String normalizedEmail;
    @Column(name = "password_hash")
    private String passwordHash;
    @Column(name = "account_status")
    private String accountStatus;
    @Column(name = "email_verified_at")
    private Instant emailVerifiedAt;
    @Column(name = "last_login_at")
    private Instant lastLoginAt;
    @Column(name = "display_name")
    private String displayName;
    @Column(name = "created_at", nullable = false)
    private Instant createdAt;
    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    protected AppUserEntity() {
    }

    public AppUserEntity(UUID id, String issuer, String externalSubject, String email, String displayName, Instant createdAt, Instant updatedAt) {
        this.id = id;
        this.issuer = issuer;
        this.externalSubject = externalSubject;
        this.email = email;
        this.displayName = displayName;
        this.createdAt = createdAt;
        this.updatedAt = updatedAt;
    }

    public UUID getId() { return id; }
    public String getIssuer() { return issuer; }
    public String getExternalSubject() { return externalSubject; }
    public String getEmail() { return email; }
    public String getDisplayName() { return displayName; }
    public Instant getCreatedAt() { return createdAt; }
    public Instant getUpdatedAt() { return updatedAt; }
    public void setEmail(String email) { this.email = email; }
    public void setDisplayName(String displayName) { this.displayName = displayName; }
    public void setUpdatedAt(Instant updatedAt) { this.updatedAt = updatedAt; }
    public String getNormalizedEmail() { return normalizedEmail; }
    public String getPasswordHash() { return passwordHash; }
    public String getAccountStatus() { return accountStatus; }
    public Instant getEmailVerifiedAt() { return emailVerifiedAt; }
    public Instant getLastLoginAt() { return lastLoginAt; }
    public void activate(Instant now) { this.accountStatus = "ACTIVE"; this.emailVerifiedAt = now; this.updatedAt = now; }
    public void recordLogin(Instant now) { this.lastLoginAt = now; this.updatedAt = now; }
    public void resetPassword(String encodedPassword, Instant now) { this.passwordHash = encodedPassword; this.updatedAt = now; }
    public void refreshPendingRegistration(String email, String passwordHash, String displayName, Instant now) {
        this.email = email;
        this.passwordHash = passwordHash;
        this.displayName = displayName;
        this.updatedAt = now;
    }
    public static AppUserEntity local(UUID id, String issuer, String email, String normalizedEmail, String passwordHash, String displayName, Instant now) {
        AppUserEntity user = new AppUserEntity(id, issuer, id.toString(), email, displayName, now, now);
        user.normalizedEmail = normalizedEmail; user.passwordHash = passwordHash; user.accountStatus = "EMAIL_VERIFICATION_PENDING";
        return user;
    }
}
