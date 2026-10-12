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
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.*;

/**
 * Pure Mockito unit tests (no Spring context, no real DB) for the bounded, idempotent NSE ETF
 * bootstrap pass -- kept off the shared H2 test database for the same reason documented on
 * {@link AuthoritativeAssetTypeReclassificationServiceTest}.
 */
class NseEtfUniverseBootstrapServiceTest {
    private final InstrumentMasterRepository masters = mock(InstrumentMasterRepository.class);
    private final NseOfficialEtfSecurityList etfSecurityList = mock(NseOfficialEtfSecurityList.class);
    private final NseOfficialSecurityMaster securityMaster = mock(NseOfficialSecurityMaster.class);
    private final InstrumentMasterService instruments = mock(InstrumentMasterService.class);
    private final NseEtfUniverseBootstrapService service =
            new NseEtfUniverseBootstrapService(masters, etfSecurityList, securityMaster, instruments);

    private static InstrumentMasterEntity existingRow(String isin, String symbol, AssetType type) {
        return new InstrumentMasterEntity(UUID.randomUUID(), isin, symbol + " Limited", type, "INR", "IN", "NSE",
                symbol, "ACTIVE", Instant.now());
    }

    /** IV-series source available, but with no overlap with any ETF-list ISIN used in these tests. */
    private void ivSeriesAvailableWithNoOverlap() {
        when(securityMaster.listedEquities()).thenReturn(List.of(
                new NseOfficialSecurityMaster.Listing("UNRELATED", "INE999Z01999", "Unrelated Co", "EQ")));
    }

    // 1. A genuinely missing ISIN, with no authoritative conflict, is created exactly once.
    @Test void missingEtfIsinWithNoConflictIsCreatedExactlyOnce() {
        ivSeriesAvailableWithNoOverlap();
        when(etfSecurityList.listAll()).thenReturn(List.of(
                new NseOfficialEtfSecurityList.Listing("GOLDBEES", "INE001A01001", "Gold BeES", "GOLD")));
        when(masters.findByNormalizedIsin("INE001A01001")).thenReturn(Optional.empty());

        var summary = service.bootstrapMissingEtfs();

        verify(instruments, times(1)).canonicalizeOfficialEtf("INE001A01001", "GOLDBEES", "Gold BeES");
        assertThat(summary.created).isEqualTo(1);
        assertThat(summary.existing).isZero();
        assertThat(summary.examined).isEqualTo(1);
        assertThat(summary.ivSeriesSourceAvailable).isTrue();
    }

    // 2. An ISIN that already exists as EQUITY is skipped, never passed to canonicalizeOfficialEtf,
    // and never reclassified by this service.
    @Test void existingEquityIsinIsSkippedUntouched() {
        ivSeriesAvailableWithNoOverlap();
        var existing = existingRow("INE003A01001", "RELIANCE", AssetType.EQUITY);
        when(etfSecurityList.listAll()).thenReturn(List.of(
                new NseOfficialEtfSecurityList.Listing("RELIANCE", "INE003A01001", "Reliance Industries", "RELIANCE")));
        when(masters.findByNormalizedIsin("INE003A01001")).thenReturn(Optional.of(existing));

        var summary = service.bootstrapMissingEtfs();

        verify(instruments, never()).canonicalizeOfficialEtf(anyString(), anyString(), anyString());
        assertThat(existing.getAssetType()).isEqualTo(AssetType.EQUITY);
        assertThat(summary.existing).isEqualTo(1);
        assertThat(summary.created).isZero();
    }

    // 3. An ISIN that already exists as ETF is likewise skipped and counted as existing, not re-created.
    @Test void existingEtfIsinIsIdempotentlySkipped() {
        ivSeriesAvailableWithNoOverlap();
        var existing = existingRow("INE001A01001", "GOLDBEES", AssetType.ETF);
        when(etfSecurityList.listAll()).thenReturn(List.of(
                new NseOfficialEtfSecurityList.Listing("GOLDBEES", "INE001A01001", "Gold BeES", "GOLD")));
        when(masters.findByNormalizedIsin("INE001A01001")).thenReturn(Optional.of(existing));

        var summary = service.bootstrapMissingEtfs();

        verify(instruments, never()).canonicalizeOfficialEtf(anyString(), anyString(), anyString());
        assertThat(summary.existing).isEqualTo(1);
        assertThat(summary.created).isZero();
    }

    // 4. Repeated passes over the same source are fully idempotent: second pass creates nothing new.
    @Test void repeatedBootstrapPassIsIdempotent() {
        ivSeriesAvailableWithNoOverlap();
        when(etfSecurityList.listAll()).thenReturn(List.of(
                new NseOfficialEtfSecurityList.Listing("GOLDBEES", "INE001A01001", "Gold BeES", "GOLD")));
        when(masters.findByNormalizedIsin("INE001A01001"))
                .thenReturn(Optional.empty())
                .thenReturn(Optional.of(existingRow("INE001A01001", "GOLDBEES", AssetType.ETF)));

        var first = service.bootstrapMissingEtfs();
        assertThat(first.created).isEqualTo(1);

        var second = service.bootstrapMissingEtfs();
        assertThat(second.created).isZero();
        assertThat(second.existing).isEqualTo(1);
        verify(instruments, times(1)).canonicalizeOfficialEtf(anyString(), anyString(), anyString());
    }

    // 5. A malformed listing (blank symbol / name, or non-normalizable ISIN) is rejected, not guessed at.
    @Test void malformedListingIsRejectedWithoutMutation() {
        ivSeriesAvailableWithNoOverlap();
        when(etfSecurityList.listAll()).thenReturn(List.of(
                new NseOfficialEtfSecurityList.Listing("", "INE001A01001", "Gold BeES", "GOLD"),
                new NseOfficialEtfSecurityList.Listing("BADISIN", "not-an-isin", "Bad Isin Fund", "BAD"),
                new NseOfficialEtfSecurityList.Listing("NONAME", "INE008A01001", "", "NONAME")));

        var summary = service.bootstrapMissingEtfs();

        verify(instruments, never()).canonicalizeOfficialEtf(anyString(), anyString(), anyString());
        assertThat(summary.rejectedMalformed).isEqualTo(3);
        assertThat(summary.created).isZero();
    }

    // 6. A duplicate ISIN within the official source is rejected IN FULL -- including the FIRST
    // occurrence, which the prior implementation incorrectly accepted and created.
    @Test void duplicateIsinWithinOfficialSourceRejectsAllOccurrencesIncludingFirst() {
        ivSeriesAvailableWithNoOverlap();
        when(etfSecurityList.listAll()).thenReturn(List.of(
                new NseOfficialEtfSecurityList.Listing("GOLDBEES", "INE001A01001", "Gold BeES", "GOLD"),
                new NseOfficialEtfSecurityList.Listing("GOLDBEES2", "INE001A01001", "Gold BeES Duplicate", "GOLD")));
        when(masters.findByNormalizedIsin("INE001A01001")).thenReturn(Optional.empty());

        var summary = service.bootstrapMissingEtfs();

        verify(instruments, never()).canonicalizeOfficialEtf(anyString(), anyString(), anyString());
        assertThat(summary.created).isZero();
        assertThat(summary.rejectedDuplicateIsin).isEqualTo(2);
    }

    // 7. Provider unavailable (listAll throws) => zero reads/writes against instrument_master, bounded summary flag set.
    @Test void providerUnavailableCausesNoMutationAtAll() {
        when(etfSecurityList.listAll()).thenThrow(new IllegalStateException("NSE_ETF_LIST_UNAVAILABLE"));

        var summary = service.bootstrapMissingEtfs();

        assertThat(summary.providerUnavailable).isTrue();
        assertThat(summary.examined).isZero();
        verifyNoInteractions(masters, instruments, securityMaster);
    }

    // 8. A failure creating one ISIN (e.g. an identity-mismatch guard inside canonicalizeOfficialEtf)
    // is isolated to that row -- it is counted as failed and does not prevent the next, independent
    // row's own transaction from succeeding.
    @Test void perRowFailureIsIsolatedAndDoesNotBlockOtherRows() {
        ivSeriesAvailableWithNoOverlap();
        when(etfSecurityList.listAll()).thenReturn(List.of(
                new NseOfficialEtfSecurityList.Listing("BADONE", "INE011A01001", "Bad One Fund", "BAD1"),
                new NseOfficialEtfSecurityList.Listing("GOODONE", "INE012A01001", "Good One Fund", "GOOD1")));
        when(masters.findByNormalizedIsin("INE011A01001")).thenReturn(Optional.empty());
        when(masters.findByNormalizedIsin("INE012A01001")).thenReturn(Optional.empty());
        when(instruments.canonicalizeOfficialEtf("INE011A01001", "BADONE", "Bad One Fund"))
                .thenThrow(new IllegalStateException("NSE_IDENTITY_MISMATCH"));

        var summary = service.bootstrapMissingEtfs();

        verify(instruments).canonicalizeOfficialEtf("INE011A01001", "BADONE", "Bad One Fund");
        verify(instruments).canonicalizeOfficialEtf("INE012A01001", "GOODONE", "Good One Fund");
        assertThat(summary.failed).isEqualTo(1);
        assertThat(summary.created).isEqualTo(1);
        assertThat(summary.examined).isEqualTo(2);
    }

    // 9. An ISIN present in BOTH the official ETF list and the IV series is an authoritative
    // conflict and is NEVER created.
    @Test void authoritativeConflictIsinIsNeverCreated() {
        when(etfSecurityList.listAll()).thenReturn(List.of(
                new NseOfficialEtfSecurityList.Listing("CONFLICT", "INE010A01001", "Conflict Security", "X")));
        when(masters.findByNormalizedIsin("INE010A01001")).thenReturn(Optional.empty());
        when(securityMaster.listedEquities()).thenReturn(List.of(
                new NseOfficialSecurityMaster.Listing("CONFLICT", "INE010A01001", "Conflict Security", "IV")));

        var summary = service.bootstrapMissingEtfs();

        verify(instruments, never()).canonicalizeOfficialEtf(anyString(), anyString(), anyString());
        assertThat(summary.rejectedConflict).isEqualTo(1);
        assertThat(summary.created).isZero();
    }

    // 10. If the IV-series source is unavailable, an otherwise-missing ISIN's conflict status is
    // UNKNOWN -- it must NOT be silently treated as safe and created; it is skipped instead.
    @Test void unverifiableConflictWhenIvSeriesSourceUnavailableIsNeverCreated() {
        when(etfSecurityList.listAll()).thenReturn(List.of(
                new NseOfficialEtfSecurityList.Listing("GOLDBEES", "INE001A01001", "Gold BeES", "GOLD")));
        when(masters.findByNormalizedIsin("INE001A01001")).thenReturn(Optional.empty());
        when(securityMaster.listedEquities()).thenReturn(List.of()); // empty == unavailable, per existing contract

        var summary = service.bootstrapMissingEtfs();

        verify(instruments, never()).canonicalizeOfficialEtf(anyString(), anyString(), anyString());
        assertThat(summary.ivSeriesSourceAvailable).isFalse();
        assertThat(summary.skippedUnverifiableConflict).isEqualTo(1);
        assertThat(summary.created).isZero();
        assertThat(summary.rejectedConflict).isZero();
    }

    // 11. Preview and bootstrap share the exact same validation logic (NseEtfListingValidation),
    // so an existing-row ISIN is still read-only-checked even when it is also part of a duplicate
    // group -- duplicates are rejected before any instrument_master read for that ISIN happens via
    // this service's own creation path, confirming no partial/arbitrary acceptance of one duplicate.
    @Test void allDuplicateOccurrencesAreRejectedEvenWhenAnInstrumentAlreadyExistsAtThatIsin() {
        ivSeriesAvailableWithNoOverlap();
        when(etfSecurityList.listAll()).thenReturn(List.of(
                new NseOfficialEtfSecurityList.Listing("DUP1", "INE020A01001", "Dup Fund One", "DUP"),
                new NseOfficialEtfSecurityList.Listing("DUP2", "INE020A01001", "Dup Fund Two", "DUP")));

        var summary = service.bootstrapMissingEtfs();

        verify(masters, never()).findByNormalizedIsin(anyString());
        verify(instruments, never()).canonicalizeOfficialEtf(anyString(), anyString(), anyString());
        assertThat(summary.rejectedDuplicateIsin).isEqualTo(2);
        assertThat(summary.existing).isZero();
    }
}
