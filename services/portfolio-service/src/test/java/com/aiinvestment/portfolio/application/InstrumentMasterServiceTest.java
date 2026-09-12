package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentEntity;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterEntity;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterRepository;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentProviderMappingEntity;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentProviderMappingRepository;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentRepository;
import com.aiinvestment.shared.domain.AssetType;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.jdbc.core.ConnectionCallback;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.context.ApplicationEventPublisher;

import java.math.BigDecimal;
import java.util.List;
import java.util.Optional;
import java.util.UUID;
import org.springframework.data.domain.PageImpl;
import org.springframework.data.domain.PageRequest;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.*;

class InstrumentMasterServiceTest {
    private InstrumentMasterRepository masters;
    private InstrumentProviderMappingRepository mappings;
    private InstrumentRepository legacy;
    private ApplicationEventPublisher events;
    private InstrumentMasterService service;

    @BeforeEach
    void setUp() {
        masters = mock(InstrumentMasterRepository.class);
        mappings = mock(InstrumentProviderMappingRepository.class);
        legacy = mock(InstrumentRepository.class);
        JdbcTemplate jdbc = mock(JdbcTemplate.class);
        events = mock(ApplicationEventPublisher.class);
        doReturn(false).when(jdbc).execute(any(ConnectionCallback.class));
        service = new InstrumentMasterService(masters, mappings, legacy, jdbc, events);

        when(masters.findByNormalizedIsin(anyString())).thenReturn(Optional.empty());
        when(mappings.findByProviderAndProviderInstrumentId(anyString(), anyString())).thenReturn(Optional.empty());
        when(mappings.findByProviderAndExchangeIgnoreCaseAndProviderSymbolIgnoreCase(anyString(), anyString(), anyString()))
                .thenReturn(Optional.empty());
        when(mappings.findFirstByInstrumentIdAndProviderAndStatusIn(any(), anyString(), any())).thenReturn(Optional.empty());
        when(masters.saveAndFlush(any(InstrumentMasterEntity.class))).thenAnswer(invocation -> invocation.getArgument(0));
        when(mappings.save(any(InstrumentProviderMappingEntity.class))).thenAnswer(invocation -> invocation.getArgument(0));
    }

    @Test
    void searchMappingsBatchesCanonicalIdsAndExcludesUnverifiedAliases() {
        UUID id = UUID.randomUUID();
        var verified = mock(InstrumentProviderMappingEntity.class);
        when(verified.getInstrumentId()).thenReturn(id);
        when(verified.getStatus()).thenReturn("VERIFIED");
        var unresolved = mock(InstrumentProviderMappingEntity.class);
        when(unresolved.getStatus()).thenReturn("UNRESOLVED");
        when(mappings.findByInstrumentIdIn(List.of(id))).thenReturn(List.of(verified, unresolved));
        assertThat(service.searchMappings(List.of(id))).containsEntry(id, List.of(verified));
        verify(mappings).findByInstrumentIdIn(List.of(id));
        verify(mappings, never()).findByInstrumentId(any());
        assertThat(service.searchMappings(List.of())).isEmpty();
    }

    @Test
    void brokerNseSymbolIsPersistedOnlyForTheBrokerProvider() {
        service.resolveAndAttach(row("INE187D01029", "Talbros Automotive Components"), "ICICI_DIRECT", "icici-1",
                "TALAUT", "NSE", "BROKER_IMPORT", new BigDecimal("0.95"));

        assertProvidersSavedExactly("ICICI_DIRECT");
        assertThat(savedMappings().get(0).getProviderSymbol()).isEqualTo("TALAUT");
        assertThat(savedMappings().get(0).getExchange()).isEqualTo("NSE");
    }

    @Test
    void attachingAMasterPublishesTheGlobalInstrumentLifecycleEvent() {
        InstrumentMasterEntity master = service.resolveAndAttach(row("INE187D01029", "Talbros Automotive Components"),
                "ICICI_DIRECT", "icici-1", "TALAUT", "NSE", "BROKER_IMPORT", new BigDecimal("0.95"));

        ArgumentCaptor<GlobalInstrumentAttachedEvent> event = ArgumentCaptor.forClass(GlobalInstrumentAttachedEvent.class);
        verify(events).publishEvent(event.capture());
        assertThat(event.getValue().globalInstrumentId()).isEqualTo(master.getInstrumentId());
    }

    @Test
    void brokerBseSymbolIsPersistedOnlyForTheBrokerProvider() {
        service.resolveAndAttach(row("INE187D01029", "Talbros Automotive Components"), "HDFC_SECURITIES", "hdfc-1",
                "TALAUT", "BSE", "BROKER_IMPORT", new BigDecimal("0.95"));

        assertProvidersSavedExactly("HDFC_SECURITIES");
    }

    @Test
    void nseProviderImportStillPersistsItsValidatedNseSymbol() {
        service.resolveAndAttach(row("INE187D01029", "Talbros Automotive Components"), "NSE", "nse-1",
                "TALBROAUTO", "NSE", "EXCHANGE_IMPORT", new BigDecimal("0.95"));

        assertProvidersSavedExactly("NSE");
        assertThat(savedMappings().get(0).getProviderSymbol()).isEqualTo("TALBROAUTO");
    }

    @Test
    void explicitlyValidatedNseMappingCanStillBeSaved() {
        UUID masterId = UUID.randomUUID();
        when(masters.existsById(masterId)).thenReturn(true);

        InstrumentProviderMappingEntity mapping = service.saveResolvedMapping(masterId, "NSE", "TALBROAUTO", null,
                "NSE", "INR", "VERIFIED", "EXCHANGE_RESOLUTION", new BigDecimal("0.99"));

        assertThat(mapping.getProvider()).isEqualTo("NSE");
        assertThat(mapping.getProviderSymbol()).isEqualTo("TALBROAUTO");
        assertThat(mapping.getStatus()).isEqualTo("VERIFIED");
    }

    @Test
    void verifiedInternationalMappingsPersistOnlyAgainstExistingMasters() {
        UUID masterId = UUID.randomUUID();
        when(masters.existsById(masterId)).thenReturn(true);

        var sec = service.saveVerifiedExternalMapping(masterId, "SEC_CIK", "MSFT", "0000789019",
                "XNAS", "USD", "SEC_EDGAR_COMPANYFACTS", new BigDecimal("0.90"));
        var eodhd = service.saveVerifiedExternalMapping(masterId, "EODHD", "AIXA", "AIXA.F",
                "XFRA", "EUR", "EODHD_FUNDAMENTALS", new BigDecimal("0.95"));

        assertThat(sec.getStatus()).isEqualTo("VERIFIED");
        assertThat(eodhd.getStatus()).isEqualTo("VERIFIED");
        verify(masters, never()).saveAndFlush(any());
    }

    @Test
    void unverifiedExternalMappingIsRejectedWithoutPersistence() {
        UUID masterId = UUID.randomUUID(); when(masters.existsById(masterId)).thenReturn(true);
        org.assertj.core.api.Assertions.assertThatThrownBy(() -> service.saveVerifiedExternalMapping(masterId,
                "SEC_CIK", "MSFT", "0000789019", "XNAS", "USD", "SEC", new BigDecimal("0.89")))
                .isInstanceOf(IllegalArgumentException.class);
        verify(mappings, never()).save(any());
    }

    @Test
    void activeEquityUniverseIsReadOnlyAndDelegatesToFilteredRepositoryQuery() {
        var first = new InstrumentMasterEntity(UUID.randomUUID(), null, "Alpha", AssetType.EQUITY, "USD", "US", "XNAS", "AAA", "ACTIVE", java.time.Instant.now());
        var page = PageRequest.of(0, 100);
        when(mappings.findByProviderAndProviderInstrumentId(anyString(), anyString())).thenReturn(Optional.empty());
        when(masters.findByStatusIgnoreCaseAndAssetType("ACTIVE", AssetType.EQUITY, page)).thenReturn(new PageImpl<>(List.of(first), page, 1));
        var result = service.enumerate("ACTIVE", AssetType.EQUITY, page);
        assertThat(result.getContent()).containsExactly(first);
        assertThat(result.getTotalElements()).isEqualTo(1);
        verify(masters, never()).saveAndFlush(any()); verify(mappings, never()).save(any());
    }

    @Test
    void authoritativeSuccessPromotesAnExistingInvalidMappingWithoutCreatingAnotherRow() {
        UUID masterId = UUID.randomUUID();
        when(masters.existsById(masterId)).thenReturn(true);
        InstrumentProviderMappingEntity invalid = new InstrumentProviderMappingEntity(UUID.randomUUID(), masterId,
                "NSE", "TALBROAUTO", null, "NSE", "INR", "INVALID", "NSE_VALIDATION_FAILED",
                BigDecimal.ZERO, java.time.Instant.now());
        invalid.markInvalid("NSE_UNAVAILABLE", java.time.Instant.now());
        when(mappings.findByProviderAndExchangeIgnoreCaseAndProviderSymbolIgnoreCase("NSE", "NSE", "TALBROAUTO"))
                .thenReturn(Optional.of(invalid));

        InstrumentProviderMappingEntity promoted = service.saveResolvedMapping(masterId, "NSE", "TALBROAUTO", null,
                "NSE", "INR", "VERIFIED", "NSE_VALIDATED_RESOLUTION", new BigDecimal("0.99"));

        assertThat(promoted).isSameAs(invalid);
        assertThat(promoted.getStatus()).isEqualTo("VERIFIED");
        assertThat(promoted.getFailureReason()).isNull();
        assertThat(promoted.getVerifiedAt()).isNotNull();
        assertThat(promoted.getLastValidationAt()).isNotNull();
        verify(mappings, never()).save(any(InstrumentProviderMappingEntity.class));
    }

    @Test
    void authoritativeDifferentSymbolCreatesVerifiedMappingWithoutMutatingRetiredInvalidAlias() {
        UUID masterId = UUID.randomUUID();
        when(masters.existsById(masterId)).thenReturn(true);
        InstrumentProviderMappingEntity retired = new InstrumentProviderMappingEntity(UUID.randomUUID(), masterId,
                "NSE", "BROKER_ALIAS", null, "NSE", "INR", "INVALID", "NSE_VALIDATION_FAILED",
                BigDecimal.ZERO, java.time.Instant.now());
        retired.markInvalid("NSE_SYMBOL_NOT_RECOGNIZED", java.time.Instant.now());

        InstrumentProviderMappingEntity resolved = service.saveResolvedMapping(masterId, "NSE", "OFFICIAL", null,
                "NSE", "INR", "VERIFIED", "NSE_VALIDATED_RESOLUTION", new BigDecimal("0.99"));

        assertThat(resolved.getStatus()).isEqualTo("VERIFIED");
        assertThat(resolved.getProviderSymbol()).isEqualTo("OFFICIAL");
        assertThat(retired.getStatus()).isEqualTo("INVALID");
        assertThat(retired.getFailureReason()).isEqualTo("NSE_SYMBOL_NOT_RECOGNIZED");
    }

    @Test
    void repeatedFailuresUpdateOneExistingInvalidMappingAndNeverFabricateVerification() {
        UUID masterId = UUID.randomUUID();
        when(masters.existsById(masterId)).thenReturn(true);
        InstrumentProviderMappingEntity invalid = new InstrumentProviderMappingEntity(UUID.randomUUID(), masterId,
                "NSE", "TALBROAUTO", null, "NSE", "INR", "INVALID", "NSE_VALIDATION_FAILED",
                BigDecimal.ZERO, java.time.Instant.now());
        when(mappings.findByProviderAndExchangeIgnoreCaseAndProviderSymbolIgnoreCase("NSE", "NSE", "TALBROAUTO"))
                .thenReturn(Optional.of(invalid));

        service.recordMappingFailure(masterId, "NSE", "TALBROAUTO", null, "NSE", "INR",
                "NSE_VALIDATION_FAILED", BigDecimal.ZERO, "NSE_UNAVAILABLE");
        service.recordMappingFailure(masterId, "NSE", "TALBROAUTO", null, "NSE", "INR",
                "NSE_VALIDATION_FAILED", BigDecimal.ZERO, "NSE_EMPTY_RESPONSE");

        assertThat(invalid.getStatus()).isEqualTo("INVALID");
        assertThat(invalid.getFailureReason()).isEqualTo("NSE_EMPTY_RESPONSE");
        assertThat(invalid.getVerifiedAt()).isNull();
        verify(mappings, never()).save(any(InstrumentProviderMappingEntity.class));
    }

    @Test
    void repeatedSuccessfulVerificationReusesTheStableMappingIdentity() {
        UUID masterId = UUID.randomUUID();
        when(masters.existsById(masterId)).thenReturn(true);
        InstrumentProviderMappingEntity verified = new InstrumentProviderMappingEntity(UUID.randomUUID(), masterId,
                "NSE", "TALBROAUTO", null, "NSE", "INR", "VERIFIED", "NSE_VALIDATED_RESOLUTION",
                new BigDecimal("0.99"), java.time.Instant.now());
        when(mappings.findFirstByInstrumentIdAndProviderAndStatusIn(masterId, "NSE", List.of("VERIFIED")))
                .thenReturn(Optional.of(verified));

        InstrumentProviderMappingEntity repeated = service.saveResolvedMapping(masterId, "NSE", "TALBROAUTO", null,
                "NSE", "INR", "VERIFIED", "NSE_VALIDATED_RESOLUTION", new BigDecimal("0.99"));

        assertThat(repeated).isSameAs(verified);
        verify(mappings, never()).save(any(InstrumentProviderMappingEntity.class));
    }

    @Test
    void existingMasterIsResolvedByIsinWhileTheNewProviderMappingStaysScoped() {
        UUID masterId = UUID.randomUUID();
        InstrumentMasterEntity existing = new InstrumentMasterEntity(masterId, "INE187D01029", "Talbros Automotive Components",
                AssetType.EQUITY, "INR", "IN", "NSE", "TALBROAUTO", "ACTIVE", java.time.Instant.now());
        when(masters.findByNormalizedIsin("INE187D01029")).thenReturn(Optional.of(existing));

        InstrumentMasterEntity resolved = service.resolveAndAttach(row("INE187D01029", "Talbros Automotive Components"),
                "ICICI_DIRECT", "icici-1", "TALAUT", "NSE", "BROKER_IMPORT", new BigDecimal("0.95"));

        assertThat(resolved.getInstrumentId()).isEqualTo(masterId);
        verify(masters, never()).saveAndFlush(any());
        assertProvidersSavedExactly("ICICI_DIRECT");
    }

    @Test
    void exactOfficialNseIdentityPromotesAnExistingUnknownMaster() {
        UUID masterId = UUID.randomUUID();
        InstrumentMasterEntity existing = new InstrumentMasterEntity(masterId, "ine551w01018",
                "UJJIVAN SMALL FINANCE BANK LTD", AssetType.EQUITY, "INR", "IN", "UNKNOWN", "UJJSM", "ACTIVE",
                java.time.Instant.now());
        when(masters.findByNormalizedIsin("INE551W01018")).thenReturn(Optional.of(existing));

        InstrumentMasterEntity resolved = service.canonicalizeOfficialNse(
                " INE551W01018 ", "UJJIVANSFB", "Ujjivan Small Finance Bank Limited");

        assertThat(resolved).isSameAs(existing);
        assertThat(resolved.getPrimaryExchange()).isEqualTo("NSE");
        assertThat(resolved.getPrimarySymbol()).isEqualTo("UJJIVANSFB");
        assertThat(resolved.getCanonicalName()).isEqualTo("Ujjivan Small Finance Bank Limited");
        assertProvidersSavedExactly("NSE");
        verify(masters, never()).saveAndFlush(any());
    }

    @Test
    void existingMasterIsResolvedByItsProviderMapping() {
        UUID masterId = UUID.randomUUID();
        InstrumentMasterEntity existing = new InstrumentMasterEntity(masterId, null, "Existing equity", AssetType.EQUITY,
                "INR", "IN", "NSE", "TALBROAUTO", "ACTIVE", java.time.Instant.now());
        InstrumentProviderMappingEntity existingMapping = new InstrumentProviderMappingEntity(UUID.randomUUID(), masterId,
                "ICICI_DIRECT", "TALAUT", "icici-1", "NSE", "INR", "VERIFIED", "BROKER_IMPORT",
                new BigDecimal("0.95"), java.time.Instant.now());
        when(mappings.findByProviderAndProviderInstrumentId("ICICI_DIRECT", "icici-1")).thenReturn(Optional.of(existingMapping));
        when(masters.findById(masterId)).thenReturn(Optional.of(existing));

        InstrumentMasterEntity resolved = service.resolveAndAttach(row(null, "Talbros Automotive Components"),
                "ICICI_DIRECT", "icici-1", "TALAUT", "NSE", "BROKER_IMPORT", new BigDecimal("0.95"));

        assertThat(resolved.getInstrumentId()).isEqualTo(masterId);
        verify(masters, never()).saveAndFlush(any());
        verify(mappings, never()).save(any(InstrumentProviderMappingEntity.class));
    }

    private InstrumentEntity row(String isin, String company) {
        return new InstrumentEntity(UUID.randomUUID(), "ICICI_DIRECT", "source-row", isin, "TALAUT", "NSE", "XNSE",
                company, AssetType.EQUITY, "IN", "INR", null, null);
    }

    private List<InstrumentProviderMappingEntity> savedMappings() {
        ArgumentCaptor<InstrumentProviderMappingEntity> captor = ArgumentCaptor.forClass(InstrumentProviderMappingEntity.class);
        verify(mappings, atLeastOnce()).save(captor.capture());
        return captor.getAllValues();
    }

    private void assertProvidersSavedExactly(String provider) {
        assertThat(savedMappings()).extracting(InstrumentProviderMappingEntity::getProvider).containsExactly(provider);
    }
}
