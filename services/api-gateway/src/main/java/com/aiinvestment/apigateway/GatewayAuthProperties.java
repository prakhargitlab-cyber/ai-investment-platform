package com.aiinvestment.apigateway;

import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties(prefix = "auth")
public record GatewayAuthProperties(
        String issuer,
        String jwtSecret
) {
}
