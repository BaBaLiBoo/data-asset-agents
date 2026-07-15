import os

import pytest
from sqlalchemy import create_engine

from data_asset_agents.core.config import Settings
from data_asset_agents.core.errors import OntologyError
from data_asset_agents.execution.executor import QueryExecutor
from data_asset_agents.metadata import MetadataInspector
from data_asset_agents.ontology.builder import CandidateGenerator, OntologyBuildService
from data_asset_agents.ontology.models import (
    CandidateReviewRequest,
    OntologyBuildRequest,
    OntologyPublishRequest,
    ReviewStatus,
)
from data_asset_agents.ontology.repository import (
    PostgresOntologyRepository,
    YamlOntologyRepository,
)
from data_asset_agents.ontology.service import OntologyService
from data_asset_agents.sql_assets import HistoricalSQLParser
from data_asset_agents.text2sql.graph import build_text2sql_graph

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="set RUN_POSTGRES_INTEGRATION=1 with an initialized PostgreSQL database",
)


def test_reviewed_publication_is_versioned_and_candidate_isolated() -> None:
    settings = Settings(
        database_url=os.getenv(
            "DATABASE_URL",
            "postgresql+psycopg://minibank:minibank@localhost:5432/minibank",
        )
    )
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    runtime = PostgresOntologyRepository(engine)
    yaml_repository = YamlOntologyRepository(settings.ontology_path)
    seed_bundle = yaml_repository.load()
    builder = OntologyBuildService(
        inspector=MetadataInspector(engine),
        sql_parser=HistoricalSQLParser(),
        generator=CandidateGenerator(settings),
        repository=runtime,
        seed_bundle=seed_bundle,
        historical_sql_path=settings.historical_sql_path,
    )
    try:
        result = builder.build(
            OntologyBuildRequest(tables=["dim_branch"], sample_limit=3)
        )
        by_column = {item.column_name: item for item in result.concepts}
        branch_id_concept = by_column["branch_id"]
        region_concept = by_column["region"]
        branch_mapping = next(
            item
            for item in result.mappings
            if item.candidate_concept_id == branch_id_concept.id
        )

        review = CandidateReviewRequest(reviewer="integration-test")
        assert builder.verify(branch_id_concept.id, review).status == ReviewStatus.VERIFIED
        assert builder.verify(branch_mapping.id, review).status == ReviewStatus.VERIFIED
        assert builder.reject(region_concept.id, review).status == ReviewStatus.REJECTED
        with pytest.raises(OntologyError, match="immutable"):
            builder.verify(branch_id_concept.id, review)

        version = builder.publish(
            OntologyPublishRequest(
                version="integration-builder-0.2",
                description="integration publication",
                published_by="integration-test",
                snapshot_id=result.snapshot.id,
            )
        )
        assert version.is_current
        published = runtime.load_latest_published_bundle()
        assert published is not None
        mapping_ids = {mapping.concept_id for mapping in published.mappings}
        assert branch_id_concept.semantic_id in mapping_ids
        assert region_concept.semantic_id not in mapping_ids

        online = OntologyService(yaml_repository, runtime)
        assert online.bundle.domain["version"] == "integration-builder-0.2"
        executor = QueryExecutor(settings, online.bundle, engine=engine)
        graph_result = build_text2sql_graph(online, executor).invoke(
            {
                "question": "查询近30天各分行信用卡交易金额和交易笔数。",
                "query_mode": "ontology",
                "retry_count": 0,
                "trace_steps": [],
            }
        )
        assert graph_result["status"] == "success"
        assert graph_result["execution_result"].row_count > 0
        assert builder.versions()[0].version == "integration-builder-0.2"
    finally:
        engine.dispose()
