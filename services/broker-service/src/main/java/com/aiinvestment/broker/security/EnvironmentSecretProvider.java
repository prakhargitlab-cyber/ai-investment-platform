package com.aiinvestment.broker.security;

import org.springframework.stereotype.Component;

import java.util.Optional;

@Component
public class EnvironmentSecretProvider implements SecretProvider {
    @Override
    public Optional<String> getSecret(String key) {
        return Optional.ofNullable(System.getenv(key));
    }
}
