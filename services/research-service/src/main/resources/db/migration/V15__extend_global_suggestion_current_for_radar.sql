-- Extend global_stock_suggestion_current with the full public-radar
-- action/state detail: real values already computed by
-- prepare_cycle/RecommendationEngineV1 per cycle, previously dropped on
-- write. Mirrors app/opportunity_persistence.py's SQLite schema.
--
-- V14 is already applied against this environment's database, so this
-- widening ships as a forward migration rather than an edit to V14.
--
-- All nine columns are added nullable with no default: existing rows
-- keep NULL until the next opportunity cycle republishes and repopulates
-- them via the normal write path (_upsert_global_current). No backfill of
-- historical rows is performed here -- see the accompanying report for why.
ALTER TABLE research.global_stock_suggestion_current
    ADD COLUMN data_state TEXT,
    ADD COLUMN missing_areas JSON,
    ADD COLUMN stale_areas JSON,
    ADD COLUMN short_term_state TEXT,
    ADD COLUMN long_term_state TEXT,
    ADD COLUMN new_investor_action TEXT,
    ADD COLUMN existing_holder_action TEXT,
    ADD COLUMN current_short_action TEXT,
    ADD COLUMN current_long_action TEXT;
