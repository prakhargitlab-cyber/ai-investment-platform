package com.aiinvestment.portfolio.application;

import org.junit.jupiter.api.Test;
import java.util.List;
import static org.mockito.Mockito.*;

class CanonicalIdentityBootstrapFailureTest {
    @Test void temporaryOfficialOutageDoesNotMarkUniverseLoadedOrCreateMappings() {
        var official=mock(NseOfficialSecurityMaster.class);
        var nifty=mock(Nifty500ReferenceService.class);
        var instruments=mock(InstrumentMasterService.class);
        var reconciliation=mock(GlobalInstrumentReconciliationService.class);
        var store=mock(CanonicalIdentityBootstrapStore.class);
        when(store.claim(any(),any())).thenReturn(true);
        when(store.universeDue(any())).thenReturn(true);
        when(official.listedEquities()).thenReturn(List.of());
        var bootstrap=new CanonicalIdentityBootstrap(official,nifty,instruments,reconciliation,store);
        try {bootstrap.tick();bootstrap.tick();} finally {bootstrap.stop();}
        verify(official,times(1)).listedEquities(); // bounded retry cooldown
        verify(store,never()).universeLoaded(any());
        verifyNoInteractions(instruments,reconciliation,nifty);
        verify(store).release(any());
    }

    @Test void ambiguousOfficialRowsNeverReachCanonicalizationOrYahoo() {
        var official=mock(NseOfficialSecurityMaster.class);
        var nifty=mock(Nifty500ReferenceService.class);
        var instruments=mock(InstrumentMasterService.class);
        var reconciliation=mock(GlobalInstrumentReconciliationService.class);
        var store=mock(CanonicalIdentityBootstrapStore.class);
        when(store.claim(any(),any())).thenReturn(true);
        when(store.universeDue(any())).thenReturn(true);
        when(official.listedEquities()).thenReturn(List.of(
                new NseOfficialSecurityMaster.Listing("ALPHA","INE111A01010","Alpha Limited","EQ"),
                new NseOfficialSecurityMaster.Listing("BETA","INE111A01010","Beta Limited","EQ")));
        var bootstrap=new CanonicalIdentityBootstrap(official,nifty,instruments,reconciliation,store);
        try {bootstrap.tick();} finally {bootstrap.stop();}
        verifyNoInteractions(instruments,reconciliation);
    }
}
