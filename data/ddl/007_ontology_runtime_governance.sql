-- Object semantic runtime, drift governance, and versioned ontology indexes.
CREATE TABLE IF NOT EXISTS draft_physical_join (
    draft_id VARCHAR(160) NOT NULL REFERENCES ontology_draft(draft_id) ON DELETE CASCADE,
    physical_join_id VARCHAR(160) NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (draft_id, physical_join_id)
);

CREATE TABLE IF NOT EXISTS published_physical_join (
    ontology_version_id VARCHAR(160) NOT NULL
        REFERENCES ontology_version(version_id) ON DELETE CASCADE,
    physical_join_id VARCHAR(160) NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (ontology_version_id, physical_join_id)
);

ALTER TABLE ontology_version_object_resource
    DROP CONSTRAINT IF EXISTS ontology_version_object_resource_resource_type_check;
ALTER TABLE ontology_version_object_resource
    ADD CONSTRAINT ontology_version_object_resource_resource_type_check CHECK (
        resource_type IN ('OBJECT_TYPE', 'PROPERTY', 'LINK_TYPE', 'BINDING', 'PHYSICAL_JOIN')
    );

CREATE TABLE IF NOT EXISTS ontology_sync_run (
    run_id VARCHAR(160) PRIMARY KEY,
    ontology_version_id VARCHAR(160) REFERENCES ontology_version(version_id),
    status VARCHAR(20) NOT NULL CHECK (status IN ('RUNNING', 'READY', 'FAILED')),
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    error_message TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS ontology_schema_drift (
    report_id VARCHAR(160) PRIMARY KEY,
    run_id VARCHAR(160) REFERENCES ontology_sync_run(run_id) ON DELETE CASCADE,
    ontology_version_id VARCHAR(160) NOT NULL REFERENCES ontology_version(version_id),
    binding_id VARCHAR(160) NOT NULL,
    severity VARCHAR(20) NOT NULL CHECK (severity IN ('NONE', 'ADDITIVE', 'BREAKING')),
    inspected_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS ontology_index_build (
    build_id VARCHAR(160) PRIMARY KEY,
    ontology_version_id VARCHAR(160) NOT NULL REFERENCES ontology_version(version_id),
    index_type VARCHAR(30) NOT NULL CHECK (
        index_type IN ('BUSINESS_CONCEPT', 'OBJECT_TYPE', 'PROPERTY', 'LINK_TYPE')
    ),
    status VARCHAR(20) NOT NULL CHECK (status IN ('BUILDING', 'READY', 'FAILED', 'STALE')),
    source_hash VARCHAR(128) NOT NULL,
    embedding_model VARCHAR(200) NOT NULL,
    embedding_dimensions INTEGER NOT NULL CHECK (embedding_dimensions > 0),
    document_count INTEGER NOT NULL DEFAULT 0 CHECK (document_count >= 0),
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    error_message TEXT,
    is_current BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ontology_index_current
    ON ontology_index_build(ontology_version_id, index_type) WHERE is_current;

CREATE TABLE IF NOT EXISTS ontology_search_document (
    ontology_version_id VARCHAR(160) NOT NULL REFERENCES ontology_version(version_id),
    build_id VARCHAR(160) NOT NULL REFERENCES ontology_index_build(build_id) ON DELETE CASCADE,
    resource_type VARCHAR(30) NOT NULL,
    resource_id VARCHAR(200) NOT NULL,
    name VARCHAR(300) NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    synonyms JSONB NOT NULL DEFAULT '[]'::jsonb,
    search_text TEXT NOT NULL,
    embedding VECTOR(1024) NOT NULL,
    PRIMARY KEY (build_id, resource_type, resource_id)
);

CREATE INDEX IF NOT EXISTS idx_ontology_search_document_fts
    ON ontology_search_document USING GIN (to_tsvector('simple', search_text));
CREATE INDEX IF NOT EXISTS idx_ontology_search_document_vector
    ON ontology_search_document USING ivfflat (embedding vector_cosine_ops) WITH (lists = 10);

CREATE TABLE IF NOT EXISTS ontology_draft_change_set (
    draft_id VARCHAR(160) PRIMARY KEY REFERENCES ontology_draft(draft_id) ON DELETE CASCADE,
    calculated_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS ontology_draft_impact (
    draft_id VARCHAR(160) PRIMARY KEY REFERENCES ontology_draft(draft_id) ON DELETE CASCADE,
    calculated_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ontology_sync_run_status
    ON ontology_sync_run(status, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_ontology_drift_version
    ON ontology_schema_drift(ontology_version_id, severity, inspected_at DESC);
CREATE INDEX IF NOT EXISTS idx_ontology_index_ready
    ON ontology_index_build(ontology_version_id, index_type, status, completed_at DESC);
