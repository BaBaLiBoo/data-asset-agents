CREATE TABLE IF NOT EXISTS evaluation_run (
    run_id UUID PRIMARY KEY,
    run_kind VARCHAR(16) NOT NULL CHECK (run_kind IN ('smoke', 'live')),
    query_mode VARCHAR(32) NOT NULL,
    strategy_variant VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL CHECK (status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED')),
    benchmark_version VARCHAR(128) NOT NULL,
    database_snapshot_hash CHAR(64) NOT NULL,
    model_name VARCHAR(256) NOT NULL,
    payload JSONB NOT NULL,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_evaluation_run_compare
    ON evaluation_run (run_kind, benchmark_version, database_snapshot_hash, model_name);

CREATE TABLE IF NOT EXISTS evaluation_case_result (
    run_id UUID NOT NULL REFERENCES evaluation_run(run_id) ON DELETE CASCADE,
    case_id VARCHAR(160) NOT NULL,
    status VARCHAR(16) NOT NULL CHECK (status IN ('COMPLETED', 'FAILED')),
    predicted_status VARCHAR(64) NOT NULL,
    success BOOLEAN NOT NULL,
    failure_category VARCHAR(64),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id, case_id)
);

CREATE INDEX IF NOT EXISTS idx_evaluation_case_failure
    ON evaluation_case_result (run_id, failure_category, success);

CREATE TABLE IF NOT EXISTS physical_rag_build (
    build_id UUID PRIMARY KEY,
    source_hash CHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL CHECK (status IN ('BUILDING', 'READY', 'FAILED')),
    document_count INTEGER NOT NULL DEFAULT 0,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS physical_rag_document (
    build_id UUID NOT NULL REFERENCES physical_rag_build(build_id) ON DELETE CASCADE,
    document_id VARCHAR(160) NOT NULL,
    document_type VARCHAR(32) NOT NULL,
    search_text TEXT NOT NULL,
    embedding vector(1024),
    payload JSONB NOT NULL,
    PRIMARY KEY (build_id, document_id)
);
