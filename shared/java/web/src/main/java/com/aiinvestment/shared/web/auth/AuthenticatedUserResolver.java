package com.aiinvestment.shared.web.auth;

import jakarta.servlet.http.HttpServletRequest;
import org.springframework.http.HttpStatus;
import org.springframework.web.server.ResponseStatusException;

import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.List;
import java.util.UUID;

public final class AuthenticatedUserResolver {
    private AuthenticatedUserResolver() {
    }

    public static AuthenticatedUser require(HttpServletRequest request) {
        String userId = requireHeader(request, AuthenticationHeaders.USER_ID);
        String issuer = requireHeader(request, AuthenticationHeaders.ISSUER);
        String subject = requireHeader(request, AuthenticationHeaders.SUBJECT);
        String email = optionalHeader(request, AuthenticationHeaders.EMAIL);
        String displayName = optionalHeader(request, AuthenticationHeaders.DISPLAY_NAME);
        List<String> roles = roles(optionalHeader(request, AuthenticationHeaders.ROLES));
        return new AuthenticatedUser(UUID.fromString(userId), issuer, subject, email, displayName, roles);
    }

    public static UUID stableUserId(String issuer, String subject) {
        return UUID.nameUUIDFromBytes(("oidc|" + issuer + "|" + subject).getBytes(StandardCharsets.UTF_8));
    }

    private static String requireHeader(HttpServletRequest request, String name) {
        String value = optionalHeader(request, name);
        if (value == null || value.isBlank()) {
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "Authenticated user context is required");
        }
        return value;
    }

    private static String optionalHeader(HttpServletRequest request, String name) {
        String value = request.getHeader(name);
        return value == null || value.isBlank() ? null : value.trim();
    }

    private static List<String> roles(String value) {
        if (value == null || value.isBlank()) {
            return List.of();
        }
        return Arrays.stream(value.split(",")).map(String::trim).filter(role -> !role.isBlank()).toList();
    }
}
