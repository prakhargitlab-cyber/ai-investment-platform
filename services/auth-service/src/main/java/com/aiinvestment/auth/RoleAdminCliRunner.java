package com.aiinvestment.auth;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.boot.ExitCodeGenerator;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Component;

import java.util.Locale;

@Component
@Profile("role-admin-cli")
public class RoleAdminCliRunner implements ApplicationRunner, ExitCodeGenerator {
    private static final Logger log = LoggerFactory.getLogger(RoleAdminCliRunner.class);

    private final RoleAdminService service;
    private final RoleAdminProperties properties;
    private int exitCode;

    public RoleAdminCliRunner(RoleAdminService service, RoleAdminProperties properties) {
        this.service = service;
        this.properties = properties;
    }

    @Override
    public void run(ApplicationArguments args) {
        exitCode = 0;
        try {
            String action = requireValue(properties.action(), "role-admin.action (GRANT|REVOKE)");
            String email = requireValue(properties.email(), "role-admin.email");
            String operator = requireValue(properties.operator(), "role-admin.operator");
            String role = properties.role() == null || properties.role().isBlank() ? "ADMIN" : properties.role();
            String normalizedAction = action.trim().toUpperCase(Locale.ROOT);
            if (normalizedAction.equals("GRANT")) {
                service.grantRole(email, role, operator, properties.reason());
                log.info("Granted role {} to {} (operator={})", role, email, operator);
            } else if (normalizedAction.equals("REVOKE")) {
                service.revokeRole(email, role, operator, properties.reason());
                log.info("Revoked role {} from {} (operator={})", role, email, operator);
            } else {
                throw new IllegalArgumentException("Unsupported role-admin.action: " + action + " (expected GRANT or REVOKE)");
            }
        } catch (RoleAdminException | IllegalArgumentException exc) {
            log.error("Role admin operation failed: {}", exc.getMessage());
            exitCode = 1;
        } catch (Exception exc) {
            log.error("Role admin operation failed ({})", exc.getClass().getSimpleName());
            exitCode = 1;
        }
    }

    @Override
    public int getExitCode() { return exitCode; }

    private static String requireValue(String value, String name) {
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException(name + " is required");
        }
        return value;
    }
}
