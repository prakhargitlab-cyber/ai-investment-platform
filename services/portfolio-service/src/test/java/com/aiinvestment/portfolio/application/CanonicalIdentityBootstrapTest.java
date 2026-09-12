package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.infrastructure.persistence.*;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.mock.mockito.MockBean;
import org.springframework.test.context.ActiveProfiles;
import java.math.BigDecimal;
import java.time.Instant;
import java.util.*;
import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

@SpringBootTest
@ActiveProfiles("test")
class CanonicalIdentityBootstrapTest {
    @Autowired InstrumentMasterService instruments;
    @Autowired InstrumentMasterRepository masters;
    @Autowired InstrumentProviderMappingRepository mappings;
    @Autowired PortfolioPositionRepository positions;
    @Autowired CanonicalIdentityBootstrapStore store;
    @Autowired GlobalInstrumentReconciliationService reconciliation;
    @MockBean NseOfficialSecurityMaster official;
    @MockBean Nifty500ReferenceService nifty;
    @MockBean StructuredMarketClient structured;

    @Test void emptyDatabaseBootstrapPersistsOnlyIdentitiesAndIsRestartSafe() {
        assertThat(masters.count()).isZero();
        var rows = List.of(new NseOfficialSecurityMaster.Listing("ALPHA", "INE111A01010", "Alpha Components Limited", "EQ"),
                new NseOfficialSecurityMaster.Listing("BETA", "INE222A01010", "Beta Engineering Limited", "EQ"),
                new NseOfficialSecurityMaster.Listing("GAMMA", "INE333A01010", "Gamma Services Limited", "EQ"));
        when(official.listedEquities()).thenReturn(rows);
        when(structured.resolveGlobalIdentity(any())).thenAnswer(call -> {
            UUID id = call.getArgument(0);
            var master = instruments.globalInstrument(id).orElseThrow().master();
            instruments.saveResolvedMapping(id,"YAHOO_FINANCE",master.getPrimarySymbol()+".NS",null,"NSE","INR","VERIFIED","YAHOO_FROM_VERIFIED_NSE",new BigDecimal("0.95"));
            return null;
        });
        CanonicalIdentityBootstrap bootstrap = new CanonicalIdentityBootstrap(official,nifty,instruments,reconciliation,store);
        try { bootstrap.tick(); } finally { bootstrap.stop(); }
        assertThat(masters.count()).isEqualTo(3);
        assertThat(mappings.count()).isEqualTo(6);
        assertThat(positions.count()).isZero();
        Map<UUID,Instant> verified = new HashMap<>();
        mappings.findAll().forEach(m -> verified.put(m.getMappingId(),m.getVerifiedAt()));
        CanonicalIdentityBootstrap restarted = new CanonicalIdentityBootstrap(official,nifty,instruments,reconciliation,store);
        try { restarted.tick(); } finally { restarted.stop(); }
        assertThat(mappings.count()).isEqualTo(6);
        mappings.findAll().forEach(m -> assertThat(m.getVerifiedAt()).isEqualTo(verified.get(m.getMappingId())));
        verify(official,times(1)).listedEquities();
        verify(structured,times(3)).resolveGlobalIdentity(any());
        verify(structured,never()).fetchGlobal(any());
        verify(structured,never()).fetch(any());
        assertThat(store.dueMappings(40,Instant.now())).isEmpty();
        for (var master : masters.findAll()) {
            instruments.canonicalizeOfficialNse(master.getIsin(),master.getPrimarySymbol(),master.getCanonicalName());
        }
        mappings.findAll().forEach(m -> assertThat(m.getVerifiedAt()).isEqualTo(verified.get(m.getMappingId())));

        var failed = instruments.canonicalizeOfficialNse("INE444A01010","DELTA","Delta Manufacturing Limited");
        doThrow(new IllegalStateException("PROVIDER_TEMPORARILY_UNAVAILABLE")).when(structured).resolveGlobalIdentity(failed.getInstrumentId());
        var unavailable = reconciliation.reconcile(failed.getInstrumentId());
        assertThat(unavailable.status()).isEqualTo("UNAVAILABLE");
        assertThat(instruments.reusableMapping(failed.getInstrumentId(),"YAHOO_FINANCE")).isEmpty();
        assertThat(instruments.reusableMapping(failed.getInstrumentId(),"NSE")).isPresent();
        instruments.recordMappingFailure(failed.getInstrumentId(),"YAHOO_FINANCE","WRONG.NS",null,"NSE","INR","TEST_REJECTION",BigDecimal.ZERO,"ISIN_MISMATCH");
        clearInvocations(structured);
        assertThat(reconciliation.reconcile(failed.getInstrumentId()).status()).isEqualTo("REJECTED");
        assertThat(instruments.mappings(failed.getInstrumentId())).filteredOn(m -> m.getProvider().equals("YAHOO_FINANCE"))
                .allMatch(m -> m.getStatus().equals("INVALID"));
        verifyNoInteractions(structured);
        assertThatThrownBy(() -> instruments.canonicalizeOfficialNse("INE555A01010","DELTA","Different Company"))
                .isInstanceOf(IllegalStateException.class).hasMessageContaining("NSE_IDENTITY_MISMATCH");
    }
}
