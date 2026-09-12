package com.aiinvestment.portfolio.application;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.*;

/** Durable work/lease metadata, never an alternative instrument or mapping store. */
@Repository
public class CanonicalIdentityBootstrapStore {
    private final JdbcTemplate jdbc;
    public CanonicalIdentityBootstrapStore(JdbcTemplate jdbc) { this.jdbc = jdbc; }
    public boolean claim(String owner, Instant now) {
        return jdbc.update("UPDATE portfolio.canonical_identity_bootstrap SET lease_owner=?, lease_until=? WHERE region='INDIA' AND lease_until<?",
                owner, Timestamp.from(now.plusSeconds(300)), Timestamp.from(now)) == 1;
    }
    public void release(String owner) {
        jdbc.update("UPDATE portfolio.canonical_identity_bootstrap SET lease_until=? WHERE region='INDIA' AND lease_owner=?", Timestamp.from(Instant.EPOCH), owner);
    }
    public boolean universeDue(Instant now) { return due("universe_loaded_at", now); }
    public boolean referenceDue(Instant now) { return due("reference_loaded_at", now); }
    private boolean due(String column, Instant now) {
        Timestamp value = jdbc.queryForObject("SELECT " + column + " FROM portfolio.canonical_identity_bootstrap WHERE region='INDIA'", Timestamp.class);
        return value == null || value.toInstant().isBefore(now.minusSeconds(86400));
    }
    public void universeLoaded(Instant now) { jdbc.update("UPDATE portfolio.canonical_identity_bootstrap SET universe_loaded_at=? WHERE region='INDIA'", Timestamp.from(now)); }
    public void referenceLoaded(Instant now) { jdbc.update("UPDATE portfolio.canonical_identity_bootstrap SET reference_loaded_at=? WHERE region='INDIA'", Timestamp.from(now)); }
    public void enqueue() {
        jdbc.update("""
            INSERT INTO portfolio.canonical_identity_mapping_jobs(instrument_id,status,next_attempt_at,updated_at)
            SELECT m.instrument_id,'PENDING',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP FROM portfolio.instrument_master m
            WHERE m.country='IN' AND m.primary_exchange='NSE' AND m.asset_type='EQUITY' AND m.status='ACTIVE'
            AND NOT EXISTS (SELECT 1 FROM portfolio.canonical_identity_mapping_jobs j WHERE j.instrument_id=m.instrument_id)
            """);
    }
    public List<UUID> dueMappings(int limit, Instant now) {
        return jdbc.query("""
            SELECT j.instrument_id FROM portfolio.canonical_identity_mapping_jobs j
            LEFT JOIN portfolio.nifty500_universe u ON u.instrument_id=j.instrument_id
            WHERE j.status NOT IN ('VALIDATED','SKIPPED') AND j.next_attempt_at<=?
            ORDER BY CASE WHEN u.instrument_id IS NULL THEN 1 ELSE 0 END, j.next_attempt_at, j.instrument_id LIMIT ?
            """, (rs,n) -> rs.getObject(1, UUID.class), Timestamp.from(now), limit);
    }
    public void completed(UUID id, GlobalInstrumentReconciliationService.Outcome outcome, Instant now) {
        long delay = "UNAVAILABLE".equals(outcome.status()) ? 300 : 86400;
        jdbc.update("UPDATE portfolio.canonical_identity_mapping_jobs SET status=?,reason=?,attempts=attempts+1,next_attempt_at=?,updated_at=? WHERE instrument_id=?",
                outcome.status(), outcome.reason(), Timestamp.from(now.plusSeconds(delay)), Timestamp.from(now), id);
    }
    public Map<String, Object> counts() {
        Map<String,Object> counts = new LinkedHashMap<>();
        counts.put("canonicalUniverseLoaded", jdbc.queryForObject("SELECT count(*) FROM portfolio.instrument_master WHERE country='IN' AND primary_exchange='NSE' AND asset_type='EQUITY' AND status='ACTIVE'", Long.class));
        counts.put("verifiedNseMappings", jdbc.queryForObject("SELECT count(DISTINCT instrument_id) FROM portfolio.instrument_provider_mappings WHERE provider='NSE' AND status='VERIFIED' AND resolution_source<>'BROKER_IMPORT_IDENTITY'", Long.class));
        counts.put("yahooMappingsValidated", jdbc.queryForObject("SELECT count(DISTINCT instrument_id) FROM portfolio.instrument_provider_mappings WHERE provider='YAHOO_FINANCE' AND status='VERIFIED'", Long.class));
        counts.put("yahooMappingsSkipped", jdbc.queryForObject("SELECT count(*) FROM portfolio.canonical_identity_mapping_jobs WHERE status='SKIPPED'", Long.class));
        counts.put("yahooMappingsFailed", jdbc.queryForObject("SELECT count(*) FROM portfolio.canonical_identity_mapping_jobs WHERE status IN ('REJECTED','UNAVAILABLE')", Long.class));
        counts.put("yahooMappingsPending", jdbc.queryForObject("SELECT count(*) FROM portfolio.canonical_identity_mapping_jobs WHERE status='PENDING'", Long.class));
        return counts;
    }
}
