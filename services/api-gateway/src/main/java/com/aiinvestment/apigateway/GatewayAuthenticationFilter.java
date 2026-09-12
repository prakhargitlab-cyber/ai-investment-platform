package com.aiinvestment.apigateway;

import com.aiinvestment.shared.web.auth.AuthenticatedUserResolver;
import com.aiinvestment.shared.web.auth.AuthenticationHeaders;
import com.aiinvestment.shared.web.auth.HmacJwtService;
import com.aiinvestment.shared.web.auth.JwtClaims;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.http.HttpHeaders;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;
import java.util.UUID;

@Component
public class GatewayAuthenticationFilter extends OncePerRequestFilter {
    public static final String ATTR_USER_ID = "aip.auth.userId";
    public static final String ATTR_ISSUER = "aip.auth.issuer";
    public static final String ATTR_SUBJECT = "aip.auth.subject";
    public static final String ATTR_EMAIL = "aip.auth.email";
    public static final String ATTR_DISPLAY_NAME = "aip.auth.displayName";
    public static final String ATTR_ROLES = "aip.auth.roles";

    private final GatewayAuthProperties properties;

    public GatewayAuthenticationFilter(GatewayAuthProperties properties) {
        this.properties = properties;
    }

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response, FilterChain filterChain)
            throws ServletException, IOException {
        if (!requiresAuthentication(request)) {
            filterChain.doFilter(request, response);
            return;
        }
        String authorization = request.getHeader(HttpHeaders.AUTHORIZATION);
        if (authorization == null || !authorization.startsWith("Bearer ")) {
            response.sendError(HttpServletResponse.SC_UNAUTHORIZED, "Bearer access token is required");
            return;
        }
        try {
            JwtClaims claims = new HmacJwtService(properties.jwtSecret()).verify(authorization.substring("Bearer ".length()), properties.issuer());
            UUID userId = localOrExternalUserId(claims);
            request.setAttribute(ATTR_USER_ID, userId.toString());
            request.setAttribute(ATTR_ISSUER, claims.issuer());
            request.setAttribute(ATTR_SUBJECT, claims.subject());
            request.setAttribute(ATTR_EMAIL, claims.email());
            request.setAttribute(ATTR_DISPLAY_NAME, claims.displayName());
            request.setAttribute(ATTR_ROLES, String.join(",", claims.roles()));
            filterChain.doFilter(request, response);
        } catch (IllegalArgumentException exc) {
            response.sendError(HttpServletResponse.SC_UNAUTHORIZED, "Invalid bearer access token");
        }
    }

    private UUID localOrExternalUserId(JwtClaims claims) {
        try {
            return UUID.fromString(claims.subject());
        } catch (IllegalArgumentException ignored) {
            return AuthenticatedUserResolver.stableUserId(claims.issuer(), claims.subject());
        }
    }

    private boolean requiresAuthentication(HttpServletRequest request) {
        if ("OPTIONS".equalsIgnoreCase(request.getMethod())) {
            return false;
        }
        String path = request.getRequestURI();
        return path.startsWith("/api/")
                && !path.startsWith("/api/v1/auth/")
                && !path.startsWith("/api/auth-service/");
    }

    public static boolean isInternalIdentityHeader(String name) {
        return name.equalsIgnoreCase(AuthenticationHeaders.USER_ID)
                || name.equalsIgnoreCase(AuthenticationHeaders.ISSUER)
                || name.equalsIgnoreCase(AuthenticationHeaders.SUBJECT)
                || name.equalsIgnoreCase(AuthenticationHeaders.EMAIL)
                || name.equalsIgnoreCase(AuthenticationHeaders.DISPLAY_NAME)
                || name.equalsIgnoreCase(AuthenticationHeaders.ROLES);
    }
}
