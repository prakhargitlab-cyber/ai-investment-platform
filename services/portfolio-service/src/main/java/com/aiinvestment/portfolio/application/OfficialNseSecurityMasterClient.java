package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterEntity;
import org.apache.commons.csv.CSVFormat;
import org.apache.commons.csv.CSVParser;
import org.apache.commons.csv.CSVRecord;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
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
import java.util.Locale;

/** Uses NSE's public listed-equity CSV, whose rows contain symbol, company, series and ISIN. */
@Component
public class OfficialNseSecurityMasterClient implements NseOfficialSecurityMaster {
    private static final Logger LOGGER = LoggerFactory.getLogger(OfficialNseSecurityMasterClient.class);
    private static final List<String> REQUIRED_LISTING_HEADERS = List.of(
            "SYMBOL", "NAME OF COMPANY", "SERIES", "ISIN NUMBER"
    );
    private static final String USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            + "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";
    private final String securityListUrl;
    private final HttpClient http;

    @Autowired
    public OfficialNseSecurityMasterClient(@Value("${nse.official.equity-security-list-url:https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv}") String securityListUrl) {
        this(securityListUrl, HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).followRedirects(HttpClient.Redirect.NORMAL).build());
    }

    OfficialNseSecurityMasterClient(String securityListUrl, HttpClient http) {
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
    @Override
    public List<Listing> listedEquities() {
        try {
            HttpResponse<String> response = http.send(
                    request(),
                    HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8)
            );
            String body = response.body();
            int bodyLength = body == null ? 0 : body.length();
            LOGGER.info("NSE listed-equities response received: status={}, bodyLength={}",
                    response.statusCode(), bodyLength);
            if (response.statusCode() != 200) return List.of();

            LOGGER.info("NSE listed-equities parser starting: bodyLength={}", bodyLength);
            List<Listing> listings = parseListings(body);
            LOGGER.info("NSE listed-equities parser completed: listingCount={}", listings.size());
            return listings;
        } catch (InterruptedException exception) {
            Thread.currentThread().interrupt();
            logListedEquitiesFailure(exception);
            return List.of();
        } catch (Exception exception) {
            logListedEquitiesFailure(exception);
            return List.of();
        }
    }

    private static void logListedEquitiesFailure(Exception exception) {
        LOGGER.warn("NSE listed-equities request or parsing failed: exceptionClass={}, message={}",
                exception.getClass().getName(), safeExceptionMessage(exception));
    }

    private static String safeExceptionMessage(Exception exception) {
        String message = exception.getMessage();
        return message != null && message.matches("NSE_MASTER_[A-Z_]+") ? message : "omitted";
    }

    private HttpRequest request() { return HttpRequest.newBuilder(URI.create(securityListUrl)).timeout(Duration.ofSeconds(10))
            .header("Accept", "text/csv,text/plain;q=0.9,*/*;q=0.8").header("Accept-Language", "en-US,en;q=0.9").header("Referer", "https://www.nseindia.com/").header("User-Agent", USER_AGENT).GET().build(); }
    static List<Listing> parseListings(String csv) throws Exception {
        if (csv == null) throw new IllegalArgumentException("NSE_MASTER_BODY_REQUIRED");
        List<Listing> out = new ArrayList<>();
        try (CSVParser parser = CSVFormat.DEFAULT.builder().setHeader().setSkipHeaderRecord(true).setTrim(true).build()
                .parse(new StringReader(csv))) {
            if (!parser.getHeaderMap().keySet().containsAll(REQUIRED_LISTING_HEADERS)) {
                throw new IllegalArgumentException("NSE_MASTER_REQUIRED_HEADERS_MISSING");
            }
            for (CSVRecord row : parser) {
                String isin = InstrumentMasterEntity.normalizeIsin(value(row, "ISIN NUMBER"));
                String symbol = value(row, "SYMBOL");
                if (isin != null && symbol != null) {
                    out.add(new Listing(symbol, isin, value(row, "NAME OF COMPANY"), value(row, "SERIES")));
                }
            }
        }
        return out;
    }

    static Lookup parse(String expectedIsin, String csv) throws Exception {
        List<Lookup> matches = new ArrayList<>();
        try (CSVParser parser = CSVFormat.DEFAULT.builder().setHeader().setSkipHeaderRecord(true).setTrim(true).build()
                .parse(new StringReader(csv))) {
            for (CSVRecord row : parser) {
                String isin = InstrumentMasterEntity.normalizeIsin(value(row, "ISIN NUMBER"));
                if (!expectedIsin.equals(isin)) continue;
                matches.add(Lookup.matched(value(row, "SYMBOL"), isin, value(row, "NAME OF COMPANY"), value(row, "SERIES")));
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
