package com.aiinvestment.broker.security;

import java.util.Optional;

public interface SecretProvider {
    Optional<String> getSecret(String key);
}
