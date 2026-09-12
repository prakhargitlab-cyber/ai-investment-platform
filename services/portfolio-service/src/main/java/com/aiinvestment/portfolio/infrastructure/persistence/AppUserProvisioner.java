package com.aiinvestment.portfolio.infrastructure.persistence;

import com.aiinvestment.shared.web.auth.AuthenticatedUser;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;

import java.sql.Timestamp;
import java.time.Instant;

@Component
public class AppUserProvisioner {
    private final JdbcTemplate jdbcTemplate;
    private final String appUsersTable;
    private final boolean h2;

    public AppUserProvisioner(JdbcTemplate jdbcTemplate,
                              @Value("${spring.jpa.properties.hibernate.default_schema:}") String schema,
                              @Value("${spring.datasource.url:}") String datasourceUrl) {
        this.jdbcTemplate = jdbcTemplate;
        this.appUsersTable = schema == null || schema.isBlank() ? "app_users" : schema + ".app_users";
        this.h2 = datasourceUrl != null && datasourceUrl.startsWith("jdbc:h2:");
    }

    public void upsert(AuthenticatedUser user) {
        Instant now = Instant.now();
        if (h2) {
            jdbcTemplate.update("""
                            MERGE INTO %s (id, issuer, external_subject, email, display_name, created_at, updated_at)
                            KEY (id) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """.formatted(appUsersTable),
                    user.userId(),
                    user.issuer(),
                    user.subject(),
                    user.email(),
                    user.displayName(),
                    Timestamp.from(now),
                    Timestamp.from(now));
            return;
        }
        // Match H2 KEY(id) behavior: LOCAL MCP reuses the same durable user id as
        // auth/dev-login under a service issuer/subject, so conflict must be on id.
        jdbcTemplate.update("""
                        INSERT INTO %s (id, issuer, external_subject, email, display_name, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (id) DO UPDATE
                        SET email = COALESCE(EXCLUDED.email, %s.email),
                            display_name = COALESCE(EXCLUDED.display_name, %s.display_name),
                            updated_at = EXCLUDED.updated_at
                        """.formatted(appUsersTable, appUsersTable, appUsersTable),
                user.userId(),
                user.issuer(),
                user.subject(),
                user.email(),
                user.displayName(),
                Timestamp.from(now),
                Timestamp.from(now));
    }
}
