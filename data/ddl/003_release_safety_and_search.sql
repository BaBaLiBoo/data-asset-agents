CREATE TABLE IF NOT EXISTS ontology_version_candidate (
    version_id VARCHAR(160) NOT NULL REFERENCES ontology_version(version_id) ON DELETE CASCADE,
    candidate_id VARCHAR(500) NOT NULL REFERENCES ontology_candidate(candidate_id),
    snapshot_id VARCHAR(160) NOT NULL REFERENCES metadata_snapshot(snapshot_id),
    PRIMARY KEY (version_id, candidate_id)
);
CREATE INDEX IF NOT EXISTS idx_ontology_version_candidate_snapshot
    ON ontology_version_candidate(snapshot_id, version_id);

CREATE TABLE IF NOT EXISTS semantic_concept_index (
    version_id VARCHAR(160) NOT NULL REFERENCES ontology_version(version_id) ON DELETE CASCADE,
    concept_id VARCHAR(300) NOT NULL,
    concept_kind VARCHAR(30) NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    synonyms TEXT[] NOT NULL DEFAULT '{}',
    embedding VECTOR(1024) NOT NULL,
    search_document TSVECTOR NOT NULL,
    PRIMARY KEY (version_id, concept_kind, concept_id)
);
CREATE INDEX IF NOT EXISTS idx_semantic_concept_index_search
    ON semantic_concept_index USING GIN(search_document);
CREATE INDEX IF NOT EXISTS idx_semantic_concept_index_embedding
    ON semantic_concept_index USING hnsw (embedding vector_cosine_ops);
