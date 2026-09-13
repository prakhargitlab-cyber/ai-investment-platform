package com.aiinvestment.research;

import org.junit.jupiter.api.Test;
import org.flywaydb.core.Flyway;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

@SpringBootTest
@ActiveProfiles("test")
class ResearchFlywayMigrationTest {
    @Autowired
    private JdbcTemplate jdbcTemplate;

    @Test
    void flywayMigratesResearchSchemaFromEmptyDatabase() {
        List<String> tables = jdbcTemplate.queryForList(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'research'
                ORDER BY table_name
                """,
                String.class
        );

        assertThat(tables).contains(
                "flyway_schema_history_research",
                "research_documents",
                "research_events",
                "research_event_sources",
                "research_refresh_runs",
                "research_acquisition_observations",
                "global_shareholding_snapshots",
                "global_shareholding_snapshot_values",
                "global_financial_facts",
                "global_structured_market_snapshots",
                "global_market_price_observations",
                "global_daily_market_bars",
                "global_stock_rule_engine_results",
                "market_trading_schedules",
                "market_trading_calendar_exceptions"
        );
        assertThat(tables).doesNotContain("research_refresh_jobs");

        List<String> financialFactColumns = jdbcTemplate.queryForList(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'research' AND table_name = 'global_financial_facts'",
                String.class
        );
        assertThat(financialFactColumns).contains(
                "instrument_id", "metric", "period_end", "period_type", "reporting_basis", "fact_value",
                "source_provider", "source_identity", "source_url", "source_mode", "source_tier"
        );

        List<String> documentColumns = jdbcTemplate.queryForList(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'research' AND table_name = 'research_documents'",
                String.class
        );
        assertThat(documentColumns).contains("document_subtype");

        List<String> marketPriceColumns = jdbcTemplate.queryForList(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'research' AND table_name = 'global_market_price_observations'",
                String.class
        );
        assertThat(marketPriceColumns).contains(
                "instrument_id", "observed_at", "price", "currency",
                "provider", "source_url", "retrieved_at"
        );

        List<String> ruleEngineColumns = jdbcTemplate.queryForList(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'research' AND table_name = 'global_stock_rule_engine_results'",
                String.class
        );
        assertThat(ruleEngineColumns).contains(
                "global_instrument_id", "rule_engine_version", "input_fingerprint",
                "calculated_at", "input_as_of", "overall_score", "quality_score",
                "opportunity_score", "risk_score", "confidence_score",
                "decision_signal", "partial", "result_json"
        );

        String version = jdbcTemplate.queryForObject(
                """
                SELECT version
                FROM research.flyway_schema_history_research
                WHERE success = TRUE
                ORDER BY installed_rank DESC
                LIMIT 1
                """,
                String.class
        );
        assertThat(version).isEqualTo("11");

        Integer nseSessions = jdbcTemplate.queryForObject(
                "SELECT count(*) FROM research.market_trading_schedules WHERE market_code = 'NSE'", Integer.class
        );
        assertThat(nseSessions).isEqualTo(5);
        List<String> nseSchedule = jdbcTemplate.queryForList(
                "SELECT mic || '|' || timezone || '|' || regular_open_time || '|' || regular_close_time "
                        + "FROM research.market_trading_schedules WHERE market_code = 'NSE'", String.class
        );
        assertThat(nseSchedule).allSatisfy(value -> assertThat(value).isEqualTo("XNSE|Asia/Kolkata|09:15:00|15:30:00"));
        Integer exceptions = jdbcTemplate.queryForObject(
                "SELECT count(*) FROM research.market_trading_calendar_exceptions", Integer.class
        );
        assertThat(exceptions).isZero();
    }

    @Test
    void dailyBarBootstrapCreatesKeysIndexesDateTypesAndConstraints() throws Exception {
        try (var connection = jdbcTemplate.getDataSource().getConnection()) {
            var keys = new java.util.TreeMap<Short, String>();
            try (var rows = connection.getMetaData().getPrimaryKeys(null, "research", "global_daily_market_bars")) {
                while (rows.next()) keys.put(rows.getShort("KEY_SEQ"), rows.getString("COLUMN_NAME"));
            }
            assertThat(keys.values()).containsExactly("global_instrument_id", "trading_date", "provider");
            var indexes = new java.util.HashSet<String>();
            try (var rows = connection.getMetaData().getIndexInfo(null, "research", "global_daily_market_bars", false, false)) {
                while (rows.next()) indexes.add(rows.getString("INDEX_NAME"));
            }
            assertThat(indexes).contains("idx_daily_market_bars_date_instrument");
            try (var rows = connection.getMetaData().getColumns(null, "research", "global_daily_market_bars", "trading_date")) {
                assertThat(rows.next()).isTrue();
                assertThat(rows.getInt("DATA_TYPE")).isEqualTo(java.sql.Types.DATE);
            }
        }
        String insert = """
            INSERT INTO research.global_daily_market_bars
                (global_instrument_id,trading_date,provider,currency,source_mode,source_url,retrieved_at,high_price,low_price,volume)
            VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',DATE '2026-09-01','TEST','INR','REAL','https://example.test',CURRENT_TIMESTAMP,?,?,?)
            """;
        assertThatThrownBy(() -> jdbcTemplate.update(insert, 10, 20, 1)).isInstanceOf(org.springframework.dao.DataAccessException.class);
        assertThatThrownBy(() -> jdbcTemplate.update(insert, 20, 10, -1)).isInstanceOf(org.springframework.dao.DataAccessException.class);
        jdbcTemplate.update(insert, 20, 10, 0);
        assertThatThrownBy(() -> jdbcTemplate.update(insert, 20, 10, 0)).isInstanceOf(org.springframework.dao.DataAccessException.class);
        jdbcTemplate.update("DELETE FROM research.global_daily_market_bars WHERE global_instrument_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'");
    }

    @Test
    void existingVersionTenUpgradesAndRepeatedMigrationIsANoOp() {
        // Optional disposable PostgreSQL database lets the same upgrade test run
        // against the production dialect without changing application bootstrap.
        String url = System.getProperty("dailyBarsUpgradeJdbcUrl",
                "jdbc:h2:mem:dailyBarUpgrade;DB_CLOSE_DELAY=-1;MODE=PostgreSQL;DATABASE_TO_LOWER=TRUE");
        String username = System.getProperty("dailyBarsUpgradeUsername", "sa");
        String password = System.getProperty("dailyBarsUpgradePassword", "");
        Flyway before = Flyway.configure().dataSource(url, username, password)
                .schemas("research").defaultSchema("research").table("flyway_schema_history_research")
                .target("10").load();
        before.migrate();
        JdbcTemplate existing = new JdbcTemplate(new org.springframework.jdbc.datasource.DriverManagerDataSource(url, username, password));
        existing.update("""
            INSERT INTO research.global_market_price_observations
                (instrument_id,observed_at,price,provider,source_url,retrieved_at)
            VALUES ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',CURRENT_TIMESTAMP,100,'EXISTING','https://example.test',CURRENT_TIMESTAMP)
            """);
        Flyway upgrade = Flyway.configure().dataSource(url, username, password)
                .schemas("research").defaultSchema("research").table("flyway_schema_history_research").load();
        assertThat(upgrade.migrate().migrationsExecuted).isEqualTo(1);
        assertThat(upgrade.migrate().migrationsExecuted).isZero();
        assertThat(existing.queryForObject("SELECT COUNT(*) FROM research.global_daily_market_bars", Integer.class)).isZero();
        assertThat(existing.queryForObject("SELECT COUNT(*) FROM research.global_market_price_observations", Integer.class)).isEqualTo(1);
    }
}

