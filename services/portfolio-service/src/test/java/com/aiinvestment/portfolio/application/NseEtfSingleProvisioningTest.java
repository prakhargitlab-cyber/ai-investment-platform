package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.infrastructure.persistence.*;
import com.aiinvestment.shared.domain.AssetType;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.NullSource;
import org.junit.jupiter.params.provider.ValueSource;
import java.time.Instant;
import java.util.*;
import java.util.concurrent.*;
import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

class NseEtfSingleProvisioningTest {
    static final String ISIN = "INF204KB14I2";
    final InstrumentMasterRepository masters = mock(InstrumentMasterRepository.class);
    final NseOfficialEtfSecurityList etfs = mock(NseOfficialEtfSecurityList.class);
    final NseOfficialSecurityMaster equities = mock(NseOfficialSecurityMaster.class);
    final InstrumentMasterService instruments = mock(InstrumentMasterService.class);
    final NseEtfUniverseBootstrapService bootstrap = spy(new NseEtfUniverseBootstrapService(masters, etfs, equities, instruments));
    final NseEtfUniverseApplyService service = new NseEtfUniverseApplyService(bootstrap);

    void sources() {
        when(etfs.listAll()).thenReturn(List.of(new NseOfficialEtfSecurityList.Listing("NIFTYBEES", ISIN, "Official fund", null),
                new NseOfficialEtfSecurityList.Listing("JUNIORBEES", "INF732E01045", "Other fund", null)));
        when(equities.listedEquities()).thenReturn(List.of(new NseOfficialSecurityMaster.Listing("OTHER", "INE009A01021", "Other", "EQ")));
    }

    @Test void singleScopeAndRetryUseFreshSourcesNeverBulk() {
        sources();
        var entity = new InstrumentMasterEntity(UUID.randomUUID(), ISIN, "Official fund", AssetType.ETF, "INR", "IN", "NSE", "NIFTYBEES", "ACTIVE", Instant.now());
        when(instruments.canonicalizeSelectedOfficialEtf(ISIN, "NIFTYBEES", "Official fund")).thenReturn(entity);
        for (int i=0; i<2; i++) {
            var result = service.applySingle(ISIN).orElseThrow();
            assertThat(result.status()).isEqualTo(NseEtfUniverseApplyService.Status.COMPLETED);
            assertThat(result.globalInstrumentId()).isEqualTo(entity.getInstrumentId());
            assertThat(result.outcome()).isEqualTo("VERIFIED");
        }
        verify(etfs, times(2)).listAll(); verify(equities, times(2)).listedEquities();
        verify(instruments, times(2)).canonicalizeSelectedOfficialEtf(ISIN, "NIFTYBEES", "Official fund");
        verifyNoMoreInteractions(instruments);
        verify(bootstrap, never()).bootstrapMissingEtfs();
        verifyNoInteractions(masters);
    }

    @ParameterizedTest @NullSource
    @ValueSource(strings={"", "inf204kb14i2", " INF204KB14I2", "INF204KB14I3", "INF204KB14I2,INF732E01045", "*", "IN000"})
    void invalidSelectorsFailBeforeAnySourceOrWrite(String value) {
        assertThatThrownBy(() -> service.applySingle(value)).isInstanceOf(IllegalArgumentException.class);
        verifyNoInteractions(etfs, equities, instruments, masters);
        verify(bootstrap, never()).bootstrapMissingEtfs();
    }

    @ParameterizedTest @ValueSource(strings={"etf-down", "iv-down", "iv-empty", "absent", "duplicate", "malformed-duplicate", "malformed", "iv-conflict", "symbol-conflict", "canonical-conflict", "storage-failure"})
    void sourceAndIdentityFailuresFailClosed(String scenario) {
        sources();
        switch (scenario) {
            case "etf-down" -> when(etfs.listAll()).thenThrow(new IllegalStateException("private provider response"));
            case "iv-down" -> when(equities.listedEquities()).thenThrow(new IllegalStateException("private provider response"));
            case "iv-empty" -> when(equities.listedEquities()).thenReturn(List.of());
            case "absent" -> when(etfs.listAll()).thenReturn(List.of(new NseOfficialEtfSecurityList.Listing("OTHER", "INF732E01045", "Other", null)));
            case "duplicate", "malformed-duplicate" -> when(etfs.listAll()).thenReturn(List.of(
                    new NseOfficialEtfSecurityList.Listing("NIFTYBEES", ISIN, "Fund", null),
                    new NseOfficialEtfSecurityList.Listing("CONFLICT", ISIN, scenario.equals("duplicate") ? "Other" : "", null)));
            case "malformed" -> when(etfs.listAll()).thenReturn(List.of(new NseOfficialEtfSecurityList.Listing("", ISIN, "Fund", null)));
            case "iv-conflict" -> when(equities.listedEquities()).thenReturn(List.of(new NseOfficialSecurityMaster.Listing("NIFTYBEES", ISIN, "Fund", "IV")));
            case "symbol-conflict" -> when(etfs.listAll()).thenReturn(List.of(new NseOfficialEtfSecurityList.Listing("NIFTYBEES", ISIN, "Fund", null),
                    new NseOfficialEtfSecurityList.Listing("NIFTYBEES", "INF732E01045", "Other", null)));
            case "canonical-conflict" -> when(instruments.canonicalizeSelectedOfficialEtf(anyString(), anyString(), anyString())).thenThrow(new IllegalStateException("private identity"));
            case "storage-failure" -> when(instruments.canonicalizeSelectedOfficialEtf(anyString(), anyString(), anyString())).thenThrow(new org.springframework.dao.DataIntegrityViolationException("private SQL"));
        }
        var result = service.applySingle(ISIN).orElseThrow();
        assertThat(result.status()).isEqualTo(NseEtfUniverseApplyService.Status.ABORTED);
        assertThat(result.globalInstrumentId()).isNull();
        String expected = switch (scenario) {
            case "etf-down", "iv-down", "iv-empty" -> "OFFICIAL_SOURCE_UNAVAILABLE";
            case "absent" -> "ISIN_NOT_IN_OFFICIAL_ETF_UNIVERSE";
            case "duplicate", "malformed-duplicate", "malformed" -> "OFFICIAL_ETF_IDENTITY_INVALID_OR_DUPLICATE";
            case "iv-conflict" -> "ISIN_IN_BOTH_ETF_LIST_AND_IV_SERIES";
            case "symbol-conflict" -> "OFFICIAL_ETF_SYMBOL_CONFLICT";
            case "canonical-conflict" -> "CANONICAL_IDENTITY_OR_MAPPING_CONFLICT";
            default -> "SINGLE_ETF_PROVISIONING_FAILED";
        };
        assertThat(result.outcome()).isEqualTo(expected);
        if (!scenario.equals("canonical-conflict") && !scenario.equals("storage-failure")) verifyNoInteractions(instruments);
        verify(bootstrap, never()).bootstrapMissingEtfs();
    }

    @Test void concurrentSingleAndBulkAreRejectedAndAdmissionGuardIsReleased() throws Exception {
        sources();
        var entered = new CountDownLatch(1); var release = new CountDownLatch(1);
        when(etfs.listAll()).thenAnswer(call -> { entered.countDown(); assertThat(release.await(3, TimeUnit.SECONDS)).isTrue(); throw new IllegalStateException(); });
        var executor = Executors.newSingleThreadExecutor();
        try {
            var first = executor.submit(() -> service.applySingle(ISIN));
            assertThat(entered.await(3, TimeUnit.SECONDS)).isTrue();
            assertThat(service.applySingle(ISIN)).isEmpty();
            assertThat(service.apply()).isEmpty();
            release.countDown(); first.get(3, TimeUnit.SECONDS);
            assertThat(service.applySingle(ISIN)).isPresent();
            verify(bootstrap, never()).bootstrapMissingEtfs();
        } finally { release.countDown(); executor.shutdownNow(); }
    }
}
