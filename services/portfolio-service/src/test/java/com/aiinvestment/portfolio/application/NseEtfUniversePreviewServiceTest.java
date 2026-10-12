package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterEntity;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterRepository;
import com.aiinvestment.shared.domain.AssetType;
import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.util.List;
import java.util.Optional;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

/**
 * Pure Mockito unit tests (no Spring context, no real DB) for the strictly read-only NSE ETF
 * universe preview comparison -- kept off the shared H2 test database for the same reason
 * documented on {@link AuthoritativeAssetTypeReclassificationServiceTest}.
 */
class NseEtfUniversePreviewServiceTest {
    private final InstrumentMasterRepository masters = mock(InstrumentMasterRepository.class);
    private final NseOfficialEtfSecurityList etfSecurityList = mock(NseOfficialEtfSecurityList.class);
    private final NseOfficialSecurityMaster securityMaster = mock(NseOfficialSecurityMaster.class);
    private final NseEtfUniversePreviewService service =
            new NseEtfUniversePreviewService(masters, etfSecurityList, securityMaster);

    private static InstrumentMasterEntity existingRow(String isin, String symbol, AssetType type) {
        return new InstrumentMasterEntity(UUID.randomUUID(), isin, symbol + " Limited", type, "INR", "IN", "NSE",
                symbol, "ACTIVE", Instant.now());
    }

    private void ivSeriesAvailableWithNoOverlap() {
        when(securityMaster.listedEquities()).thenReturn(List.of(
                new NseOfficialSecurityMaster.Listing("UNRELATED", "INE999Z01999", "Unrelated Co", "EQ")));
    }

    // 1. Preview never calls any mutating/canonicalize/reconciliation method on any collaborator --
    // only the read-only lookups it was given. instrument_master is only ever read by normalized
    // ISIN, never saved or deleted.
    @Test void previewNeverInvokesAnyMutatingOrReconciliationCollaborator() {
        ivSeriesAvailableWithNoOverlap();
        when(etfSecurityList.listAll()).thenReturn(List.of(
                new NseOfficialEtfSecurityList.Listing("GOLDBEES", "INE001A01001", "Gold BeES", "GOLD")));
        when(masters.findByNormalizedIsin("INE001A01001")).thenReturn(Optional.empty());

        service.preview();

        verify(masters, times(1)).findByNormalizedIsin("INE001A01001");
        verify(masters, never()).save(any());
        verify(masters, never()).saveAndFlush(any());
        verify(masters, never()).delete(any());
        verify(masters, never()).deleteAll();
        verify(etfSecurityList, never()).lookupByIsin(anyString());
        verify(securityMaster, never()).lookupByIsin(anyString());
        verifyNoMoreInteractions(masters);
    }

    // 2. Missing ETF ISIN, with no authoritative conflict, is correctly classified and counted.
    @Test void missingEtfIsinWithNoConflictIsReportedAsMissing() {
        ivSeriesAvailableWithNoOverlap();
        when(etfSecurityList.listAll()).thenReturn(List.of(
                new NseOfficialEtfSecurityList.Listing("GOLDBEES", "INE001A01001", "Gold BeES", "GOLD")));
        when(masters.findByNormalizedIsin("INE001A01001")).thenReturn(Optional.empty());

        var preview = service.preview();

        assertThat(preview.sourceAvailable()).isTrue();
        assertThat(preview.missingEtfs()).hasSize(1);
        assertThat(preview.missingEtfs().get(0).isin()).isEqualTo("INE001A01001");
        assertThat(preview.equityToEtfCandidates()).isEmpty();
        assertThat(preview.alreadyClassifiedEtfs()).isEmpty();
        assertThat(preview.authoritativeConflicts()).isEmpty();
        assertThat(preview.conflictVerificationIncomplete()).isEmpty();
    }

    // 3. Existing EQUITY row at an official ETF ISIN, with no authoritative conflict, is an
    // equity-to-ETF candidate.
    @Test void existingEquityAtOfficialEtfIsinWithNoConflictIsEquityToEtfCandidate() {
        ivSeriesAvailableWithNoOverlap();
        var existing = existingRow("INE003A01001", "RELIANCE", AssetType.EQUITY);
        when(etfSecurityList.listAll()).thenReturn(List.of(
                new NseOfficialEtfSecurityList.Listing("RELIANCE", "INE003A01001", "Reliance Industries", "RELIANCE")));
        when(masters.findByNormalizedIsin("INE003A01001")).thenReturn(Optional.of(existing));

        var preview = service.preview();

        assertThat(preview.equityToEtfCandidates()).hasSize(1);
        assertThat(preview.authoritativeConflicts()).isEmpty();
    }

    // 4. Existing ETF row at an official ETF ISIN is reported as already-classified, not a candidate.
    @Test void existingEtfIsAlreadyClassified() {
        ivSeriesAvailableWithNoOverlap();
        var existing = existingRow("INE001A01001", "GOLDBEES", AssetType.ETF);
        when(etfSecurityList.listAll()).thenReturn(List.of(
                new NseOfficialEtfSecurityList.Listing("GOLDBEES", "INE001A01001", "Gold BeES", "GOLD")));
        when(masters.findByNormalizedIsin("INE001A01001")).thenReturn(Optional.of(existing));

        var preview = service.preview();

        assertThat(preview.alreadyClassifiedEtfs()).hasSize(1);
        assertThat(preview.missingEtfs()).isEmpty();
        assertThat(preview.equityToEtfCandidates()).isEmpty();
    }

    // 5. Exact-ISIN matching: a near-miss / differently formatted ISIN never matches.
    @Test void isinMatchingIsExactNormalizedIsinOnly() {
        ivSeriesAvailableWithNoOverlap();
        when(etfSecurityList.listAll()).thenReturn(List.of(
                new NseOfficialEtfSecurityList.Listing("GOLDBEES", "INE001A01001", "Gold BeES", "GOLD")));
        when(masters.findByNormalizedIsin("INE001A01001")).thenReturn(Optional.empty());
        when(masters.findByNormalizedIsin("INE001A01002")).thenReturn(Optional.of(
                existingRow("INE001A01002", "SOMETHINGELSE", AssetType.EQUITY)));

        var preview = service.preview();

        verify(masters, never()).findByNormalizedIsin("INE001A01002");
        assertThat(preview.missingEtfs()).hasSize(1);
    }

    // 6. An ISIN that is BRAND NEW (no instrument_master row) but ALSO present in the IV series
    // must be reported as a conflict, NEVER as "missing" -- the conflict check must run BEFORE the
    // missing classification, not after.
    @Test void newIsinAlsoInIvSeriesIsConflictNotMissing() {
        when(etfSecurityList.listAll()).thenReturn(List.of(
                new NseOfficialEtfSecurityList.Listing("CONFLICT", "INE010A01001", "Conflict Security", "X")));
        when(masters.findByNormalizedIsin("INE010A01001")).thenReturn(Optional.empty());
        when(securityMaster.listedEquities()).thenReturn(List.of(
                new NseOfficialSecurityMaster.Listing("CONFLICT", "INE010A01001", "Conflict Security", "IV")));

        var preview = service.preview();

        assertThat(preview.missingEtfs()).isEmpty();
        assertThat(preview.authoritativeConflicts()).hasSize(1);
        assertThat(preview.authoritativeConflicts().get(0).reason()).isEqualTo("ISIN_IN_BOTH_ETF_LIST_AND_IV_SERIES");
    }

    // 7. An existing EQUITY row whose ISIN is also in the IV series is a conflict, never an
    // equity-to-ETF candidate (conflict check runs before candidate classification).
    @Test void isinInBothEtfListAndIvSeriesIsReportedAsConflictNotCandidate() {
        var existing = existingRow("INE010A01001", "CONFLICT", AssetType.EQUITY);
        when(etfSecurityList.listAll()).thenReturn(List.of(
                new NseOfficialEtfSecurityList.Listing("CONFLICT", "INE010A01001", "Conflict Security", "X")));
        when(masters.findByNormalizedIsin("INE010A01001")).thenReturn(Optional.of(existing));
        when(securityMaster.listedEquities()).thenReturn(List.of(
                new NseOfficialSecurityMaster.Listing("CONFLICT", "INE010A01001", "Conflict Security", "IV")));

        var preview = service.preview();

        assertThat(preview.authoritativeConflicts()).hasSize(1);
        assertThat(preview.equityToEtfCandidates()).isEmpty();
    }

    // 8. Malformed official-list record (blank symbol/name, non-normalizable ISIN) is reported, not guessed at.
    @Test void malformedOfficialRecordIsReportedAsMalformed() {
        when(etfSecurityList.listAll()).thenReturn(List.of(
                new NseOfficialEtfSecurityList.Listing("", "INE001A01001", "Gold BeES", "GOLD"),
                new NseOfficialEtfSecurityList.Listing("BADISIN", "not-an-isin", "Bad Isin Fund", "BAD")));
        when(securityMaster.listedEquities()).thenReturn(List.of());

        var preview = service.preview();

        assertThat(preview.malformed()).hasSize(2);
        verifyNoInteractions(masters);
    }

    // 9. Duplicate ISIN within the official ETF list is reported as a conflict for BOTH occurrences
    // -- including what would otherwise be "the first" -- never for just the later one(s).
    @Test void duplicateIsinWithinOfficialEtfListRejectsAllOccurrences() {
        ivSeriesAvailableWithNoOverlap();
        when(etfSecurityList.listAll()).thenReturn(List.of(
                new NseOfficialEtfSecurityList.Listing("GOLDBEES", "INE001A01001", "Gold BeES", "GOLD"),
                new NseOfficialEtfSecurityList.Listing("GOLDBEES2", "INE001A01001", "Gold BeES Duplicate", "GOLD")));
        when(masters.findByNormalizedIsin("INE001A01001")).thenReturn(Optional.empty());

        var preview = service.preview();

        assertThat(preview.authoritativeConflicts()).hasSize(2);
        assertThat(preview.authoritativeConflicts()).allMatch(e -> e.reason().equals("DUPLICATE_ISIN_IN_OFFICIAL_ETF_LIST"));
        assertThat(preview.missingEtfs()).isEmpty();
        verify(masters, never()).findByNormalizedIsin(anyString());
    }

    // 10. Missing/unavailable official ETF source => preview reports unavailable and nothing is read
    // from instrument_master at all (fail closed, no partial comparison).
    @Test void unavailableOfficialEtfSourceFailsClosedWithNoRepositoryReads() {
        when(etfSecurityList.listAll()).thenThrow(new IllegalStateException("NSE_ETF_LIST_UNAVAILABLE"));

        var preview = service.preview();

        assertThat(preview.sourceAvailable()).isFalse();
        assertThat(preview.missingEtfs()).isEmpty();
        assertThat(preview.equityToEtfCandidates()).isEmpty();
        assertThat(preview.alreadyClassifiedEtfs()).isEmpty();
        assertThat(preview.malformed()).isEmpty();
        assertThat(preview.authoritativeConflicts()).isEmpty();
        assertThat(preview.conflictVerificationIncomplete()).isEmpty();
        verifyNoInteractions(masters, securityMaster);
    }

    // 11. IV-series source unavailable (empty listedEquities(), per its existing fail-closed contract)
    // must NOT silently classify an otherwise-missing ISIN as safe: it is reported as
    // conflictVerificationIncomplete, never folded into missingEtfs.
    @Test void ivSeriesSourceUnavailableReportsMissingIsinAsConflictVerificationIncomplete() {
        when(etfSecurityList.listAll()).thenReturn(List.of(
                new NseOfficialEtfSecurityList.Listing("GOLDBEES", "INE001A01001", "Gold BeES", "GOLD")));
        when(masters.findByNormalizedIsin("INE001A01001")).thenReturn(Optional.empty());
        when(securityMaster.listedEquities()).thenReturn(List.of());

        var preview = service.preview();

        assertThat(preview.sourceAvailable()).isTrue();
        assertThat(preview.ivSeriesSourceAvailable()).isFalse();
        assertThat(preview.missingEtfs()).isEmpty();
        assertThat(preview.conflictVerificationIncomplete()).hasSize(1);
        assertThat(preview.conflictVerificationIncomplete().get(0).isin()).isEqualTo("INE001A01001");
    }

    // 12. IV-series source unavailable must likewise NOT silently classify an existing-EQUITY ISIN
    // as a safe equity-to-ETF candidate: it is reported as conflictVerificationIncomplete instead.
    @Test void ivSeriesSourceUnavailableReportsEquityCandidateAsConflictVerificationIncomplete() {
        var existing = existingRow("INE003A01001", "RELIANCE", AssetType.EQUITY);
        when(etfSecurityList.listAll()).thenReturn(List.of(
                new NseOfficialEtfSecurityList.Listing("RELIANCE", "INE003A01001", "Reliance Industries", "RELIANCE")));
        when(masters.findByNormalizedIsin("INE003A01001")).thenReturn(Optional.of(existing));
        when(securityMaster.listedEquities()).thenReturn(List.of());

        var preview = service.preview();

        assertThat(preview.sourceAvailable()).isTrue();
        assertThat(preview.ivSeriesSourceAvailable()).isFalse();
        assertThat(preview.equityToEtfCandidates()).isEmpty();
        assertThat(preview.conflictVerificationIncomplete()).hasSize(1);
    }
}
