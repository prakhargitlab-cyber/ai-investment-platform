package com.aiinvestment.broker.security;

import java.util.Optional;

public class AzureKeyVaultSecretProvider implements SecretProvider {
    @Override
    public Optional<String> getSecret(String key) {
        throw new UnsupportedOperationException("Azure Key Vault integration is a PRD boundary and is not required for local DEV.");
    }
}
