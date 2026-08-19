package com.aiinvestment.shared.domain.broker.auth;

public record SensitiveTokenReference(String keyRef) {
    public SensitiveTokenReference {
        if (keyRef == null || keyRef.isBlank()) {
            throw new IllegalArgumentException("Token reference is required");
        }
    }
}
