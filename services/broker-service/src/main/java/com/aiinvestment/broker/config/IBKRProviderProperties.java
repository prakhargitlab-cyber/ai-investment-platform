package com.aiinvestment.broker.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties(prefix = "broker.ibkr")
public record IBKRProviderProperties(
        boolean enabled,
        String baseUrl,
        String oauth2BaseUrl,
        String gatewayBaseUrl,
        String clientId,
        String clientKeyId,
        String callbackUrl,
        String authMethod,
        boolean officialDocumentationVerified,
        boolean marketDataEnabled,
        boolean insecureTls,
        String connectorBaseUrl,
        String connectorRuntimeMode,
        String connectorLoginUrl,
        String connectorInternalToken,
        int connectorIdleTimeoutSeconds,
        int connectorSessionTimeoutSeconds,
        String privateKeyReference,
        String sessionReference
) {
}
