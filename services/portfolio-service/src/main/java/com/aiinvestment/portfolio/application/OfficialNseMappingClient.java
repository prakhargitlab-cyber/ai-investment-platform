package com.aiinvestment.portfolio.application;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Set;
import java.util.TreeSet;

/** Verifies a symbol-to-ISIN association using NSE's public corporate-announcements security metadata. */
@Component
public class OfficialNseMappingClient implements NseOfficialMappingVerifier {
    private static final Logger log = LoggerFactory.getLogger(OfficialNseMappingClient.class);
    private static final String USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            + "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";
    private final ObjectMapper mapper;
    private final String announcementsUrl;
    private final HttpClient http;

    @Autowired
    public OfficialNseMappingClient(ObjectMapper mapper,
            @Value("${nse.official.announcements-url:https://www.nseindia.com/api/corporate-announcements}") String announcementsUrl) {
        this(mapper, announcementsUrl, publicHttpClient());
    }

    OfficialNseMappingClient(ObjectMapper mapper, String announcementsUrl, HttpClient http) {
        this.mapper = mapper; this.announcementsUrl = announcementsUrl; this.http = http;
    }

    @Override
    public Verification verify(String candidateSymbol) {
        if (candidateSymbol == null || candidateSymbol.isBlank()) return Verification.rejected("EMPTY_CANDIDATE");
        try {
            String candidate = candidateSymbol.trim().toUpperCase();
            URI uri = URI.create(announcementsUrl + "?index=equities&symbol="
                    + URLEncoder.encode(candidate, StandardCharsets.UTF_8));
            HttpRequest request = publicRequest(uri);
            HttpResponse<String> response = http.send(request, HttpResponse.BodyHandlers.ofString());
            log.info("nse_official_source source=NSE_CORPORATE_ANNOUNCEMENTS status={} candidateSymbol={}",
                    response.statusCode(), candidate);
            if (response.statusCode() != 200) return rejected("NSE_HTTP_" + response.statusCode());
            JsonNode root = mapper.readTree(response.body());
            if (!root.isArray() || root.isEmpty()) return rejected("NSE_EMPTY_RESPONSE");
            Set<String> isins = new TreeSet<>();
            Set<String> names = new TreeSet<>();
            boolean symbolMatched = false;
            for (JsonNode row : root) {
                if (!row.isObject() || !candidate.equalsIgnoreCase(text(row, "symbol", null))) continue;
                symbolMatched = true;
                String isin = text(row, "sm_isin", null);
                if (isin != null) isins.add(isin.toUpperCase());
                String name = text(row, "sm_name", null);
                if (name != null) names.add(name);
            }
            log.info("nse_official_identity source=NSE_CORPORATE_ANNOUNCEMENTS candidateSymbol={} symbolMatched={} isinPresent={}",
                    candidate, symbolMatched, !isins.isEmpty());
            if (!symbolMatched) return rejected("NSE_SYMBOL_NOT_RECOGNIZED");
            if (isins.isEmpty()) return rejected("NSE_ISIN_UNAVAILABLE");
            if (isins.size() != 1) return rejected("NSE_AMBIGUOUS_ISIN");
            if (names.size() != 1) return rejected("NSE_COMPANY_NAME_UNAVAILABLE_OR_AMBIGUOUS");
            String isin = isins.iterator().next();
            log.info("nse_official_verification source=NSE_CORPORATE_ANNOUNCEMENTS candidateSymbol={} result=RECOGNIZED", candidate);
            return new Verification(true, candidate, isin, names.iterator().next(), null);
        } catch (InterruptedException exception) {
            Thread.currentThread().interrupt();
            return rejected("NSE_INTERRUPTED");
        } catch (Exception exception) {
            return rejected("NSE_UNAVAILABLE");
        }
    }

    private HttpRequest publicRequest(URI uri) {
        return HttpRequest.newBuilder(uri).timeout(Duration.ofSeconds(10)).header("Accept", "application/json")
                .header("Accept-Language", "en-US,en;q=0.9").header("Referer", "https://www.nseindia.com/")
                .header("User-Agent", USER_AGENT).GET().build();
    }

    private Verification rejected(String reason) {
        log.info("nse_official_verification result=REJECTED reason={}", reason);
        return Verification.rejected(reason);
    }

    private static HttpClient publicHttpClient() {
        return HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).followRedirects(HttpClient.Redirect.NORMAL).build();
    }

    private static String text(JsonNode node, String field, String fallback) {
        JsonNode value = node.path(field);
        return value.isTextual() && !value.asText().isBlank() ? value.asText().trim() : fallback;
    }
}
