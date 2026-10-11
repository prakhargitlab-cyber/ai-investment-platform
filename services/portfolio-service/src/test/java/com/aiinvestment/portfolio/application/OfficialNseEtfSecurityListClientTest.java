package com.aiinvestment.portfolio.application;

import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.Test;

import java.net.InetSocketAddress;
import java.net.http.HttpClient;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class OfficialNseEtfSecurityListClientTest {
    private static final String HEADER = "Symbol,Underlying,SecurityName,DateofListing,MarketLot,ISINNumber,FaceValue\n";
    private static final String HDFC = "HDFCNEXT50,HDFCNIFTYNEXT50ETF,HDFCAMC-HDFCNEXT50,11-Aug-22,1,INF179KC1HS2,418.18\n";
    private static final String GOLD = "GOLDBEES,Gold,NIPINDETFLGOLDBEES,19-Mar-07,1,INF204KB17I5,1\n";

    @Test
    void resolvesPublishedShapedHdfcAndGoldRowsByExactIsin() throws Exception {
        assertThat(OfficialNseEtfSecurityListClient.parse(" inf179kc1hs2 ", HEADER + HDFC + GOLD).symbol()).isEqualTo("HDFCNEXT50");
        assertThat(OfficialNseEtfSecurityListClient.parse("INF204KB17I5", HEADER + HDFC + GOLD).symbol()).isEqualTo("GOLDBEES");
    }

    @Test
    void failsClosedForNoMatchDuplicateBlankRequiredFieldsAndMissingHeaders() throws Exception {
        assertThat(OfficialNseEtfSecurityListClient.parse("INF000000000", HEADER + HDFC).status()).isEqualTo("NO_ISIN_MATCH");
        assertThat(OfficialNseEtfSecurityListClient.parse("INF179KC1HS2", HEADER + HDFC + HDFC).status()).isEqualTo("AMBIGUOUS");
        assertThat(OfficialNseEtfSecurityListClient.parse("INF179KC1HS2", HEADER
                + ",HDFCNIFTYNEXT50ETF,HDFCAMC-HDFCNEXT50,11-Aug-22,1,INF179KC1HS2,1\n").status()).isEqualTo("INVALID");
        assertThat(OfficialNseEtfSecurityListClient.parse("INF179KC1HS2", "Symbol,ISINNumber\nHDFCNEXT50,INF179KC1HS2\n").status()).isEqualTo("INVALID");
    }

    @Test
    void returnsUnavailableForNon200Response() throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress(0), 0);
        server.createContext("/etf.csv", exchange -> { exchange.sendResponseHeaders(503, -1); exchange.close(); });
        server.start();
        try {
            var client = new OfficialNseEtfSecurityListClient("http://localhost:" + server.getAddress().getPort() + "/etf.csv",
                    HttpClient.newHttpClient());
            assertThat(client.lookupByIsin("INF179KC1HS2").status()).isEqualTo("UNAVAILABLE");
        } finally {
            server.stop(0);
        }
    }

    // --- NSE transport-fix regression coverage: HTTP/1.1, headers, listAll() fail-closed behavior ---
    // (ai-investment-platform: NSE ETF source connectivity fix). None of these touch the live NSE host.

    @Test
    void productionConstructorConfiguresHttp11AndExistingTimeoutsAndRedirects() throws Exception {
        var client = new OfficialNseEtfSecurityListClient("https://example.invalid/etf.csv");
        var httpField = OfficialNseEtfSecurityListClient.class.getDeclaredField("http");
        httpField.setAccessible(true);
        HttpClient http = (HttpClient) httpField.get(client);

        assertThat(http.version()).isEqualTo(HttpClient.Version.HTTP_1_1);
        assertThat(http.connectTimeout()).contains(Duration.ofSeconds(5));
        assertThat(http.followRedirects()).isEqualTo(HttpClient.Redirect.NORMAL);
    }

    @Test
    void sendsBrowserStyleHeadersOnEveryRequest() throws Exception {
        AtomicReference<Headers> seenHeaders = new AtomicReference<>();
        HttpServer server = HttpServer.create(new InetSocketAddress(0), 0);
        server.createContext("/etf.csv", exchange -> {
            seenHeaders.set(exchange.getRequestHeaders());
            byte[] body = (HEADER + HDFC).getBytes(StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(200, body.length);
            exchange.getResponseBody().write(body);
            exchange.close();
        });
        server.start();
        try {
            var client = new OfficialNseEtfSecurityListClient("http://localhost:" + server.getAddress().getPort() + "/etf.csv",
                    HttpClient.newHttpClient());
            assertThat(client.lookupByIsin("INF179KC1HS2").status()).isEqualTo("MATCHED");
        } finally {
            server.stop(0);
        }

        Headers headers = seenHeaders.get();
        assertThat(headers.get("User-Agent").get(0)).contains("Mozilla/5.0");
        assertThat(headers.getFirst("Accept")).contains("text/csv");
        assertThat(headers.getFirst("Accept-Language")).contains("en-US");
        assertThat(headers.getFirst("Referer")).isEqualTo("https://www.nseindia.com/");
    }

    @Test
    void listAllParsesEveryWellFormedRow() throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress(0), 0);
        server.createContext("/etf.csv", exchange -> {
            byte[] body = (HEADER + HDFC + GOLD).getBytes(StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(200, body.length);
            exchange.getResponseBody().write(body);
            exchange.close();
        });
        server.start();
        try {
            var client = new OfficialNseEtfSecurityListClient("http://localhost:" + server.getAddress().getPort() + "/etf.csv",
                    HttpClient.newHttpClient());
            List<OfficialNseEtfSecurityListClient.Listing> listings = client.listAll();

            assertThat(listings).hasSize(2);
            assertThat(listings.get(0).symbol()).isEqualTo("HDFCNEXT50");
            assertThat(listings.get(0).isin()).isEqualTo("INF179KC1HS2");
            assertThat(listings.get(1).symbol()).isEqualTo("GOLDBEES");
        } finally {
            server.stop(0);
        }
    }

    @Test
    void listAllThrowsRatherThanReturningEmptyWhenTheProviderIsUnavailable() throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress(0), 0);
        server.createContext("/etf.csv", exchange -> { exchange.sendResponseHeaders(503, -1); exchange.close(); });
        server.start();
        try {
            var client = new OfficialNseEtfSecurityListClient("http://localhost:" + server.getAddress().getPort() + "/etf.csv",
                    HttpClient.newHttpClient());
            // Must throw, never silently return List.of() -- an empty list here would be
            // indistinguishable from "fetched successfully, authoritatively zero ETFs" and
            // would let a caller wrongly treat every existing ETF as absent.
            assertThatThrownBy(client::listAll)
                    .isInstanceOf(IllegalStateException.class)
                    .hasMessage("NSE_ETF_LIST_UNAVAILABLE");
        } finally {
            server.stop(0);
        }
    }

    @Test
    void listAllThrowsRatherThanReturningEmptyForMalformedCsv() throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress(0), 0);
        server.createContext("/etf.csv", exchange -> {
            byte[] body = "Symbol,ISINNumber\nHDFCNEXT50,INF179KC1HS2\n".getBytes(StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(200, body.length);
            exchange.getResponseBody().write(body);
            exchange.close();
        });
        server.start();
        try {
            var client = new OfficialNseEtfSecurityListClient("http://localhost:" + server.getAddress().getPort() + "/etf.csv",
                    HttpClient.newHttpClient());
            assertThatThrownBy(client::listAll)
                    .isInstanceOf(IllegalStateException.class)
                    .hasMessage("NSE_ETF_LIST_INVALID");
        } finally {
            server.stop(0);
        }
    }
}
