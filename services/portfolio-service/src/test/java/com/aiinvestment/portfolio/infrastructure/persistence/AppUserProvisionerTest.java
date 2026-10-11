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

    // The production conflict target is deliberately `id`, not `(issuer, external_subject)`: the
    // MCP gateway (ai/mcp-gateway/app/contracts.py, McpAuthContext.identity_headers()) calls back
    // into portfolio-service on an already-known user's behalf using that same durable `id`, but
    // under its own service identity as `issuer` and the id itself as `subject` -- a different
    // (issuer, external_subject) pair than that person's own login produced. Conflicting on
    // (issuer, external_subject) instead would make every such MCP call collide with the existing
    // row's primary key and fail. `email`/`display_name` use COALESCE rather than a straight
    // overwrite for the same reason: the MCP gateway never sends either header, so an unconditional
    // overwrite would null out a real user's metadata the first time an MCP-originated call touched
    // their row. `issuer`/`external_subject` are never reassigned on conflict so that an MCP call's
    // service-identity values can never overwrite a user's real, originally-recorded identity
    // provenance.
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
            assertThat(statement).contains("ON CONFLICT (id) DO UPDATE");
            assertThat(statement).contains("email = COALESCE(EXCLUDED.email, portfolio.app_users.email)");
            assertThat(statement).contains("display_name = COALESCE(EXCLUDED.display_name, portfolio.app_users.display_name)");
            assertThat(statement).doesNotContain("issuer = EXCLUDED.issuer");
            assertThat(statement).doesNotContain("external_subject = EXCLUDED.external_subject");
        });
    }

    // Explicit regression coverage for the MCP gateway path: the same user `id` is upserted again
    // under a different (issuer, external_subject) pair -- exactly the shape
    // McpAuthContext.identity_headers() sends (serviceIdentity as issuer, the user id itself as
    // subject, no email/display name). This must succeed via the same `ON CONFLICT (id)` statement
    // with no app-level branching or rejection, and must not attempt to reassign the identity
    // columns to the MCP-supplied values.
    @Test
    void sameUserIdWithDifferentIssuerAndSubjectUpsertsWithoutIdentityConflict_mcpGatewayPath() {
        JdbcTemplate jdbc = mock(JdbcTemplate.class);
        AppUserProvisioner provisioner = new AppUserProvisioner(jdbc, "portfolio", "jdbc:postgresql://localhost/portfolio");
        UUID id = UUID.randomUUID();
        AuthenticatedUser loggedInUser = user(id, "person@example.test", "Real Person");
        AuthenticatedUser mcpGatewayUser = new AuthenticatedUser(id, "mcp-gateway", id.toString(), null, null, List.of("USER"));

        provisioner.upsert(loggedInUser);
        provisioner.upsert(mcpGatewayUser);

        ArgumentCaptor<String> sql = ArgumentCaptor.forClass(String.class);
        ArgumentCaptor<Object[]> args = ArgumentCaptor.forClass(Object[].class);
        verify(jdbc, times(2)).update(sql.capture(), args.capture());

        assertThat(sql.getAllValues()).allSatisfy(statement -> {
            assertThat(statement).contains("ON CONFLICT (id) DO UPDATE");
            assertThat(statement).doesNotContain("issuer = EXCLUDED.issuer");
            assertThat(statement).doesNotContain("external_subject = EXCLUDED.external_subject");
        });

        Object[] mcpCallParams = args.getAllValues().get(1);
        assertThat(mcpCallParams[0]).isEqualTo(id);
        assertThat(mcpCallParams[1]).isEqualTo("mcp-gateway");
        assertThat(mcpCallParams[2]).isEqualTo(id.toString());
    }

    // Explicit regression coverage for null-metadata preservation: when a caller (e.g. the MCP
    // gateway, which never supplies email/display name at all) upserts with null metadata, the SQL
    // must fall back to the existing stored value via COALESCE rather than overwrite it -- and the
    // bound parameters sent for email/display name are exactly the null values the caller provided,
    // confirming the fallback is delegated to the database, not silently defaulted in application code.
    @Test
    void nullMetadataIsCoalescedAgainstExistingValuesNotOverwritten() {
        JdbcTemplate jdbc = mock(JdbcTemplate.class);
        AppUserProvisioner provisioner = new AppUserProvisioner(jdbc, "portfolio", "jdbc:postgresql://localhost/portfolio");
        UUID id = UUID.randomUUID();
        AuthenticatedUser mcpGatewayUser = new AuthenticatedUser(id, "mcp-gateway", id.toString(), null, null, List.of("USER"));

        provisioner.upsert(mcpGatewayUser);

        ArgumentCaptor<String> sql = ArgumentCaptor.forClass(String.class);
        ArgumentCaptor<Object[]> args = ArgumentCaptor.forClass(Object[].class);
        verify(jdbc).update(sql.capture(), args.capture());

        assertThat(sql.getValue()).contains("email = COALESCE(EXCLUDED.email, portfolio.app_users.email)");
        assertThat(sql.getValue()).contains("display_name = COALESCE(EXCLUDED.display_name, portfolio.app_users.display_name)");
        assertThat(args.getValue()[3]).isNull();
        assertThat(args.getValue()[4]).isNull();
    }

    // Complement to the null-metadata case: when a caller does supply fresh non-null values (a
    // normal re-login with an updated profile), those values are what get bound and handed to
    // COALESCE, so the refresh takes effect instead of being preserved.
    @Test
    void nonNullMetadataIsBoundForRefreshOnSubsequentUpsert() {
        JdbcTemplate jdbc = mock(JdbcTemplate.class);
        AppUserProvisioner provisioner = new AppUserProvisioner(jdbc, "portfolio", "jdbc:postgresql://localhost/portfolio");
        UUID id = UUID.randomUUID();

        provisioner.upsert(user(id, "first@example.test", "First Name"));
        provisioner.upsert(user(id, "updated@example.test", "Updated Name"));

        ArgumentCaptor<Object[]> args = ArgumentCaptor.forClass(Object[].class);
        verify(jdbc, times(2)).update(anyString(), args.capture());
        Object[] secondCallParams = args.getAllValues().get(1);
        assertThat(secondCallParams[3]).isEqualTo("updated@example.test");
        assertThat(secondCallParams[4]).isEqualTo("Updated Name");
    }

    // Explicit regression coverage for existing-identity immutability under the MCP path
    // specifically: even though the MCP-originated call supplies a *different* issuer/subject in
    // its own VALUES clause (as any INSERT must), the SET clause guarantees those bound values are
    // never applied to an existing row's issuer/external_subject columns -- the row's original,
    // real identity provenance from its first insert is preserved regardless of what a later
    // same-id caller's own issuer/subject happen to be.
    @Test
    void existingIssuerAndExternalSubjectAreNeverReassignedWhenSameIdIsUpsertedUnderADifferentIdentity() {
        JdbcTemplate jdbc = mock(JdbcTemplate.class);
        AppUserProvisioner provisioner = new AppUserProvisioner(jdbc, "portfolio", "jdbc:postgresql://localhost/portfolio");
        UUID id = UUID.randomUUID();

        provisioner.upsert(user(id, "person@example.test", "Real Person"));
        provisioner.upsert(new AuthenticatedUser(id, "mcp-gateway", id.toString(), null, null, List.of("USER")));

        ArgumentCaptor<String> sql = ArgumentCaptor.forClass(String.class);
        verify(jdbc, times(2)).update(sql.capture(), any(Object[].class));
        assertThat(sql.getAllValues()).allSatisfy(statement -> {
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
