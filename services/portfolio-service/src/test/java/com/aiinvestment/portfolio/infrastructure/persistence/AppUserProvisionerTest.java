package com.aiinvestment.portfolio.infrastructure.persistence;

import com.aiinvestment.shared.web.auth.AuthenticatedUser;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.jdbc.core.JdbcTemplate;

import java.util.List;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;

class AppUserProvisionerTest {

    @Test
    void postgresqlUsesAtomicIdentityUpsertAndRefreshesOnlyMutableMetadata() {
        JdbcTemplate jdbc = mock(JdbcTemplate.class);
        AppUserProvisioner provisioner = new AppUserProvisioner(jdbc, "portfolio", "jdbc:postgresql://localhost/portfolio");
        AuthenticatedUser user = user(UUID.randomUUID(), "first@example.test", "First Name");

        provisioner.upsert(user);
        provisioner.upsert(user(user.userId(), "updated@example.test", "Updated Name"));

        ArgumentCaptor<String> sql = ArgumentCaptor.forClass(String.class);
        verify(jdbc, times(2)).update(sql.capture(), any(Object[].class));
        assertThat(sql.getAllValues()).allSatisfy(statement -> {
            assertThat(statement).contains("ON CONFLICT (issuer, external_subject) DO UPDATE");
            assertThat(statement).contains("email = EXCLUDED.email");
            assertThat(statement).contains("display_name = EXCLUDED.display_name");
            assertThat(statement).doesNotContain("issuer = EXCLUDED.issuer");
            assertThat(statement).doesNotContain("external_subject = EXCLUDED.external_subject");
        });
    }

    @Test
    void postgresqlIdentityConflictsAreNotSwallowed() {
        JdbcTemplate jdbc = mock(JdbcTemplate.class);
        doThrow(new DataIntegrityViolationException("identity conflict"))
                .when(jdbc).update(anyString(), any(Object[].class));
        AppUserProvisioner provisioner = new AppUserProvisioner(jdbc, "portfolio", "jdbc:postgresql://localhost/portfolio");

        assertThatThrownBy(() -> provisioner.upsert(user(UUID.randomUUID(), "user@example.test", "User")))
                .isInstanceOf(DataIntegrityViolationException.class);
    }

    @Test
    void h2UsesEquivalentIdentityMergeForTestCompatibility() {
        JdbcTemplate jdbc = mock(JdbcTemplate.class);
        AppUserProvisioner provisioner = new AppUserProvisioner(jdbc, "portfolio", "jdbc:h2:mem:portfolio");

        provisioner.upsert(user(UUID.randomUUID(), "user@example.test", "User"));

        ArgumentCaptor<String> sql = ArgumentCaptor.forClass(String.class);
        verify(jdbc).update(sql.capture(), any(Object[].class));
        assertThat(sql.getValue()).contains("MERGE INTO portfolio.app_users");
        assertThat(sql.getValue()).contains("KEY (id)");
    }

    private static AuthenticatedUser user(UUID id, String email, String displayName) {
        return new AuthenticatedUser(id, "https://issuer.example.test", "subject-" + id,
                email, displayName, List.of("USER"));
    }
}
