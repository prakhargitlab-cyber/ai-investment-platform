package com.aiinvestment.auth;

import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties(prefix = "role-admin")
public record RoleAdminProperties(String action, String email, String role, String operator, String reason) {
}
