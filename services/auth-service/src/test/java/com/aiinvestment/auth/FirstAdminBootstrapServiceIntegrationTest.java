package com.aiinvestment.auth;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.transaction.PlatformTransactionManager;

import java.time.Instant;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Covers the required test matrix for Feature 1 (first-ADMIN bootstrap):
 * fresh empty database, existing ADMIN, existing USER without ADMIN,
 * repeated/idempotent bootstrap, and failure recovery (invalid input leaves
 * no partial state). Runs against the same H2-in-PostgreSQL-mode database
 * already used by RoleAdminServiceIntegrationTest.
 *
 * Concurrency is covered separately, as a best-effort check: H2's row
 * locking does not guarantee the same semantics as real PostgreSQL "SELECT
 * ... FOR UPDATE", so the authoritative concurrency guarantee should be
 * re-verified with a disposable real PostgreSQL database, following the
 * same opt-in pattern as RoleAdminPostgresVerificationTest.
 */
@SpringBootTest
@ActiveProfiles("test")
class FirstAdminBootstrapServiceIntegrationTest {
    @Autowired FirstAdminBootstrapService bootstrapService;
    @Autowired AppUserRepository users;
    @Autowired AppUserRoleRepository userRoles;
    @Autowired AppUserRoleAuditRepository roleAudit;
    @Autowired EmailVerificationTokenRepository verificationTokens;
    @Autowired PasswordResetTokenRepository resetTokens;
    @Autowired RoleAdminService roleAdminService;
    @Autowired PlatformTransactionManager transactionManager;

    @BeforeEach
    void clearData() {
        roleAudit.deleteAll();
        userRoles.deleteAll();
        verificationTokens.deleteAll();
        resetTokens.deleteAll();
        users.deleteAll();
    }

    @Test
    void freshEmptyDatabaseCreatesFirstAdmin() {
        FirstAdminBootstrapOutcome outcome = bootstrapService.bootstrap("first-admin@example.test", "correct-password-123");

        assertThat(outcome).isEqualTo(FirstAdminBootstrapOutcome.CREATED);
        AppUserEntity user = users.findByNormalizedEmail("first-admin@example.test").orElseThrow();
        assertThat(user.getAccountStatus()).isEqualTo("ACTIVE");
        assertThat(user.getEmailVerifiedAt()).isNotNull();
        assertThat(user.getPasswordHash()).isNotEqualTo("correct-password-123"); // hashed, never stored raw
        assertThat(userRoles.existsByUserIdAndRole(user.getId(), "USER")).isTrue();
        assertThat(userRoles.existsByUserIdAndRole(user.getId(), "ADMIN")).isTrue();

        List<AppUserRoleAuditEntity> audit = roleAudit.findByUserIdOrderByCreatedAtDesc(user.getId());
        assertThat(audit).hasSize(1);
        assertThat(audit.get(0).getAction()).isEqualTo("GRANT");
        assertThat(audit.get(0).getRole()).isEqualTo("ADMIN");
        assertThat(audit.get(0).getOperator()).isEqualTo("system:first-admin-bootstrap");
    }

    @Test
    void existingAdminIsPreservedAndNeverDuplicated() {
        bootstrapService.bootstrap("first-admin@example.test", "correct-password-123");

        FirstAdminBootstrapOutcome second = bootstrapService.bootstrap("second-admin@example.test", "another-password-123");

        assertThat(second).isEqualTo(FirstAdminBootstrapOutcome.ADMIN_ALREADY_PRESENT);
        assertThat(users.findByNormalizedEmail("second-admin@example.test")).isEmpty();
        assertThat(users.count()).isEqualTo(1);
        assertThat(roleAudit.count()).isEqualTo(1);
    }

    @Test
    void repeatedBootstrapWithSameRequestIsIdempotent() {
        FirstAdminBootstrapOutcome first = bootstrapService.bootstrap("first-admin@example.test", "correct-password-123");
        FirstAdminBootstrapOutcome repeat = bootstrapService.bootstrap("first-admin@example.test", "correct-password-123");

        assertThat(first).isEqualTo(FirstAdminBootstrapOutcome.CREATED);
        assertThat(repeat).isEqualTo(FirstAdminBootstrapOutcome.ADMIN_ALREADY_PRESENT);
        assertThat(users.count()).isEqualTo(1);
        assertThat(roleAudit.count()).isEqualTo(1);
    }

    @Test
    void existingUsersWithoutAdminFailsClosedAndChangesNothing() {
        registerPlainUser("legacy-user@example.test");

        FirstAdminBootstrapOutcome outcome = bootstrapService.bootstrap("would-be-admin@example.test", "correct-password-123");

        assertThat(outcome).isEqualTo(FirstAdminBootstrapOutcome.EXISTING_USERS_WITHOUT_ADMIN);
        assertThat(users.count()).isEqualTo(1);
        assertThat(users.findByNormalizedEmail("would-be-admin@example.test")).isEmpty();
        assertThat(roleAudit.count()).isZero();
        assertThat(userRoles.existsByRole("ADMIN")).isFalse();
    }

    @Test
    void invalidPasswordLeavesNoPartialState() {
        assertThatThrownBy(() -> bootstrapService.bootstrap("first-admin@example.test", "short"))
                .isInstanceOf(IllegalArgumentException.class);

        assertThat(users.count()).isZero();
        assertThat(roleAudit.count()).isZero();
    }

    @Test
    void invalidEmailLeavesNoPartialState() {
        assertThatThrownBy(() -> bootstrapService.bootstrap("not-an-email", "correct-password-123"))
                .isInstanceOf(IllegalArgumentException.class);

        assertThat(users.count()).isZero();
    }

    /**
     * Best-effort concurrency check: two overlapping transactions both
     * attempt to bootstrap the first ADMIN. Exactly one must succeed with
     * CREATED and exactly one account/grant/audit row must exist afterward,
     * regardless of which thread "wins". See the class-level note on why
     * this is not a substitute for a real-PostgreSQL verification run.
     */
    @Test
    void concurrentBootstrapAttemptsProduceExactlyOneGrant() throws Exception {
        CountDownLatch bothStarted = new CountDownLatch(2);

        CompletableFuture<FirstAdminBootstrapOutcome> first = CompletableFuture.supplyAsync(() -> {
            bothStarted.countDown();
            awaitQuietly(bothStarted);
            return bootstrapService.bootstrap("racer-one@example.test", "correct-password-123");
        });
        CompletableFuture<FirstAdminBootstrapOutcome> second = CompletableFuture.supplyAsync(() -> {
            bothStarted.countDown();
            awaitQuietly(bothStarted);
            return bootstrapService.bootstrap("racer-two@example.test", "correct-password-123");
        });

        List<FirstAdminBootstrapOutcome> outcomes = List.of(first.get(10, TimeUnit.SECONDS), second.get(10, TimeUnit.SECONDS));
        assertThat(outcomes).containsExactlyInAnyOrder(
                FirstAdminBootstrapOutcome.CREATED, FirstAdminBootstrapOutcome.ADMIN_ALREADY_PRESENT);
        assertThat(users.count()).isEqualTo(1);
        assertThat(userRoles.count()).isEqualTo(2); // USER + ADMIN for the single created account
        assertThat(roleAudit.count()).isEqualTo(1);
    }

    private static void awaitQuietly(CountDownLatch latch) {
        try {
            latch.await(5, TimeUnit.SECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    private AppUserEntity registerPlainUser(String email) {
        Instant now = Instant.now();
        AppUserEntity user = AppUserEntity.local(java.util.UUID.randomUUID(), "test-auth-issuer", email, email,
                "$2a$12$abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ12", "Legacy User", now);
        user.activate(now);
        user = users.save(user);
        userRoles.save(new AppUserRoleEntity(user.getId(), "USER", now));
        return user;
    }
}
