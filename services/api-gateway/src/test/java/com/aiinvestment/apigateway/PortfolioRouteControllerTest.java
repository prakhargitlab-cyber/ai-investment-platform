package com.aiinvestment.apigateway;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.http.client.JdkClientHttpRequestFactory;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.web.bind.annotation.RequestMapping;

import java.io.IOException;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.net.InetSocketAddress;
import java.net.http.HttpClient;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class PortfolioRouteControllerTest {

    @Test
    void connectorSessionUrlTargetsItsDynamicRuntimeService() throws Exception {
        Method target = PortfolioRouteController.class.getDeclaredMethod("connectorRuntimeBaseUrl", String.class);
        target.setAccessible(true);

        assertThat(target.invoke(null, "/connector-sessions/82a964f1-569e-42fc-8bbf-e9229ae2c169/login/sso/Login"))
                .isEqualTo("http://ibkr-runtime-82a964f1569e");
    }
    private HttpServer server;

    @AfterEach
    void stopServer() {
        if (server != null) {
            server.stop(0);
        }
    }

    @Test
    void browserResetPasswordRoutesAreMappedToTheFrontendController() throws Exception {
        Method routeFrontend = PortfolioRouteController.class.getDeclaredMethod("routeFrontend", jakarta.servlet.http.HttpServletRequest.class);
        RequestMapping mapping = routeFrontend.getAnnotation(RequestMapping.class);

        assertThat(mapping.value()).contains("/verify-email", "/reset-password");
    }

    @Test
    void connectorSessionRequestFactoryUsesHttp11JdkClient() throws Exception {
        JdkClientHttpRequestFactory factory = PortfolioRouteController.connectorSessionRequestFactory();

        Field httpClientField = JdkClientHttpRequestFactory.class.getDeclaredField("httpClient");
        httpClientField.setAccessible(true);
        HttpClient httpClient = (HttpClient) httpClientField.get(factory);

        assertThat(httpClient.version()).isEqualTo(HttpClient.Version.HTTP_1_1);
    }

    @Test
    void connectorSessionForwardingStripsHopByHopRequestHeaders() throws Exception {
        AtomicReference<HeadersSeen> seen = new AtomicReference<>();
        startServer(exchange -> {
            seen.set(HeadersSeen.from(exchange));
            writeResponse(exchange, HttpStatus.ACCEPTED.value(), "text/plain", "accepted".getBytes(StandardCharsets.UTF_8));
        });
        PortfolioRouteController controller = controllerForServer();
        MockHttpServletRequest request = new MockHttpServletRequest("POST", "/connector-sessions/test");
        request.setContentType("application/x-www-form-urlencoded");
        request.setContent("probe=safe".getBytes(StandardCharsets.UTF_8));
        request.addHeader(HttpHeaders.CONNECTION, "keep-alive, upgrade");
        request.addHeader(HttpHeaders.HOST, "browser.example");
        request.addHeader(HttpHeaders.TRANSFER_ENCODING, "chunked");
        request.addHeader(HttpHeaders.UPGRADE, "websocket");
        request.addHeader(HttpHeaders.TE, "trailers");
        request.addHeader("Trailer", "X-Trailer");
        request.addHeader(HttpHeaders.CONTENT_LENGTH, "999");

        controller.routeConnectorLogin(request);

        assertThat(seen.get().contentType()).isEqualTo("application/x-www-form-urlencoded");
        assertThat(seen.get().host()).isNotEqualTo("browser.example");
        assertThat(seen.get().contentLength()).isNotEqualTo("999");
        assertThat(seen.get().headerNames()).doesNotContain(
                "connection",
                "transfer-encoding",
                "upgrade",
                "te",
                "trailer"
        );
    }

    @Test
    void connectorSessionForwardingPreservesDownstreamStatusHeadersAndBodyBytes() throws Exception {
        byte[] responseBytes = new byte[]{0, 1, 2, 3, 4, 5};
        startServer(exchange -> {
            exchange.getResponseHeaders().add(HttpHeaders.SET_COOKIE, "a=1; Path=/connector-sessions");
            exchange.getResponseHeaders().add(HttpHeaders.SET_COOKIE, "b=2; Path=/connector-sessions");
            writeResponse(exchange, 207, "application/octet-stream", responseBytes);
        });
        PortfolioRouteController controller = controllerForServer();
        MockHttpServletRequest request = new MockHttpServletRequest("POST", "/connector-sessions/test");
        request.setContent("probe=safe".getBytes(StandardCharsets.UTF_8));

        ResponseEntity<byte[]> response = controller.routeConnectorLogin(request);

        assertThat(response.getStatusCode().value()).isEqualTo(207);
        assertThat(response.getHeaders().getFirst(HttpHeaders.CONTENT_TYPE)).isEqualTo("application/octet-stream");
        assertThat(response.getHeaders().get(HttpHeaders.SET_COOKIE)).containsExactly(
                "a=1; Path=/connector-sessions",
                "b=2; Path=/connector-sessions"
        );
        assertThat(response.getBody()).isEqualTo(responseBytes);
    }

    @Test
    void rootFrontendForwardingPreservesHtmlResponse() throws Exception {
        byte[] html = "<html><body>AI Investment</body></html>".getBytes(StandardCharsets.UTF_8);
        startServer(exchange -> writeResponse(exchange, 200, "text/html", html));
        PortfolioRouteController controller = controllerForServer();

        ResponseEntity<byte[]> response = controller.routeFrontend(new MockHttpServletRequest("GET", "/"));

        assertThat(response.getStatusCode().value()).isEqualTo(200);
        assertThat(response.getHeaders().getFirst(HttpHeaders.CONTENT_TYPE)).startsWith("text/html");
        assertThat(response.getBody()).isEqualTo(html);
    }

    @Test
    void portfolioMultipartForwardingPreservesTheExactBodyAndContentType() throws Exception {
        byte[] multipart = new byte[]{
                '-', '-', 'b', '\r', '\n', 0,
                (byte) 0x80, (byte) 0xff, '\r', '\n', '-', '-', 'b', '-', '-'
        };
        AtomicReference<byte[]> seenBody = new AtomicReference<>();
        AtomicReference<String> seenContentType = new AtomicReference<>();
        startServer(exchange -> {
            seenContentType.set(exchange.getRequestHeaders().getFirst(HttpHeaders.CONTENT_TYPE));
            seenBody.set(exchange.getRequestBody().readAllBytes());
            writeResponse(exchange, 200, "application/json", "{}".getBytes(StandardCharsets.UTF_8));
        });
        PortfolioRouteController controller = controllerForServer();
        MockHttpServletRequest request = new MockHttpServletRequest(
                "POST", "/api/v1/portfolios/imports/icici_direct/preview");
        request.setContentType("multipart/form-data; boundary=b");
        request.setContent(multipart);
        addTrustedIdentity(request);

        ResponseEntity<byte[]> response = controller.route(request);

        assertThat(response.getStatusCode().value()).isEqualTo(200);
        assertThat(seenContentType.get()).isEqualTo("multipart/form-data; boundary=b");
        assertThat(seenBody.get()).isEqualTo(multipart);
    }

    @Test
    void researchTransportTimeoutIsAnExplicitGatewayTimeoutInsteadOfGeneric500() throws Exception {
        startServer(exchange -> {
            try {
                Thread.sleep(200);
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
            }
            writeResponse(exchange, 200, "application/json", "{}".getBytes(StandardCharsets.UTF_8));
        });
        String local = "http://127.0.0.1:" + server.getAddress().getPort();
        PortfolioRouteController controller = new PortfolioRouteController(
                local, local, local, local, local, local, local,
                Duration.ofSeconds(1), Duration.ofMillis(30));
        MockHttpServletRequest request = new MockHttpServletRequest(
                "POST", "/api/v1/research/readiness/213c841a-3992-4db9-99e4-680fe5ce15e1/ensure");
        request.setContent("{}".getBytes(StandardCharsets.UTF_8));
        addTrustedIdentity(request);

        // routeResearch now returns ResponseEntity<byte[]> (byte-exact
        // transport fix, runtime defect closure); the timeout error body is
        // still UTF-8 JSON text, just carried as bytes like every other
        // research-engine response now is.
        ResponseEntity<byte[]> response = controller.routeResearch(request);

        assertThat(response.getStatusCode()).isEqualTo(HttpStatus.GATEWAY_TIMEOUT);
        assertThat(new String(response.getBody(), StandardCharsets.UTF_8)).contains("RESEARCH_ENGINE_TIMEOUT");
    }

    private void startServer(ExchangeHandler handler) throws IOException {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", exchange -> {
            try {
                handler.handle(exchange);
            } finally {
                exchange.close();
            }
        });
        server.start();
    }

    @Test
    void etfRadarRoutesAreMappedToTheResearchEngineController() throws Exception {
        // RADAR UI + API ROUTING closure -- the confirmed root cause of the
        // reported 404 on /api/v1/etf-radar/cycles: that path had no
        // @RequestMapping at all on this controller (research-engine mounts
        // ETF Radar endpoints as a sibling of /api/v1/research/**, not a
        // child of it). This asserts the fix without a full Spring MVC
        // dispatch context, matching browserResetPasswordRoutesAreMappedToTheFrontendController's
        // existing reflection-based pattern above.
        Method routeResearch = PortfolioRouteController.class.getDeclaredMethod("routeResearch", jakarta.servlet.http.HttpServletRequest.class);
        RequestMapping mapping = routeResearch.getAnnotation(RequestMapping.class);

        assertThat(mapping.value()).contains("/api/v1/etf-radar/**", "/api/v1/etf-radar");
        // Equity Radar's own cycle endpoints already live under
        // /api/v1/research/opportunities/** and must stay routed there --
        // this fix must never introduce a second, duplicate mapping for them.
        assertThat(mapping.value()).contains("/api/v1/research/**", "/api/v1/research");
    }

    @Test
    void etfRadarCycleCreationIsForwardedToResearchEngineByteExact() throws Exception {
        // End-to-end confirmation (mirrors researchSearchPreservesEncodedCompanyNamesWithoutDoubleEncoding
        // below): a POST to /api/v1/etf-radar/cycles reaches the research-engine
        // backend through the SAME byte-exact forwarding /api/v1/research/**
        // already uses -- no duplicate endpoint, no new forwarding path.
        AtomicReference<String> seenPath = new AtomicReference<>();
        AtomicReference<String> seenMethod = new AtomicReference<>();
        startServer(exchange -> {
            seenPath.set(exchange.getRequestURI().getPath());
            seenMethod.set(exchange.getRequestMethod());
            writeResponse(exchange, 202, "application/json", "{\"cycle_id\":\"test\"}".getBytes(StandardCharsets.UTF_8));
        });
        String local = "http://127.0.0.1:" + server.getAddress().getPort();
        var controller = new PortfolioRouteController(local, local, local, local, local, local, local);
        var request = new MockHttpServletRequest("POST", "/api/v1/etf-radar/cycles");
        request.setContent("{\"top_n\":10}".getBytes(StandardCharsets.UTF_8));
        request.setContentType("application/json");
        addTrustedIdentity(request);
        ResponseEntity<byte[]> response = controller.routeResearch(request);
        assertThat(response.getStatusCode()).isEqualTo(HttpStatus.ACCEPTED);
        assertThat(seenPath.get()).isEqualTo("/api/v1/etf-radar/cycles");
        assertThat(seenMethod.get()).isEqualTo("POST");
    }

    @Test
    void researchSearchPreservesEncodedCompanyNamesWithoutDoubleEncoding() throws Exception {
        AtomicReference<String> query = new AtomicReference<>();
        startServer(exchange -> {
            query.set(exchange.getRequestURI().getRawQuery());
            writeResponse(exchange, 200, "application/json", "[]".getBytes(StandardCharsets.UTF_8));
        });
        String local = "http://127.0.0.1:" + server.getAddress().getPort();
        var controller = new PortfolioRouteController(local,local,local,local,local,local,local);
        var request = new MockHttpServletRequest("GET", "/api/v1/research/instruments/search");
        request.setQueryString("q=Alpha%20%26%20Beta&region=INDIA&limit=15");
        addTrustedIdentity(request);
        assertThat(controller.routeResearch(request).getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(query.get()).isEqualTo(request.getQueryString());
    }

    @Test
    void marketUniverseRoutesAreMappedToThePortfolioServiceController() throws Exception {
        // Root cause closure: PortfolioRouteController routed /api/v1/portfolios/** and
        // /api/v1/instruments/** to portfolio-service, but had no mapping at all for
        // /api/v1/market-universe/**, where the already-deployed NSE ETF preview/apply
        // endpoints live -- a request to either 404'd before ever reaching the gateway's
        // auth/forwarding logic. This asserts the fix the same way
        // etfRadarRoutesAreMappedToTheResearchEngineController does above, and additionally
        // confirms the existing /api/v1/portfolios/** and /api/v1/instruments/** mappings
        // on this SAME method are untouched by the change (no accidental removal).
        Method route = PortfolioRouteController.class.getDeclaredMethod("route", jakarta.servlet.http.HttpServletRequest.class);
        RequestMapping mapping = route.getAnnotation(RequestMapping.class);

        assertThat(mapping.value()).contains("/api/v1/market-universe", "/api/v1/market-universe/**");
        assertThat(mapping.value()).contains(
                "/api/v1/portfolios", "/api/v1/portfolios/**",
                "/api/v1/instruments", "/api/v1/instruments/**");
    }

    @Test
    void etfPreviewRouteForwardsGetToPortfolioServiceByteExact() throws Exception {
        AtomicReference<String> seenPath = new AtomicReference<>();
        AtomicReference<String> seenMethod = new AtomicReference<>();
        byte[] previewResponse = "{\"sourceAvailable\":true,\"missingEtfs\":[]}".getBytes(StandardCharsets.UTF_8);
        startServer(exchange -> {
            seenPath.set(exchange.getRequestURI().getPath());
            seenMethod.set(exchange.getRequestMethod());
            writeResponse(exchange, 200, "application/json", previewResponse);
        });
        PortfolioRouteController controller = controllerForServer();
        MockHttpServletRequest request = new MockHttpServletRequest(
                "GET", "/api/v1/market-universe/india/nse-etf/preview");
        addTrustedIdentity(request);

        ResponseEntity<byte[]> response = controller.route(request);

        assertThat(seenPath.get()).isEqualTo("/api/v1/market-universe/india/nse-etf/preview");
        assertThat(seenMethod.get()).isEqualTo("GET");
        assertThat(response.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(response.getBody()).isEqualTo(previewResponse);
    }

    @Test
    void etfApplyRouteForwardsPostToPortfolioServiceByteExact() throws Exception {
        // Mocks the downstream portfolio-service only (a local HttpServer standing in for
        // it) -- this never invokes the real /apply endpoint or mutates anything, per the
        // task's "do not invoke /apply" constraint. It only confirms the gateway forwards
        // the POST to portfolio-service byte-exactly, the same way portfolioMultipartForwardingPreservesTheExactBodyAndContentType
        // already confirms for the existing /api/v1/portfolios/** route on this same method.
        AtomicReference<String> seenPath = new AtomicReference<>();
        AtomicReference<String> seenMethod = new AtomicReference<>();
        AtomicReference<byte[]> seenBody = new AtomicReference<>();
        byte[] applyResponse = "{\"status\":\"COMPLETED\"}".getBytes(StandardCharsets.UTF_8);
        startServer(exchange -> {
            seenPath.set(exchange.getRequestURI().getPath());
            seenMethod.set(exchange.getRequestMethod());
            seenBody.set(exchange.getRequestBody().readAllBytes());
            writeResponse(exchange, 200, "application/json", applyResponse);
        });
        PortfolioRouteController controller = controllerForServer();
        MockHttpServletRequest request = new MockHttpServletRequest(
                "POST", "/api/v1/market-universe/india/nse-etf/apply");
        request.setContentType("application/json");
        request.setContent(new byte[0]);
        addTrustedIdentity(request);

        ResponseEntity<byte[]> response = controller.route(request);

        assertThat(seenPath.get()).isEqualTo("/api/v1/market-universe/india/nse-etf/apply");
        assertThat(seenMethod.get()).isEqualTo("POST");
        assertThat(seenBody.get()).isEmpty();
        assertThat(response.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(response.getBody()).isEqualTo(applyResponse);
    }

    @Test
    void marketUniverseRouteRejectsSpoofedIdentityHeadersWithoutTrustedAttributes() throws Exception {
        // Fail-closed confirmation: a caller cannot grant itself ADMIN access to the new
        // market-universe route by sending the trusted identity headers directly -- those
        // headers are always stripped from the inbound request (GatewayAuthenticationFilter
        // .isInternalIdentityHeader) and the outbound identity is built ONLY from request
        // ATTRIBUTES that GatewayAuthenticationFilter itself sets after verifying a real
        // bearer token (see addTrustedIdentity/requiredAttribute). A request with spoofed
        // headers but no such attributes must be rejected before any forwarding happens --
        // the downstream portfolio-service must never even see the request.
        AtomicReference<Boolean> downstreamWasCalled = new AtomicReference<>(false);
        startServer(exchange -> {
            downstreamWasCalled.set(true);
            writeResponse(exchange, 200, "application/json", "{}".getBytes(StandardCharsets.UTF_8));
        });
        PortfolioRouteController controller = controllerForServer();
        MockHttpServletRequest request = new MockHttpServletRequest(
                "POST", "/api/v1/market-universe/india/nse-etf/apply");
        request.setContentType("application/json");
        request.setContent(new byte[0]);
        request.addHeader("X-AIP-User-Id", "00000000-0000-0000-0000-000000000999");
        request.addHeader("X-AIP-User-Issuer", "spoofed");
        request.addHeader("X-AIP-User-Subject", "attacker");
        request.addHeader("X-AIP-User-Roles", "ADMIN");
        // Deliberately NOT calling addTrustedIdentity(request): no GatewayAuthenticationFilter
        // attributes are set, exactly as would be the case for a request that skipped real
        // bearer-token verification.

        assertThatThrownBy(() -> controller.route(request))
                .isInstanceOf(IllegalStateException.class)
                .hasMessageContaining("Missing trusted gateway identity");
        assertThat(downstreamWasCalled.get()).isFalse();
    }

        private PortfolioRouteController controllerForServer() {
        return new PortfolioRouteController(
                "http://127.0.0.1:" + server.getAddress().getPort(),
                "http://broker-service",
                "http://127.0.0.1:" + server.getAddress().getPort(),
                "http://company-service",
                "http://auth-service",
                "http://research-engine",
                "http://127.0.0.1:" + server.getAddress().getPort()
        );
    }

    private static void addTrustedIdentity(MockHttpServletRequest request) {
        request.setAttribute(GatewayAuthenticationFilter.ATTR_USER_ID, "00000000-0000-0000-0000-000000000001");
        request.setAttribute(GatewayAuthenticationFilter.ATTR_ISSUER, "test");
        request.setAttribute(GatewayAuthenticationFilter.ATTR_SUBJECT, "user-a");
    }

    private static void writeResponse(HttpExchange exchange, int status, String contentType, byte[] body) throws IOException {
        exchange.getResponseHeaders().set(HttpHeaders.CONTENT_TYPE, contentType);
        exchange.sendResponseHeaders(status, body.length);
        exchange.getResponseBody().write(body);
    }

    @FunctionalInterface
    private interface ExchangeHandler {
        void handle(HttpExchange exchange) throws IOException;
    }

    private record HeadersSeen(String contentType, String host, String contentLength, List<String> headerNames) {
        static HeadersSeen from(HttpExchange exchange) {
            return new HeadersSeen(
                    exchange.getRequestHeaders().getFirst(HttpHeaders.CONTENT_TYPE),
                    exchange.getRequestHeaders().getFirst(HttpHeaders.HOST),
                    exchange.getRequestHeaders().getFirst(HttpHeaders.CONTENT_LENGTH),
                    exchange.getRequestHeaders().keySet().stream()
                            .map(String::toLowerCase)
                            .toList()
            );
        }
    }
}
