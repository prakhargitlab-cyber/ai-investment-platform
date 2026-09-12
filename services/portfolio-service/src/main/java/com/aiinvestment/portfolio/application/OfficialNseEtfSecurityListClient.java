package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterEntity;
import org.apache.commons.csv.CSVFormat;
import org.apache.commons.csv.CSVParser;
import org.apache.commons.csv.CSVRecord;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.io.StringReader;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;

/** Uses NSE's public ETF securities available-for-trading CSV for exact-ISIN identity resolution. */
@Component
public class OfficialNseEtfSecurityListClient implements NseOfficialEtfSecurityList {
    private static final String USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            + "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";
    private final String securityListUrl;
    private final HttpClient http;

    @Autowired
    public OfficialNseEtfSecurityListClient(@Value("${nse.official.etf-security-list-url:https://nsearchives.nseindia.com/content/equities/eq_etfseclist.csv}") String securityListUrl) {
        this(securityListUrl, HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).followRedirects(HttpClient.Redirect.NORMAL).build());
    }

    OfficialNseEtfSecurityListClient(String securityListUrl, HttpClient http) {
        this.securityListUrl = securityListUrl;
        this.http = http;
    }

    @Override
    public Lookup lookupByIsin(String isin) {
        String expected = InstrumentMasterEntity.normalizeIsin(isin);
        if (expected == null) return Lookup.noIsinMatch();
        try {
            HttpRequest request = HttpRequest.newBuilder(URI.create(securityListUrl)).timeout(Duration.ofSeconds(10))
                    .header("Accept", "text/csv,text/plain;q=0.9,*/*;q=0.8")
                    .header("Accept-Language", "en-US,en;q=0.9").header("Referer", "https://www.nseindia.com/")
                    .header("User-Agent", USER_AGENT).GET().build();
            HttpResponse<String> response = http.send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
            if (response.statusCode() != 200) return Lookup.unavailable();
            return parse(expected, response.body());
        } catch (InterruptedException exception) {
            Thread.currentThread().interrupt();
            return Lookup.unavailable();
        } catch (Exception exception) {
            return Lookup.unavailable();
        }
    }

    static Lookup parse(String expectedIsin, String csv) throws Exception {
        expectedIsin = InstrumentMasterEntity.normalizeIsin(expectedIsin);
        if (expectedIsin == null) return Lookup.noIsinMatch();
        List<Lookup> matches = new ArrayList<>();
        try (CSVParser parser = CSVFormat.DEFAULT.builder().setHeader().setSkipHeaderRecord(true).setTrim(true).build()
                .parse(new StringReader(csv))) {
            if (!parser.getHeaderMap().keySet().containsAll(List.of("Symbol", "SecurityName", "ISINNumber"))) return Lookup.invalid();
            for (CSVRecord row : parser) {
                String isin = InstrumentMasterEntity.normalizeIsin(value(row, "ISINNumber"));
                if (!expectedIsin.equals(isin)) continue;
                String symbol = value(row, "Symbol");
                String securityName = value(row, "SecurityName");
                if (symbol == null || securityName == null) return Lookup.invalid();
                matches.add(Lookup.matched(symbol, isin, securityName, value(row, "Underlying")));
            }
        }
        if (matches.isEmpty()) return Lookup.noIsinMatch();
        if (matches.size() != 1) return Lookup.ambiguous();
        return matches.get(0);
    }

    private static String value(CSVRecord row, String header) {
        if (!row.isMapped(header)) return null;
        String value = row.get(header);
        return value == null || value.isBlank() ? null : value.trim();
    }
}
