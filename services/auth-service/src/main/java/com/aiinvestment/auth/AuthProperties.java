package com.aiinvestment.auth;

import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties(prefix = "auth")
public record AuthProperties(
        String issuer,
        String jwtSecret,
        long tokenTtlSeconds,
        long verificationTokenTtlSeconds,
        long passwordResetTokenTtlSeconds,
        boolean devLoginEnabled,
        String emailMode,
        String smtpHost,
        int smtpPort,
        String smtpUsername,
        String smtpPassword,
        String smtpFrom,
        String appPublicUrl
) {
}
