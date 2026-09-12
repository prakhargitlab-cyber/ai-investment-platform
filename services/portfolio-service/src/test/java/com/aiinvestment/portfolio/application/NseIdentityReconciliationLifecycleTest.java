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

import static org.mockito.Mockito.*;

class NseIdentityReconciliationLifecycleTest {
    private InstrumentMasterService instruments;
    private NseMappingReconciliationService nse;
    private NseIdentityReconciliationLifecycle lifecycle;
    private UUID id;

    @BeforeEach
    void setUp() {
        instruments = mock(InstrumentMasterService.class);
        nse = mock(NseMappingReconciliationService.class);
        lifecycle = new NseIdentityReconciliationLifecycle(instruments, nse);
        id = UUID.randomUUID();
    }

    @Test
    void newOrExistingUnresolvedIndianNseEquityAndEtfTriggerGlobalReconciliation() {
        when(instruments.globalInstrument(id)).thenReturn(Optional.of(global(AssetType.EQUITY, "IN", "NSE", List.of())));
        lifecycle.reconcileAfterAttachment(new GlobalInstrumentAttachedEvent(id));
        verify(nse).reconcile(id);

        reset(nse);
        when(instruments.globalInstrument(id)).thenReturn(Optional.of(global(AssetType.ETF, "IN", "NSE", List.of(invalidNse()))));
        lifecycle.reconcileAfterAttachment(new GlobalInstrumentAttachedEvent(id));
        verify(nse).reconcile(id);
    }

    @Test
    void importedUnknownExchangeTriggersOnlyWithValidIndianIsin() {
        when(instruments.globalInstrument(id)).thenReturn(Optional.of(
                global(AssetType.EQUITY, "IN", "UNKNOWN", "INE551W01018", List.of())));
        lifecycle.reconcileAfterAttachment(new GlobalInstrumentAttachedEvent(id));
        verify(nse).reconcile(id);

        reset(nse);
        when(instruments.globalInstrument(id)).thenReturn(Optional.of(
                global(AssetType.EQUITY, "IN", "UNKNOWN", "not-an-isin", List.of())));
        lifecycle.reconcileAfterAttachment(new GlobalInstrumentAttachedEvent(id));
        verifyNoInteractions(nse);
    }

    @Test
    void trustedNseAndIneligibleInstrumentsDoNotTriggerReconciliation() {
        when(instruments.globalInstrument(id)).thenReturn(Optional.of(global(AssetType.EQUITY, "IN", "NSE", List.of(verifiedNse()))));
        lifecycle.reconcileAfterAttachment(new GlobalInstrumentAttachedEvent(id));
        verifyNoInteractions(nse);

        when(instruments.globalInstrument(id)).thenReturn(Optional.of(global(AssetType.EQUITY, "US", "NSE", List.of())));
        lifecycle.reconcileAfterAttachment(new GlobalInstrumentAttachedEvent(id));
        when(instruments.globalInstrument(id)).thenReturn(Optional.of(global(AssetType.EQUITY, "IN", "BSE", List.of())));
        lifecycle.reconcileAfterAttachment(new GlobalInstrumentAttachedEvent(id));
        when(instruments.globalInstrument(id)).thenReturn(Optional.of(global(AssetType.FUND, "IN", "NSE", List.of())));
        lifecycle.reconcileAfterAttachment(new GlobalInstrumentAttachedEvent(id));
        verifyNoInteractions(nse);
    }

    @Test
    void sourceFailureIsContainedForLaterLifecycleRetry() {
        when(instruments.globalInstrument(id)).thenReturn(Optional.of(global(AssetType.EQUITY, "IN", "NSE", List.of())));
        doThrow(new IllegalStateException("NSE_UNAVAILABLE")).when(nse).reconcile(id);
        lifecycle.reconcileAfterAttachment(new GlobalInstrumentAttachedEvent(id));
        verify(nse).reconcile(id);
    }

    private InstrumentMasterService.GlobalInstrument global(AssetType type, String country, String exchange,
            List<InstrumentProviderMappingEntity> mappings) {
        return global(type, country, exchange, "INE000A01010", mappings);
    }
    private InstrumentMasterService.GlobalInstrument global(AssetType type, String country, String exchange, String isin,
            List<InstrumentProviderMappingEntity> mappings) {
        return new InstrumentMasterService.GlobalInstrument(new InstrumentMasterEntity(id, isin, "Example Limited", type,
                "INR", country, exchange, "BROKER", "ACTIVE", Instant.now()), mappings);
    }
    private InstrumentProviderMappingEntity verifiedNse() { return mapping("VERIFIED"); }
    private InstrumentProviderMappingEntity invalidNse() { return mapping("INVALID"); }
    private InstrumentProviderMappingEntity mapping(String status) {
        return new InstrumentProviderMappingEntity(UUID.randomUUID(), id, "NSE", "OFFICIAL", null, "NSE", "INR", status,
                "NSE_OFFICIAL_ISIN_BOOTSTRAP", new BigDecimal("0.99"), Instant.now());
    }
}
