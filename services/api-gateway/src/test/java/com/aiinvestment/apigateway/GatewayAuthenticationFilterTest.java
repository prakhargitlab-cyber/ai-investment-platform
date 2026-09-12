package com.aiinvestment.apigateway;

import com.aiinvestment.shared.web.auth.HmacJwtService;
import com.aiinvestment.shared.web.auth.JwtClaims;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpHeaders;
import org.springframework.mock.web.MockFilterChain;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;

import java.time.Instant;
import java.util.List;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;

class GatewayAuthenticationFilterTest {
    private static final String ISSUER = "test-issuer";
    private static final String SECRET = "test-phase5a-jwt-secret-32-characters";

    private final GatewayAuthenticationFilter filter =
            new GatewayAuthenticationFilter(new GatewayAuthProperties(ISSUER, SECRET));

    @Test
    void privateApiRequiresBearerToken() throws Exception {
        MockHttpServletRequest request = new MockHttpServletRequest("GET", "/api/v1/portfolios");
        MockHttpServletResponse response = new MockHttpServletResponse();

        filter.doFilter(request, response, new MockFilterChain());

        assertThat(response.getStatus()).isEqualTo(401);
    }

    @Test
    void validBearerTokenEstablishesNormalizedPrincipalAndIgnoresSpoofedUserHeader() throws Exception {
        String token = new HmacJwtService(SECRET).issue(new JwtClaims(
                ISSUER,
                "user-a",
                "user.a@example.invalid",
                "User A",
                List.of("USER"),
                Instant.now().plusSeconds(300)
        ));
        MockHttpServletRequest request = new MockHttpServletRequest("GET", "/api/v1/portfolios");
        request.addHeader(HttpHeaders.AUTHORIZATION, "Bearer " + token);
        request.addHeader("X-AIP-User-Id", "20000000-0000-0000-0000-000000000002");
        MockHttpServletResponse response = new MockHttpServletResponse();

        filter.doFilter(request, response, new MockFilterChain());

        UUID expectedUserId = UUID.nameUUIDFromBytes(("oidc|" + ISSUER + "|user-a").getBytes());
        assertThat(response.getStatus()).isEqualTo(200);
        assertThat(request.getAttribute(GatewayAuthenticationFilter.ATTR_USER_ID)).isEqualTo(expectedUserId.toString());
        assertThat(request.getAttribute(GatewayAuthenticationFilter.ATTR_SUBJECT)).isEqualTo("user-a");
    }

    @Test
    void localUserUuidSubjectIsPropagatedWithoutLegacyIdentityDerivation() throws Exception {
        UUID localUserId = UUID.fromString("77d9564c-f64f-4b7f-8754-eacbcb2d77be");
        String token = new HmacJwtService(SECRET).issue(new JwtClaims(
                ISSUER, localUserId.toString(), "real.user@example.test", "Real User", List.of("USER"),
                Instant.now(), Instant.now().plusSeconds(300)
        ));
        MockHttpServletRequest request = new MockHttpServletRequest("GET", "/api/v1/portfolios");
        request.addHeader(HttpHeaders.AUTHORIZATION, "Bearer " + token);

        filter.doFilter(request, new MockHttpServletResponse(), new MockFilterChain());

        assertThat(request.getAttribute(GatewayAuthenticationFilter.ATTR_USER_ID)).isEqualTo(localUserId.toString());
        assertThat(request.getAttribute(GatewayAuthenticationFilter.ATTR_SUBJECT)).isEqualTo(localUserId.toString());
    }

    @Test
    void invalidBearerTokenIsRejected() throws Exception {
        MockHttpServletRequest request = new MockHttpServletRequest("GET", "/api/v1/portfolios");
        request.addHeader(HttpHeaders.AUTHORIZATION, "Bearer invalid.token.value");
        MockHttpServletResponse response = new MockHttpServletResponse();

        filter.doFilter(request, response, new MockFilterChain());

        assertThat(response.getStatus()).isEqualTo(401);
    }
}
