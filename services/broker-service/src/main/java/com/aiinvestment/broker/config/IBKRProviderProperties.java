package com.aiinvestment.broker.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties(prefix = "broker.ibkr")
public record IBKRProviderProperties(
        boolean enabled,
        String baseUrl,
        String clientId,
        String callbackUrl,
        String authMethod,
        boolean officialDocumentationVerified
) {
}
