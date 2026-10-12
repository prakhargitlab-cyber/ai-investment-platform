package com.aiinvestment.auth;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.boot.ExitCodeGenerator;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Component;

/**
 * One-shot runner for the "first-admin-bootstrap" Spring profile.
 *
 * Exit codes (consumed by platform.ps1's Invoke-FirstAdminBootstrapIfNeeded):
 *   0 - CREATED or ADMIN_ALREADY_PRESENT (both are a successful no-error outcome)
 *   1 - validation / business-rule failure (RoleAdminException, IllegalArgumentException)
 *   2 - unexpected infrastructure failure
 *   3 - EXISTING_USERS_WITHOUT_ADMIN: fail-closed; requires explicit role-admin-cli recovery
 *
 * Never logs the supplied password, at any log level, in any branch.
 */
@Component
@Profile("first-admin-bootstrap")
public class FirstAdminBootstrapRunner implements ApplicationRunner, ExitCodeGenerator {
    private static final Logger log = LoggerFactory.getLogger(FirstAdminBootstrapRunner.class);

    private final FirstAdminBootstrapService service;
    private final FirstAdminBootstrapProperties properties;
    private int exitCode;

    public FirstAdminBootstrapRunner(FirstAdminBootstrapService service, FirstAdminBootstrapProperties properties) {
        this.service = service;
        this.properties = properties;
    }

    @Override
    public void run(ApplicationArguments args) {
        exitCode = 0;
        try {
            String email = requireValue(properties.email(), "first-admin-bootstrap.email");
            String password = requireValue(properties.password(), "first-admin-bootstrap.password");

            FirstAdminBootstrapOutcome outcome = service.bootstrap(email, password);
            switch (outcome) {
                case CREATED -> {
                    log.info("first_admin_bootstrap_outcome=CREATED email={}", email);
                    exitCode = 0;
                }
                case ADMIN_ALREADY_PRESENT -> {
                    log.info("first_admin_bootstrap_outcome=ADMIN_ALREADY_PRESENT");
                    exitCode = 0;
                }
                case EXISTING_USERS_WITHOUT_ADMIN -> {
                    log.error("first_admin_bootstrap_outcome=EXISTING_USERS_WITHOUT_ADMIN; "
                            + "accounts exist but none holds ADMIN; automatic bootstrap refuses to act; "
                            + "use the role-admin-cli profile after explicit authorized review");
                    exitCode = 3;
                }
            }
        } catch (RoleAdminException | IllegalArgumentException exc) {
            log.error("first_admin_bootstrap_failed message={}", exc.getMessage());
            exitCode = 1;
        } catch (Exception exc) {
            log.error("first_admin_bootstrap_failed exceptionType={}", exc.getClass().getSimpleName());
            exitCode = 2;
        }
    }

    @Override
    public int getExitCode() {
        return exitCode;
    }

    private static String requireValue(String value, String name) {
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException(name + " is required");
        }
        return value;
    }
}
