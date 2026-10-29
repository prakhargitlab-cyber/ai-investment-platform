package com.aiinvestment.research;

import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.Test;
import org.flywaydb.core.Flyway;
import org.springframework.jdbc.core.JdbcTemplate;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Opt-in, PostgreSQL-only Flyway integration test for the V23 -> V24
 * ETF_ONBOARD checkpoint-phase migration.
 *
 * Deliberately a PLAIN JUnit 5 test class (no @SpringBootTest, no Spring
 * context, no @Autowired dependencies): every method below builds its own
 * standalone JDBC/Flyway setup exactly like
 * ResearchFlywayMigrationTest#existingVersionTenUpgradesAndRepeatedMigrationIsANoOp.
 * This is intentional and load-bearing -- {@link ResearchFlywayMigrationTest}
 * is annotated with @SpringBootTest, which creates a full Spring application
 * context (including Spring Boot's automatic Flyway-against-H2 migration) for
 * EVERY test method in that class regardless of Maven's -Dtest=Class#method
 * filtering. H2 cannot execute V12's PostgreSQL-specific TIMESTAMPTZ usage,
 * so that automatic H2 migration fails before this method's own
 * Assumptions.assumeTrue(...) opt-out ever gets a chance to run. Keeping
 * this method in its own Spring-free class lets it be skipped cleanly (via
 * the same Assumptions.assumeTrue opt-out, unchanged) when no disposable
 * PostgreSQL instance is supplied, with no Spring/H2 context created at all.
 */
class ResearchFlywayEtfOnboardMigrationTest {

    @Test
    void etfOnboardMigrationUpgradesAndPreservesCheckpoints() {
        // Opt-in, PostgreSQL-only: V21/V23/V24 add PL/pgSQL-incompatible or
        // dialect-specific constraint rewrites that H2 (MODE=PostgreSQL)
        // cannot execute faithfully, exactly like existingVersionTenUpgradesAndRepeatedMigrationIsANoOp
        // above. Run against a disposable PostgreSQL instance only -- this
        // test never touches the main or ETF-test application databases;
        // it only ever sees the JDBC URL handed to it via system property.
        Assumptions.assumeTrue(System.getProperty("etfOnboardMigrationJdbcUrl") != null,
                "ETF_ONBOARD phase-constraint migration (V23 -> V24) requires a disposable PostgreSQL instance: "
                        + "set -DetfOnboardMigrationJdbcUrl=<jdbc:postgresql://...>");
        String url = System.getProperty("etfOnboardMigrationJdbcUrl");
        if (url == null || !url.startsWith("jdbc:postgresql:")) {
            throw new IllegalStateException(
                    "etfOnboardMigrationJdbcUrl must be a jdbc:postgresql:// URL pointing at a disposable "
                            + "PostgreSQL instance (H2 cannot execute V21/V23/V24's constraint rewrites); got: " + url);
        }
        String username = System.getProperty("etfOnboardMigrationUsername", "postgres");
        String password = System.getProperty("etfOnboardMigrationPassword", "");

        // Fail-closed instance-identity gate: checked BEFORE any Flyway call
        // or schema-touching JDBC use. A JDBC URL, database name, or
        // localhost port is never sufficient proof that this is really a
        // disposable instance distinct from the main or ETF-test databases
        // -- both properties below are REQUIRED (no defaults) once the
        // opt-in itself is engaged, so a bare etfOnboardMigrationJdbcUrl can
        // no longer run this test on its own. scripts/database/validate-
        // research-migration.ps1 is the only intended caller: it
        // independently queries the real system_identifier of the main and
        // ETF-test PostgreSQL instances (never derived from this test's own
        // target) and passes them here for this test to independently
        // re-derive and re-check against the SAME candidate connection --
        // neither side trusts the other's verdict, only its own query.
        String forbiddenIdsProperty = System.getProperty("etfOnboardMigrationForbiddenSystemIdentifiers");
        String approvedId = System.getProperty("etfOnboardMigrationApprovedSystemIdentifier");
        if (forbiddenIdsProperty == null || forbiddenIdsProperty.isBlank()) {
            throw new IllegalStateException(
                    "etfOnboardMigrationForbiddenSystemIdentifiers is required once etfOnboardMigrationJdbcUrl is "
                            + "set: the real system_identifier of the main and ETF-test PostgreSQL instances, "
                            + "independently obtained (never derived from the candidate URL itself). "
                            + "Refusing to run without it -- a URL, database name, or port is not sufficient proof "
                            + "of isolation.");
        }
        if (approvedId == null || approvedId.isBlank()) {
            throw new IllegalStateException(
                    "etfOnboardMigrationApprovedSystemIdentifier is required once etfOnboardMigrationJdbcUrl is "
                            + "set: the pre-approved disposable instance's own system_identifier. Refusing to run "
                            + "without an explicit, independently-verified approval for this specific instance.");
        }
        java.util.Set<String> forbiddenIds = java.util.Arrays.stream(forbiddenIdsProperty.split(","))
                .map(String::trim).filter(s -> !s.isEmpty()).collect(java.util.stream.Collectors.toSet());
        if (forbiddenIds.isEmpty()) {
            throw new IllegalStateException(
                    "etfOnboardMigrationForbiddenSystemIdentifiers was set but contained no usable values.");
        }

        // The ONE JDBC connection opened before any Flyway/schema work: a
        // single read-only identity probe, nothing else.
        String actualId;
        try (var identityConnection = java.sql.DriverManager.getConnection(url, username, password);
             var statement = identityConnection.createStatement();
             var rows = statement.executeQuery("SELECT system_identifier FROM pg_control_system()")) {
            if (!rows.next()) {
                throw new IllegalStateException(
                        "Unable to determine the candidate instance's system_identifier (pg_control_system() "
                                + "returned no row). Refusing to proceed without proof this is the approved "
                                + "disposable instance.");
            }
            actualId = String.valueOf(rows.getLong(1));
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(
                    "Unable to query the candidate instance's system_identifier via pg_control_system() -- "
                            + "refusing to proceed without independently verified instance identity.", e);
        }
        if (forbiddenIds.contains(actualId)) {
            throw new IllegalStateException(
                    "Refusing to run: the supplied etfOnboardMigrationJdbcUrl resolves to a FORBIDDEN "
                            + "system_identifier (" + actualId + ") -- this is the SAME physical PostgreSQL "
                            + "instance as the main or existing ETF-test database, whatever its database name or "
                            + "port claims. A port-forward or alias to that cluster is not a disposable instance.");
        }
        if (!actualId.equals(approvedId)) {
            throw new IllegalStateException(
                    "Refusing to run: the candidate instance's system_identifier (" + actualId + ") does not "
                            + "match the pre-approved disposable instance's system_identifier (" + approvedId
                            + "). This is not the instance that was independently verified and approved for "
                            + "this validation run.");
        }

        // Step 1: migrate a fresh disposable database up to the real V23
        // baseline only, via the real Flyway Java API with Research
        // Service's actual schema/history-table configuration.
        Flyway baseline = Flyway.configure().dataSource(url, username, password)
                .schemas("research").defaultSchema("research").table("flyway_schema_history_research")
                .target("23").load();
        baseline.migrate();

        JdbcTemplate target = new JdbcTemplate(
                new org.springframework.jdbc.datasource.DriverManagerDataSource(url, username, password));

        // Step 2: confirm V23 is really where we landed before asserting
        // anything about its constraints.
        String versionAfterBaseline = target.queryForObject("""
                SELECT version FROM research.flyway_schema_history_research
                WHERE success = TRUE ORDER BY installed_rank DESC LIMIT 1
                """, String.class);
        assertThat(versionAfterBaseline).isEqualTo("23");

        // Step 3: valid parent cycle + BASELINE/DEEP checkpoint fixtures,
        // satisfying every NOT NULL column and foreign key the real V23
        // schema requires -- never a bare single-column insert, so a
        // rejection can only ever be attributed to the phase CHECK itself.
        String cycleId = "etf-onboard-validation-" + java.util.UUID.randomUUID();
        String nowIso = java.time.Instant.now().toString();
        target.update("""
                INSERT INTO research.global_opportunity_cycle_run
                    (cycle_id, market, status, parameters, created_at, updated_at)
                VALUES (?, 'NSE', 'RUNNING', '{}', ?, ?)
                """, cycleId, nowIso, nowIso);

        String baselineInstrumentId = "11111111-1111-1111-1111-111111111111";
        String deepInstrumentId = "22222222-2222-2222-2222-222222222222";
        target.update("""
                INSERT INTO research.global_opportunity_cycle_progress
                    (cycle_id, phase, global_instrument_id, state, attempts, payload, updated_at)
                VALUES (?, 'BASELINE', ?, 'COMPLETED', 1, '{"a":1}', ?)
                """, cycleId, baselineInstrumentId, nowIso);
        target.update("""
                INSERT INTO research.global_opportunity_cycle_progress
                    (cycle_id, phase, global_instrument_id, state, attempts, payload, updated_at)
                VALUES (?, 'DEEP', ?, 'COMPLETED', 1, '{"b":2}', ?)
                """, cycleId, deepInstrumentId, nowIso);

        // Step 4: capture the fixture rows' complete, stable values before
        // V24 touches anything.
        List<java.util.Map<String, Object>> checkpointsBeforeV24 = target.queryForList("""
                SELECT cycle_id, phase, global_instrument_id, state, attempts, payload, updated_at
                FROM research.global_opportunity_cycle_progress
                WHERE cycle_id = ? ORDER BY phase
                """, cycleId);
        assertThat(checkpointsBeforeV24).hasSize(2);

        // Step 5: ETF_ONBOARD must be rejected before V24, and specifically
        // by the phase CHECK constraint -- SQLSTATE 23514 and the exact
        // constraint name -- never inferred from a generic nonzero result.
        assertPhaseCheckViolation(target, cycleId, "ETF_ONBOARD", "pre-v24-probe");

        // Step 6: apply V24 via real Flyway -- exactly one new migration,
        // recorded as successful in the real schema-history table.
        Flyway upgraded = Flyway.configure().dataSource(url, username, password)
                .schemas("research").defaultSchema("research").table("flyway_schema_history_research").load();
        assertThat(upgraded.migrate().migrationsExecuted).isEqualTo(1);
        String versionAfterUpgrade = target.queryForObject("""
                SELECT version FROM research.flyway_schema_history_research
                WHERE success = TRUE ORDER BY installed_rank DESC LIMIT 1
                """, String.class);
        assertThat(versionAfterUpgrade).isEqualTo("24");

        // Step 7: the BASELINE/DEEP rows inserted before V24 must be
        // byte-for-byte unchanged.
        List<java.util.Map<String, Object>> checkpointsAfterV24 = target.queryForList("""
                SELECT cycle_id, phase, global_instrument_id, state, attempts, payload, updated_at
                FROM research.global_opportunity_cycle_progress
                WHERE cycle_id = ? ORDER BY phase
                """, cycleId);
        assertThat(checkpointsAfterV24).isEqualTo(checkpointsBeforeV24);

        // Step 8: ETF_ONBOARD must now be accepted.
        String etfOnboardInstrumentId = "33333333-3333-3333-3333-333333333333";
        target.update("""
                INSERT INTO research.global_opportunity_cycle_progress
                    (cycle_id, phase, global_instrument_id, state, attempts, payload, updated_at)
                VALUES (?, 'ETF_ONBOARD', ?, 'COMPLETED', 1, '{"c":3}', ?)
                """, cycleId, etfOnboardInstrumentId, nowIso);
        Integer etfOnboardRowCount = target.queryForObject("""
                SELECT COUNT(*) FROM research.global_opportunity_cycle_progress
                WHERE cycle_id = ? AND phase = 'ETF_ONBOARD'
                """, Integer.class, cycleId);
        assertThat(etfOnboardRowCount).isEqualTo(1);

        // Step 9: an unrecognized phase must still be rejected by the same
        // named constraint after V24 widened it.
        assertPhaseCheckViolation(target, cycleId, "NOT_A_REAL_PHASE", "post-v24-unknown-phase");

        // Step 10: re-invoking Flyway against the now-V24 database must be
        // a true no-op per Flyway's own schema-history bookkeeping -- never
        // a second raw execution of V24's SQL text.
        assertThat(upgraded.migrate().migrationsExecuted).isZero();
    }

    private static void assertPhaseCheckViolation(JdbcTemplate target, String cycleId, String phase, String probeLabel) {
        String probeInstrumentId = java.util.UUID.randomUUID().toString();
        try {
            target.update("""
                    INSERT INTO research.global_opportunity_cycle_progress
                        (cycle_id, phase, global_instrument_id, state, attempts, payload, updated_at)
                    VALUES (?, ?, ?, 'COMPLETED', 1, '{}', ?)
                    """, cycleId, phase, probeInstrumentId, java.time.Instant.now().toString());
        } catch (org.springframework.dao.DataIntegrityViolationException e) {
            Throwable cause = e.getMostSpecificCause();
            assertThat(cause)
                    .as("%s: expected a real PostgreSQL check-constraint violation, not some other failure mode", probeLabel)
                    .isInstanceOf(org.postgresql.util.PSQLException.class);
            org.postgresql.util.PSQLException psql = (org.postgresql.util.PSQLException) cause;
            assertThat(psql.getSQLState())
                    .as("%s: must be rejected by SQLSTATE 23514 (check_violation), not some other error", probeLabel)
                    .isEqualTo("23514");
            assertThat(psql.getServerErrorMessage())
                    .as("%s: PostgreSQL must report which constraint fired", probeLabel)
                    .isNotNull();
            assertThat(psql.getServerErrorMessage().getConstraint())
                    .as("%s: must be rejected specifically by the phase CHECK constraint, not an incidental one", probeLabel)
                    .isEqualTo("global_opportunity_cycle_progress_phase_check");
            return;
        }
        throw new AssertionError(probeLabel + ": expected phase '" + phase + "' to be rejected by "
                + "global_opportunity_cycle_progress_phase_check, but the insert unexpectedly succeeded.");
    }
}
