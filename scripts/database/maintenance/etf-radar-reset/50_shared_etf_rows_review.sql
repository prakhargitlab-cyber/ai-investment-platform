-- READ-ONLY. Review of ETF-id rows in the five shared tables that 20_reset.sql deliberately does NOT delete.
-- psql -f 50_shared_etf_rows_review.sql   (cwd = this directory, with etf_ids.csv)
-- Findings that drive the decision:
--  * The normal ETF cycle never writes these tables (it writes etf_* tables and global_daily_market_bars only).
--    ETF rows can exist only from operator-driven manual-evidence / official-document ingestion for ETF ids.
--  * research_documents is referenced by research_events, research_event_sources, global_shareholding_snapshots and
--    research_event_impact_features (immutable). A document with any dependent MUST NOT be deleted.
\i _scope.sql
\echo == ETF-id rows per shared table
SELECT 'research_documents' t, count(*) FROM research_documents WHERE instrument_id::text IN (SELECT instrument_id FROM etf_scope)
UNION ALL SELECT 'global_manual_evidence', count(*) FROM global_manual_evidence WHERE instrument_id::text IN (SELECT instrument_id FROM etf_scope)
UNION ALL SELECT 'global_market_price_observations', count(*) FROM global_market_price_observations WHERE instrument_id::text IN (SELECT instrument_id FROM etf_scope)
UNION ALL SELECT 'global_structured_market_snapshots', count(*) FROM global_structured_market_snapshots WHERE instrument_id::text IN (SELECT instrument_id FROM etf_scope)
UNION ALL SELECT 'research_acquisition_observations', count(*) FROM research_acquisition_observations WHERE instrument_id::text IN (SELECT instrument_id FROM etf_scope);
\echo == ETF documents with dependents (these must be kept)
SELECT d.document_id,
  (SELECT count(*) FROM research_events e WHERE e.source_document_id = d.document_id) AS events,
  (SELECT count(*) FROM research_event_sources s WHERE s.document_id = d.document_id) AS event_sources,
  (SELECT count(*) FROM global_shareholding_snapshots h WHERE h.research_document_id = d.document_id) AS shareholding,
  (SELECT count(*) FROM research_event_impact_features f WHERE f.source_document_id = d.document_id) AS impact_features,
  (SELECT count(*) FROM research_documents x WHERE x.duplicate_of_document_id = d.document_id) AS duplicates
FROM research_documents d WHERE d.instrument_id::text IN (SELECT instrument_id FROM etf_scope)
ORDER BY 1;
\echo == Documents shared across instruments by content hash (never delete when another instrument shares the hash)
SELECT d.content_hash, count(DISTINCT d.instrument_id) AS instruments
FROM research_documents d
WHERE d.content_hash IN (SELECT content_hash FROM research_documents WHERE instrument_id::text IN (SELECT instrument_id FROM etf_scope))
GROUP BY 1 HAVING count(DISTINCT d.instrument_id) > 1;
