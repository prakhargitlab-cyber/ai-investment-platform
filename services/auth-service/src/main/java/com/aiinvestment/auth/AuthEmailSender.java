package com.aiinvestment.auth;

import java.util.UUID;

public interface AuthEmailSender {
    boolean isConfigured();
    void sendVerification(UUID userId, String email, String rawToken);
    void sendPasswordReset(UUID userId, String email, String rawToken);
}
