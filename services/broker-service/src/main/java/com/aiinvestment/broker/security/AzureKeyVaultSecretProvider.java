package com.aiinvestment.broker.security;

import java.util.Optional;

public class AzureKeyVaultSecretProvider implements SecretProvider {
    /**
     * Key Vault secrets are projected by the Secrets Store CSI driver and
     * exposed to the process as environment variables. This adapter therefore
     * keeps Azure SDK concerns out of the broker domain and requires no client
     * secret or credential file.
     */
    @Override
    public Optional<String> getSecret(String key) {
        return Optional.ofNullable(System.getenv(key));
    }
}
