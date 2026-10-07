-- Manual-evidence provenance ledger (app/manual_evidence.py ManualEvidenceIngestor).
--
-- This table was referenced by the Python persistence layer
-- (app/persistence.py SqliteResearchPersistence.upsert_manual_evidence_draft /
-- load_manual_evidence_drafts) from the moment the manual-evidence feature was
-- introduced, but no corresponding Flyway migration was ever added for the
-- PostgreSQL-backed deployment target. The SQLite dev/test backend self-creates
-- this table inline (CREATE TABLE IF NOT EXISTS), which masked the gap until a
-- real PostgreSQL-backed request hit "UndefinedTable: relation
-- global_manual_evidence does not exist".
--
-- Records one row per accepted manual-evidence upload (any evidence type:
-- SHAREHOLDING, CURRENT_NEWS, LATEST_PRICE, HISTORICAL_PRICE_SERIES,
-- VALUATION_INPUTS, SECTOR_MACRO, ...), used to deduplicate re-uploads of the
-- same file for the same evidence type and to list prior accepted drafts.
--
-- Column shapes mirror the existing SQLite schema 1:1, typed per this
-- repository's established PostgreSQL conventions (UUID identifiers, CHAR(64)
-- for sha256 hex digests as used by research_documents.content_hash,
-- TIMESTAMP WITH TIME ZONE for instants).
CREATE TABLE global_manual_evidence (
    draft_id UUID PRIMARY KEY,
    evidence_type VARCHAR(40) NOT NULL,
    content_hash CHAR(64) NOT NULL,
    original_filename VARCHAR(500) NOT NULL,
    content_type VARCHAR(120) NOT NULL,
    instrument_id UUID,
    reporting_period TIMESTAMP WITH TIME ZONE,
    extraction_method VARCHAR(80) NOT NULL,
    extraction_results TEXT NOT NULL,
    validation_results TEXT NOT NULL,
    corrections TEXT NOT NULL,
    accepted_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT uq_global_manual_evidence_content_hash_type UNIQUE (content_hash, evidence_type)
);

CREATE INDEX idx_global_manual_evidence_instrument
    ON global_manual_evidence (instrument_id);
CREATE INDEX idx_global_manual_evidence_content_hash
    ON global_manual_evidence (content_hash);
