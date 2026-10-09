package com.aiinvestment.apigateway;

import com.aiinvestment.shared.web.auth.HmacJwtService;
import com.aiinvestment.shared.web.auth.JwtClaims;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.List;
import java.util.concurrent.atomic.AtomicReference;
import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

class AdminAuthProxyTest {
    @Test void adminProxyPreservesBearerReasonAndStatusAndStripsSpoofedIdentity() throws Exception {
        AtomicReference<String> bearer = new AtomicReference<>(), body = new AtomicReference<>(), spoof = new AtomicReference<>();
        HttpServer downstream = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        downstream.createContext("/", exchange -> {
            bearer.set(exchange.getRequestHeaders().getFirst("Authorization"));
            spoof.set(exchange.getRequestHeaders().getFirst("X-AIP-User-Roles"));
            body.set(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            exchange.sendResponseHeaders(204, -1); exchange.close();
        });
        downstream.start();
        try {
            String url = "http://127.0.0.1:" + downstream.getAddress().getPort();
            String secret = "gateway-test-secret-at-least-32-characters";
            var mvc = MockMvcBuilders.standaloneSetup(new PortfolioRouteController(url, url, url, url, url, url, url))
                    .addFilters(new GatewayAuthenticationFilter(new GatewayAuthProperties("issuer", secret))).build();
            String jwt = "Bearer " + new HmacJwtService(secret).issue(new JwtClaims("issuer", "subject", "admin@example.test", "Admin", List.of("ADMIN"), Instant.now().plusSeconds(300)));
            String reason = "{\"reason\":\"Approved ticket\"}";
            mvc.perform(delete("/api/v1/auth/admin/users/target/roles/ADMIN").header("Authorization", jwt)
                    .header("X-AIP-User-Roles", "FORGED").contentType(MediaType.APPLICATION_JSON).content(reason))
                    .andExpect(status().isNoContent());
            assertThat(bearer.get()).isEqualTo(jwt);
            assertThat(body.get()).isEqualTo(reason);
            assertThat(spoof.get()).isNull();
        } finally { downstream.stop(0); }
    }
}
