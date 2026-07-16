CREATE TABLE IF NOT EXISTS sql_asset_build (
    build_id VARCHAR(160) PRIMARY KEY,
    ontology_version_id VARCHAR(160) NOT NULL,
    source_hash CHAR(64) NOT NULL,
    source_path TEXT NOT NULL,
    status VARCHAR(20) NOT NULL CHECK (status IN ('BUILDING', 'READY', 'FAILED')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    error_message TEXT,
    asset_count INTEGER NOT NULL DEFAULT 0,
    eligible_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sql_asset_build_ready
    ON sql_asset_build(ontology_version_id, status, completed_at DESC);

INSERT INTO sql_asset_build (
    build_id, ontology_version_id, source_hash, source_path, status,
    started_at, completed_at, asset_count, eligible_count
)
SELECT
    'legacy-sql-assets', 'legacy-ontology', repeat('0', 64),
    'data/historical_sql/examples.json', 'READY',
    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, COUNT(*),
    COUNT(*) FILTER (
        WHERE certified AND lifecycle_valid AND parse_valid AND explain_valid
    )
FROM sql_asset
HAVING COUNT(*) > 0
ON CONFLICT (build_id) DO NOTHING;

ALTER TABLE sql_asset ADD COLUMN IF NOT EXISTS ontology_version_id VARCHAR(160);
ALTER TABLE sql_asset ADD COLUMN IF NOT EXISTS build_id VARCHAR(160);
ALTER TABLE sql_asset ADD COLUMN IF NOT EXISTS source_hash CHAR(64);
ALTER TABLE sql_asset ADD COLUMN IF NOT EXISTS source_path TEXT;
ALTER TABLE sql_asset ADD COLUMN IF NOT EXISTS indexed_at TIMESTAMPTZ;
ALTER TABLE sql_asset ADD COLUMN IF NOT EXISTS semantic_policy_valid BOOLEAN;
ALTER TABLE sql_asset ADD COLUMN IF NOT EXISTS metric_policy_violations TEXT[];

UPDATE sql_asset SET
    ontology_version_id = COALESCE(ontology_version_id, 'legacy-ontology'),
    build_id = COALESCE(build_id, 'legacy-sql-assets'),
    source_hash = COALESCE(source_hash, repeat('0', 64)),
    source_path = COALESCE(source_path, 'data/historical_sql/examples.json'),
    indexed_at = COALESCE(indexed_at, updated_at),
    semantic_policy_valid = COALESCE(semantic_policy_valid, FALSE),
    metric_policy_violations = COALESCE(metric_policy_violations, '{}');

ALTER TABLE sql_asset ALTER COLUMN ontology_version_id SET NOT NULL;
ALTER TABLE sql_asset ALTER COLUMN build_id SET NOT NULL;
ALTER TABLE sql_asset ALTER COLUMN source_hash SET NOT NULL;
ALTER TABLE sql_asset ALTER COLUMN source_path SET NOT NULL;
ALTER TABLE sql_asset ALTER COLUMN semantic_policy_valid SET NOT NULL;
ALTER TABLE sql_asset ALTER COLUMN semantic_policy_valid SET DEFAULT FALSE;
ALTER TABLE sql_asset ALTER COLUMN metric_policy_violations SET NOT NULL;
ALTER TABLE sql_asset ALTER COLUMN metric_policy_violations SET DEFAULT '{}';

ALTER TABLE sql_asset DROP CONSTRAINT IF EXISTS sql_asset_pkey;
ALTER TABLE sql_asset ADD CONSTRAINT sql_asset_pkey PRIMARY KEY (build_id, asset_id);
ALTER TABLE sql_asset DROP CONSTRAINT IF EXISTS fk_sql_asset_build;
ALTER TABLE sql_asset ADD CONSTRAINT fk_sql_asset_build
    FOREIGN KEY (build_id) REFERENCES sql_asset_build(build_id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_sql_asset_version_build
    ON sql_asset(ontology_version_id, build_id);
CREATE INDEX IF NOT EXISTS idx_sql_asset_semantic_policy
    ON sql_asset(semantic_policy_valid);
