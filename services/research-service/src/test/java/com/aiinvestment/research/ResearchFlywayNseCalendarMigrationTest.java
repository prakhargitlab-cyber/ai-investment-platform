package com.aiinvestment.research;

import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.core.io.ClassPathResource;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.jdbc.datasource.init.ScriptUtils;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;

/** Exercises the actual calendar migration with Flyway and isolated H2 only.
 * V5 supplies the unchanged calendar schema. Unrelated PostgreSQL-only V12+
 * migrations are outside this data migration's focused, offline fixture.
 */
class ResearchFlywayNseCalendarMigrationTest {
    private static final String SEED = "V25__nse_cash_equity_calendar_2025_2026.sql";

    @TempDir
    Path migrations;

    private String prepare() throws Exception {
        for (String name : new String[]{"V5__structured_market_snapshots_and_market_sessions.sql", SEED}) {
            try (var input = new ClassPathResource("db/migration/" + name).getInputStream()) {
                Files.copy(input, migrations.resolve(name));
            }
        }
        // No supplied URL, environment or application datasource can reach this fixture.
        return "jdbc:h2:mem:nse_" + UUID.randomUUID()
                + ";DB_CLOSE_DELAY=-1;MODE=PostgreSQL;DATABASE_TO_LOWER=TRUE";
    }

    private Flyway flyway(String url, String target) {
        return Flyway.configure().dataSource(url, "sa", "")
                .locations("filesystem:" + migrations.toAbsolutePath())
                .schemas("research").defaultSchema("research")
                .table("flyway_schema_history_research").target(target).load();
    }

    @Test
    void freshCalendarHasProvenancedClosuresAndSpecialSessions() throws Exception {
        String url = prepare();
        Flyway migration = flyway(url, "25");
        assertThat(migration.migrate().migrationsExecuted).isEqualTo(2);
        JdbcTemplate jdbc = new JdbcTemplate(new DriverManagerDataSource(url, "sa", ""));
        assertThat(jdbc.queryForObject("SELECT count(*) FROM research.market_trading_calendar_exceptions",
                Integer.class)).isEqualTo(40);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM research.market_trading_calendar_exceptions "
                + "WHERE market_code = 'NSE' AND exception_type = 'CLOSED'", Integer.class)).isEqualTo(36);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM research.market_trading_calendar_exceptions "
                + "WHERE source_reference NOT LIKE 'https://nsearchives.nseindia.com/content/circulars/CMTR%.pdf'",
                Integer.class)).isZero();
        assertThat(jdbc.queryForObject("SELECT exception_type FROM research.market_trading_calendar_exceptions "
                + "WHERE trading_date = DATE '2026-01-15'", String.class)).isEqualTo("CLOSED");
        assertThat(jdbc.queryForObject("SELECT CAST(open_time AS VARCHAR) FROM research.market_trading_calendar_exceptions "
                + "WHERE trading_date = DATE '2025-10-21'", String.class)).isEqualTo("13:45:00");
        assertThat(jdbc.queryForObject("SELECT CAST(close_time AS VARCHAR) FROM research.market_trading_calendar_exceptions "
                + "WHERE trading_date = DATE '2025-10-21'", String.class)).isEqualTo("14:45:00");
        assertThat(jdbc.queryForObject("SELECT exception_type FROM research.market_trading_calendar_exceptions "
                + "WHERE trading_date = DATE '2026-02-01'", String.class)).isEqualTo("SPECIAL_SESSION");
        assertThat(jdbc.queryForObject("SELECT open_time FROM research.market_trading_calendar_exceptions "
                + "WHERE trading_date = DATE '2026-11-08'", java.sql.Time.class)).isNull();
        migration.validate();
        assertThat(migration.migrate().migrationsExecuted).isZero();
    }

    @Test
    void existingCalendarUpgradesOnceAndPreservesOperatorAndOtherMarketExceptions() throws Exception {
        String url = prepare();
        flyway(url, "5").migrate();
        JdbcTemplate jdbc = new JdbcTemplate(new DriverManagerDataSource(url, "sa", ""));
        jdbc.update("INSERT INTO research.market_trading_calendar_exceptions "
                + "(market_code,trading_date,exception_type,open_time,close_time,reason,source_reference) "
                + "VALUES ('NSE','2025-10-21','SPECIAL_SESSION','13:45','14:45','Operator verified','operator-source')");
        jdbc.update("INSERT INTO research.market_trading_calendar_exceptions "
                + "(market_code,trading_date,exception_type,reason) "
                + "VALUES ('NYSE','2026-10-02','CLOSED','Other exchange')");
        Flyway upgrade = flyway(url, "25");
        assertThat(upgrade.migrate().migrationsExecuted).isEqualTo(1);
        assertThat(upgrade.migrate().migrationsExecuted).isZero();
        // Replaying the actual SQL is idempotent even outside Flyway's version guard.
        try (var connection = new DriverManagerDataSource(url, "sa", "").getConnection()) {
            connection.createStatement().execute("SET SCHEMA research");
            ScriptUtils.executeSqlScript(connection, new ClassPathResource("db/migration/" + SEED));
        }
        assertThat(jdbc.queryForObject("SELECT count(*) FROM research.market_trading_calendar_exceptions",
                Integer.class)).isEqualTo(41);
        assertThat(jdbc.queryForObject("SELECT source_reference FROM research.market_trading_calendar_exceptions "
                + "WHERE market_code='NSE' AND trading_date=DATE '2025-10-21'", String.class)).isEqualTo("operator-source");
        assertThat(jdbc.queryForObject("SELECT reason FROM research.market_trading_calendar_exceptions "
                + "WHERE market_code='NYSE'", String.class)).isEqualTo("Other exchange");
        assertThat(jdbc.queryForObject("SELECT count(*) FROM research.market_trading_schedules",
                Integer.class)).isEqualTo(5);
        upgrade.validate();
    }
}
