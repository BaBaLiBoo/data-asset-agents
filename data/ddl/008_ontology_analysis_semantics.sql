-- Versioned analytical semantics authored inside the object-first Ontology Draft.
CREATE TABLE IF NOT EXISTS draft_metric_definition (
    draft_id VARCHAR(160) NOT NULL REFERENCES ontology_draft(draft_id) ON DELETE CASCADE,
    metric_id VARCHAR(160) NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (draft_id, metric_id)
);

CREATE TABLE IF NOT EXISTS draft_dimension_definition (
    draft_id VARCHAR(160) NOT NULL REFERENCES ontology_draft(draft_id) ON DELETE CASCADE,
    dimension_id VARCHAR(160) NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (draft_id, dimension_id)
);

CREATE TABLE IF NOT EXISTS published_metric_definition (
    ontology_version_id VARCHAR(160) NOT NULL
        REFERENCES ontology_version(version_id) ON DELETE CASCADE,
    metric_id VARCHAR(160) NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (ontology_version_id, metric_id)
);

CREATE TABLE IF NOT EXISTS published_dimension_definition (
    ontology_version_id VARCHAR(160) NOT NULL
        REFERENCES ontology_version(version_id) ON DELETE CASCADE,
    dimension_id VARCHAR(160) NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (ontology_version_id, dimension_id)
);

ALTER TABLE ontology_version_object_resource
    DROP CONSTRAINT IF EXISTS ontology_version_object_resource_resource_type_check;
ALTER TABLE ontology_version_object_resource
    ADD CONSTRAINT ontology_version_object_resource_resource_type_check CHECK (
        resource_type IN (
            'OBJECT_TYPE', 'PROPERTY', 'LINK_TYPE', 'BINDING', 'PHYSICAL_JOIN',
            'METRIC', 'DIMENSION'
        )
    );

CREATE INDEX IF NOT EXISTS idx_published_metric_version
    ON published_metric_definition(ontology_version_id, metric_id);
CREATE INDEX IF NOT EXISTS idx_published_dimension_version
    ON published_dimension_definition(ontology_version_id, dimension_id);
