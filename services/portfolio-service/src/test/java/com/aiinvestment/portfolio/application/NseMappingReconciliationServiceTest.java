package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterEntity;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterRepository;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentProviderMappingEntity;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentProviderMappingRepository;
import com.aiinvestment.shared.domain.AssetType;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.List;
import java.util.Optional;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

class NseMappingReconciliationServiceTest {
    private InstrumentMasterRepository masters;
    private InstrumentProviderMappingRepository mappings;
    private InstrumentMasterService instrumentMaster;
    private NseOfficialMappingVerifier verifier;
    private NseOfficialSecurityMaster securityMaster;
    private NseOfficialEtfSecurityList etfSecurityList;
    private NseMappingReconciliationService service;
    private UUID instrumentId;

    @BeforeEach
    void setUp() {
        masters = mock(InstrumentMasterRepository.class);
        mappings = mock(InstrumentProviderMappingRepository.class);
        instrumentMaster = mock(InstrumentMasterService.class);
        verifier = mock(NseOfficialMappingVerifier.class);
        securityMaster = mock(NseOfficialSecurityMaster.class);
        etfSecurityList = mock(NseOfficialEtfSecurityList.class);
        when(securityMaster.lookupByIsin(anyString())).thenReturn(NseOfficialSecurityMaster.Lookup.noIsinMatch());
        when(etfSecurityList.lookupByIsin(anyString())).thenReturn(NseOfficialEtfSecurityList.Lookup.noIsinMatch());
        service = new NseMappingReconciliationService(masters, mappings, instrumentMaster, verifier, securityMaster, etfSecurityList);
        instrumentId = UUID.randomUUID();
        when(masters.findById(instrumentId)).thenReturn(Optional.of(master(AssetType.EQUITY, "NSE")));
    }

    @Test
    void brokerAliasIsNotPromotedToAnNseCandidate() {
        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of(mapping("ICICI_DIRECT", "TALAUT", "VERIFIED", "BROKER_IMPORT")));

        var outcome = service.reconcile(instrumentId);

        assertThat(outcome.reason()).isEqualTo("OFFICIAL_MASTER_NO_ISIN_MATCH");
        verifyNoInteractions(verifier);
        verify(instrumentMaster).recordMappingFailure(eq(instrumentId), eq("NSE"), eq("TALAUT"),
                eq("ISIN:INE187D01029"), eq("NSE"), eq("INR"), eq("NSE_OFFICIAL_ISIN_LOOKUP"),
                eq(BigDecimal.ZERO), eq("OFFICIAL_MASTER_NO_ISIN_MATCH"));
    }

    @Test
    void exactOfficialIsinBootstrapsVerifiedNseWithoutTrustingBrokerAlias() {
        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of(mapping("ICICI_DIRECT", "BROKER_ALIAS", "VERIFIED", "BROKER_IMPORT")));
        when(securityMaster.lookupByIsin("INE187D01029")).thenReturn(NseOfficialSecurityMaster.Lookup.matched(
                "OFFICIAL_SYMBOL", "INE187D01029", "Talbros Automotive Components Limited", "EQ"));

        var outcome = service.reconcile(instrumentId);

        assertThat(outcome.status()).isEqualTo("PERSISTED");
        assertThat(outcome.reason()).isEqualTo("OFFICIAL_MASTER_MATCHED");
        verifyNoInteractions(verifier);
        verify(instrumentMaster).saveResolvedMapping(eq(instrumentId), eq("NSE"), eq("OFFICIAL_SYMBOL"), isNull(), eq("NSE"),
                eq("INR"), eq("VERIFIED"), eq("NSE_OFFICIAL_ISIN_BOOTSTRAP"), eq(new BigDecimal("0.99")));
    }

    @Test
    void importedUnknownExchangeIsPromotedOnlyAfterExactOfficialIsinMatch() {
        InstrumentMasterEntity imported = master(AssetType.EQUITY, "UNKNOWN", "INE551W01018",
                "UJJIVAN SMALL FINANCE BANK LTD", "UJJSM");
        when(masters.findById(instrumentId)).thenReturn(Optional.of(imported));
        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of(
                mapping("ICICI_DIRECT", "UJJSM", "VERIFIED", "MANUAL_CSV_IMPORT")));
        when(securityMaster.lookupByIsin("INE551W01018")).thenReturn(NseOfficialSecurityMaster.Lookup.matched(
                "UJJIVANSFB", "INE551W01018", "Ujjivan Small Finance Bank Limited", "EQ"));

        var outcome = service.reconcile(instrumentId);

        assertThat(outcome.status()).isEqualTo("PERSISTED");
        assertThat(imported.getPrimaryExchange()).isEqualTo("NSE");
        assertThat(imported.getPrimarySymbol()).isEqualTo("UJJIVANSFB");
        assertThat(imported.getCanonicalName()).isEqualTo("Ujjivan Small Finance Bank Limited");
        verify(instrumentMaster).saveResolvedMapping(eq(instrumentId), eq("NSE"), eq("UJJIVANSFB"), isNull(), eq("NSE"),
                eq("INR"), eq("VERIFIED"), eq("NSE_OFFICIAL_ISIN_BOOTSTRAP"), eq(new BigDecimal("0.99")));
        verifyNoInteractions(verifier);
    }

    @Test
    void importedUnknownExchangeStaysUnresolvedWhenOfficialIsinIsAmbiguous() {
        InstrumentMasterEntity imported = master(AssetType.EQUITY, "UNKNOWN", "INE551W01018",
                "UJJIVAN SMALL FINANCE BANK LTD", "UJJSM");
        when(masters.findById(instrumentId)).thenReturn(Optional.of(imported));
        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of());
        when(securityMaster.lookupByIsin("INE551W01018")).thenReturn(NseOfficialSecurityMaster.Lookup.ambiguous());

        assertThat(service.reconcile(instrumentId).reason()).isEqualTo("OFFICIAL_MASTER_AMBIGUOUS");
        assertThat(imported.getPrimaryExchange()).isEqualTo("UNKNOWN");
        assertThat(imported.getPrimarySymbol()).isEqualTo("UJJSM");
        verifyNoInteractions(verifier);
        verify(instrumentMaster).recordMappingFailure(eq(instrumentId), eq("NSE"), eq("UJJSM"),
                eq("ISIN:INE551W01018"), eq("NSE"), eq("INR"), eq("NSE_OFFICIAL_ISIN_LOOKUP"),
                eq(BigDecimal.ZERO), eq("OFFICIAL_MASTER_AMBIGUOUS"));
    }

    @Test
    void exactOfficialIsinBootstrapAcceptsUnambiguousCorporateAbbreviations() {
        when(masters.findById(instrumentId)).thenReturn(Optional.of(new InstrumentMasterEntity(
                instrumentId, "INE053F01010", "INDIAN RAILWAY FIN CORP LTD", AssetType.EQUITY,
                "INR", "IN", "NSE", "INDR", "ACTIVE", Instant.now())));
        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of(mapping("ICICI_DIRECT", "INDR", "VERIFIED", "LEGACY_ADOPTION")));
        when(securityMaster.lookupByIsin("INE053F01010")).thenReturn(NseOfficialSecurityMaster.Lookup.matched(
                "IRFC", "INE053F01010", "Indian Railway Finance Corporation Limited", "EQ"));

        var outcome = service.reconcile(instrumentId);

        assertThat(outcome.status()).isEqualTo("PERSISTED");
        assertThat(outcome.reason()).isEqualTo("OFFICIAL_MASTER_MATCHED");
        verify(instrumentMaster).saveResolvedMapping(eq(instrumentId), eq("NSE"), eq("IRFC"), isNull(), eq("NSE"),
                eq("INR"), eq("VERIFIED"), eq("NSE_OFFICIAL_ISIN_BOOTSTRAP"), eq(new BigDecimal("0.99")));
        verifyNoInteractions(verifier);
    }

    @Test
    void officialMasterNoMatchAmbiguityAndUnavailabilityRecordNonReusableFailure() {
        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of());
        when(securityMaster.lookupByIsin("INE187D01029")).thenReturn(NseOfficialSecurityMaster.Lookup.noIsinMatch());
        assertThat(service.reconcile(instrumentId).reason()).isEqualTo("OFFICIAL_MASTER_NO_ISIN_MATCH");
        when(securityMaster.lookupByIsin("INE187D01029")).thenReturn(NseOfficialSecurityMaster.Lookup.ambiguous());
        assertThat(service.reconcile(instrumentId).reason()).isEqualTo("OFFICIAL_MASTER_AMBIGUOUS");
        when(securityMaster.lookupByIsin("INE187D01029")).thenReturn(NseOfficialSecurityMaster.Lookup.unavailable());
        assertThat(service.reconcile(instrumentId).reason()).isEqualTo("OFFICIAL_MASTER_UNAVAILABLE");
        verifyNoInteractions(verifier);
        verify(instrumentMaster, times(3)).recordMappingFailure(eq(instrumentId), eq("NSE"), eq("TALAUT"),
                eq("ISIN:INE187D01029"), eq("NSE"), eq("INR"), eq("NSE_OFFICIAL_ISIN_LOOKUP"),
                eq(BigDecimal.ZERO), anyString());
    }

    @Test
    void incompatibleOfficialSeriesIsRejectedButExactOfficialIsinWinsOverLegalNameVariation() {
        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of());
        when(securityMaster.lookupByIsin("INE187D01029")).thenReturn(NseOfficialSecurityMaster.Lookup.matched(
                "OFFICIAL", "INE187D01029", "Talbros Automotive Components Limited", "ETF"));
        assertThat(service.reconcile(instrumentId).reason()).isEqualTo("OFFICIAL_MASTER_NOT_EQUITY_COMPATIBLE");
        when(securityMaster.lookupByIsin("INE187D01029")).thenReturn(NseOfficialSecurityMaster.Lookup.matched(
                "OFFICIAL", "INE187D01029", "Other Company Limited", "EQ"));
        assertThat(service.reconcile(instrumentId).status()).isEqualTo("PERSISTED");
        verify(instrumentMaster).saveResolvedMapping(eq(instrumentId), eq("NSE"), eq("OFFICIAL"), isNull(), eq("NSE"),
                eq("INR"), eq("VERIFIED"), eq("NSE_OFFICIAL_ISIN_BOOTSTRAP"), eq(new BigDecimal("0.99")));
        verifyNoInteractions(verifier);
    }

    @Test
    void verifiedYahooNseTickerGeneratesCandidateByRemovingOnlyNsSuffix() {
        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of(yahoo("TALBROAUTO.NS")));
        when(verifier.verify("TALBROAUTO")).thenReturn(new NseOfficialMappingVerifier.Verification(false, null, null, null, "NOT_FOUND"));

        var outcome = service.reconcile(instrumentId);

        assertThat(outcome.candidate()).isEqualTo("TALBROAUTO");
        verify(verifier).verify("TALBROAUTO");
        verify(instrumentMaster).recordMappingFailure(eq(instrumentId), eq("NSE"), eq("TALBROAUTO"), isNull(), eq("NSE"),
                eq("INR"), eq("NSE_VALIDATION_FAILED"), eq(BigDecimal.ZERO), eq("NOT_FOUND"));
    }

    @Test
    void candidateIsNotPersistedWithoutOfficialValidation() {
        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of(yahoo("TALBROAUTO.NS")));
        when(verifier.verify(anyString())).thenReturn(NseOfficialMappingVerifier.Verification.rejected("NSE_HTTP_403"));

        assertThat(service.reconcile(instrumentId).status()).isEqualTo("REJECTED");
        verify(instrumentMaster).recordMappingFailure(eq(instrumentId), eq("NSE"), eq("TALBROAUTO"), isNull(), eq("NSE"),
                eq("INR"), eq("NSE_VALIDATION_FAILED"), eq(BigDecimal.ZERO), eq("NSE_HTTP_403"));
    }

    @Test
    void officialNseIsinMatchPersistsAuthoritativeMapping() {
        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of(yahoo("TALBROAUTO.NS")));
        when(verifier.verify("TALBROAUTO")).thenReturn(new NseOfficialMappingVerifier.Verification(true, "TALBROAUTO", "INE187D01029", null, null));

        var outcome = service.reconcile(instrumentId);

        assertThat(outcome.status()).isEqualTo("PERSISTED");
        verify(instrumentMaster).saveResolvedMapping(eq(instrumentId), eq("NSE"), eq("TALBROAUTO"), isNull(), eq("NSE"),
                eq("INR"), eq("VERIFIED"), eq("NSE_VALIDATED_RESOLUTION"), eq(new BigDecimal("0.99")));
    }

    @Test
    void equityOfficialMasterExactIsinWinsBeforeYahooCandidateAndCorporateVerifier() {
        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of(yahoo("TALBROAUTO.NS")));
        when(securityMaster.lookupByIsin("INE187D01029")).thenReturn(NseOfficialSecurityMaster.Lookup.matched(
                "OFFICIAL_EQUITY", "INE187D01029", "Talbros Automotive Components Limited", "EQ"));

        assertThat(service.reconcile(instrumentId).status()).isEqualTo("PERSISTED");
        verify(instrumentMaster).saveResolvedMapping(eq(instrumentId), eq("NSE"), eq("OFFICIAL_EQUITY"), isNull(), eq("NSE"),
                eq("INR"), eq("VERIFIED"), eq("NSE_OFFICIAL_ISIN_BOOTSTRAP"), eq(new BigDecimal("0.99")));
        verifyNoInteractions(verifier);
    }

    @Test
    void equityOfficialMasterNoMatchOrUnavailableMayUseExistingCorporateFallbackButAmbiguityCannot() {
        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of(yahoo("TALBROAUTO.NS")));
        when(securityMaster.lookupByIsin("INE187D01029")).thenReturn(NseOfficialSecurityMaster.Lookup.noIsinMatch());
        when(verifier.verify("TALBROAUTO")).thenReturn(verification("TALBROAUTO", "INE187D01029", "Talbros Automotive Components Limited"));
        assertThat(service.reconcile(instrumentId).status()).isEqualTo("PERSISTED");

        reset(instrumentMaster, verifier);
        when(securityMaster.lookupByIsin("INE187D01029")).thenReturn(NseOfficialSecurityMaster.Lookup.unavailable());
        when(verifier.verify("TALBROAUTO")).thenReturn(verification("TALBROAUTO", "INE187D01029", "Talbros Automotive Components Limited"));
        assertThat(service.reconcile(instrumentId).status()).isEqualTo("PERSISTED");

        reset(instrumentMaster, verifier);
        when(masters.findById(instrumentId)).thenReturn(Optional.of(master(AssetType.EQUITY, "NSE")));
        when(securityMaster.lookupByIsin("INE187D01029")).thenReturn(NseOfficialSecurityMaster.Lookup.ambiguous());
        assertThat(service.reconcile(instrumentId).reason()).isEqualTo("OFFICIAL_MASTER_AMBIGUOUS");
        verifyNoInteractions(verifier);
        verify(instrumentMaster).recordMappingFailure(eq(instrumentId), eq("NSE"), eq("TALAUT"),
                eq("ISIN:INE187D01029"), eq("NSE"), eq("INR"), eq("NSE_OFFICIAL_ISIN_LOOKUP"),
                eq(BigDecimal.ZERO), eq("OFFICIAL_MASTER_AMBIGUOUS"));
    }

    @Test
    void equityOfficialMasterConflictDoesNotFallThroughToYahooCandidate() {
        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of(yahoo("GEEKAYWIRE.NS")));
        when(masters.findById(instrumentId)).thenReturn(Optional.of(master(AssetType.EQUITY, "NSE", "INE669X01032", "GEEKAY WIRE LIMITED", "GEEWIR")));
        when(securityMaster.lookupByIsin("INE669X01032")).thenReturn(NseOfficialSecurityMaster.Lookup.matched(
                "GEEKAYWIRE", "INE669X01032", "GEEKAY WIRE LIMITED", "ZZ"));

        assertThat(service.reconcile(instrumentId).reason()).isEqualTo("OFFICIAL_MASTER_NOT_EQUITY_COMPATIBLE");
        verifyNoInteractions(verifier);
        verify(instrumentMaster).recordMappingFailure(eq(instrumentId), eq("NSE"), eq("GEEKAYWIRE"), isNull(),
                eq("NSE"), eq("INR"), eq("NSE_VALIDATION_FAILED"), eq(BigDecimal.ZERO),
                eq("OFFICIAL_MASTER_NOT_EQUITY_COMPATIBLE"));
    }

    @Test
    void geekayAndCholamandalamStyleBrokerSymbolsResolveFromExactOfficialMasterIsin() {
        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of(yahoo("GEEKAYWIRE.NS")));
        when(masters.findById(instrumentId)).thenReturn(Optional.of(master(AssetType.EQUITY, "NSE", "INE669X01032", "GEEKAY WIRE LIMITED", "GEEWIR")));
        when(securityMaster.lookupByIsin("INE669X01032")).thenReturn(NseOfficialSecurityMaster.Lookup.matched(
                "GEEKAYWIRE", "INE669X01032", "GEEKAY WIRE LIMITED", "EQ"));
        assertThat(service.reconcile(instrumentId).candidate()).isEqualTo("GEEKAYWIRE");
        verifyNoInteractions(verifier);

        reset(instrumentMaster, verifier);
        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of(yahoo("CHOLAFIN.NS")));
        when(masters.findById(instrumentId)).thenReturn(Optional.of(master(AssetType.EQUITY, "NSE", "INE121A01024",
                "CHOLAMANDALAM INVESTMENT AND FINANCE COMPANY LIMITED", "CHOINV")));
        when(securityMaster.lookupByIsin("INE121A01024")).thenReturn(NseOfficialSecurityMaster.Lookup.matched(
                "CHOLAFIN", "INE121A01024", "CHOLA INVESTMENT AND FINANCE", "EQ"));
        assertThat(service.reconcile(instrumentId).candidate()).isEqualTo("CHOLAFIN");
        verifyNoInteractions(verifier);
    }

    @Test
    void incompatibleOfficialIsinIsRejectedEvenWithMatchingCompanyAndTrustedCurrentIsin() {
        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of(yahoo("TALBROAUTO.NS"), trustedBrokerIsin("INE187D01029")));
        when(verifier.verify(anyString())).thenReturn(verification("TALBROAUTO", "INE999Z01011", "Talbros Automotive Components Limited"));

        assertThat(service.reconcile(instrumentId).reason()).isEqualTo("NSE_ISIN_NOT_COMPATIBLE");
        verify(instrumentMaster).recordMappingFailure(eq(instrumentId), eq("NSE"), eq("TALBROAUTO"), isNull(), eq("NSE"),
                eq("INR"), eq("NSE_VALIDATION_FAILED"), eq(BigDecimal.ZERO), eq("NSE_ISIN_NOT_COMPATIBLE"));
    }

    @Test
    void unrelatedNonEquityAndNonNseInstrumentsAreNotPromoted() {
        when(masters.findById(instrumentId)).thenReturn(Optional.of(master(AssetType.FUND, "NSE")));
        assertThat(service.reconcile(instrumentId).reason()).isEqualTo("NOT_NSE_EQUITY");
        when(masters.findById(instrumentId)).thenReturn(Optional.of(master(AssetType.EQUITY, "BSE")));
        assertThat(service.reconcile(instrumentId).reason()).isEqualTo("NOT_NSE_EQUITY");
        when(masters.findById(instrumentId)).thenReturn(Optional.of(master(AssetType.EQUITY, "UNKNOWN", null,
                "Missing identity", "BROKER_ALIAS")));
        assertThat(service.reconcile(instrumentId).reason()).isEqualTo("NOT_NSE_EQUITY");
        when(masters.findById(instrumentId)).thenReturn(Optional.of(new InstrumentMasterEntity(instrumentId,
                "US5949181045", "Foreign equity", AssetType.EQUITY, "USD", "US", "UNKNOWN", "MSFT", "ACTIVE", Instant.now())));
        assertThat(service.reconcile(instrumentId).reason()).isEqualTo("NOT_NSE_EQUITY");
        verifyNoInteractions(mappings, verifier, instrumentMaster);
    }

    @Test
    void indianNseEtfUsesExactOfficialListBeforeYahooOrCorporateAnnouncements() {
        when(masters.findById(instrumentId)).thenReturn(Optional.of(master(AssetType.ETF, "NSE", "INF179KC1HS2", "HDFC NIFTY NEXT 50 ETF", "HDFN50")));
        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of(yahoo("HDFCNEXT50.NS"),
                mapping("NSE", "HDFCNEXT50", "INVALID", "NSE_VALIDATION_FAILED")));
        when(etfSecurityList.lookupByIsin("INF179KC1HS2")).thenReturn(NseOfficialEtfSecurityList.Lookup.matched(
                "HDFCNEXT50", "INF179KC1HS2", "HDFCAMC-HDFCNEXT50", "HDFCNIFTYNEXT50ETF"));

        assertThat(service.reconcile(instrumentId).status()).isEqualTo("PERSISTED");
        verify(instrumentMaster).saveResolvedMapping(eq(instrumentId), eq("NSE"), eq("HDFCNEXT50"), isNull(), eq("NSE"),
                eq("INR"), eq("VERIFIED"), eq("NSE_OFFICIAL_ETF_SECURITY_LIST"), eq(new BigDecimal("0.99")));
        verifyNoInteractions(verifier, securityMaster);
    }

    @Test
    void indianNseEtfListFailureRemainsUnresolvedAndNeverPromotesYahoo() {
        when(masters.findById(instrumentId)).thenReturn(Optional.of(master(AssetType.ETF, "NSE", "INF204KB17I5", "NIPPON INDIA ETF GOLD BEES", "GOLDEX")));
        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of(yahoo("GOLDBEES.NS")));
        when(etfSecurityList.lookupByIsin("INF204KB17I5")).thenReturn(NseOfficialEtfSecurityList.Lookup.unavailable());

        assertThat(service.reconcile(instrumentId).reason()).isEqualTo("OFFICIAL_ETF_LIST_UNAVAILABLE");
        verifyNoInteractions(instrumentMaster, verifier, securityMaster);
    }

    @Test
    void etfOfficialListWinsEvenWhenCorporateAnnouncementsWouldBeEmpty() {
        when(masters.findById(instrumentId)).thenReturn(Optional.of(master(AssetType.ETF, "NSE", "INF179KC1HS2", "HDFC NIFTY NEXT 50 ETF", "HDFN50")));
        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of(yahoo("HDFCNEXT50.NS")));
        when(etfSecurityList.lookupByIsin("INF179KC1HS2")).thenReturn(NseOfficialEtfSecurityList.Lookup.matched(
                "HDFCNEXT50", "INF179KC1HS2", "HDFCAMC-HDFCNEXT50", "HDFCNIFTYNEXT50ETF"));

        assertThat(service.reconcile(instrumentId).status()).isEqualTo("PERSISTED");
        verify(instrumentMaster).saveResolvedMapping(eq(instrumentId), eq("NSE"), eq("HDFCNEXT50"), isNull(), eq("NSE"),
                eq("INR"), eq("VERIFIED"), eq("NSE_OFFICIAL_ETF_SECURITY_LIST"), eq(new BigDecimal("0.99")));
        verify(instrumentMaster, never()).recordMappingFailure(any(), any(), any(), any(), any(), any(), any(), any(), any());
    }

    @Test
    void etfOfficialListCanCorrectYahooCandidateWithoutTrustingIt() {
        when(masters.findById(instrumentId)).thenReturn(Optional.of(master(AssetType.ETF, "NSE", "INF179KC1HS2", "HDFC NIFTY NEXT 50 ETF", "HDFN50")));
        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of(yahoo("HDFCNEXT50.NS")));
        when(etfSecurityList.lookupByIsin("INF179KC1HS2")).thenReturn(NseOfficialEtfSecurityList.Lookup.matched(
                "OFFICIAL_ETF", "INF179KC1HS2", "HDFCAMC-HDFCNEXT50", "HDFCNIFTYNEXT50ETF"));

        assertThat(service.reconcile(instrumentId).candidate()).isEqualTo("OFFICIAL_ETF");
        verify(instrumentMaster).saveResolvedMapping(eq(instrumentId), eq("NSE"), eq("OFFICIAL_ETF"), isNull(), eq("NSE"),
                eq("INR"), eq("VERIFIED"), eq("NSE_OFFICIAL_ETF_SECURITY_LIST"), eq(new BigDecimal("0.99")));
    }

    @Test
    void etfOfficialListNoMatchRemainsUnresolvedWithoutTryingYahooCandidate() {
        when(masters.findById(instrumentId)).thenReturn(Optional.of(master(AssetType.ETF, "NSE", "INF179KC1HS2", "HDFC NIFTY NEXT 50 ETF", "HDFN50")));
        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of(yahoo("HDFCNEXT50.NS")));
        when(etfSecurityList.lookupByIsin("INF179KC1HS2")).thenReturn(NseOfficialEtfSecurityList.Lookup.noIsinMatch());

        assertThat(service.reconcile(instrumentId).reason()).isEqualTo("OFFICIAL_ETF_LIST_NO_ISIN_MATCH");
        verifyNoInteractions(instrumentMaster, verifier, securityMaster);
    }

    @Test
    void exactOfficialEtfListBootstrapWorksWithoutYahooMapping() {
        when(masters.findById(instrumentId)).thenReturn(Optional.of(master(AssetType.ETF, "NSE", "INF204KB17I5", "NIPPON INDIA ETF GOLD BEES", "GOLDEX")));
        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of());
        when(etfSecurityList.lookupByIsin("INF204KB17I5")).thenReturn(NseOfficialEtfSecurityList.Lookup.matched(
                "GOLDBEES", "INF204KB17I5", "NIPINDETFLGOLDBEES", "Gold"));

        assertThat(service.reconcile(instrumentId).status()).isEqualTo("PERSISTED");
        verify(instrumentMaster).saveResolvedMapping(eq(instrumentId), eq("NSE"), eq("GOLDBEES"), isNull(), eq("NSE"),
                eq("INR"), eq("VERIFIED"), eq("NSE_OFFICIAL_ETF_SECURITY_LIST"), eq(new BigDecimal("0.99")));
    }

    @Test
    void legacyBrokerIdentityRowBlocksUnsafeCoexistenceUntilControlledRepair() {
        InstrumentProviderMappingEntity broker = mapping("ICICI_DIRECT", "TALAUT", "VERIFIED", "BROKER_IMPORT");
        InstrumentProviderMappingEntity badNse = mapping("NSE", "TALAUT", "VERIFIED", "BROKER_IMPORT_IDENTITY");
        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of(broker, yahoo("TALBROAUTO.NS"), badNse));

        assertThat(service.reconcile(instrumentId).reason()).isEqualTo("LEGACY_NSE_MAPPING_REPAIR_REQUIRED");
        verifyNoInteractions(verifier, instrumentMaster);
    }

    @Test
    void invalidLegacyAliasDoesNotBlockOfficialNseReconciliationOrChangeOtherProviders() {
        InstrumentProviderMappingEntity broker = mapping("ICICI_DIRECT", "TALAUT", "VERIFIED", "BROKER_IMPORT");
        InstrumentProviderMappingEntity yahoo = yahoo("TALBROAUTO.NS");
        InstrumentProviderMappingEntity retiredAlias = mapping("NSE", "TALAUT", "INVALID", "BROKER_IMPORT_IDENTITY");
        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of(broker, yahoo, retiredAlias));
        when(verifier.verify("TALBROAUTO"))
                .thenReturn(new NseOfficialMappingVerifier.Verification(true, "TALBROAUTO", "INE187D01029", null, null));

        var outcome = service.reconcile(instrumentId);

        assertThat(outcome.status()).isEqualTo("PERSISTED");
        verify(instrumentMaster).saveResolvedMapping(eq(instrumentId), eq("NSE"), eq("TALBROAUTO"), isNull(), eq("NSE"),
                eq("INR"), eq("VERIFIED"), eq("NSE_VALIDATED_RESOLUTION"), eq(new BigDecimal("0.99")));
        verify(mappings).findByInstrumentId(instrumentId);
        verifyNoMoreInteractions(mappings);
    }

    @Test
    void existingNonLegacyVerifiedNseMappingPreventsUnnecessaryReconciliation() {
        InstrumentProviderMappingEntity trustedNse = mapping("NSE", "TALBROAUTO", "VERIFIED", "NSE_VALIDATED_RESOLUTION");
        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of(yahoo("TALBROAUTO.NS"), trustedNse));

        assertThat(service.reconcile(instrumentId).reason()).isEqualTo("TRUSTED_NSE_MAPPING_EXISTS");
        verifyNoInteractions(verifier, instrumentMaster);
    }

    @Test
    void compatibleHistoricalIsinPersistsOnlyWithExactCompanyAndTrustedCurrentIsin() {
        InstrumentProviderMappingEntity retiredAlias = mapping("NSE", "OLD_ALIAS", "INVALID", "BROKER_IMPORT_IDENTITY");
        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of(yahoo("CANONICAL.NS"), trustedBrokerIsin("INE187D01029"), retiredAlias));
        when(verifier.verify("CANONICAL")).thenReturn(new NseOfficialMappingVerifier.Verification(true, "CANONICAL",
                "INE187D01011", "Talbros Automotive Components Limited", null));

        assertThat(service.reconcile(instrumentId).status()).isEqualTo("PERSISTED");
        verify(instrumentMaster).saveResolvedMapping(eq(instrumentId), eq("NSE"), eq("CANONICAL"), isNull(), eq("NSE"),
                eq("INR"), eq("VERIFIED"), eq("NSE_VALIDATED_RESOLUTION"), eq(new BigDecimal("0.99")));
    }

    @Test
    void successorPathRejectsCompanyMismatchAndMissingOrWrongTrustedCurrentIsin() {
        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of(yahoo("CANONICAL.NS"), trustedBrokerIsin("INE187D01029")));
        when(verifier.verify("CANONICAL")).thenReturn(new NseOfficialMappingVerifier.Verification(true, "CANONICAL",
                "INE187D01011", "Different Company Limited", null));
        assertThat(service.reconcile(instrumentId).reason()).isEqualTo("NSE_COMPANY_NAME_MISMATCH");

        reset(instrumentMaster);
        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of(yahoo("CANONICAL.NS")));
        when(verifier.verify("CANONICAL")).thenReturn(verification("CANONICAL", "INE187D01011", "Talbros Automotive Components Limited"));
        assertThat(service.reconcile(instrumentId).reason()).isEqualTo("CURRENT_ISIN_NOT_TRUSTED");

        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of(yahoo("CANONICAL.NS"), trustedBrokerIsin("INE187D01011")));
        assertThat(service.reconcile(instrumentId).reason()).isEqualTo("CURRENT_ISIN_NOT_TRUSTED");
    }

    @Test
    void recognizedOfficialDifferentSymbolIsRejected() {
        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of(yahoo("CANONICAL.NS")));
        when(verifier.verify("CANONICAL")).thenReturn(new NseOfficialMappingVerifier.Verification(true, "OTHER",
                "INE187D01029", "Talbros Automotive Components Limited", null));
        assertThat(service.reconcile(instrumentId).reason()).isEqualTo("NSE_SYMBOL_MISMATCH");
        verify(instrumentMaster).recordMappingFailure(eq(instrumentId), eq("NSE"), eq("CANONICAL"), isNull(), eq("NSE"),
                eq("INR"), eq("NSE_VALIDATION_FAILED"), eq(BigDecimal.ZERO), eq("NSE_SYMBOL_MISMATCH"));
    }

    @Test
    void identityEvidenceIsNullSafeAndDoesNotChangeNameMismatchRejection() {
        when(mappings.findByInstrumentId(instrumentId)).thenReturn(List.of(yahoo("TALBROAUTO.NS")));
        when(verifier.verify("TALBROAUTO")).thenReturn(new NseOfficialMappingVerifier.Verification(true, "TALBROAUTO",
                "INE999Z01011", null, null));

        assertThat(service.reconcile(instrumentId).reason()).isEqualTo("NSE_COMPANY_NAME_MISMATCH");
        var evidence = NseMappingReconciliationService.validationEvidence(null, null, null, null);
        assertThat(evidence.exactIsinMatch()).isFalse();
        assertThat(evidence.companyNameMatch()).isTrue();
        assertThat(evidence.normalizedMasterName()).isEmpty();
        assertThat(evidence.normalizedOfficialName()).isEmpty();
    }

    private InstrumentMasterEntity master(AssetType type, String exchange) {
        return master(type, exchange, "INE187D01029", "Talbros Automotive Components", "TALAUT");
    }

    private InstrumentMasterEntity master(AssetType type, String exchange, String isin, String name, String symbol) {
        return new InstrumentMasterEntity(instrumentId, isin, name, type, "INR", "IN", exchange, symbol, "ACTIVE", Instant.now());
    }

    private InstrumentProviderMappingEntity yahoo(String symbol) {
        return mapping("YAHOO_FINANCE", symbol, "VERIFIED", "YAHOO_VALIDATED_RESOLUTION");
    }
    private InstrumentProviderMappingEntity trustedBrokerIsin(String isin) {
        return new InstrumentProviderMappingEntity(UUID.randomUUID(), instrumentId, "BROKER_PROVIDER", "BROKER_ALIAS", "ISIN:" + isin,
                "NSE", "INR", "VERIFIED", "BROKER_IMPORT", new BigDecimal("0.99"), Instant.now());
    }
    private static NseOfficialMappingVerifier.Verification verification(String symbol, String isin, String company) {
        return new NseOfficialMappingVerifier.Verification(true, symbol, isin, company, null);
    }

    private InstrumentProviderMappingEntity mapping(String provider, String symbol, String status, String source) {
        return new InstrumentProviderMappingEntity(UUID.randomUUID(), instrumentId, provider, symbol, null, "NSE", "INR",
                status, source, new BigDecimal("0.95"), Instant.now());
    }
}
