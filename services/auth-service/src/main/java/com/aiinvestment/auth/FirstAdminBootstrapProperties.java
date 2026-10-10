package com.aiinvestment.auth;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * Configuration for the one-shot "first-admin-bootstrap" Spring profile.
 *
 * Only the target account's email and password are accepted here. The
 * operator identity and audit reason recorded against the ADMIN grant are
 * intentionally fixed in {@link FirstAdminBootstrapService} rather than made
 * configurable, to keep this automated path narrow and auditable.
 */
@ConfigurationProperties(prefix = "first-admin-bootstrap")
public record FirstAdminBootstrapProperties(String email, String password) {
}
