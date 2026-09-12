package com.aiinvestment.research;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

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
        assertThat(version).isEqualTo("10");

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
}

