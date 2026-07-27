ALTER TABLE ontology_draft
    ADD COLUMN IF NOT EXISTS construction_run_id VARCHAR(160);
ALTER TABLE ontology_compiled_artifact
    ADD COLUMN IF NOT EXISTS construction_run_id VARCHAR(160);
ALTER TABLE ontology_compiled_artifact
    ADD COLUMN IF NOT EXISTS construction_evidence_summary JSONB NOT NULL
    DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS ontology_construction_run (
    run_id VARCHAR(160) PRIMARY KEY,
    source_snapshot_id VARCHAR(160) NOT NULL,
    status VARCHAR(40) NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ontology_construction_run_status
    ON ontology_construction_run(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS ontology_construction_candidate (
    run_id VARCHAR(160) NOT NULL
        REFERENCES ontology_construction_run(run_id) ON DELETE CASCADE,
    candidate_id VARCHAR(500) NOT NULL,
    resource_type VARCHAR(40) NOT NULL,
    status VARCHAR(40) NOT NULL,
    revision BIGINT NOT NULL DEFAULT 0,
    payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_ontology_construction_candidate_review
    ON ontology_construction_candidate(run_id, resource_type, status);

CREATE TABLE IF NOT EXISTS ontology_construction_candidate_review (
    review_id VARCHAR(160) PRIMARY KEY,
    run_id VARCHAR(160) NOT NULL,
    candidate_id VARCHAR(500) NOT NULL,
    revision BIGINT NOT NULL,
    reviewer VARCHAR(100) NOT NULL,
    decision VARCHAR(40) NOT NULL,
    payload JSONB NOT NULL,
    reviewed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (run_id, candidate_id)
        REFERENCES ontology_construction_candidate(run_id, candidate_id) ON DELETE CASCADE
);
