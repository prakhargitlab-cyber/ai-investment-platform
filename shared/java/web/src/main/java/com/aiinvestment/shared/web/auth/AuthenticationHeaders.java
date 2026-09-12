package com.aiinvestment.shared.web.auth;

public final class AuthenticationHeaders {
    public static final String USER_ID = "X-AIP-User-Id";
    public static final String ISSUER = "X-AIP-User-Issuer";
    public static final String SUBJECT = "X-AIP-User-Subject";
    public static final String EMAIL = "X-AIP-User-Email";
    public static final String DISPLAY_NAME = "X-AIP-User-Display-Name";
    public static final String ROLES = "X-AIP-User-Roles";

    private AuthenticationHeaders() {
    }
}
