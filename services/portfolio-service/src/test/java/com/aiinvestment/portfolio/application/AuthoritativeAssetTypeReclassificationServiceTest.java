package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterEntity;
import com.aiinvestment.portfolio.infrastructure.persistence.InstrumentMasterRepository;
import com.aiinvestment.shared.domain.AssetType;
import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.util.List;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.*;

/**
 * Pure Mockito unit tests (no Spring context, no real DB) for the bounded historical asset-type
 * reclassification pass -- deliberately kept off the shared H2 test database, since this session's
 * earlier investigation found that any test in this package touching the real DB without careful
 * scoped cleanup can leave cross-test-class pollution (see CanonicalIdentityBootstrapTest).
 */
class AuthoritativeAssetTypeReclassificationServiceTest {
    private final InstrumentMasterRepository masters = mock(InstrumentMasterRepository.class);
    private final NseOfficialSecurityMaster securityMaster = mock(NseOfficialSecurityMaster.class);
    private final NseOfficialEtfSecurityList etfSecurityList = mock(NseOfficialEtfSecurityList.class);
    private final AuthoritativeAssetTypeReclassificationService service =
            new AuthoritativeAssetTypeReclassificationService(masters, securityMaster, etfSecurityList);

    private static InstrumentMasterEntity nseInstrument(String isin, String symbol, AssetType type) {
        return new InstrumentMasterEntity(UUID.randomUUID(), isin, symbol + " Limited", type, "INR", "IN", "NSE",
                symbol, "ACTIVE", Instant.now());
    }

    private void candidates(InstrumentMasterEntity... rows) {
        when(masters.findByCountryIgnoreCaseAndPrimaryExchangeIgnoreCaseAndStatusIgnoreCase("IN", "NSE", "ACTIVE"))
                .thenReturn(List.of(rows));
    }

    // A. Historical EQUITY + ETF-list ISIN match => ETF
    @Test void historicalEquityWithAuthoritativeEtfListMatchBecomesEtf() {
        var row = nseInstrument("INE001A01001", "GOLDBEES", AssetType.EQUITY);
        candidates(row);
        when(etfSecurityList.listAll()).thenReturn(List.of(new NseOfficialEtfSecurityList.Listing("GOLDBEES", "INE001A01001", "Gold BeES", "GOLD")));
        when(securityMaster.listedEquities()).thenReturn(List.of(new NseOfficialSecurityMaster.Listing("GOLDBEES", "INE001A01001", "Gold BeES", "EQ")));

        var summary = service.reclassifyExistingNseInstruments();

        assertThat(row.getAssetType()).isEqualTo(AssetType.ETF);
        assertThat(summary.correctedToEtf).isEqualTo(1);
        assertThat(summary.examined).isEqualTo(1);
    }

    // B. Historical EQUITY + NSE IV-series ISIN match => OTHER
    @Test void historicalEquityWithAuthoritativeIvSeriesMatchBecomesOther() {
        var row = nseInstrument("INE002A01001", "EMBASY", AssetType.EQUITY);
        candidates(row);
        when(etfSecurityList.listAll()).thenReturn(List.of());
        when(securityMaster.listedEquities()).thenReturn(List.of(new NseOfficialSecurityMaster.Listing("EMBASY", "INE002A01001", "Embassy Office Parks REIT", "IV")));

        var summary = service.reclassifyExistingNseInstruments();

        assertThat(row.getAssetType()).isEqualTo(AssetType.OTHER);
        assertThat(summary.correctedToOther).isEqualTo(1);
    }

    // C. Historical ordinary EQ equity => remains EQUITY
    @Test void historicalOrdinaryEquityWithNoAuthoritativeMatchRemainsEquity() {
        var row = nseInstrument("INE003A01001", "RELIANCE", AssetType.EQUITY);
        candidates(row);
        when(etfSecurityList.listAll()).thenReturn(List.of());
        when(securityMaster.listedEquities()).thenReturn(List.of(new NseOfficialSecurityMaster.Listing("RELIANCE", "INE003A01001", "Reliance Industries", "EQ")));

        var summary = service.reclassifyExistingNseInstruments();

        assertThat(row.getAssetType()).isEqualTo(AssetType.EQUITY);
        assertThat(summary.noAuthoritativeMatch).isEqualTo(1);
        assertThat(summary.correctedToEtf).isZero();
        assertThat(summary.correctedToOther).isZero();
    }

    // D. Historical SM/ST/BE/BZ operating equity => remains EQUITY (series must not be read as non-equity)
    @Test void historicalSmeAndTradeToTradeSeriesEquitiesRemainEquity() {
        var sm = nseInstrument("INE004A01001", "SMESM", AssetType.EQUITY);
        var st = nseInstrument("INE005A01001", "SMEST", AssetType.EQUITY);
        var be = nseInstrument("INE006A01001", "BSURV", AssetType.EQUITY);
        var bz = nseInstrument("INE007A01001", "BZTRD", AssetType.EQUITY);
        candidates(sm, st, be, bz);
        when(etfSecurityList.listAll()).thenReturn(List.of());
        when(securityMaster.listedEquities()).thenReturn(List.of(
                new NseOfficialSecurityMaster.Listing("SMESM", "INE004A01001", "Small Manufacturing SME", "SM"),
                new NseOfficialSecurityMaster.Listing("SMEST", "INE005A01001", "Small Trading SME", "ST"),
                new NseOfficialSecurityMaster.Listing("BSURV", "INE006A01001", "Beta Surveillance", "BE"),
                new NseOfficialSecurityMaster.Listing("BZTRD", "INE007A01001", "Bravo Trade To Trade", "BZ")));

        var summary = service.reclassifyExistingNseInstruments();

        assertThat(List.of(sm, st, be, bz)).extracting(InstrumentMasterEntity::getAssetType)
                .containsOnly(AssetType.EQUITY);
        assertThat(summary.correctedToOther).isZero();
        assertThat(summary.noAuthoritativeMatch).isEqualTo(4);
    }

    // E. Already ETF => unchanged/idempotent
    @Test void alreadyEtfWithAuthoritativeEtfMatchIsUnchanged() {
        var row = nseInstrument("INE001A01001", "GOLDBEES", AssetType.ETF);
        candidates(row);
        when(etfSecurityList.listAll()).thenReturn(List.of(new NseOfficialEtfSecurityList.Listing("GOLDBEES", "INE001A01001", "Gold BeES", "GOLD")));
        when(securityMaster.listedEquities()).thenReturn(List.of());

        var summary = service.reclassifyExistingNseInstruments();

        assertThat(row.getAssetType()).isEqualTo(AssetType.ETF);
        assertThat(summary.correctedToEtf).isZero();
        assertThat(summary.alreadyCorrect).isEqualTo(1);
    }

    // F. Already OTHER => unchanged/idempotent
    @Test void alreadyOtherWithAuthoritativeIvMatchIsUnchanged() {
        var row = nseInstrument("INE002A01001", "EMBASY", AssetType.OTHER);
        candidates(row);
        when(etfSecurityList.listAll()).thenReturn(List.of());
        when(securityMaster.listedEquities()).thenReturn(List.of(new NseOfficialSecurityMaster.Listing("EMBASY", "INE002A01001", "Embassy Office Parks REIT", "IV")));

        var summary = service.reclassifyExistingNseInstruments();

        assertThat(row.getAssetType()).isEqualTo(AssetType.OTHER);
        assertThat(summary.correctedToOther).isZero();
        assertThat(summary.alreadyCorrect).isEqualTo(1);
    }

    // G. Missing ISIN => unchanged
    @Test void missingIsinIsSkippedWithoutMutation() {
        var row = nseInstrument(null, "NOISIN", AssetType.EQUITY);
        candidates(row);
        when(etfSecurityList.listAll()).thenReturn(List.of());
        when(securityMaster.listedEquities()).thenReturn(List.of());

        var summary = service.reclassifyExistingNseInstruments();

        assertThat(row.getAssetType()).isEqualTo(AssetType.EQUITY);
        assertThat(summary.missingIsin).isEqualTo(1);
        assertThat(summary.examined).isEqualTo(1);
    }

    // H. Unknown ISIN => unchanged
    @Test void unknownIsinNotInEitherAuthoritativeSourceIsUnchanged() {
        var row = nseInstrument("INE009A01001", "GHOST", AssetType.EQUITY);
        candidates(row);
        when(etfSecurityList.listAll()).thenReturn(List.of(new NseOfficialEtfSecurityList.Listing("GOLDBEES", "INE001A01001", "Gold BeES", "GOLD")));
        when(securityMaster.listedEquities()).thenReturn(List.of(new NseOfficialSecurityMaster.Listing("RELIANCE", "INE003A01001", "Reliance Industries", "EQ")));

        var summary = service.reclassifyExistingNseInstruments();

        assertThat(row.getAssetType()).isEqualTo(AssetType.EQUITY);
        assertThat(summary.noAuthoritativeMatch).isEqualTo(1);
    }

    // I. Conflicting authoritative evidence => unchanged + conflict counted
    @Test void sameIsinInBothEtfListAndIvSeriesIsLeftUnchangedAndCounted() {
        var row = nseInstrument("INE010A01001", "CONFLICT", AssetType.EQUITY);
        candidates(row);
        when(etfSecurityList.listAll()).thenReturn(List.of(new NseOfficialEtfSecurityList.Listing("CONFLICT", "INE010A01001", "Conflict Security", "X")));
        when(securityMaster.listedEquities()).thenReturn(List.of(new NseOfficialSecurityMaster.Listing("CONFLICT", "INE010A01001", "Conflict Security", "IV")));

        var summary = service.reclassifyExistingNseInstruments();

        assertThat(row.getAssetType()).isEqualTo(AssetType.EQUITY);
        assertThat(summary.conflictingAuthoritativeType).isEqualTo(1);
        assertThat(summary.correctedToEtf).isZero();
        assertThat(summary.correctedToOther).isZero();
    }

    // J. ETF source unavailable/invalid => no unsafe ETF mutation, but independently-available IV correction still proceeds
    @Test void etfSourceUnavailableBlocksOnlyEtfCorrectionNotIvCorrection() {
        var wouldBeEtf = nseInstrument("INE001A01001", "GOLDBEES", AssetType.EQUITY);
        var wouldBeOther = nseInstrument("INE002A01001", "EMBASY", AssetType.EQUITY);
        candidates(wouldBeEtf, wouldBeOther);
        when(etfSecurityList.listAll()).thenThrow(new IllegalStateException("NSE_ETF_LIST_UNAVAILABLE"));
        when(securityMaster.listedEquities()).thenReturn(List.of(new NseOfficialSecurityMaster.Listing("EMBASY", "INE002A01001", "Embassy Office Parks REIT", "IV")));

        var summary = service.reclassifyExistingNseInstruments();

        assertThat(wouldBeEtf.getAssetType()).isEqualTo(AssetType.EQUITY);
        assertThat(wouldBeOther.getAssetType()).isEqualTo(AssetType.OTHER);
        assertThat(summary.correctedToEtf).isZero();
        assertThat(summary.correctedToOther).isEqualTo(1);
    }

    // K. Security-master source unavailable/invalid => no unsafe IV mutation, but independently-available ETF correction still proceeds
    @Test void securityMasterUnavailableBlocksOnlyIvCorrectionNotEtfCorrection() {
        var wouldBeEtf = nseInstrument("INE001A01001", "GOLDBEES", AssetType.EQUITY);
        var wouldBeOther = nseInstrument("INE002A01001", "EMBASY", AssetType.EQUITY);
        candidates(wouldBeEtf, wouldBeOther);
        when(etfSecurityList.listAll()).thenReturn(List.of(new NseOfficialEtfSecurityList.Listing("GOLDBEES", "INE001A01001", "Gold BeES", "GOLD")));
        when(securityMaster.listedEquities()).thenReturn(List.of()); // empty == unavailable per listedEquities()'s existing contract

        var summary = service.reclassifyExistingNseInstruments();

        assertThat(wouldBeEtf.getAssetType()).isEqualTo(AssetType.ETF);
        assertThat(wouldBeOther.getAssetType()).isEqualTo(AssetType.EQUITY);
        assertThat(summary.correctedToEtf).isEqualTo(1);
        assertThat(summary.correctedToOther).isZero();
    }

    // L. Both providers unavailable => zero mutation
    @Test void bothProvidersUnavailableMutatesNothing() {
        var row = nseInstrument("INE001A01001", "GOLDBEES", AssetType.EQUITY);
        candidates(row);
        when(etfSecurityList.listAll()).thenThrow(new IllegalStateException("NSE_ETF_LIST_UNAVAILABLE"));
        when(securityMaster.listedEquities()).thenReturn(List.of());

        var summary = service.reclassifyExistingNseInstruments();

        assertThat(row.getAssetType()).isEqualTo(AssetType.EQUITY);
        assertThat(summary.correctedToEtf).isZero();
        assertThat(summary.correctedToOther).isZero();
        assertThat(summary.providerUnavailable).isEqualTo(1);
    }

    // M. Repeated pass => idempotent, no second update
    @Test void repeatedPassOverAlreadyCorrectedRowIsIdempotent() {
        var row = nseInstrument("INE001A01001", "GOLDBEES", AssetType.EQUITY);
        candidates(row);
        when(etfSecurityList.listAll()).thenReturn(List.of(new NseOfficialEtfSecurityList.Listing("GOLDBEES", "INE001A01001", "Gold BeES", "GOLD")));
        when(securityMaster.listedEquities()).thenReturn(List.of());

        var first = service.reclassifyExistingNseInstruments();
        assertThat(row.getAssetType()).isEqualTo(AssetType.ETF);
        assertThat(first.correctedToEtf).isEqualTo(1);

        var second = service.reclassifyExistingNseInstruments();
        assertThat(row.getAssetType()).isEqualTo(AssetType.ETF);
        assertThat(second.correctedToEtf).isZero();
        assertThat(second.alreadyCorrect).isEqualTo(1);
    }

    // N. Authoritative datasets are fetched once per pass, not once per instrument
    @Test void authoritativeDatasetsAreFetchedExactlyOncePerPassRegardlessOfCandidateCount() {
        var rows = List.of(
                nseInstrument("INE001A01001", "GOLDBEES", AssetType.EQUITY),
                nseInstrument("INE002A01001", "EMBASY", AssetType.EQUITY),
                nseInstrument("INE003A01001", "RELIANCE", AssetType.EQUITY),
                nseInstrument("INE004A01001", "SMESM", AssetType.EQUITY));
        when(masters.findByCountryIgnoreCaseAndPrimaryExchangeIgnoreCaseAndStatusIgnoreCase("IN", "NSE", "ACTIVE"))
                .thenReturn(rows);
        when(etfSecurityList.listAll()).thenReturn(List.of(new NseOfficialEtfSecurityList.Listing("GOLDBEES", "INE001A01001", "Gold BeES", "GOLD")));
        when(securityMaster.listedEquities()).thenReturn(List.of(
                new NseOfficialSecurityMaster.Listing("EMBASY", "INE002A01001", "Embassy Office Parks REIT", "IV"),
                new NseOfficialSecurityMaster.Listing("RELIANCE", "INE003A01001", "Reliance Industries", "EQ"),
                new NseOfficialSecurityMaster.Listing("SMESM", "INE004A01001", "Small Manufacturing SME", "SM")));

        service.reclassifyExistingNseInstruments();

        verify(etfSecurityList, times(1)).listAll();
        verify(securityMaster, times(1)).listedEquities();
        verify(etfSecurityList, never()).lookupByIsin(anyString());
    }
}
