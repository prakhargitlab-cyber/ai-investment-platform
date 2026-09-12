package com.aiinvestment.portfolio.infrastructure.persistence;

import com.aiinvestment.shared.web.auth.AuthenticatedUser;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;

import java.util.List;
import java.util.UUID;
import java.util.concurrent.Callable;
import java.util.concurrent.Executors;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

@SpringBootTest
@ActiveProfiles("test")
class AppUserProvisionerIntegrationTest {
    @Autowired
    private AppUserProvisioner provisioner;
    @Autowired
    private JdbcTemplate jdbcTemplate;

    @Test
    void repeatedAndConcurrentProvisioningLeavesOneUserAndRefreshesMetadata() throws Exception {
        UUID id = UUID.randomUUID();
        AuthenticatedUser initial = user(id, "subject-" + id, "first@example.test", "First Name");
        provisioner.upsert(initial);
        provisioner.upsert(user(id, "subject-" + id, "updated@example.test", "Updated Name"));

        var executor = Executors.newFixedThreadPool(4);
        try {
            var tasks = java.util.stream.IntStream.range(0, 8)
                    .<Callable<Void>>mapToObj(ignored -> () -> {
                        provisioner.upsert(user(id, "subject-" + id, "updated@example.test", "Updated Name"));
                        return null;
                    }).toList();
            for (var future : executor.invokeAll(tasks)) {
                future.get();
            }
        } finally {
            executor.shutdownNow();
        }

        assertThat(jdbcTemplate.queryForObject("SELECT COUNT(*) FROM portfolio.app_users WHERE id = ?", Integer.class, id))
                .isEqualTo(1);
        var row = jdbcTemplate.queryForMap("SELECT issuer, external_subject, email, display_name FROM portfolio.app_users WHERE id = ?", id);
        assertThat(row).containsEntry("issuer", "https://issuer.example.test")
                .containsEntry("external_subject", "subject-" + id)
                .containsEntry("email", "updated@example.test")
                .containsEntry("display_name", "Updated Name");
    }

    @Test
    void conflictingIssuerSubjectForDifferentUserIdRemainsFailClosed() {
        String subject = "conflicting-subject-" + UUID.randomUUID();
        provisioner.upsert(user(UUID.randomUUID(), subject, "first@example.test", "First"));

        assertThatThrownBy(() -> provisioner.upsert(user(UUID.randomUUID(), subject, "second@example.test", "Second")))
                .isInstanceOf(DataIntegrityViolationException.class);
    }

    private static AuthenticatedUser user(UUID id, String subject, String email, String displayName) {
        return new AuthenticatedUser(id, "https://issuer.example.test", subject, email, displayName, List.of("USER"));
    }
}
