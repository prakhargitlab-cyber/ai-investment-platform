package com.aiinvestment.shared.web.auth;

import java.time.Instant;
import java.util.List;

public record JwtClaims(
        String issuer,
        String subject,
        String email,
        String displayName,
        List<String> roles,
        Instant issuedAt,
        Instant expiresAt
) {
    public JwtClaims {
        roles = roles == null ? List.of() : List.copyOf(roles);
    }
    public JwtClaims(String issuer, String subject, String email, String displayName, List<String> roles, Instant expiresAt) {
        this(issuer, subject, email, displayName, roles, Instant.now(), expiresAt);
    }
}
