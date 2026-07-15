CREATE TABLE IF NOT EXISTS metadata_snapshot (
    snapshot_id VARCHAR(160) PRIMARY KEY,
    schema_name VARCHAR(100) NOT NULL,
    source VARCHAR(50) NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS metadata_table_snapshot (
    snapshot_id VARCHAR(160) NOT NULL REFERENCES metadata_snapshot(snapshot_id) ON DELETE CASCADE,
    table_name VARCHAR(200) NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (snapshot_id, table_name)
);

CREATE TABLE IF NOT EXISTS column_profile (
    snapshot_id VARCHAR(160) NOT NULL REFERENCES metadata_snapshot(snapshot_id) ON DELETE CASCADE,
    table_name VARCHAR(200) NOT NULL,
    column_name VARCHAR(200) NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (snapshot_id, table_name, column_name)
);

CREATE TABLE IF NOT EXISTS historical_sql_analysis (
    analysis_id VARCHAR(160) PRIMARY KEY,
    snapshot_id VARCHAR(160) NOT NULL REFERENCES metadata_snapshot(snapshot_id) ON DELETE CASCADE,
    certified BOOLEAN NOT NULL DEFAULT false,
    payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS ontology_candidate (
    candidate_id VARCHAR(500) PRIMARY KEY,
    snapshot_id VARCHAR(160) NOT NULL REFERENCES metadata_snapshot(snapshot_id) ON DELETE CASCADE,
    candidate_type VARCHAR(30) NOT NULL CHECK (candidate_type IN ('concept', 'mapping', 'join')),
    status VARCHAR(30) NOT NULL CHECK (status IN ('CANDIDATE', 'VERIFIED', 'REJECTED')),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at TIMESTAMPTZ,
    reviewer VARCHAR(100),
    review_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_ontology_candidate_status
    ON ontology_candidate(status, candidate_type);

CREATE TABLE IF NOT EXISTS candidate_review (
    review_id BIGSERIAL PRIMARY KEY,
    candidate_id VARCHAR(500) NOT NULL REFERENCES ontology_candidate(candidate_id),
    from_status VARCHAR(30) NOT NULL,
    to_status VARCHAR(30) NOT NULL,
    reviewer VARCHAR(100) NOT NULL,
    review_note TEXT NOT NULL DEFAULT '',
    reviewed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS ontology_version (
    version_id VARCHAR(160) PRIMARY KEY,
    version VARCHAR(50) NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    source_snapshot_id VARCHAR(160) REFERENCES metadata_snapshot(snapshot_id),
    status VARCHAR(30) NOT NULL DEFAULT 'PUBLISHED',
    concept_count INTEGER NOT NULL,
    mapping_count INTEGER NOT NULL,
    join_count INTEGER NOT NULL,
    published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_by VARCHAR(100) NOT NULL,
    is_current BOOLEAN NOT NULL DEFAULT true,
    bundle_json JSONB NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_current_ontology_version
    ON ontology_version(is_current) WHERE is_current;

CREATE TABLE IF NOT EXISTS published_semantic_concept (
    version_id VARCHAR(160) NOT NULL REFERENCES ontology_version(version_id) ON DELETE CASCADE,
    concept_id VARCHAR(200) NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (version_id, concept_id)
);

CREATE TABLE IF NOT EXISTS published_metric (
    version_id VARCHAR(160) NOT NULL REFERENCES ontology_version(version_id) ON DELETE CASCADE,
    metric_id VARCHAR(200) NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (version_id, metric_id)
);

CREATE TABLE IF NOT EXISTS published_dimension (
    version_id VARCHAR(160) NOT NULL REFERENCES ontology_version(version_id) ON DELETE CASCADE,
    dimension_id VARCHAR(200) NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (version_id, dimension_id)
);

CREATE TABLE IF NOT EXISTS published_semantic_attribute (
    version_id VARCHAR(160) NOT NULL REFERENCES ontology_version(version_id) ON DELETE CASCADE,
    attribute_id VARCHAR(300) NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (version_id, attribute_id)
);

CREATE TABLE IF NOT EXISTS published_physical_mapping (
    version_id VARCHAR(160) NOT NULL REFERENCES ontology_version(version_id) ON DELETE CASCADE,
    concept_id VARCHAR(300) NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (version_id, concept_id)
);

CREATE TABLE IF NOT EXISTS published_semantic_join (
    version_id VARCHAR(160) NOT NULL REFERENCES ontology_version(version_id) ON DELETE CASCADE,
    join_key VARCHAR(500) NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (version_id, join_key)
);

CREATE TABLE IF NOT EXISTS published_table_asset (
    version_id VARCHAR(160) NOT NULL REFERENCES ontology_version(version_id) ON DELETE CASCADE,
    table_name VARCHAR(200) NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (version_id, table_name)
);

CREATE TABLE IF NOT EXISTS published_semantic_rule (
    version_id VARCHAR(160) NOT NULL REFERENCES ontology_version(version_id) ON DELETE CASCADE,
    rule_key VARCHAR(300) NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (version_id, rule_key)
);
