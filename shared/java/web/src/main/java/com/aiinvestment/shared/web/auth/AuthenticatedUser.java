package com.aiinvestment.shared.web.auth;

import java.util.List;
import java.util.UUID;

public record AuthenticatedUser(
        UUID userId,
        String issuer,
        String subject,
        String email,
        String displayName,
        List<String> roles
) {
    public AuthenticatedUser {
        roles = roles == null ? List.of() : List.copyOf(roles);
    }
}
