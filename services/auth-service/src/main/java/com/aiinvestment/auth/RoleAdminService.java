package com.aiinvestment.auth;

import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;
import java.util.List;
import java.util.Locale;
import java.util.UUID;
import jakarta.persistence.EntityManager;
import jakarta.persistence.PersistenceContext;
import com.aiinvestment.shared.web.auth.JwtClaims;
import org.springframework.http.HttpStatus;
import org.springframework.web.server.ResponseStatusException;

@Service
public class RoleAdminService {
    private static final String ADMIN_ROLE = "ADMIN";

    private final AppUserRepository users;
    private final AppUserRoleRepository roles;
    private final AppUserRoleAuditRepository audit;
    @PersistenceContext private EntityManager entityManager;

    public RoleAdminService(AppUserRepository users, AppUserRoleRepository roles, AppUserRoleAuditRepository audit) {
        this.users = users;
        this.roles = roles;
        this.audit = audit;
    }

    @Transactional
    public void grantRole(String email, String role, String operator, String reason) {
        lockChanges();
        grantTo(requireAccount(email), role, operator, reason);
    }

    private void grantTo(AppUserEntity user, String role, String operator, String reason) {
        String normalizedRole = requireManagedRole(role);
        String normalizedOperator = requireOperator(operator);
        rejectSelfManagement(user, normalizedOperator);
        reason = requireReason(reason);
        requireActive(user);
        Instant now = Instant.now();
        if (roles.existsByUserIdAndRole(user.getId(), normalizedRole)) return;
        roles.save(new AppUserRoleEntity(user.getId(), normalizedRole, now));
        audit.save(new AppUserRoleAuditEntity(UUID.randomUUID(), user.getId(), user.getEmail(), normalizedRole, "GRANT", normalizedOperator, reason, now));
    }

    @Transactional
    public void revokeRole(String email, String role, String operator, String reason) {
        lockChanges();
        revokeFrom(requireAccount(email), role, operator, reason);
    }

    private void revokeFrom(AppUserEntity user, String role, String operator, String reason) {
        String normalizedRole = requireManagedRole(role);
        String normalizedOperator = requireOperator(operator);
        rejectSelfManagement(user, normalizedOperator);
        reason = requireReason(reason);
        if (!roles.existsByUserIdAndRole(user.getId(), normalizedRole)) return;
        Long activeAdmins = entityManager.createQuery("select count(u) from AppUserEntity u where u.accountStatus = 'ACTIVE' and u.emailVerifiedAt is not null and exists (select r from AppUserRoleEntity r where r.userId = u.id and r.role = 'ADMIN')", Long.class).getSingleResult();
        if ("ACTIVE".equals(user.getAccountStatus()) && user.getEmailVerifiedAt() != null && activeAdmins <= 1) {
            throw new RoleAdminException("The last active administrator cannot be removed");
        }
        Instant now = Instant.now();
        roles.deleteByUserIdAndRole(user.getId(), normalizedRole);
        audit.save(new AppUserRoleAuditEntity(UUID.randomUUID(), user.getId(), user.getEmail(), normalizedRole, "REVOKE", normalizedOperator, reason, now));
    }

    public List<String> currentRoles(UUID userId) {
        return roles.findByUserId(userId).stream().map(AppUserRoleEntity::getRole).toList();
    }

    // The lock must be taken before reading actor permissions or target roles.
    private void lockChanges() {
        entityManager.createNativeQuery("select id from auth.role_admin_lock where id = 1 for update").getSingleResult();
    }

    public AppUserEntity requireAdministrator(JwtClaims claims) {
        if (!claims.roles().contains(ADMIN_ROLE)) throw new ResponseStatusException(HttpStatus.FORBIDDEN, "Administrator access required");
        AppUserEntity actor = users.findByIssuerAndExternalSubject(claims.issuer(), claims.subject())
                .orElseThrow(() -> new ResponseStatusException(HttpStatus.FORBIDDEN, "Administrator access required"));
        if (!"ACTIVE".equals(actor.getAccountStatus()) || actor.getEmailVerifiedAt() == null
                || !roles.existsByUserIdAndRole(actor.getId(), ADMIN_ROLE)) {
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "Administrator access required");
        }
        return actor;
    }

    @Transactional
    public void changeRole(JwtClaims claims, UUID targetId, String role, String reason, boolean grant) {
        lockChanges();
        AppUserEntity actor = requireAdministrator(claims);
        if (actor.getId().equals(targetId)) throw new ResponseStatusException(HttpStatus.FORBIDDEN, "You cannot manage your own roles");
        AppUserEntity target = users.findById(targetId).orElseThrow(() -> new ResponseStatusException(HttpStatus.NOT_FOUND, "User not found"));
        String operator = "user:" + actor.getId();
        if (grant) grantTo(target, role, operator, reason);
        else revokeFrom(target, role, operator, reason);
    }

    private static String requireReason(String reason) {
        if (reason == null || reason.isBlank() || reason.length() > 500) throw new RoleAdminException("A reason of 1 to 500 characters is required");
        return reason.trim();
    }

    private static void requireActive(AppUserEntity user) {
        if (!"ACTIVE".equals(user.getAccountStatus()) || user.getEmailVerifiedAt() == null) throw new RoleAdminException("ADMIN requires an active, verified account");
    }

    private AppUserEntity requireAccount(String email) {
        if (email == null || email.isBlank()) {
            throw new RoleAdminException("An account email is required");
        }
        return users.findByNormalizedEmail(normalize(email))
                .orElseThrow(() -> new RoleAdminException("No account exists for " + email));
    }

    private static String requireManagedRole(String role) {
        String normalized = role == null ? "" : role.trim().toUpperCase(Locale.ROOT);
        if (!ADMIN_ROLE.equals(normalized)) {
            throw new RoleAdminException("Only the ADMIN role can be provisioned through this procedure");
        }
        return normalized;
    }

    private static String requireOperator(String operator) {
        if (operator == null || operator.isBlank() || operator.length() > 240) {
            throw new RoleAdminException("An operator identifier is required for audit purposes");
        }
        return operator.trim();
    }

    private static void rejectSelfManagement(AppUserEntity user, String operator) {
        if (operator.equalsIgnoreCase(user.getId().toString()) || operator.equalsIgnoreCase("user:" + user.getId())
                || (user.getEmail() != null && normalize(user.getEmail()).equals(normalize(operator)))) {
            throw new RoleAdminException("An operator cannot grant or revoke their own role");
        }
    }

    private static String normalize(String value) {
        return value.trim().toLowerCase(Locale.ROOT);
    }
}
