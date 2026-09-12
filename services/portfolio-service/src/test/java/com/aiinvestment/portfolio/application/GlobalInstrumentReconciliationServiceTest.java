package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterEntity;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentProviderMappingEntity;
import com.aiinvestment.shared.domain.AssetType;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.List;
import java.util.Optional;
import java.util.UUID;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

class GlobalInstrumentReconciliationServiceTest {
    private InstrumentMasterService instruments;
    private NseMappingReconciliationService nse;
    private StructuredMarketClient structured;
    private GlobalInstrumentReconciliationService service;
    private UUID id;

    @BeforeEach
    void setUp() {
        instruments = mock(InstrumentMasterService.class);
        nse = mock(NseMappingReconciliationService.class);
        structured = mock(StructuredMarketClient.class);
        service = new GlobalInstrumentReconciliationService(instruments, nse, structured);
        id = UUID.randomUUID();
        when(instruments.globalInstrument(id)).thenReturn(Optional.of(global(List.of(officialNse("OFFICIAL")))));
        when(nse.reconcile(id)).thenReturn(new NseMappingReconciliationService.Outcome("SKIPPED", null, "TRUSTED_NSE_MAPPING_EXISTS"));
        when(instruments.reusableMapping(id, "YAHOO_FINANCE")).thenReturn(Optional.empty());
        when(instruments.mappings(id)).thenReturn(List.of(officialNse("OFFICIAL")));
    }

    @Test
    void trustedOfficialNseWithoutYahooUsesExistingStructuredValidationPath() {
        service.reconcile(id);

        verify(nse).reconcile(id);
        verify(structured).fetchGlobal(id);
    }

    @Test
    void existingVerifiedYahooIsReusedWithoutStructuredProviderCall() {
        when(instruments.reusableMapping(id, "YAHOO_FINANCE")).thenReturn(Optional.of(mapping("YAHOO_FINANCE", "OFFICIAL.NS", "VERIFIED", "YAHOO_FROM_VERIFIED_NSE")));

        service.reconcile(id);

        verify(nse).reconcile(id);
        verifyNoInteractions(structured);
    }

    @Test
    void invalidBrokerDerivedNseAliasCannotTriggerYahooValidation() {
        var invalid = mapping("NSE", "BROKER_ALIAS", "INVALID", "BROKER_IMPORT_IDENTITY");
        when(instruments.globalInstrument(id)).thenReturn(Optional.of(global(List.of(invalid))));
        when(instruments.mappings(id)).thenReturn(List.of(invalid));

        service.reconcile(id);

        verifyNoInteractions(structured);
    }

    @Test
    void structuredProviderFailureIsContainedForFutureRetry() {
        doThrow(new IllegalStateException("STRUCTURED_PROVIDER_UNAVAILABLE")).when(structured).fetchGlobal(id);

        service.reconcile(id);

        verify(structured).fetchGlobal(id);
        verify(instruments, never()).saveResolvedMapping(any(), any(), any(), any(), any(), any(), any(), any(), any());
    }

    private InstrumentMasterService.GlobalInstrument global(List<InstrumentProviderMappingEntity> mappings) {
        return new InstrumentMasterService.GlobalInstrument(new InstrumentMasterEntity(id, "INE000A01010", "Generic Components Limited",
                AssetType.EQUITY, "INR", "IN", "NSE", "BROKER_ALIAS", "ACTIVE", Instant.now()), mappings);
    }

    private InstrumentProviderMappingEntity officialNse(String symbol) {
        return mapping("NSE", symbol, "VERIFIED", "NSE_OFFICIAL_ISIN_BOOTSTRAP");
    }

    private InstrumentProviderMappingEntity mapping(String provider, String symbol, String status, String source) {
        return new InstrumentProviderMappingEntity(UUID.randomUUID(), id, provider, symbol, null, "NSE", "INR", status, source,
                new BigDecimal("0.99"), Instant.now());
    }
}
