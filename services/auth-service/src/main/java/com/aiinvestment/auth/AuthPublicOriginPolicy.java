package com.aiinvestment.auth;

import java.net.URI;
import java.net.URISyntaxException;
import java.util.Arrays;

/**
 * Choose an email-link base only from explicitly trusted origins.
 * Never trust arbitrary Origin, Host, or X-Forwarded-* headers.
 */
public final class AuthPublicOriginPolicy {
    private AuthPublicOriginPolicy() {}

    public static String resolve(String origin, String configuredOrigins, String fallback) {
        if (origin == null || configuredOrigins == null) return fallback;
        String candidate = canonicalOrigin(origin);
        if (candidate == null) return fallback;
        boolean permitted = Arrays.stream(configuredOrigins.split(","))
                .map(String::trim)
                .map(AuthPublicOriginPolicy::canonicalOrigin)
                .anyMatch(candidate::equals);
        return permitted ? candidate : fallback;
    }

    private static String canonicalOrigin(String value) {
        if (value == null || value.isBlank() || !value.equals(value.trim())) return null;
        try {
            URI uri = new URI(value);
            if (uri.isOpaque() || uri.getRawUserInfo() != null || uri.getHost() == null
                    || uri.getRawPath() != null && !uri.getRawPath().isEmpty()
                    || uri.getRawQuery() != null || uri.getRawFragment() != null
                    || uri.getPort() < -1) return null;
            String scheme = uri.getScheme();
            if (!"http".equals(scheme) && !"https".equals(scheme)) return null;
            // Never allow http origins outside the loopback hosts.
            if ("http".equals(scheme) && !("localhost".equalsIgnoreCase(uri.getHost())
                    || "127.0.0.1".equals(uri.getHost()))) return null;
            return uri.toASCIIString();
        } catch (URISyntaxException e) {
            return null;
        }
    }
}
