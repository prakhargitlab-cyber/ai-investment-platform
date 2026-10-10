package com.aiinvestment.auth;

/**
 * Result of a {@link FirstAdminBootstrapService#bootstrap(String, String)} attempt.
 * Deliberately has no FAILED case: validation/infrastructure errors are
 * reported as exceptions so the transaction rolls back cleanly, leaving no
 * partially-created account or grant behind.
 */
public enum FirstAdminBootstrapOutcome {
    /** No account existed anywhere; the first ADMIN account was created and granted. */
    CREATED,
    /** At least one account already holds ADMIN; nothing was changed. */
    ADMIN_ALREADY_PRESENT,
    /** Accounts exist but none holds ADMIN; automatic bootstrap refuses to act. */
    EXISTING_USERS_WITHOUT_ADMIN
}
