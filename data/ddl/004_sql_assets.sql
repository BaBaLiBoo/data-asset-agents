CREATE TABLE IF NOT EXISTS sql_asset (
    asset_id VARCHAR(160) PRIMARY KEY,
    question TEXT NOT NULL,
    business_summary TEXT NOT NULL DEFAULT '',
    certified BOOLEAN NOT NULL DEFAULT FALSE,
    certification_level VARCHAR(30) NOT NULL DEFAULT 'NONE',
    sql_text TEXT NOT NULL,
    dialect VARCHAR(30) NOT NULL DEFAULT 'postgres',
    metrics TEXT[] NOT NULL DEFAULT '{}',
    dimensions TEXT[] NOT NULL DEFAULT '{}',
    tables TEXT[] NOT NULL DEFAULT '{}',
    ast_fingerprint CHAR(64) NOT NULL DEFAULT '',
    structural_tags TEXT[] NOT NULL DEFAULT '{}',
    execution_status VARCHAR(30) NOT NULL DEFAULT 'NOT_CHECKED',
    lifecycle_valid BOOLEAN NOT NULL DEFAULT FALSE,
    parse_valid BOOLEAN NOT NULL DEFAULT FALSE,
    explain_valid BOOLEAN NOT NULL DEFAULT FALSE,
    payload JSONB NOT NULL,
    embedding VECTOR(1024) NOT NULL,
    search_document TSVECTOR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sql_asset_safety
    ON sql_asset(certified, lifecycle_valid, parse_valid, explain_valid);
CREATE INDEX IF NOT EXISTS idx_sql_asset_metrics ON sql_asset USING GIN(metrics);
CREATE INDEX IF NOT EXISTS idx_sql_asset_dimensions ON sql_asset USING GIN(dimensions);
CREATE INDEX IF NOT EXISTS idx_sql_asset_tables ON sql_asset USING GIN(tables);
CREATE INDEX IF NOT EXISTS idx_sql_asset_search
    ON sql_asset USING GIN(search_document);
CREATE INDEX IF NOT EXISTS idx_sql_asset_embedding
    ON sql_asset USING hnsw (embedding vector_cosine_ops);
