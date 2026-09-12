package com.aiinvestment.portfolio.application;

import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.Test;

import java.net.InetSocketAddress;
import java.net.http.HttpClient;

import static org.assertj.core.api.Assertions.assertThat;

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
}
