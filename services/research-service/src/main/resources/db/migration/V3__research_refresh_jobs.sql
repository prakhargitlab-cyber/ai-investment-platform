CREATE TABLE research_refresh_jobs (
    job_id UUID PRIMARY KEY,
    portfolio_id UUID NOT NULL,
    requested_by VARCHAR(240) NOT NULL,
    status VARCHAR(40) NOT NULL,
    total_instruments INTEGER NOT NULL,
    completed_instruments INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    unchanged_count INTEGER NOT NULL DEFAULT 0,
    partial_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    last_progress_at TIMESTAMP,
    safe_error_code VARCHAR(120),
    safe_error_message VARCHAR(500)
);

CREATE INDEX idx_research_refresh_jobs_active
    ON research_refresh_jobs (portfolio_id, requested_by, status, created_at DESC);
