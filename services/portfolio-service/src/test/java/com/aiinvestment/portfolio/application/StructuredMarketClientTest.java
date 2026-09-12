package com.aiinvestment.portfolio.application;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentProviderMappingEntity;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;
import java.math.BigDecimal;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.HashMap;
import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicReference;
import com.aiinvestment.shared.domain.AssetType;
import com.aiinvestment.shared.domain.Instrument;

class StructuredMarketClientTest {
    @Test
    void parsesProviderNeutralFastApiSnakeCaseContract() {
        var client = new StructuredMarketClient(new ObjectMapper().findAndRegisterModules(),
                org.mockito.Mockito.mock(InstrumentMasterService.class), "http://research");
        var snapshot = client.parse("""
            {"resolution":{"provider_ticker":"ZENTEC.NS","exchange":"NSE","currency":"INR","quote_type":"EQUITY"},
             "facts":{"latestPrice":{"value":1825.60,"unit":"INR","source_name":"Yahoo Finance"}},
             "market_as_of":"2026-08-30T10:00:00Z","retrieved_at":"2026-08-30T10:00:01Z","status":"STRUCTURED_PROVIDER_PARTIAL"}
            """);
        assertThat(snapshot.providerTicker()).isEqualTo("ZENTEC.NS");
        assertThat(snapshot.price()).isEqualByComparingTo("1825.60");
        assertThat(snapshot.marketAsOf()).hasToString("2026-08-30T10:00:00Z");
        assertThat(snapshot.source()).isEqualTo("Yahoo Finance");
    }

    @Test
    void parsesDeployedFastApiCamelCaseContract() {
        var client = new StructuredMarketClient(new ObjectMapper().findAndRegisterModules(),
                org.mockito.Mockito.mock(InstrumentMasterService.class), "http://research");
        var snapshot = client.parse("""
            {"resolution":{"providerTicker":"ZENTEC.NS","exchange":"NSI","currency":"INR","quoteType":"EQUITY"},
             "facts":{"latestPrice":{"value":"1826.80","unit":"INR","sourceName":"Yahoo Finance"}},
             "marketAsOf":"2026-08-28T09:59:57Z","retrievedAt":"2026-08-30T21:17:06Z","status":"STRUCTURED_PROVIDER_AVAILABLE"}
            """);
        assertThat(snapshot.providerTicker()).isEqualTo("ZENTEC.NS");
        assertThat(snapshot.price()).isEqualByComparingTo("1826.80");
        assertThat(snapshot.quoteType()).isEqualTo("EQUITY");
        assertThat(snapshot.marketAsOf()).hasToString("2026-08-28T09:59:57Z");
        assertThat(snapshot.source()).isEqualTo("Yahoo Finance");
    }

    @Test
    void sendsFullCompanyOverviewWhileKeepingBrokerSymbolSeparate() {
        var instrument = new Instrument(UUID.randomUUID(), "IBKR", "316537032", null, "BESI", "SMART", null,
                "BE SEMICONDUCTOR INDUSTRIES", AssetType.EQUITY, "NL", "EUR", null, null,
                "BESI", "BESI", "AEB", "BESI", "BE SEMICONDUCTOR INDUSTRIES", "SMART", null, "EQUITY");
        var payload = new HashMap<String,Object>();
        StructuredMarketClient.addDiscoveryIdentity(payload, instrument);
        assertThat(payload.get("overview")).isEqualTo("BE SEMICONDUCTOR INDUSTRIES · BESI");
        assertThat(instrument.brokerSymbol()).isEqualTo("BESI");
        assertThat(payload.values()).noneMatch(value -> String.valueOf(value).endsWith(".AS") || String.valueOf(value).endsWith(".DE"));
    }

    @Test
    void existingVerifiedYahooMappingStillTriggersNseReconciliationAfterStructuredRefresh() throws Exception {
        InstrumentMasterService master = mock(InstrumentMasterService.class);
        NseMappingReconciliationService reconciliation = mock(NseMappingReconciliationService.class);
        UUID globalId = UUID.randomUUID();
        when(master.ensureMaster(any())).thenReturn(globalId);
        when(master.reusableMapping(globalId, "YAHOO_FINANCE")).thenReturn(Optional.of(mapping("TALBROAUTO.NS")));

        fetchFromLocalStructuredEndpoint(master, reconciliation);

        verify(master).saveResolvedMapping(eq(globalId), eq("YAHOO_FINANCE"), eq("TALBROAUTO.NS"), isNull(), eq("NSE"),
                eq("INR"), eq("VERIFIED"), eq("YAHOO_VALIDATED_RESOLUTION"), eq(new BigDecimal("0.90")));
        verify(reconciliation, times(2)).reconcile(globalId);
    }

    @Test
    void newlyResolvedYahooMappingTriggersNseReconciliationAfterStructuredRefresh() throws Exception {
        InstrumentMasterService master = mock(InstrumentMasterService.class);
        NseMappingReconciliationService reconciliation = mock(NseMappingReconciliationService.class);
        UUID globalId = UUID.randomUUID();
        when(master.ensureMaster(any())).thenReturn(globalId);
        when(master.reusableMapping(globalId, "YAHOO_FINANCE")).thenReturn(Optional.empty());

        fetchFromLocalStructuredEndpoint(master, reconciliation);

        verify(master).saveResolvedMapping(eq(globalId), eq("YAHOO_FINANCE"), eq("TALBROAUTO.NS"), isNull(), eq("NSE"),
                eq("INR"), eq("VERIFIED"), eq("YAHOO_VALIDATED_RESOLUTION"), eq(new BigDecimal("0.90")));
        verify(reconciliation, times(2)).reconcile(globalId);
    }

    @Test
    void trustedVerifiedNseMappingPersistsYahooWithVerifiedNseSource() throws Exception {
        InstrumentMasterService master = mock(InstrumentMasterService.class);
        NseMappingReconciliationService reconciliation = mock(NseMappingReconciliationService.class);
        UUID globalId = UUID.randomUUID();
        when(master.ensureMaster(any())).thenReturn(globalId);
        when(master.reusableMapping(globalId, "YAHOO_FINANCE")).thenReturn(Optional.empty());
        when(master.mappings(globalId)).thenReturn(java.util.List.of(new InstrumentProviderMappingEntity(UUID.randomUUID(), globalId,
                "NSE", "OFFICIAL", null, "NSE", "INR", "VERIFIED", "NSE_OFFICIAL_ISIN_BOOTSTRAP", new BigDecimal("0.99"), Instant.now())));

        fetchFromLocalStructuredEndpoint(master, reconciliation);

        verify(master).saveResolvedMapping(eq(globalId), eq("YAHOO_FINANCE"), eq("TALBROAUTO.NS"), isNull(), eq("NSE"),
                eq("INR"), eq("VERIFIED"), eq("YAHOO_FROM_VERIFIED_NSE"), eq(new BigDecimal("0.90")));
    }

    @Test
    void globalFetchUsesTrustedNseSymbolNotPrimaryOrInvalidBrokerAlias() throws Exception {
        InstrumentMasterService master = mock(InstrumentMasterService.class);
        NseMappingReconciliationService reconciliation = mock(NseMappingReconciliationService.class);
        UUID globalId = UUID.randomUUID();
        var official = new InstrumentProviderMappingEntity(UUID.randomUUID(), globalId, "NSE", "OFFICIAL", null,
                "NSE", "INR", "VERIFIED", "NSE_OFFICIAL_ISIN_BOOTSTRAP", new BigDecimal("0.99"), Instant.now());
        var invalid = new InstrumentProviderMappingEntity(UUID.randomUUID(), globalId, "NSE", "BROKER_ALIAS", null,
                "NSE", "INR", "INVALID", "BROKER_IMPORT_IDENTITY", new BigDecimal("0.10"), Instant.now());
        var global = new InstrumentMasterService.GlobalInstrument(new com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterEntity(
                globalId, "INE000A01010", "Generic Components Limited", AssetType.EQUITY, "INR", "IN", "NSE", "BROKER_ALIAS", "ACTIVE", Instant.now()),
                java.util.List.of(official, invalid));
        when(master.globalInstrument(globalId)).thenReturn(Optional.of(global));
        when(master.reusableMapping(globalId, "YAHOO_FINANCE")).thenReturn(Optional.empty());
        AtomicReference<String> body = new AtomicReference<>();
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        byte[] response = """
                {"resolution":{"providerTicker":"OFFICIAL.NS","exchange":"NSE","currency":"INR","quoteType":"EQUITY"},
                 "facts":{"latestPrice":{"value":"100.00","unit":"INR"}},"status":"STRUCTURED_PROVIDER_AVAILABLE"}
                """.getBytes(StandardCharsets.UTF_8);
        server.createContext("/api/v1/research/structured-market/snapshot", exchange -> {
            body.set(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            exchange.sendResponseHeaders(200, response.length);
            exchange.getResponseBody().write(response);
            exchange.close();
        });
        server.start();
        try {
            new StructuredMarketClient(new ObjectMapper().findAndRegisterModules(), master, reconciliation,
                    "http://127.0.0.1:" + server.getAddress().getPort()).fetchGlobal(globalId);
        } finally {
            server.stop(0);
        }
        assertThat(body.get()).contains("\"structuredNseCandidateTicker\":\"OFFICIAL.NS\"");
        assertThat(body.get()).doesNotContain("BROKER_ALIAS.NS");
        verify(master).saveResolvedMapping(eq(globalId), eq("YAHOO_FINANCE"), eq("OFFICIAL.NS"), isNull(), eq("NSE"),
                eq("INR"), eq("VERIFIED"), eq("YAHOO_FROM_VERIFIED_NSE"), eq(new BigDecimal("0.90")));
    }

    private static void fetchFromLocalStructuredEndpoint(InstrumentMasterService master,
            NseMappingReconciliationService reconciliation) throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        byte[] response = """
                {"resolution":{"providerTicker":"TALBROAUTO.NS","exchange":"NSE","currency":"INR","quoteType":"EQUITY"},
                 "facts":{"latestPrice":{"value":"100.00","unit":"INR","sourceName":"Yahoo Finance"}},
                 "status":"STRUCTURED_PROVIDER_AVAILABLE"}
                """.getBytes(StandardCharsets.UTF_8);
        server.createContext("/api/v1/research/structured-market/snapshot", exchange -> {
            exchange.sendResponseHeaders(200, response.length);
            exchange.getResponseBody().write(response);
            exchange.close();
        });
        server.start();
        try {
            String baseUrl = "http://127.0.0.1:" + server.getAddress().getPort();
            var client = new StructuredMarketClient(new ObjectMapper().findAndRegisterModules(), master, reconciliation, baseUrl);
            client.fetch(indianEquity());
        } finally {
            server.stop(0);
        }
    }

    private static Instrument indianEquity() {
        return new Instrument(UUID.randomUUID(), "ICICI_DIRECT", "icici-1", "INE187D01029", "TALAUT", "NSE", "XNSE",
                "Talbros Automotive Components", AssetType.EQUITY, "IN", "INR", null, null, "TALAUT",
                "Talbros Automotive Components", "NSE", "TALAUT", "Talbros Automotive Components", "NSE", "XNSE", "EQUITY");
    }

    private static InstrumentProviderMappingEntity mapping(String symbol) {
        return new InstrumentProviderMappingEntity(UUID.randomUUID(), UUID.randomUUID(), "YAHOO_FINANCE", symbol, null,
                "NSE", "INR", "VERIFIED", "YAHOO_VALIDATED_RESOLUTION", new BigDecimal("0.90"), Instant.now());
    }
}
