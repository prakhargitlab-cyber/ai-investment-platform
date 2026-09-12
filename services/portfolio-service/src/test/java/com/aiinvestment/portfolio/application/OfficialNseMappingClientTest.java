package com.aiinvestment.portfolio.application;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.Test;
import org.springframework.context.annotation.AnnotationConfigApplicationContext;

import java.net.InetSocketAddress;
import java.net.http.HttpClient;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;

class OfficialNseMappingClientTest {
    @Test
    void springCanConstructProductionBeanWithoutDefaultConstructor() {
        try (AnnotationConfigApplicationContext context = new AnnotationConfigApplicationContext()) {
            context.getBeanFactory().registerSingleton("objectMapper", new ObjectMapper());
            context.register(OfficialNseMappingClient.class);
            context.refresh();

            assertThat(context.getBean(OfficialNseMappingClient.class)).isNotNull();
        }
    }

    @Test
    void acceptsExactOfficialAnnouncementSymbolAndIsin() throws Exception {
        AtomicReference<String> query = new AtomicReference<>();
        AtomicReference<String> referer = new AtomicReference<>();
        withServer(server -> {
            server.createContext("/api/corporate-announcements", exchange -> {
                query.set(exchange.getRequestURI().getQuery());
                referer.set(exchange.getRequestHeaders().getFirst("Referer"));
                respond(exchange, 200, "[{\"symbol\":\"CANONICAL\",\"sm_isin\":\"INE000A01010\",\"sm_name\":\"Canonical Limited\"}]");
            });
        }, client -> {
            var result = client.verify("canonical");
            assertThat(result.recognized()).isTrue();
            assertThat(result.symbol()).isEqualTo("CANONICAL");
            assertThat(result.isin()).isEqualTo("INE000A01010");
        });
        assertThat(query.get()).contains("index=equities").contains("symbol=CANONICAL");
        assertThat(referer.get()).isNotBlank();
    }

    @Test
    void http403RemainsRejected() throws Exception {
        withServer(server -> {
            server.createContext("/api/corporate-announcements", exchange -> respond(exchange, 403, "forbidden"));
        }, client -> assertThat(client.verify("CANONICAL").reason()).isEqualTo("NSE_HTTP_403"));
    }

    @Test
    void http404And5xxRemainRejected() throws Exception {
        withServer(server -> server.createContext("/api/corporate-announcements", exchange -> respond(exchange, 404, "missing")),
                client -> assertThat(client.verify("CANONICAL").reason()).isEqualTo("NSE_HTTP_404"));
        withServer(server -> server.createContext("/api/corporate-announcements", exchange -> respond(exchange, 503, "unavailable")),
                client -> assertThat(client.verify("CANONICAL").reason()).isEqualTo("NSE_HTTP_503"));
    }

    @Test
    void malformedQuoteResponseIsRejected() throws Exception {
        withServer(server -> {
            server.createContext("/api/corporate-announcements", exchange -> respond(exchange, 200, "not-json"));
        }, client -> assertThat(client.verify("CANONICAL").reason()).isEqualTo("NSE_UNAVAILABLE"));
    }

    @Test
    void officialSymbolMismatchIsRejected() throws Exception {
        withServer(server -> {
            server.createContext("/api/corporate-announcements", exchange -> respond(exchange, 200,
                    "[{\"symbol\":\"OTHER\",\"sm_isin\":\"INE000A01010\"}]"));
        }, client -> assertThat(client.verify("CANONICAL").reason()).isEqualTo("NSE_SYMBOL_NOT_RECOGNIZED"));
    }

    @Test
    void missingOfficialIsinAndEmptyOfficialResponseAreRejected() throws Exception {
        withServer(server -> {
            server.createContext("/api/corporate-announcements", exchange -> respond(exchange, 200,
                    "[{\"symbol\":\"CANONICAL\"}]"));
        }, client -> assertThat(client.verify("CANONICAL").reason()).isEqualTo("NSE_ISIN_UNAVAILABLE"));
        withServer(server -> {
            server.createContext("/api/corporate-announcements", exchange -> respond(exchange, 200, "[]"));
        }, client -> assertThat(client.verify("CANONICAL").reason()).isEqualTo("NSE_EMPTY_RESPONSE"));
    }

    private static void withServer(ServerSetup setup, ClientCheck check) throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        setup.configure(server);
        server.start();
        try {
            String root = "http://127.0.0.1:" + server.getAddress().getPort();
            HttpClient http = HttpClient.newBuilder().followRedirects(HttpClient.Redirect.NORMAL).build();
            check.run(new OfficialNseMappingClient(new ObjectMapper(), root + "/api/corporate-announcements", http));
        } finally {
            server.stop(0);
        }
    }

    private static void respond(com.sun.net.httpserver.HttpExchange exchange, int status, String body) throws java.io.IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        exchange.sendResponseHeaders(status, bytes.length);
        exchange.getResponseBody().write(bytes);
        exchange.close();
    }

    @FunctionalInterface private interface ServerSetup { void configure(HttpServer server) throws Exception; }
    @FunctionalInterface private interface ClientCheck { void run(OfficialNseMappingClient client) throws Exception; }
}
