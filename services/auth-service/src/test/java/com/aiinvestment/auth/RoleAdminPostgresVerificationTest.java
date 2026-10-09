package com.aiinvestment.auth;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfSystemProperty;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.springframework.aop.support.AopUtils;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.DefaultApplicationArguments;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import javax.sql.DataSource;
import java.sql.Connection;
import java.sql.SQLException;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import static org.assertj.core.api.Assertions.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;

/** Opt-in only. Inherits the admin regression suite, overriding H2 with disposable PostgreSQL. */
@EnabledIfSystemProperty(named = "roleAdmin.disposablePostgresUrl", matches = "jdbc:postgresql://127\\.0\\.0\\.1:[0-9]+/role_admin_verify_[0-9a-f]{12}")
class RoleAdminPostgresVerificationTest extends AdminUserIntegrationTest {
    private static final long COMMIT_GATE = 731942067L;
    @Autowired DataSource dataSource;
    @Autowired JdbcTemplate jdbc;

    @DynamicPropertySource
    static void disposableDatabase(DynamicPropertyRegistry registry) {
        String url = System.getProperty("roleAdmin.disposablePostgresUrl", "");
        if (!url.matches("jdbc:postgresql://127\\.0\\.0\\.1:[0-9]+/role_admin_verify_[0-9a-f]{12}")) {
            throw new IllegalStateException("An explicitly named disposable loopback PostgreSQL database is required");
        }
        registry.add("spring.datasource.url", () -> url);
        registry.add("spring.datasource.username", () -> "postgres");
        registry.add("spring.datasource.password", () -> "");
        registry.add("spring.datasource.driver-class-name", () -> "org.postgresql.Driver");
        registry.add("spring.flyway.url", () -> url);
        registry.add("spring.flyway.user", () -> "postgres");
        registry.add("spring.flyway.password", () -> "");
    }

    @BeforeEach
    void installDisposableWitnesses() {
        assertThat(jdbc.queryForObject("select current_database()", String.class)).startsWith("role_admin_verify_");
        jdbc.execute("create table if not exists auth.verification_events (txid bigint, target uuid, source text, operation text)");
        jdbc.execute("truncate auth.verification_events");
        jdbc.execute("""
                create or replace function auth.verification_witness() returns trigger language plpgsql as $$
                begin
                  if TG_OP = 'DELETE' then
                    insert into auth.verification_events values (txid_current(), OLD.user_id, TG_TABLE_NAME, TG_OP);
                    return OLD;
                  end if;
                  insert into auth.verification_events values (txid_current(), NEW.user_id, TG_TABLE_NAME, TG_OP);
                  return NEW;
                end $$
                """);
        for (String table : List.of("app_user_roles", "app_user_role_audit")) {
            jdbc.execute("drop trigger if exists verification_witness on auth." + table);
            jdbc.execute("create trigger verification_witness after insert or delete on auth." + table
                    + " for each row execute function auth.verification_witness()");
        }
        jdbc.execute("""
                create or replace function auth.verification_commit_check() returns trigger language plpgsql as $$
                begin
                  if NEW.reason = 'VERIFY_FAIL_AT_COMMIT' then
                    raise exception 'Disposable verification: injected failure at commit';
                  end if;
                  if NEW.reason = 'VERIFY_PAUSE_AT_COMMIT' then
                    perform pg_advisory_xact_lock(731942067);
                  end if;
                  return NEW;
                end $$
                """);
        jdbc.execute("drop trigger if exists verification_commit_check on auth.app_user_role_audit");
        jdbc.execute("""
                create constraint trigger verification_commit_check after insert on auth.app_user_role_audit
                deferrable initially deferred for each row execute function auth.verification_commit_check()
                """);
    }

    @Test void realPostgresAndSpringTransactionProxyAreInUse() throws Exception {
        try (Connection connection = dataSource.getConnection()) {
            assertThat(connection.getMetaData().getDatabaseProductName()).isEqualTo("PostgreSQL");
            assertThat(connection.getTransactionIsolation()).isEqualTo(Connection.TRANSACTION_READ_COMMITTED);
            System.out.println("DISPOSABLE_POSTGRES_VERSION=" + connection.getMetaData().getDatabaseProductVersion());
        }
        assertThat(AopUtils.isAopProxy(service)).isTrue();
    }

    @ParameterizedTest
    @CsvSource({"CLI,GRANT", "CLI,REVOKE", "HTTP,GRANT", "HTTP,REVOKE"})
    void deferredCommitFailureRollsBackRoleAndAudit(String path, String action) throws Exception {
        boolean grant = action.equals("GRANT");
        if (!grant) {
            // Synthetic fixture only, in the guarded disposable database.
            jdbc.update("insert into auth.app_user_roles(user_id, role, granted_at) values (?, 'ADMIN', current_timestamp)", user.getId());
        }
        jdbc.execute("truncate auth.verification_events");
        if (path.equals("CLI")) {
            // Actual runner + Spring-proxied service, without activating a startup CLI profile.
            RoleAdminCliRunner runner = new RoleAdminCliRunner(service,
                    new RoleAdminProperties(action, user.getEmail(), "ADMIN", "verification-operator", "VERIFY_FAIL_AT_COMMIT"));
            runner.run(new DefaultApplicationArguments());
            assertThat(runner.getExitCode()).isEqualTo(1);
        } else {
            var request = grant ? put(roleUrl()) : delete(roleUrl());
            assertThatThrownBy(() -> mvc.perform(request.header("Authorization", adminToken()).contentType(MediaType.APPLICATION_JSON)
                    .content("{\"reason\":\"VERIFY_FAIL_AT_COMMIT\"}"))).hasStackTraceContaining("injected failure at commit");
        }
        assertThat(roles.existsByUserIdAndRole(user.getId(), "ADMIN")).isEqualTo(!grant);
        assertThat(audit.findByUserIdOrderByCreatedAtDesc(user.getId())).isEmpty();
        assertThat(jdbc.queryForObject("select count(*) from auth.verification_events where target = ?", Long.class, user.getId())).isZero();
        assertMutexReleased();
        System.out.println("ROLLBACK_VERIFIED path=" + path + " action=" + action + " roleUnchanged=true auditAbsent=true mutexReleased=true");
    }

    @ParameterizedTest
    @CsvSource({"CLI,GRANT", "CLI,REVOKE", "HTTP,GRANT", "HTTP,REVOKE"})
    void mutexIsHeldThroughCommitAndRoleAndAuditShareTransaction(String path, String action) throws Exception {
        boolean grant = action.equals("GRANT");
        if (!grant) jdbc.update("insert into auth.app_user_roles(user_id, role, granted_at) values (?, 'ADMIN', current_timestamp)", user.getId());
        jdbc.execute("truncate auth.verification_events");
        try (Connection gate = dataSource.getConnection()) {
            gate.createStatement().execute("select pg_advisory_lock(" + COMMIT_GATE + ")");
            var pidResult = gate.createStatement().executeQuery("select pg_backend_pid()");
            pidResult.next();
            int gatePid = pidResult.getInt(1);
            CompletableFuture<Integer> operation = CompletableFuture.supplyAsync(() -> {
                if (path.equals("CLI")) {
                    RoleAdminCliRunner runner = new RoleAdminCliRunner(service,
                            new RoleAdminProperties(action, user.getEmail(), "ADMIN", "verification-operator", "VERIFY_PAUSE_AT_COMMIT"));
                    runner.run(new DefaultApplicationArguments());
                    return runner.getExitCode();
                }
                try {
                    var request = grant ? put(roleUrl()) : delete(roleUrl());
                    return mvc.perform(request.header("Authorization", adminToken()).contentType(MediaType.APPLICATION_JSON)
                            .content("{\"reason\":\"VERIFY_PAUSE_AT_COMMIT\"}")).andReturn().getResponse().getStatus();
                } catch (Exception ex) { throw new IllegalStateException(ex); }
            });
            try {
                Instant deadline = Instant.now().plus(Duration.ofSeconds(15));
                while (jdbc.queryForObject("select count(*) from pg_stat_activity where ? = any(pg_blocking_pids(pid)) and wait_event = 'advisory'", Long.class, gatePid) == 0) {
                    assertThat(Instant.now()).isBefore(deadline);
                    assertThat(operation.isDone()).isFalse();
                    Thread.sleep(50);
                }
                // COMMIT is blocked after both writes. Neither write is visible yet.
                assertThat(roles.existsByUserIdAndRole(user.getId(), "ADMIN")).isEqualTo(!grant);
                assertThat(audit.findByUserIdOrderByCreatedAtDesc(user.getId())).isEmpty();
                try (Connection probe = dataSource.getConnection()) {
                    probe.setAutoCommit(false);
                    try {
                        assertThatThrownBy(() -> probe.createStatement().execute("select id from auth.role_admin_lock where id = 1 for update nowait"))
                                .isInstanceOfSatisfying(SQLException.class, ex -> assertThat(ex.getSQLState()).isEqualTo("55P03"));
                    } finally { probe.rollback(); }
                }
            } finally {
                gate.createStatement().execute("select pg_advisory_unlock(" + COMMIT_GATE + ")");
                assertThat(operation.get(15, TimeUnit.SECONDS)).isEqualTo(path.equals("CLI") ? 0 : 204);
            }
        }
        assertThat(roles.existsByUserIdAndRole(user.getId(), "ADMIN")).isEqualTo(grant);
        assertThat(audit.findByUserIdOrderByCreatedAtDesc(user.getId())).hasSize(1);
        var witnesses = jdbc.queryForList("select txid, source, operation from auth.verification_events where target = ?", user.getId());
        assertThat(witnesses).hasSize(2);
        assertThat(witnesses.stream().map(row -> row.get("txid")).distinct()).hasSize(1);
        assertThat(witnesses.stream().map(row -> row.get("source"))).containsExactlyInAnyOrder("app_user_roles", "app_user_role_audit");
        assertMutexReleased();
        System.out.println("COMMIT_VERIFIED path=" + path + " action=" + action + " sameTxid=" + witnesses.get(0).get("txid")
                + " mutexBlockedUntilCommit=true mutexReleased=true");
    }

    private void assertMutexReleased() throws Exception {
        try (Connection connection = dataSource.getConnection()) {
            connection.setAutoCommit(false);
            try { connection.createStatement().execute("select id from auth.role_admin_lock where id = 1 for update nowait"); }
            finally { connection.rollback(); }
        }
    }

    private String roleUrl() { return "/api/v1/auth/admin/users/" + user.getId() + "/roles/ADMIN"; }
    private String adminToken() {
        return "Bearer " + new com.aiinvestment.shared.web.auth.HmacJwtService("test-auth-jwt-secret-at-least-32-characters")
                .issue(new com.aiinvestment.shared.web.auth.JwtClaims("test-auth-issuer", admin.getExternalSubject(), admin.getEmail(), "Fixture admin", List.of("ADMIN"), Instant.now().plusSeconds(300)));
    }
}
