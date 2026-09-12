package com.aiinvestment.auth;
import jakarta.persistence.*;
import java.time.Instant;
import java.util.UUID;
@Entity @Table(name = "email_verification_tokens")
public class EmailVerificationTokenEntity {
    @Id private UUID id; @Column(name="user_id",nullable=false) private UUID userId; @Column(name="token_hash",nullable=false) private String tokenHash;
    @Column(name="expires_at",nullable=false) private Instant expiresAt; @Column(name="consumed_at") private Instant consumedAt; @Column(name="created_at",nullable=false) private Instant createdAt;
    protected EmailVerificationTokenEntity() {} public EmailVerificationTokenEntity(UUID id,UUID userId,String tokenHash,Instant expiresAt,Instant createdAt){this.id=id;this.userId=userId;this.tokenHash=tokenHash;this.expiresAt=expiresAt;this.createdAt=createdAt;}
    public UUID getUserId(){return userId;} public Instant getExpiresAt(){return expiresAt;} public Instant getConsumedAt(){return consumedAt;} public void consume(Instant now){consumedAt=now;}
}
