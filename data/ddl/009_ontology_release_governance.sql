-- Ontology Release Governance V2: exact Draft snapshots and immutable runtime output.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

ALTER TABLE ontology_draft ADD COLUMN IF NOT EXISTS resource_revision BIGINT NOT NULL DEFAULT 0;
ALTER TABLE ontology_draft ADD COLUMN IF NOT EXISTS resource_hash VARCHAR(64) NOT NULL DEFAULT '';
ALTER TABLE ontology_draft ADD COLUMN IF NOT EXISTS validated_revision BIGINT;
ALTER TABLE ontology_draft ADD COLUMN IF NOT EXISTS validated_hash VARCHAR(64);
ALTER TABLE ontology_draft ADD COLUMN IF NOT EXISTS submitted_revision BIGINT;
ALTER TABLE ontology_draft ADD COLUMN IF NOT EXISTS submitted_hash VARCHAR(64);
ALTER TABLE ontology_draft ADD COLUMN IF NOT EXISTS validation_state VARCHAR(30)
    NOT NULL DEFAULT 'NEVER_VALIDATED';

ALTER TABLE ontology_draft DROP CONSTRAINT IF EXISTS ontology_draft_validation_state_check;
ALTER TABLE ontology_draft ADD CONSTRAINT ontology_draft_validation_state_check CHECK (
    validation_state IN ('NEVER_VALIDATED', 'VALID', 'STALE', 'FAILED')
);

-- Existing installations receive a deterministic non-empty migration identity. New and copied
-- Drafts are always recalculated by the application from the complete canonical resource set.
UPDATE ontology_draft
SET resource_hash = encode(digest('legacy-draft:' || draft_id, 'sha256'), 'hex'),
    validation_state = CASE
        WHEN validation_report IS NULL THEN 'NEVER_VALIDATED'
        ELSE 'STALE'
    END,
    validated_revision = NULL,
    validated_hash = NULL,
    submitted_revision = NULL,
    submitted_hash = NULL
WHERE resource_hash = '';

CREATE TABLE IF NOT EXISTS ontology_compiled_artifact (
    artifact_id VARCHAR(200) NOT NULL
        CONSTRAINT ontology_compiled_artifact_pk PRIMARY KEY,
    ontology_version_id VARCHAR(160) NOT NULL
        CONSTRAINT ontology_compiled_artifact_version_fk
        REFERENCES ontology_version(version_id) ON DELETE RESTRICT,
    source_draft_id VARCHAR(160) NOT NULL
        CONSTRAINT ontology_compiled_artifact_source_draft_fk
        REFERENCES ontology_draft(draft_id) ON DELETE RESTRICT,
    source_revision BIGINT NOT NULL
        CONSTRAINT ontology_compiled_artifact_source_revision_check
        CHECK (source_revision >= 0),
    source_resource_hash VARCHAR(64) NOT NULL,
    compiler_name VARCHAR(100) NOT NULL,
    compiler_version VARCHAR(40) NOT NULL,
    compiler_source_hash VARCHAR(64) NOT NULL,
    status VARCHAR(20) NOT NULL
        CONSTRAINT ontology_compiled_artifact_status_check
        CHECK (status IN ('BUILDING', 'READY', 'FAILED')),
    bundle_hash VARCHAR(64) NOT NULL,
    bundle_json JSONB NOT NULL,
    property_bindings JSONB NOT NULL DEFAULT '{}'::jsonb,
    metric_compilation_evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    dimension_compilation_evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    join_compilation_evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL,
    error_message TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ontology_compiled_artifact_ready_version
    ON ontology_compiled_artifact(ontology_version_id) WHERE status = 'READY';
CREATE INDEX IF NOT EXISTS idx_ontology_compiled_artifact_version
    ON ontology_compiled_artifact(ontology_version_id, status);

CREATE TABLE IF NOT EXISTS ontology_audit_event (
    event_id VARCHAR(200) NOT NULL CONSTRAINT ontology_audit_event_pk PRIMARY KEY,
    draft_id VARCHAR(160),
    ontology_version_id VARCHAR(160),
    actor VARCHAR(100) NOT NULL,
    action VARCHAR(40) NOT NULL
        CONSTRAINT ontology_audit_event_action_check CHECK (action IN (
        'DRAFT_CREATED', 'RESOURCE_CREATED', 'RESOURCE_UPDATED', 'RESOURCE_DELETED',
        'CANDIDATES_IMPORTED', 'VALIDATION_STARTED', 'VALIDATION_PASSED',
        'VALIDATION_FAILED', 'SUBMITTED', 'APPROVED', 'REJECTED', 'PUBLISHED',
        'ACTIVATED', 'ACTIVATION_FAILED', 'ROLLED_BACK'
    )),
    resource_type VARCHAR(80),
    resource_id VARCHAR(200),
    before_revision BIGINT,
    after_revision BIGINT,
    before_hash VARCHAR(64),
    after_hash VARCHAR(64),
    request_id VARCHAR(160),
    created_at TIMESTAMPTZ NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_ontology_draft_revision
    ON ontology_draft(draft_id, resource_revision);
CREATE INDEX IF NOT EXISTS idx_ontology_draft_validation_state
    ON ontology_draft(validation_state, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ontology_audit_draft_created
    ON ontology_audit_event(draft_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ontology_audit_version_created
    ON ontology_audit_event(ontology_version_id, created_at DESC);

CREATE OR REPLACE FUNCTION reject_immutable_ontology_row() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'immutable ontology governance row cannot be changed';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_ontology_audit_append_only ON ontology_audit_event;
CREATE TRIGGER trg_ontology_audit_append_only
BEFORE UPDATE OR DELETE ON ontology_audit_event
FOR EACH ROW EXECUTE FUNCTION reject_immutable_ontology_row();

CREATE OR REPLACE FUNCTION reject_ready_artifact_change() RETURNS trigger AS $$
BEGIN
    IF OLD.status = 'READY' THEN
        RAISE EXCEPTION 'READY compiled ontology artifact is immutable';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_ready_artifact_immutable ON ontology_compiled_artifact;
CREATE TRIGGER trg_ready_artifact_immutable
BEFORE UPDATE OR DELETE ON ontology_compiled_artifact
FOR EACH ROW EXECUTE FUNCTION reject_ready_artifact_change();

ALTER TABLE ontology_index_build ADD COLUMN IF NOT EXISTS bundle_hash VARCHAR(64) NOT NULL DEFAULT '';
ALTER TABLE sql_asset_build ADD COLUMN IF NOT EXISTS bundle_hash VARCHAR(64) NOT NULL DEFAULT '';
ALTER TABLE sql_asset_build ADD COLUMN IF NOT EXISTS compiler_version VARCHAR(40) NOT NULL DEFAULT 'legacy';
