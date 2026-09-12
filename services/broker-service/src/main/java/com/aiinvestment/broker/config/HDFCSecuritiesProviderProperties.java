package com.aiinvestment.broker.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties(prefix = "broker.hdfc-securities")
public record HDFCSecuritiesProviderProperties(
        String baseUrl,
        String loginUrl,
        long sessionTtlSeconds
) {}
