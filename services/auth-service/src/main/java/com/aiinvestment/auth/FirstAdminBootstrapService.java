package com.aiinvestment.auth;

import jakarta.persistence.EntityManager;
import jakarta.persistence.PersistenceContext;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;
import java.util.Locale;
import java.util.UUID;

/**
 * Securely provisions the platform's first ADMIN account on a genuinely
 * fresh database, for use by the "first-admin-bootstrap" Spring profile
 * invoked from {@code platform.ps1 up}.
 *
 * This deliberately reuses the existing role-management primitives rather
 * than writing a parallel path:
 *  - the same {@code auth.role_admin_lock} row-lock already used by
 *    {@link RoleAdminService} serializes concurrent attempts;
 *  - the same {@link AppUserEntity} password hashing, "local" account
 *    factory and {@code activate()} lifecycle transition used by normal
 *    registration/verification are used here, just without an email round
 *    trip (there is no other administrator yet who could be the approver);
 *  - the actual ADMIN grant and its audit row are written exclusively via
 *    {@link RoleAdminService#grantRole}, never via a direct SQL insert.
 *
 * The whole decide-then-act sequence runs inside one transaction holding the
 * lock, so two concurrent bootstrap attempts against the same database can
 * never both succeed, and any failure rolls back the entire attempt instead
 * of leaving a half-created account.
 */
@Service
public class FirstAdminBootstrapService {
    private static final String ADMIN_ROLE = "ADMIN";
    private static final String DEFAULT_DISPLAY_NAME = "First Administrator";
    private static final String BOOTSTRAP_OPERATOR = "system:first-admin-bootstrap";
    private static final String BOOTSTRAP_REASON = "Automatic first-admin bootstrap on fresh database (platform.ps1 up)";

    private final AppUserRepository users;
    private final AppUserRoleRepository roles;
    private final RoleAdminService roleAdminService;
    private final AuthProperties properties;
    private final BCryptPasswordEncoder passwordEncoder = new BCryptPasswordEncoder(12);
    @PersistenceContext
    private EntityManager entityManager;

    public FirstAdminBootstrapService(AppUserRepository users, AppUserRoleRepository roles,
                                       RoleAdminService roleAdminService, AuthProperties properties) {
        this.users = users;
        this.roles = roles;
        this.roleAdminService = roleAdminService;
        this.properties = properties;
    }

    @Transactional
    public FirstAdminBootstrapOutcome bootstrap(String email, String rawPassword) {
        // Must be acquired before reading any decision-relevant state below;
        // this is the exact lock RoleAdminService.grantRole() re-acquires
        // (a no-op re-lock within the same transaction), so the full
        // check -> create -> grant sequence is serialized against any
        // concurrent bootstrap attempt or manual role-admin-cli invocation.
        entityManager.createNativeQuery("select id from auth.role_admin_lock where id = 1 for update").getSingleResult();

        if (roles.existsByRole(ADMIN_ROLE)) {
            return FirstAdminBootstrapOutcome.ADMIN_ALREADY_PRESENT;
        }
        if (users.count() > 0) {
            return FirstAdminBootstrapOutcome.EXISTING_USERS_WITHOUT_ADMIN;
        }

        String normalizedEmail = requireValidEmail(email);
        String validatedPassword = requireValidPassword(rawPassword);

        Instant now = Instant.now();
        AppUserEntity user = AppUserEntity.local(
                UUID.randomUUID(),
                properties.issuer(),
                email.trim(),
                normalizedEmail,
                passwordEncoder.encode(validatedPassword),
                DEFAULT_DISPLAY_NAME,
                now);
        // There is no other administrator who could click a verification
        // link for the very first account, so this activates it directly
        // through the same state-transition method normal email
        // verification calls (AppUserEntity.activate), rather than
        // fabricating a bypass of the account-status check itself.
        user.activate(now);
        user = users.save(user);
        roles.save(new AppUserRoleEntity(user.getId(), "USER", now));
        entityManager.flush();

        roleAdminService.grantRole(email, ADMIN_ROLE, BOOTSTRAP_OPERATOR, BOOTSTRAP_REASON);
        return FirstAdminBootstrapOutcome.CREATED;
    }

    private static String requireValidEmail(String email) {
        if (email == null || email.isBlank()) {
            throw new IllegalArgumentException("first-admin-bootstrap.email is required");
        }
        String trimmed = email.trim();
        if (!trimmed.matches("^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$")) {
            throw new IllegalArgumentException("first-admin-bootstrap.email is not a valid email address");
        }
        return trimmed.toLowerCase(Locale.ROOT);
    }

    private static String requireValidPassword(String password) {
        if (password == null || password.length() < 12 || password.length() > 200) {
            throw new IllegalArgumentException("first-admin-bootstrap.password must be 12-200 characters");
        }
        return password;
    }
}
