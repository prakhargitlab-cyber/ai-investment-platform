package com.aiinvestment.apigateway;

import com.aiinvestment.shared.web.auth.HmacJwtService;
import com.aiinvestment.shared.web.auth.JwtClaims;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

class EtfRadarProxyTest {
    private static final String SECRET = "etf-routing-test-secret-at-least-32-characters";
    private final AtomicInteger calls = new AtomicInteger();
    private final AtomicReference<Seen> seen = new AtomicReference<>();
    private HttpServer downstream;
    private MockMvc mvc;
    private int responseStatus = 200;
    private final byte[] responseBody = "{\"result\":\"fixture\"}".getBytes(StandardCharsets.UTF_8);

    @BeforeEach
    void setUp() throws Exception {
        downstream = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        downstream.createContext("/", exchange -> {
            calls.incrementAndGet();
            seen.set(new Seen(exchange.getRequestMethod(), exchange.getRequestURI(),
                    exchange.getRequestBody().readAllBytes(),
                    exchange.getRequestHeaders().getFirst("X-AIP-User-Roles"),
                    exchange.getRequestHeaders().getFirst("X-AIP-User-Subject")));
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            exchange.sendResponseHeaders(responseStatus, responseBody.length);
            exchange.getResponseBody().write(responseBody);
            exchange.close();
        });
        downstream.start();
        String url = "http://127.0.0.1:" + downstream.getAddress().getPort();
        mvc = MockMvcBuilders.standaloneSetup(new PortfolioRouteController(url, url, url, url, url, url, url))
                .addFilters(new GatewayAuthenticationFilter(new GatewayAuthProperties("test-issuer", SECRET))).build();
    }

    @AfterEach
    void tearDown() { downstream.stop(0); }

    @ParameterizedTest
    @ValueSource(strings = {"/api/v1/etf-radar", "/api/v1/etf-radar/current",
            "/api/v1/etf-radar/cycles/cycle-fixture/status"})
    void userReadsPreservePathQueryResponseAndTrustedIdentity(String path) throws Exception {
        URI uri = URI.create(path + "?cursor=alpha%20%26%20beta&limit=10");
        mvc.perform(get(uri).header("Authorization", bearer("USER"))
                        .header("X-AIP-User-Roles", "ADMIN").header("X-AIP-User-Subject", "forged"))
                .andExpect(status().isOk()).andExpect(content().bytes(responseBody));
        assertThat(calls.get()).isEqualTo(1);
        assertThat(seen.get().method()).isEqualTo("GET");
        assertThat(seen.get().uri()).isEqualTo(uri);
        assertThat(seen.get().roles()).isEqualTo("USER");
        assertThat(seen.get().subject()).isEqualTo("verified-user");
    }

    @Test
    void userCycleRequestPreservesExactBodyMethodAndAcceptedStatus() throws Exception {
        responseStatus = 202;
        byte[] body = "{\n  \"top_n\": 10, \"label\": \"ETF \\u20b9\"\n}".getBytes(StandardCharsets.UTF_8);
        mvc.perform(post("/api/v1/etf-radar/cycles").header("Authorization", bearer("USER"))
                        .contentType("application/json").content(body))
                .andExpect(status().isAccepted()).andExpect(content().bytes(responseBody));
        assertThat(seen.get().method()).isEqualTo("POST");
        assertThat(seen.get().uri().getPath()).isEqualTo("/api/v1/etf-radar/cycles");
        assertThat(seen.get().body()).isEqualTo(body);
    }

    @Test
    void downstreamNotFoundRemainsNotFound() throws Exception {
        responseStatus = 404;
        mvc.perform(get("/api/v1/etf-radar/cycles/missing/status").header("Authorization", bearer("USER")))
                .andExpect(status().isNotFound()).andExpect(content().bytes(responseBody));
        assertThat(calls.get()).isEqualTo(1);
    }

    @Test
    void anonymousReadAndCycleRequestNeverReachDownstream() throws Exception {
        mvc.perform(get("/api/v1/etf-radar/current").header("X-AIP-User-Roles", "ADMIN"))
                .andExpect(status().isUnauthorized());
        mvc.perform(post("/api/v1/etf-radar/cycles").contentType("application/json").content("{}"))
                .andExpect(status().isUnauthorized());
        assertThat(calls.get()).isZero();
    }

    @Test
    void adminAuthorizationRemainsEnforced() throws Exception {
        mvc.perform(get("/api/v1/auth/admin/users")).andExpect(status().isUnauthorized());
        mvc.perform(get("/api/v1/auth/admin/users").header("Authorization", bearer("USER")))
                .andExpect(status().isForbidden());
        assertThat(calls.get()).isZero();
        mvc.perform(get("/api/v1/auth/admin/users").header("Authorization", bearer("ADMIN")))
                .andExpect(status().isOk());
        assertThat(calls.get()).isEqualTo(1);
    }

    private String bearer(String role) {
        return "Bearer " + new HmacJwtService(SECRET).issue(new JwtClaims("test-issuer", "verified-user",
                "user@example.test", "Test User", List.of(role), Instant.now().plusSeconds(300)));
    }

    private record Seen(String method, URI uri, byte[] body, String roles, String subject) {}
}
