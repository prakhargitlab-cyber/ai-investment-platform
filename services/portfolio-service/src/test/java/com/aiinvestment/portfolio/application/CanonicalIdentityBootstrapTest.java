package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.infrastructure.persistence.*;
import com.aiinvestment.shared.domain.AssetType;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.mock.mockito.MockBean;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.jdbc.core.JdbcTemplate;
import java.math.BigDecimal;
import java.time.Instant;
import java.util.*;
import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;
import static org.mockito.ArgumentMatchers.anyString;

@SpringBootTest
@ActiveProfiles("test")
class CanonicalIdentityBootstrapTest {
    @Autowired InstrumentMasterService instruments;
    @Autowired InstrumentMasterRepository masters;
    @Autowired InstrumentProviderMappingRepository mappings;
    @Autowired PortfolioPositionRepository positions;
    @Autowired CanonicalIdentityBootstrapStore store;
    @Autowired GlobalInstrumentReconciliationService reconciliation;
    @Autowired JdbcTemplate jdbc;
    @MockBean NseOfficialSecurityMaster official;
    @MockBean Nifty500ReferenceService nifty;
    @MockBean StructuredMarketClient structured;
    @MockBean NseOfficialEtfSecurityList etfSecurityList;

    // Both tests in this class seed fixture rows directly into the shared H2 test database via
    // bootstrap.tick() / canonicalizeOfficialNse(), with no transactional rollback (this class, like the
    // rest of this package, runs without @Transactional so state persists across test classes within the
    // same Surefire JVM fork). Left uncleaned, the resulting instrument_master / canonical_identity_mapping_jobs
    // rows break PortfolioServiceIntegrationTest's unconditional "DELETE FROM portfolio.instrument_master" via
    // the canonical_identity_mapping_jobs FK -- confirmed to reproduce from EITHER test's fixture rows in
    // isolation (not just the newer investmentVehicleSeriesIs... one), since neither test's rows are ever
    // guaranteed to be fully consumed/removed by an intervening class before PortfolioServiceIntegrationTest
    // runs. The instrument_master deletion below is scoped strictly to this class's own fixture ISINs
    // (every ISIN literal used by either @Test below -- not a broad wipe of shared state); the
    // dependent canonical_identity_mapping_jobs and instrument_provider_mappings rows (both carry a FK
    // on instrument_master(instrument_id); see V16__global_instrument_master.sql and
    // V21__canonical_identity_bootstrap.sql) are removed by the broader, diff-based cleanup explained
    // below, which also covers rows adopted from other classes' leftover instrument_master rows. Both
    // run in @AfterEach so they execute even if a test's own assertions fail, dependent rows before the
    // instrument_master rows to respect FK ordering. Neither @Test method below is modified.
    private static final List<String> CANONICAL_IDENTITY_BOOTSTRAP_FIXTURE_ISINS = List.of(
            "INE111A01010", "INE222A01010", "INE333A01010", "INE444A01010", "INE555A01010",
            "INE711A01010", "INE722A01010", "INE733A01010", "INE744A01010", "INE755A01010", "INE766A01010");

    // CanonicalIdentityBootstrapStore.enqueue() (invoked by bootstrap.tick() in both @Test methods
    // below) performs an unconditional "INSERT INTO canonical_identity_mapping_jobs ... SELECT ...
    // FROM instrument_master WHERE country='IN' AND primary_exchange='NSE' AND asset_type='EQUITY'
    // AND status='ACTIVE' AND NOT EXISTS (... a job already)" sweep across the ENTIRE shared
    // instrument_master table -- not scoped to this test's own fixture rows. So whenever an
    // unrelated, earlier-running integration test class leaves behind a real EQUITY instrument_master
    // row without cleaning it up (a separate, pre-existing test-isolation gap in that other class,
    // out of scope to fix here), this class's own tick() call "adopts" that row by creating a
    // canonical_identity_mapping_jobs row for it -- and dueMappings()/reconcile() immediately
    // afterwards can in turn create a real instrument_provider_mappings row for it too. Both adopted
    // rows are still THIS class's own side effect (neither would exist without this class calling
    // tick()), and both carry a FK on instrument_master(instrument_id) (see
    // V16__global_instrument_master.sql and V21__canonical_identity_bootstrap.sql), so left behind
    // they break PortfolioServiceIntegrationTest's unconditional "DELETE FROM portfolio.instrument_master"
    // exactly like this class's own fixture rows do. An ISIN-scoped cleanup can't reach these, since
    // they reference instrument_master rows this class never created and doesn't know the ISINs of.
    // Instead, a @BeforeEach snapshot of the canonical_identity_mapping_jobs/instrument_provider_mappings
    // row identifiers taken before each test method lets @AfterEach remove exactly the rows that are
    // new since this test began -- this class's own footprint, whichever instrument_master row they
    // happen to reference -- without ever touching another class's instrument_master row itself, so
    // it is not a broad wipe of unrelated shared state. This diff-based cleanup subsumes the
    // job/mapping rows created for this class's own fixture ISINs too, so only the instrument_master
    // deletion below still needs to be scoped explicitly by ISIN. Neither @Test method below is
    // modified.
    private Set<UUID> canonicalIdentityMappingJobInstrumentIdsBeforeTest;
    private Set<UUID> instrumentProviderMappingIdsBeforeTest;

    @BeforeEach
    void snapshotSharedReconciliationStateBeforeBootstrapTick() {
        canonicalIdentityMappingJobInstrumentIdsBeforeTest = new HashSet<>(
                jdbc.queryForList("SELECT instrument_id FROM portfolio.canonical_identity_mapping_jobs", UUID.class));
        instrumentProviderMappingIdsBeforeTest = new HashSet<>(
                jdbc.queryForList("SELECT mapping_id FROM portfolio.instrument_provider_mappings", UUID.class));
    }

    @AfterEach
    void cleanupCanonicalIdentityBootstrapFixtures() {
        List<UUID> newJobInstrumentIds = jdbc.queryForList("SELECT instrument_id FROM portfolio.canonical_identity_mapping_jobs", UUID.class)
                .stream().filter(id -> !canonicalIdentityMappingJobInstrumentIdsBeforeTest.contains(id)).toList();
        if (!newJobInstrumentIds.isEmpty()) {
            jdbc.batchUpdate("DELETE FROM portfolio.canonical_identity_mapping_jobs WHERE instrument_id=?",
                    newJobInstrumentIds.stream().map(id -> new Object[]{id}).toList());
        }
        List<UUID> newProviderMappingIds = jdbc.queryForList("SELECT mapping_id FROM portfolio.instrument_provider_mappings", UUID.class)
                .stream().filter(id -> !instrumentProviderMappingIdsBeforeTest.contains(id)).toList();
        if (!newProviderMappingIds.isEmpty()) {
            jdbc.batchUpdate("DELETE FROM portfolio.instrument_provider_mappings WHERE mapping_id=?",
                    newProviderMappingIds.stream().map(id -> new Object[]{id}).toList());
        }
        String placeholders = String.join(",", Collections.nCopies(CANONICAL_IDENTITY_BOOTSTRAP_FIXTURE_ISINS.size(), "?"));
        Object[] isinArgs = CANONICAL_IDENTITY_BOOTSTRAP_FIXTURE_ISINS.toArray();
        jdbc.update("DELETE FROM portfolio.instrument_master WHERE isin IN (" + placeholders + ")", isinArgs);
    }

    @Test void emptyDatabaseBootstrapPersistsOnlyIdentitiesAndIsRestartSafe() {
        assertThat(masters.count()).isZero();
        when(etfSecurityList.lookupByIsin(anyString())).thenReturn(NseOfficialEtfSecurityList.Lookup.noIsinMatch());
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

    @Test void investmentVehicleSeriesIsBootstrappedAsNonEquityWhileEveryOtherAcceptedSeriesStaysEquity() {
        // This test's own identities (ALPHA2/BSURV2/...) are distinct from every other test's
        // fixtures, and the universe/lease row is force-reset before ticking, so this scenario is
        // correct regardless of what other tests in this class have already run against the shared
        // test datasource (CanonicalIdentityBootstrapStore's 24h "universe due" lease is otherwise
        // still satisfied from an earlier test's run in the same process, which would silently skip
        // ingestion here and is a pre-existing ordering fragility of this store, not something this
        // test should depend on).
        jdbc.update("UPDATE portfolio.canonical_identity_bootstrap SET universe_loaded_at=NULL, lease_until=? WHERE region='INDIA'",
                java.sql.Timestamp.from(Instant.EPOCH));
        var rows = List.of(
                new NseOfficialSecurityMaster.Listing("ALPHA2", "INE711A01010", "Alpha Components Two Limited", "EQ"),
                new NseOfficialSecurityMaster.Listing("BSURV2", "INE722A01010", "Beta Surveillance Two Limited", "BE"),
                new NseOfficialSecurityMaster.Listing("BZTRD2", "INE733A01010", "Bravo Trade To Trade Two Limited", "BZ"),
                new NseOfficialSecurityMaster.Listing("SMESM2", "INE744A01010", "Small Manufacturing SME Two Limited", "SM"),
                new NseOfficialSecurityMaster.Listing("SMEST2", "INE755A01010", "Small Trading SME Two Limited", "ST"),
                new NseOfficialSecurityMaster.Listing("EMBASY2", "INE766A01010", "Embassy Office Parks Two REIT", "IV"));
        when(official.listedEquities()).thenReturn(rows);
        when(etfSecurityList.lookupByIsin(anyString())).thenReturn(NseOfficialEtfSecurityList.Lookup.noIsinMatch());
        when(structured.resolveGlobalIdentity(any())).thenReturn(null);

        CanonicalIdentityBootstrap bootstrap = new CanonicalIdentityBootstrap(official,nifty,instruments,reconciliation,store);
        try { bootstrap.tick(); } finally { bootstrap.stop(); }

        Map<String,AssetType> bySymbol = new HashMap<>();
        Map<String,UUID> idBySymbol = new HashMap<>();
        masters.findAll().forEach(m -> { bySymbol.put(m.getPrimarySymbol(), m.getAssetType()); idBySymbol.put(m.getPrimarySymbol(), m.getInstrumentId()); });
        assertThat(bySymbol.get("ALPHA2")).isEqualTo(AssetType.EQUITY);
        assertThat(bySymbol.get("BSURV2")).isEqualTo(AssetType.EQUITY);
        assertThat(bySymbol.get("BZTRD2")).isEqualTo(AssetType.EQUITY);
        assertThat(bySymbol.get("SMESM2")).isEqualTo(AssetType.EQUITY);
        assertThat(bySymbol.get("SMEST2")).isEqualTo(AssetType.EQUITY);
        assertThat(bySymbol.get("EMBASY2")).isEqualTo(AssetType.OTHER);

        // The investment-vehicle row never enters the equity reconciliation queue at all: it is
        // excluded by canonical_identity_mapping_jobs' own asset_type='EQUITY' enqueue predicate.
        // Checked directly against the synchronous enqueue() write (a plain SELECT-driven INSERT),
        // rather than via the asynchronous worker-pool reconciliation outcome, so this assertion
        // does not depend on that separate, pre-existing, unrelated worker-pool timing behavior.
        Long embassyJobs = jdbc.queryForObject("SELECT count(*) FROM portfolio.canonical_identity_mapping_jobs WHERE instrument_id=?",
                Long.class, idBySymbol.get("EMBASY2"));
        assertThat(embassyJobs).isZero();
        Long alphaJobs = jdbc.queryForObject("SELECT count(*) FROM portfolio.canonical_identity_mapping_jobs WHERE instrument_id=?",
                Long.class, idBySymbol.get("ALPHA2"));
        assertThat(alphaJobs).isEqualTo(1L);
    }
}
