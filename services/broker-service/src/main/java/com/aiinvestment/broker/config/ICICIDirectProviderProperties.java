package com.aiinvestment.broker.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties(prefix = "broker.icici-direct")
public record ICICIDirectProviderProperties(
        boolean enabled,
        String baseUrl,
        String clientId,
        String callbackUrl,
        String authMethod,
        boolean officialDocumentationVerified
) {
}
