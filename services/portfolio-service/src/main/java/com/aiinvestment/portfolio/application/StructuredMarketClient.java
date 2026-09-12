package com.aiinvestment.portfolio.application;

import com.aiinvestment.shared.domain.Instrument;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentProviderMappingEntity;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.time.Instant;
import java.math.BigDecimal;
import java.util.Map;
import java.util.HashMap;
import java.util.UUID;
import com.aiinvestment.shared.web.CorrelationIdFilter;

@Component
public class StructuredMarketClient {
    private final HttpClient http = HttpClient.newBuilder()
            .version(HttpClient.Version.HTTP_1_1)
            .connectTimeout(Duration.ofSeconds(3)).build();
    private final ObjectMapper mapper;
    private final String endpoint;
    private final InstrumentMasterService instrumentMaster;
    private final NseMappingReconciliationService nseMappings;

    @Autowired
    public StructuredMarketClient(ObjectMapper mapper, InstrumentMasterService instrumentMaster,
            NseMappingReconciliationService nseMappings,
            @Value("${research.engine.base-url:http://research-engine}") String baseUrl) {
        this.mapper = mapper;
        this.instrumentMaster = instrumentMaster;
        this.nseMappings = nseMappings;
        this.endpoint = baseUrl + "/api/v1/research/structured-market/snapshot";
    }

    /** Retained for focused parser tests that do not exercise reconciliation. */
    public StructuredMarketClient(ObjectMapper mapper, InstrumentMasterService instrumentMaster, String baseUrl) {
        this(mapper, instrumentMaster, null, baseUrl);
    }

    public Snapshot fetch(Instrument instrument) {
        try {
            UUID masterId=instrumentMaster.ensureMaster(instrument.instrumentId());
            // Bootstrap authoritative NSE identity before a Yahoo discovery attempt can fail.
            if (nseMappings != null) nseMappings.reconcile(masterId);
            @SuppressWarnings("unchecked") Map<String,Object> payload=mapper.convertValue(instrument,Map.class);
            payload.put("instrumentId",masterId.toString());
            addDiscoveryIdentity(payload, instrument);
            instrumentMaster.mappings(masterId).stream().filter(this::isTrustedNseMapping)
                    .map(InstrumentProviderMappingEntity::getProviderSymbol).filter(symbol -> symbol != null && !symbol.isBlank())
                    .map(symbol -> symbol.trim().toUpperCase() + ".NS").sorted().findFirst().ifPresent(candidate -> {
                        payload.put("structuredNseCandidateTicker", candidate);
                        payload.put("structuredNseCandidateSource", "VERIFIED_NSE");
                    });
            instrumentMaster.reusableMapping(masterId,"YAHOO_FINANCE").ifPresent(mapping -> {
                payload.put("structuredProviderTicker",mapping.getProviderSymbol());
                payload.put("structuredProviderExchange",mapping.getExchange());
                payload.put("structuredProviderCurrency",mapping.getCurrency());
                payload.put("structuredProviderStatus",mapping.getStatus());
            });
            return requestAndPersist(masterId, payload, instrument.tradingCurrency(), instrument.exchange());
        } catch (InterruptedException exception) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("STRUCTURED_PROVIDER_INTERRUPTED", exception);
        } catch (Exception exception) {
            throw new IllegalStateException("STRUCTURED_PROVIDER_UNAVAILABLE", exception);
        }
    }

    /** Resolve public structured identity directly from the global master, never a portfolio-local instrument. */
    public Snapshot fetchGlobal(UUID globalInstrumentId) {
        var global = instrumentMaster.globalInstrument(globalInstrumentId)
                .orElseThrow(() -> new IllegalArgumentException("GLOBAL_INSTRUMENT_NOT_FOUND"));
        var master = global.master();
        Map<String, Object> payload = new HashMap<>();
        payload.put("instrumentId", globalInstrumentId.toString());
        payload.put("globalInstrumentId", globalInstrumentId.toString());
        payload.put("canonicalName", master.getCanonicalName());
        payload.put("companyName", master.getCanonicalName());
        payload.put("isin", master.getIsin());
        payload.put("assetType", master.getAssetType().name());
        payload.put("country", master.getCountry());
        payload.put("currency", master.getCurrency());
        payload.put("tradingCurrency", master.getCurrency());
        payload.put("exchange", master.getPrimaryExchange());
        payload.put("canonicalExchange", master.getPrimaryExchange());
        String trustedNse = global.providerMappings().stream().filter(this::isTrustedNseMapping)
                .map(InstrumentProviderMappingEntity::getProviderSymbol).filter(symbol -> symbol != null && !symbol.isBlank())
                .map(symbol -> symbol.trim().toUpperCase()).sorted().findFirst().orElse(null);
        if (trustedNse != null) {
            payload.put("ticker", trustedNse);
            payload.put("structuredNseCandidateTicker", trustedNse + ".NS");
            payload.put("structuredNseCandidateSource", "VERIFIED_NSE");
        } else {
            payload.put("ticker", master.getPrimarySymbol());
        }
        instrumentMaster.reusableMapping(globalInstrumentId, "YAHOO_FINANCE").ifPresent(mapping -> {
            payload.put("structuredProviderTicker", mapping.getProviderSymbol());
            payload.put("structuredProviderExchange", mapping.getExchange());
            payload.put("structuredProviderCurrency", mapping.getCurrency());
            payload.put("structuredProviderStatus", mapping.getStatus());
        });
        try {
            return requestAndPersist(globalInstrumentId, payload, master.getCurrency(), master.getPrimaryExchange());
        } catch (InterruptedException exception) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("STRUCTURED_PROVIDER_INTERRUPTED", exception);
        } catch (Exception exception) {
            throw new IllegalStateException("STRUCTURED_PROVIDER_UNAVAILABLE", exception);
        }
    }

    private Snapshot requestAndPersist(UUID masterId, Map<String, Object> payload, String expectedCurrency,
            String expectedExchange) throws Exception {
        HttpRequest.Builder requestBuilder = HttpRequest.newBuilder(URI.create(endpoint)).timeout(Duration.ofSeconds(15))
                    .header("Content-Type", "application/json");
        String correlationId = CorrelationIdFilter.currentId();
        if (correlationId != null && !correlationId.isBlank()) {
            requestBuilder.header(CorrelationIdFilter.HEADER_NAME, correlationId);
            requestBuilder.header(CorrelationIdFilter.REQUEST_ID_HEADER_NAME, correlationId);
        }
        HttpRequest request = requestBuilder.POST(HttpRequest.BodyPublishers.ofString(mapper.writeValueAsString(payload))).build();
            HttpResponse<String> response = http.send(request, HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() != 200) throw new IllegalStateException("STRUCTURED_PROVIDER_UNAVAILABLE");
            Snapshot snapshot=parse(response.body());
            validateIdentity(expectedCurrency, expectedExchange, snapshot);
            String source = payload.containsKey("structuredNseCandidateTicker")
                    ? "YAHOO_FROM_VERIFIED_NSE" : "YAHOO_VALIDATED_RESOLUTION";
            instrumentMaster.saveResolvedMapping(masterId,"YAHOO_FINANCE",snapshot.providerTicker(),null,
                    snapshot.exchange(),snapshot.currency(),"VERIFIED",source,new BigDecimal("0.90"));
            instrumentMaster.applyValidatedAssetType(masterId, snapshot.quoteType());
            if (nseMappings != null) nseMappings.reconcile(masterId);
            return snapshot;
    }

    private boolean isTrustedNseMapping(com.aiinvestment.portfolio.infrastructure.persistence.InstrumentProviderMappingEntity mapping) {
        return "NSE".equalsIgnoreCase(mapping.getProvider()) && "VERIFIED".equalsIgnoreCase(mapping.getStatus())
                && mapping.getProviderSymbol() != null
                && !"BROKER_IMPORT_IDENTITY".equalsIgnoreCase(mapping.getResolutionSource());
    }

    static void addDiscoveryIdentity(Map<String,Object> payload, Instrument instrument) {
        String company = java.util.stream.Stream.of(instrument.canonicalName(), instrument.companyName(),
                        instrument.brokerDescription())
                .filter(value -> value != null && !value.isBlank())
                .findFirst().orElse(null);
        String symbol = java.util.stream.Stream.of(instrument.brokerSymbol(), instrument.ticker())
                .filter(value -> value != null && !value.isBlank())
                .findFirst().orElse(null);
        if (company != null) {
            payload.put("overview", symbol == null ? company.trim() : company.trim() + " · " + symbol.trim());
            payload.put("displayIdentity", payload.get("overview"));
        }
    }

    private static void validateIdentity(String expectedCurrency, String expectedExchange, Snapshot snapshot) {
        if (!"EQUITY".equalsIgnoreCase(snapshot.quoteType()) && !"STOCK".equalsIgnoreCase(snapshot.quoteType())
                && !"ETF".equalsIgnoreCase(snapshot.quoteType()))
            throw new IllegalArgumentException("QUOTE_TYPE_MISMATCH");
        if (expectedCurrency!=null && snapshot.currency()!=null
                && !expectedCurrency.equalsIgnoreCase(snapshot.currency()))
            throw new IllegalArgumentException("QUOTE_CURRENCY_MISMATCH");
        String expected=expectedExchange==null?"":expectedExchange.toUpperCase();
        String actual=snapshot.exchange()==null?"":snapshot.exchange().toUpperCase();
        String ticker=snapshot.providerTicker()==null?"":snapshot.providerTicker().toUpperCase();
        if ((expected.contains("NSE") || "NSE".equals(expected)) && !(actual.contains("NSE") || ticker.endsWith(".NS")))
            throw new IllegalArgumentException("QUOTE_EXCHANGE_MISMATCH");
        if ((expected.contains("BSE") || "BSE".equals(expected)) && !(actual.contains("BSE") || ticker.endsWith(".BO")))
            throw new IllegalArgumentException("QUOTE_EXCHANGE_MISMATCH");
    }

    Snapshot parse(String body) {
        try {
            JsonNode root = mapper.readTree(body);
            JsonNode resolution = root.path("resolution");
            JsonNode latest = root.path("facts").path("latestPrice");
            if (!latest.hasNonNull("value")) throw new IllegalStateException("STRUCTURED_PRICE_UNAVAILABLE");
            return new Snapshot(
                    text(resolution, "providerTicker", "provider_ticker"), resolution.path("exchange").asText(null),
                    resolution.path("currency").asText(null), text(resolution, "quoteType", "quote_type"),
                    new BigDecimal(latest.path("value").asText()), latest.path("unit").asText(null),
                    instant(first(root, "marketAsOf", "market_as_of")),
                    instant(first(root, "retrievedAt", "retrieved_at")),
                    text(latest, "sourceName", "source_name", "Yahoo Finance"), root.path("status").asText());
        } catch (Exception exception) {
            throw new IllegalStateException("STRUCTURED_PROVIDER_UNAVAILABLE", exception);
        }
    }

    private static Instant instant(JsonNode value) {
        return value.isTextual() ? Instant.parse(value.asText()) : null;
    }

    private static JsonNode first(JsonNode node, String primary, String fallback) {
        JsonNode value = node.path(primary);
        return value.isMissingNode() || value.isNull() ? node.path(fallback) : value;
    }

    private static String text(JsonNode node, String primary, String fallback) {
        return text(node, primary, fallback, null);
    }

    private static String text(JsonNode node, String primary, String fallback, String defaultValue) {
        JsonNode value = first(node, primary, fallback);
        return value.isTextual() ? value.asText() : defaultValue;
    }

    public record Snapshot(String providerTicker, String exchange, String currency, String quoteType,
                           java.math.BigDecimal price, String priceCurrency, Instant marketAsOf,
                           Instant retrievedAt, String source, String status) {}
}
