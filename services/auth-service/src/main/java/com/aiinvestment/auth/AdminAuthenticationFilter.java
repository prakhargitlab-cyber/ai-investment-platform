package com.aiinvestment.auth;

import com.aiinvestment.shared.web.auth.HmacJwtService;
import com.aiinvestment.shared.web.auth.JwtClaims;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;
import java.io.IOException;

@Component
public class AdminAuthenticationFilter extends OncePerRequestFilter {
    static final String CLAIMS = AdminAuthenticationFilter.class.getName() + ".claims";
    private final AuthProperties properties;

    public AdminAuthenticationFilter(AuthProperties properties) { this.properties = properties; }

    @Override
    protected boolean shouldNotFilter(HttpServletRequest request) {
        String path = request.getServletPath();
        if (path.isEmpty()) path = request.getRequestURI();
        return !(path.equals("/api/v1/auth/admin") || path.startsWith("/api/v1/auth/admin/"));
    }

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response, FilterChain chain)
            throws ServletException, IOException {
        response.setHeader("Cache-Control", "no-store");
        String authorization = request.getHeader("Authorization");
        JwtClaims claims;
        try {
            if (authorization == null || !authorization.startsWith("Bearer ")) throw new IllegalArgumentException();
            claims = new HmacJwtService(properties.jwtSecret()).verify(authorization.substring(7), properties.issuer());
        } catch (IllegalArgumentException ex) {
            deny(response, 401, "Valid bearer access token required");
            return;
        }
        if (!claims.roles().contains("ADMIN")) {
            deny(response, 403, "Administrator access required");
            return;
        }
        request.setAttribute(CLAIMS, claims);
        chain.doFilter(request, response);
    }

    private void deny(HttpServletResponse response, int status, String message) throws IOException {
        response.setStatus(status);
        response.setContentType("application/json");
        response.getWriter().write("{\"message\":\"" + message + "\"}");
    }
}
