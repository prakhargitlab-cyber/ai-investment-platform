package com.aiinvestment.portfolio.application;

import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.http.HttpClient;
import java.nio.charset.StandardCharsets;
import java.util.Objects;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class OfficialNseSecurityMasterClientTest {
    private static final String FIXTURE_PATH = "/fixtures/nse/EQUITY_L.csv";

    @Test
    void listedEquitiesParsesCurrentNseCsvFixture() throws Exception {
        String csv = fixture();
        HttpServer server = csvServer(200, csv);
        try {
            var listings = client(server).listedEquities();

            assertThat(listings).isNotEmpty();
            assertThat(listings.get(0)).satisfies(listing -> {
                assertThat(listing.symbol()).isEqualTo("RELIANCE");
                assertThat(listing.isin()).isEqualTo("INE002A01018");
                assertThat(listing.companyName()).isEqualTo("Reliance Industries Limited");
                assertThat(listing.series()).isEqualTo("EQ");
            });
        } finally {
            server.stop(0);
        }
    }

    @Test
    void listedEquitiesFailsSafelyForInvalidCsvHeaders() throws Exception {
        String invalidCsv = "symbol,company,isin\nBROKEN,Broken Company,INE002A01018\n";
        assertThatThrownBy(() -> OfficialNseSecurityMasterClient.parseListings(invalidCsv))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessage("NSE_MASTER_REQUIRED_HEADERS_MISSING");

        HttpServer server = csvServer(200, invalidCsv);
        try {
            assertThat(client(server).listedEquities()).isEmpty();
        } finally {
            server.stop(0);
        }
    }

    @Test
    void listedEquitiesFailsSafelyForNon200Response() throws Exception {
        HttpServer server = csvServer(503, "");
        try {
            assertThat(client(server).listedEquities()).isEmpty();
        } finally {
            server.stop(0);
        }
    }

    @Test
    void parsesOfficialEquityMasterByExactIsin() throws Exception {
        String csv = fixture();

        var result = OfficialNseSecurityMasterClient.parse("INE002A01018", csv);

        assertThat(result.status()).isEqualTo("MATCHED");
        assertThat(result.symbol()).isEqualTo("RELIANCE");
        assertThat(result.companyName()).isEqualTo("Reliance Industries Limited");
        assertThat(result.series()).isEqualTo("EQ");
    }

    @Test
    void distinguishesNoMatchAndAmbiguousOfficialRows() throws Exception {
        String csv = "SYMBOL,NAME OF COMPANY,SERIES,ISIN NUMBER\n"
                + "ONE,Example One,EQ,INE123A01012\nTWO,Example Two,EQ,INE123A01012\n";
        assertThat(OfficialNseSecurityMasterClient.parse("INE999Z01011", csv).status()).isEqualTo("NO_ISIN_MATCH");
        assertThat(OfficialNseSecurityMasterClient.parse("INE123A01012", csv).status()).isEqualTo("AMBIGUOUS");
    }

    private static OfficialNseSecurityMasterClient client(HttpServer server) {
        return new OfficialNseSecurityMasterClient(
                "http://localhost:" + server.getAddress().getPort() + "/EQUITY_L.csv",
                HttpClient.newHttpClient()
        );
    }

    private static HttpServer csvServer(int status, String body) throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        HttpServer server = HttpServer.create(new InetSocketAddress(0), 0);
        server.createContext("/EQUITY_L.csv", exchange -> {
            exchange.getResponseHeaders().add("Content-Type", "text/csv; charset=UTF-8");
            if (status == 200) {
                exchange.sendResponseHeaders(status, bytes.length);
                exchange.getResponseBody().write(bytes);
            } else {
                exchange.sendResponseHeaders(status, -1);
            }
            exchange.close();
        });
        server.start();
        return server;
    }

    private static String fixture() throws IOException {
        try (var stream = OfficialNseSecurityMasterClientTest.class.getResourceAsStream(FIXTURE_PATH)) {
            return new String(Objects.requireNonNull(stream, FIXTURE_PATH).readAllBytes(), StandardCharsets.UTF_8);
        }
    }
}
