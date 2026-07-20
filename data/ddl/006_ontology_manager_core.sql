-- Object-first ontology drafts and immutable published resources.
CREATE TABLE IF NOT EXISTS ontology_draft (
    draft_id VARCHAR(160) PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    base_version_id VARCHAR(160),
    source_snapshot_id VARCHAR(160),
    status VARCHAR(30) NOT NULL CHECK (
        status IN ('DRAFT', 'IN_REVIEW', 'VALIDATED', 'PUBLISHED', 'REJECTED')
    ),
    created_by VARCHAR(100) NOT NULL,
    submitted_by VARCHAR(100),
    reviewed_by VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    submitted_at TIMESTAMPTZ,
    reviewed_at TIMESTAMPTZ,
    validation_report JSONB,
    rejection_reason TEXT
);

CREATE TABLE IF NOT EXISTS draft_object_type (
    draft_id VARCHAR(160) NOT NULL REFERENCES ontology_draft(draft_id) ON DELETE CASCADE,
    object_type_id VARCHAR(160) NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (draft_id, object_type_id)
);
CREATE TABLE IF NOT EXISTS draft_property_definition (
    draft_id VARCHAR(160) NOT NULL REFERENCES ontology_draft(draft_id) ON DELETE CASCADE,
    property_id VARCHAR(200) NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (draft_id, property_id)
);
CREATE TABLE IF NOT EXISTS draft_link_type (
    draft_id VARCHAR(160) NOT NULL REFERENCES ontology_draft(draft_id) ON DELETE CASCADE,
    link_type_id VARCHAR(160) NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (draft_id, link_type_id)
);
CREATE TABLE IF NOT EXISTS data_source_definition (
    data_source_id VARCHAR(160) PRIMARY KEY,
    provider VARCHAR(30) NOT NULL CHECK (provider = 'POSTGRESQL'),
    connection_ref VARCHAR(100) NOT NULL CHECK (connection_ref ~ '^[A-Z][A-Z0-9_]{2,63}$'),
    payload JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS draft_object_data_source_binding (
    draft_id VARCHAR(160) NOT NULL REFERENCES ontology_draft(draft_id) ON DELETE CASCADE,
    binding_id VARCHAR(160) NOT NULL,
    data_source_id VARCHAR(160) NOT NULL REFERENCES data_source_definition(data_source_id),
    payload JSONB NOT NULL,
    PRIMARY KEY (draft_id, binding_id)
);

CREATE TABLE IF NOT EXISTS published_object_type (
    ontology_version_id VARCHAR(160) NOT NULL REFERENCES ontology_version(version_id) ON DELETE CASCADE,
    object_type_id VARCHAR(160) NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (ontology_version_id, object_type_id)
);
CREATE TABLE IF NOT EXISTS published_property_definition (
    ontology_version_id VARCHAR(160) NOT NULL REFERENCES ontology_version(version_id) ON DELETE CASCADE,
    property_id VARCHAR(200) NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (ontology_version_id, property_id)
);
CREATE TABLE IF NOT EXISTS published_link_type (
    ontology_version_id VARCHAR(160) NOT NULL REFERENCES ontology_version(version_id) ON DELETE CASCADE,
    link_type_id VARCHAR(160) NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (ontology_version_id, link_type_id)
);
CREATE TABLE IF NOT EXISTS published_object_data_source_binding (
    ontology_version_id VARCHAR(160) NOT NULL REFERENCES ontology_version(version_id) ON DELETE CASCADE,
    binding_id VARCHAR(160) NOT NULL,
    data_source_id VARCHAR(160) NOT NULL REFERENCES data_source_definition(data_source_id),
    payload JSONB NOT NULL,
    PRIMARY KEY (ontology_version_id, binding_id)
);
CREATE TABLE IF NOT EXISTS ontology_version_object_resource (
    ontology_version_id VARCHAR(160) NOT NULL REFERENCES ontology_version(version_id) ON DELETE CASCADE,
    resource_type VARCHAR(40) NOT NULL CHECK (
        resource_type IN ('OBJECT_TYPE', 'PROPERTY', 'LINK_TYPE', 'BINDING')
    ),
    resource_id VARCHAR(200) NOT NULL,
    source_draft_id VARCHAR(160) NOT NULL,
    PRIMARY KEY (ontology_version_id, resource_type, resource_id)
);

CREATE INDEX IF NOT EXISTS idx_ontology_draft_status ON ontology_draft(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_object_resource_version
    ON ontology_version_object_resource(ontology_version_id, resource_type);
