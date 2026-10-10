package com.aiinvestment.auth;

import com.aiinvestment.shared.web.auth.HmacJwtService;
import com.aiinvestment.shared.web.auth.JwtClaims;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import java.time.Instant;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.*;
import static org.assertj.core.api.Assertions.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class AdminUserIntegrationTest {
    private static final String BASE = "/api/v1/auth/admin/users";
    private static final String SECRET = "test-auth-jwt-secret-at-least-32-characters";
    @Autowired MockMvc mvc;
    @Autowired AppUserRepository users;
    @Autowired AppUserRoleRepository roles;
    @Autowired AppUserRoleAuditRepository audit;
    @Autowired EmailVerificationTokenRepository verification;
    @Autowired PasswordResetTokenRepository resets;
    @Autowired RoleAdminService service;
    AppUserEntity admin, user;

    @BeforeEach void setup() {
        audit.deleteAll(); roles.deleteAll(); verification.deleteAll(); resets.deleteAll(); users.deleteAll();
        admin = account("admin@example.test"); user = account("user@example.test");
        service.grantRole(admin.getEmail(), "ADMIN", "bootstrap-operator", "Initial administrator");
    }

    @Test void userCannotBypassUiOrSpoofOperator() throws Exception {
        String token = token(user, "USER");
        mvc.perform(get(BASE).header("Authorization", token)).andExpect(status().isForbidden());
        mvc.perform(get(BASE + "/" + admin.getId()).header("Authorization", token)).andExpect(status().isForbidden());
        mvc.perform(get(BASE + "/" + admin.getId() + "/audit").header("Authorization", token)).andExpect(status().isForbidden());
        mvc.perform(put(roleUrl(user)).header("Authorization", token).header("X-AIP-User-Roles", "ADMIN")
                .contentType(MediaType.APPLICATION_JSON).content("{\"reason\":\"self promotion\",\"operator\":\"bootstrap-operator\"}"))
                .andExpect(status().isForbidden());
        mvc.perform(delete(roleUrl(admin)).header("Authorization", token).contentType(MediaType.APPLICATION_JSON)
                .content("{\"reason\":\"unauthorized revoke\"}")).andExpect(status().isForbidden());
        mvc.perform(get(BASE).header("X-AIP-User-Roles", "ADMIN").header("X-AIP-User-Id", admin.getId()))
                .andExpect(status().isUnauthorized());
        mvc.perform(get(BASE).header("Authorization", "Bearer broken")).andExpect(status().isUnauthorized());
    }

    @Test void adminCanSearchReadGrantRevokeAndAuditAuthenticatedIdentity() throws Exception {
        String token = token(admin, "USER", "ADMIN");
        mvc.perform(get(BASE).param("search", "USER@").param("size", "1").header("Authorization", token))
                .andExpect(status().isOk()).andExpect(jsonPath("$.totalElements").value(1))
                .andExpect(jsonPath("$.content[0].passwordHash").doesNotExist()).andExpect(header().string("Cache-Control", "no-store"));
        mvc.perform(get(BASE + "/" + user.getId()).header("Authorization", token))
                .andExpect(status().isOk()).andExpect(jsonPath("$.roles[0]").value("USER"));
        mvc.perform(put(roleUrl(user)).header("Authorization", token).header("X-AIP-User-Id", user.getId())
                .contentType(MediaType.APPLICATION_JSON).content("{\"reason\":\"Approved ticket\",\"operator\":\"forged\"}"))
                .andExpect(status().isNoContent());
        mvc.perform(get(BASE + "/" + user.getId() + "/audit").header("Authorization", token))
                .andExpect(status().isOk()).andExpect(jsonPath("$.content[0].operator").value("user:" + admin.getId()))
                .andExpect(jsonPath("$.content[0].reason").value("Approved ticket"));
        mvc.perform(delete(roleUrl(user)).header("Authorization", token).contentType(MediaType.APPLICATION_JSON)
                .content("{\"reason\":\"Access ended\"}")).andExpect(status().isNoContent());
        assertThat(roles.existsByUserIdAndRole(user.getId(), "ADMIN")).isFalse();
        assertThat(audit.findByUserIdOrderByCreatedAtDesc(user.getId())).hasSize(2);
    }

    @Test void validatesSelfChangesReasonsRolesMissingUsersAndPaging() throws Exception {
        String token = token(admin, "ADMIN");
        mvc.perform(put(roleUrl(admin)).header("Authorization", token).contentType(MediaType.APPLICATION_JSON)
                .content("{\"reason\":\"self\"}")).andExpect(status().isForbidden());
        mvc.perform(delete(roleUrl(admin)).header("Authorization", token).contentType(MediaType.APPLICATION_JSON)
                .content("{\"reason\":\"self\"}")).andExpect(status().isForbidden());
        mvc.perform(put(roleUrl(user)).header("Authorization", token).contentType(MediaType.APPLICATION_JSON)
                .content("{\"reason\":\" \"}")).andExpect(status().isBadRequest());
        mvc.perform(put(BASE + "/" + user.getId() + "/roles/USER").header("Authorization", token)
                .contentType(MediaType.APPLICATION_JSON).content("{\"reason\":\"unsupported\"}")).andExpect(status().isConflict());
        mvc.perform(put(BASE + "/" + UUID.randomUUID() + "/roles/ADMIN").header("Authorization", token)
                .contentType(MediaType.APPLICATION_JSON).content("{\"reason\":\"missing\"}")).andExpect(status().isNotFound());
        mvc.perform(get(BASE).param("size", "101").header("Authorization", token)).andExpect(status().isBadRequest());
    }

    @Test void rejectsStaleTokensAndInactiveOrUnverifiedAccounts() throws Exception {
        String stale = token(user, "ADMIN");
        mvc.perform(get(BASE).header("Authorization", stale)).andExpect(status().isForbidden());
        AppUserEntity pending = AppUserEntity.local(UUID.randomUUID(), "test-auth-issuer", "pending@example.test", "pending@example.test", "unused", "Pending", Instant.now());
        users.save(pending);
        assertThatThrownBy(() -> service.grantRole(pending.getEmail(), "ADMIN", "operator", "not verified"))
                .isInstanceOf(RoleAdminException.class);
        roles.save(new AppUserRoleEntity(pending.getId(), "ADMIN", Instant.now()));
        mvc.perform(get(BASE).header("Authorization", token(pending, "ADMIN"))).andExpect(status().isForbidden());
        service.grantRole(user.getEmail(), "ADMIN", "operator", "temporary");
        service.revokeRole(user.getEmail(), "ADMIN", "operator", "expired");
        mvc.perform(get(BASE).header("Authorization", stale)).andExpect(status().isForbidden());
    }

    @Test void cliCannotRemoveLastAdminOrOmitReason() {
        assertThatThrownBy(() -> service.revokeRole(admin.getEmail(), "ADMIN", "operator", "remove last"))
                .isInstanceOf(RoleAdminException.class).hasMessageContaining("last active");
        assertThatThrownBy(() -> service.grantRole(user.getEmail(), "ADMIN", "operator", null)).isInstanceOf(RoleAdminException.class);
        assertThat(roles.existsByUserIdAndRole(admin.getId(), "ADMIN")).isTrue();
    }

    @Test void expiredWrongIssuerAndWrongSignatureTokensAreRejected() throws Exception {
        JwtClaims expired = new JwtClaims("test-auth-issuer", admin.getExternalSubject(), admin.getEmail(), "Admin", List.of("ADMIN"), Instant.now().minusSeconds(120), Instant.now().minusSeconds(60));
        JwtClaims wrongIssuer = new JwtClaims("other-issuer", admin.getExternalSubject(), admin.getEmail(), "Admin", List.of("ADMIN"), Instant.now().plusSeconds(300));
        JwtClaims validClaims = new JwtClaims("test-auth-issuer", admin.getExternalSubject(), admin.getEmail(), "Admin", List.of("ADMIN"), Instant.now().plusSeconds(300));
        for (String jwt : List.of(new HmacJwtService(SECRET).issue(expired), new HmacJwtService(SECRET).issue(wrongIssuer),
                new HmacJwtService("different-secret-with-at-least-32-characters").issue(validClaims))) {
            mvc.perform(get(BASE).header("Authorization", "Bearer " + jwt)).andExpect(status().isUnauthorized());
        }
    }

    @Test void concurrentAdministratorsCannotRevokeEachOtherUsingStaleAuthority() throws Exception {
        service.grantRole(user.getEmail(), "ADMIN", "operator", "second administrator");
        JwtClaims first = new HmacJwtService(SECRET).verify(token(admin, "ADMIN").substring(7), "test-auth-issuer");
        JwtClaims second = new HmacJwtService(SECRET).verify(token(user, "ADMIN").substring(7), "test-auth-issuer");
        java.util.concurrent.atomic.AtomicInteger denied = new java.util.concurrent.atomic.AtomicInteger();
        runConcurrently(() -> changeOrCount(first, user.getId(), denied), () -> changeOrCount(second, admin.getId(), denied));
        assertThat(denied.get()).isEqualTo(1);
        assertThat(roles.findAll().stream().filter(role -> role.getRole().equals("ADMIN"))).hasSize(1);
    }

    private void changeOrCount(JwtClaims actor, UUID target, java.util.concurrent.atomic.AtomicInteger denied) {
        try { service.changeRole(actor, target, "ADMIN", "concurrent removal", false); }
        catch (org.springframework.web.server.ResponseStatusException ex) {
            assertThat(ex.getStatusCode().value()).isEqualTo(403);
            denied.incrementAndGet();
        }
    }

    @Test void concurrentDuplicateGrantsAreIdempotent() throws Exception {
        runConcurrently(() -> service.grantRole(user.getEmail(), "ADMIN", "operator", "approved"),
                () -> service.grantRole(user.getEmail(), "ADMIN", "operator", "approved"));
        assertThat(roles.findByUserId(user.getId()).stream().filter(role -> role.getRole().equals("ADMIN"))).hasSize(1);
        assertThat(audit.findByUserIdOrderByCreatedAtDesc(user.getId())).hasSize(1);
        service.revokeRole(user.getEmail(), "ADMIN", "operator", "ended");
        service.revokeRole(user.getEmail(), "ADMIN", "operator", "ended");
        assertThat(audit.findByUserIdOrderByCreatedAtDesc(user.getId())).hasSize(2);
    }

    @Test void concurrentRevocationsPreserveOneAdministrator() throws Exception {
        service.grantRole(user.getEmail(), "ADMIN", "operator", "second administrator");
        java.util.concurrent.atomic.AtomicInteger rejected = new java.util.concurrent.atomic.AtomicInteger();
        runConcurrently(() -> revokeOrCount(admin, rejected), () -> revokeOrCount(user, rejected));
        assertThat(rejected.get()).isEqualTo(1);
        assertThat(roles.findAll().stream().filter(role -> role.getRole().equals("ADMIN"))).hasSize(1);
    }

    private void revokeOrCount(AppUserEntity target, java.util.concurrent.atomic.AtomicInteger rejected) {
        try { service.revokeRole(target.getEmail(), "ADMIN", "operator", "concurrent removal"); }
        catch (RoleAdminException ex) { rejected.incrementAndGet(); }
    }

    private void runConcurrently(Runnable first, Runnable second) throws Exception {
        ExecutorService pool = Executors.newFixedThreadPool(2);
        CountDownLatch ready = new CountDownLatch(2), start = new CountDownLatch(1);
        try {
            Future<?> a = pool.submit(() -> awaitAndRun(ready, start, first));
            Future<?> b = pool.submit(() -> awaitAndRun(ready, start, second));
            assertThat(ready.await(5, TimeUnit.SECONDS)).isTrue(); start.countDown();
            a.get(15, TimeUnit.SECONDS); b.get(15, TimeUnit.SECONDS);
        } finally { start.countDown(); pool.shutdownNow(); }
    }
    private void awaitAndRun(CountDownLatch ready, CountDownLatch start, Runnable operation) {
        ready.countDown();
        try { if (!start.await(5, TimeUnit.SECONDS)) throw new IllegalStateException("start timeout"); }
        catch (InterruptedException ex) { Thread.currentThread().interrupt(); throw new IllegalStateException(ex); }
        operation.run();
    }
    private AppUserEntity account(String email) {
        AppUserEntity account = AppUserEntity.local(UUID.randomUUID(), "test-auth-issuer", email, email, "unused", "Test User", Instant.now());
        account.activate(Instant.now()); users.save(account);
        roles.save(new AppUserRoleEntity(account.getId(), "USER", Instant.now()));
        return account;
    }
    private String token(AppUserEntity account, String... assigned) {
        return "Bearer " + new HmacJwtService(SECRET).issue(new JwtClaims("test-auth-issuer", account.getExternalSubject(), "untrusted-email@example.test", "Test", List.of(assigned), Instant.now().plusSeconds(300)));
    }
    private String roleUrl(AppUserEntity account) { return BASE + "/" + account.getId() + "/roles/ADMIN"; }
}
