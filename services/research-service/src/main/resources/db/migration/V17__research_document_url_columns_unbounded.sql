-- CURRENT_NEWS can legitimately discover article URLs longer than 1000
-- characters (aggregator/redirect links with long encoded query strings).
-- Persisting those URLs unchanged previously raised
-- psycopg.errors.StringDataRightTruncation (SQLSTATE 22001) on INSERT,
-- because canonical_url / original_url / source_url were VARCHAR(1000) on
-- research_documents, source_url was VARCHAR(1000) on research_events, and
-- source_url / canonical_url were VARCHAR(1000) on research_event_sources
-- (upsert_event() unconditionally writes the event's source URL into
-- research_event_sources too, via _upsert_event_sources() -- the same
-- complete URL is carried by all three tables).
--
-- Widening to TEXT (unbounded) rather than a larger VARCHAR(n) avoids
-- reintroducing the same class of defect at a different threshold: a URL
-- must never be truncated, since that would change resource identity and
-- break provenance, deduplication (ux_research_documents_canonical_url) and
-- future reuse. No data is rewritten; existing values are preserved as-is
-- and TEXT is binary-compatible with varchar storage in PostgreSQL, so this
-- is a metadata-only change. Indexes/uniqueness/constraints (including
-- ux_research_events_fingerprint, the research_events FK, and the
-- research_event_sources FKs / unique constraint) are untouched.
ALTER TABLE research.research_documents
    ALTER COLUMN canonical_url TYPE TEXT,
    ALTER COLUMN original_url TYPE TEXT,
    ALTER COLUMN source_url TYPE TEXT;

ALTER TABLE research.research_events
    ALTER COLUMN source_url TYPE TEXT;

ALTER TABLE research.research_event_sources
    ALTER COLUMN source_url TYPE TEXT,
    ALTER COLUMN canonical_url TYPE TEXT;
