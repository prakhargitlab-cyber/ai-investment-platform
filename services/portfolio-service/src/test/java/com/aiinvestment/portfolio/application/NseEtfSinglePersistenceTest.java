package com.aiinvestment.portfolio.application;

import com.aiinvestment.portfolio.infrastructure.persistence.*;
import com.aiinvestment.shared.domain.AssetType;
import com.aiinvestment.shared.domain.broker.BrokerType;
import com.aiinvestment.shared.domain.broker.BrokerAccountStatus;
import jakarta.persistence.EntityManager;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.orm.jpa.DataJpaTest;
import org.springframework.boot.test.autoconfigure.jdbc.AutoConfigureTestDatabase;
import org.springframework.boot.autoconfigure.domain.EntityScan;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Import;
import org.springframework.data.jpa.repository.config.EnableJpaRepositories;
import org.springframework.test.context.ContextConfiguration;
import org.springframework.jdbc.core.JdbcTemplate;
import java.math.BigDecimal;
import java.time.Instant;
import java.util.*;
import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

/** Embedded H2 ONLY, Hibernate test schema, Flyway disabled. No application
 * component scan, scheduler, providers, external datasource or production DB. */
@DataJpaTest(properties = {"spring.flyway.enabled=false", "spring.jpa.hibernate.ddl-auto=create-drop",
        "spring.jpa.properties.hibernate.default_schema=PUBLIC", "spring.jpa.show-sql=false"})
@AutoConfigureTestDatabase(replace = AutoConfigureTestDatabase.Replace.ANY)
@ContextConfiguration(classes = NseEtfSinglePersistenceTest.Config.class)
class NseEtfSinglePersistenceTest {
    @Configuration
    @EntityScan(basePackageClasses = InstrumentMasterEntity.class)
    @EnableJpaRepositories(basePackageClasses = InstrumentMasterRepository.class)
    @Import(InstrumentMasterService.class)
    static class Config {}

    @Autowired InstrumentMasterService service;
    @Autowired InstrumentMasterRepository masters;
    @Autowired InstrumentProviderMappingRepository mappings;
    @Autowired EntityManager em;
    @Autowired JdbcTemplate jdbc;
    static final String ISIN = "INF204KB14I2";

    @Test void createsExactlyOnePairAndRetryPreservesHoldingsAndEquityRelationships() {
        assertThat(jdbc.queryForObject("select h2version()", String.class)).isNotBlank();
        var now = Instant.now(); var user = UUID.randomUUID();
        var equity = new InstrumentMasterEntity(UUID.randomUUID(), "INE009A01021", "Existing equity", AssetType.EQUITY,
                "INR", "IN", "NSE", "EQUITY", "ACTIVE", now);
        em.persist(equity);
        var legacy = new InstrumentEntity(UUID.randomUUID(), "INE009A01021", "EQUITY", "NSE", "XNSE", "Existing equity", AssetType.EQUITY, "IN", "INR", null, null);
        legacy.attachMaster(equity.getInstrumentId()); em.persist(legacy);
        var portfolio = new PortfolioEntity(UUID.randomUUID(), user, "Preserve", "INR", now, now); em.persist(portfolio);
        var broker = new BrokerAccountEntity("fixture", user, BrokerType.values()[0], "fixture", "Fixture", "INR", BrokerAccountStatus.values()[0]); em.persist(broker);
        em.persist(new PortfolioPositionEntity(UUID.randomUUID(), portfolio, legacy, BigDecimal.TEN, BigDecimal.ONE,
                "INR", BigDecimal.TEN, "INR", broker, now));
        em.flush();
        var positions = jdbc.queryForList("select * from portfolio_positions");
        var portfolios = jdbc.queryForList("select * from portfolios");
        var instruments = jdbc.queryForList("select * from instruments");
        var equities = jdbc.queryForList("select * from instrument_master");
        var etfs = mock(NseOfficialEtfSecurityList.class);
        var official = mock(NseOfficialSecurityMaster.class);
        when(etfs.listAll()).thenReturn(List.of(new NseOfficialEtfSecurityList.Listing("NIFTYBEES", ISIN, "Official ETF", null),
                new NseOfficialEtfSecurityList.Listing("JUNIORBEES", "INF732E01045", "Other ETF", null)));
        when(official.listedEquities()).thenReturn(List.of(new NseOfficialSecurityMaster.Listing("EQUITY", "INE009A01021", "Existing equity", "EQ")));
        var apply = new NseEtfUniverseApplyService(new NseEtfUniverseBootstrapService(masters, etfs, official, service));
        var first = apply.applySingle(ISIN).orElseThrow();
        var retry = apply.applySingle(ISIN).orElseThrow();
        assertThat(first.status()).isEqualTo(NseEtfUniverseApplyService.Status.COMPLETED);
        assertThat(retry.globalInstrumentId()).isEqualTo(first.globalInstrumentId());
        em.flush(); em.clear();
        assertThat(masters.count()).isEqualTo(2); assertThat(mappings.count()).isEqualTo(1);
        assertThat(masters.findByNormalizedIsin("INF732E01045")).isEmpty();
        assertThat(masters.findById(first.globalInstrumentId()).orElseThrow().getAssetType()).isEqualTo(AssetType.ETF);
        assertThat(mappings.findByInstrumentId(first.globalInstrumentId())).singleElement().satisfies(mapping -> {
            assertThat(mapping.getProviderSymbol()).isEqualTo("NIFTYBEES");
            assertThat(mapping.getProviderInstrumentId()).isEqualTo(ISIN);
            assertThat(mapping.getStatus()).isEqualTo("VERIFIED");
            assertThat(mapping.getResolutionSource()).isEqualTo("NSE_OFFICIAL_ETF_BOOTSTRAP");
        });
        assertThat(jdbc.queryForList("select * from portfolio_positions")).isEqualTo(positions);
        assertThat(jdbc.queryForList("select * from portfolios")).isEqualTo(portfolios);
        assertThat(jdbc.queryForList("select * from instruments")).isEqualTo(instruments);
        assertThat(jdbc.queryForList("select * from instrument_master where asset_type='EQUITY'")).isEqualTo(equities);
    }

    @Test void existingEquityIsRejectedRatherThanReclassified() {
        var equity = new InstrumentMasterEntity(UUID.randomUUID(), ISIN, "Preserve", AssetType.EQUITY, "INR", "IN", "NSE", "NIFTYBEES", "ACTIVE", Instant.now());
        em.persist(equity); em.flush();
        var before = jdbc.queryForList("select * from instrument_master");
        assertThatThrownBy(() -> service.canonicalizeSelectedOfficialEtf(ISIN, "NIFTYBEES", "Official ETF")).isInstanceOf(IllegalStateException.class);
        assertThat(jdbc.queryForList("select * from instrument_master")).isEqualTo(before);
        assertThat(mappings.count()).isZero();
    }

    @org.junit.jupiter.params.ParameterizedTest
    @org.junit.jupiter.params.provider.ValueSource(strings={"MISSING", "INVALID", "WRONG_ISIN", "WRONG_SYMBOL", "WRONG_CURRENCY"})
    void incompleteExistingEtfIsRejectedWithoutImplicitRepair(String variant) {
        var master = new InstrumentMasterEntity(UUID.randomUUID(), ISIN, "Preserve", AssetType.ETF,
                "INR", "IN", "NSE", "NIFTYBEES", "ACTIVE", Instant.now());
        em.persist(master);
        if (!variant.equals("MISSING")) em.persist(new InstrumentProviderMappingEntity(UUID.randomUUID(), master.getInstrumentId(),
                "NSE", variant.equals("WRONG_SYMBOL") ? "OTHER" : "NIFTYBEES",
                variant.equals("WRONG_ISIN") ? "INF732E01045" : ISIN, "NSE",
                variant.equals("WRONG_CURRENCY") ? "USD" : "INR", variant.equals("INVALID") ? "INVALID" : "VERIFIED",
                "EXISTING", BigDecimal.ONE, Instant.now()));
        em.flush();
        var beforeMaster = jdbc.queryForList("select * from instrument_master");
        var beforeMappings = jdbc.queryForList("select * from instrument_provider_mappings");
        assertThatThrownBy(() -> service.canonicalizeSelectedOfficialEtf(ISIN, "NIFTYBEES", "Official ETF"))
                .isInstanceOf(IllegalStateException.class);
        assertThat(jdbc.queryForList("select * from instrument_master")).isEqualTo(beforeMaster);
        assertThat(jdbc.queryForList("select * from instrument_provider_mappings")).isEqualTo(beforeMappings);
    }

    @Test
    @org.springframework.transaction.annotation.Transactional(propagation = org.springframework.transaction.annotation.Propagation.NOT_SUPPORTED)
    void failedPostconditionRollsBackNewMasterAndMappingTogether() {
        // Fault injection below the API selector: the legacy canonicalizer
        // normalizes case, but the selected-operation postcondition requires
        // exact verified identity. Failure occurs after both inserts.
        assertThatThrownBy(() -> service.canonicalizeSelectedOfficialEtf(ISIN.toLowerCase(java.util.Locale.ROOT), "NIFTYBEES", "ETF"))
                .isInstanceOf(IllegalStateException.class);
        assertThat(masters.count()).isZero();
        assertThat(mappings.count()).isZero();
    }
}
