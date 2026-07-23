from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine, create_engine

from data_asset_agents.core.config import Settings
from data_asset_agents.evaluation.physical_rag import PhysicalRAGIndex
from data_asset_agents.evaluation.repository import PostgresEvaluationRepository
from data_asset_agents.evaluation.service import EvaluationService, database_snapshot_hash
from data_asset_agents.evaluation.strategies import (
    OntologyStrategy,
    PhysicalRAGStrategy,
    SchemaBaselineStrategy,
    StrategyRouter,
)
from data_asset_agents.execution.executor import QueryExecutor
from data_asset_agents.llm.factory import ModelFactory
from data_asset_agents.ontology.manager.hashing import calculate_bundle_hash
from data_asset_agents.ontology.manager.repository import (
    PostgresOntologyManagerRepository,
)
from data_asset_agents.ontology.manager.service import OntologyManagerService
from data_asset_agents.ontology.manager.validator import OntologyDraftValidator
from data_asset_agents.ontology.repository import (
    PostgresOntologyRepository,
    YamlOntologyRepository,
)
from data_asset_agents.ontology.service import OntologyService
from data_asset_agents.sql_assets import (
    PostgresSQLAssetRepository,
    SQLAssetService,
)
from data_asset_agents.text2sql.graph import build_text2sql_graph
from data_asset_agents.validation import EvaluationPolicyInspector


@dataclass
class EvaluationRuntime:
    engine: Engine
    service: EvaluationService
    strategies: StrategyRouter
    executor: QueryExecutor
    ontology: OntologyService
    physical_rag: PhysicalRAGIndex


def build_evaluation_runtime(settings: Settings) -> EvaluationRuntime:
    model_factory = ModelFactory(settings)
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_timeout=settings.database_connect_timeout,
        connect_args={"connect_timeout": settings.database_connect_timeout},
    )
    runtime_repository = PostgresOntologyRepository(engine)
    ontology = OntologyService(
        YamlOntologyRepository(settings.ontology_path),
        runtime_repository,
        settings=settings,
        model_factory=model_factory,
    )
    manager = OntologyManagerService(
        PostgresOntologyManagerRepository(engine),
        ontology.bundle,
        OntologyDraftValidator(ontology.bundle, engine=engine),
        engine=engine,
    )
    current_version = runtime_repository.get_current_version()
    if current_version is not None:
        governed_bundle, _, artifact = manager.load_runtime_bundle(
            current_version.id, ontology.bundle
        )
        ontology.bundle = governed_bundle
        ontology.ontology_version_id = current_version.id
        ontology.compiled_bundle_hash = artifact.bundle_hash if artifact else ""
        ontology.compiler_version = artifact.compiler_version if artifact else "legacy"
        ontology._refresh_indexes()
    executor = QueryExecutor(settings, engine=engine)
    executor.set_ontology(ontology.bundle)
    sql_repository = PostgresSQLAssetRepository(engine)
    sql_assets = SQLAssetService(
        sql_repository,
        ontology,
        executor,
        settings,
        model_factory,
    )
    sql_build = sql_assets.initialize_if_needed()
    catalog = executor.catalog.business_only()
    live_embedder = (
        model_factory.embeddings()
        if settings.llm_mode == "live"
        and settings.embedding_api_key.get_secret_value()
        else None
    )
    rag = PhysicalRAGIndex(
        dimensions=min(settings.embedding_dimensions, 128),
        embedder=live_embedder,
        embedding_identity=(
            settings.embedding_model if live_embedder is not None else "deterministic"
        ),
    )
    rag.build(catalog, settings.historical_sql_path)
    full_graph = build_text2sql_graph(ontology, executor, sql_assets=sql_assets)
    ablation_graph = build_text2sql_graph(ontology, executor, history_enabled=False)
    strategies = StrategyRouter(
        {
            "schema": SchemaBaselineStrategy(catalog, executor, settings, model_factory),
            "rag": PhysicalRAGStrategy(catalog, rag, executor, settings, model_factory),
            "ontology_no_sql_asset": OntologyStrategy(ablation_graph, sql_asset_enabled=False),
            "ontology_full": OntologyStrategy(full_graph, sql_asset_enabled=True),
        }
    )
    service = EvaluationService(
        PostgresEvaluationRepository(engine),
        strategies,
        EvaluationPolicyInspector(ontology.bundle, ontology.ontology_version_id),
        settings,
        database_snapshot_hash=database_snapshot_hash(
            catalog.model_dump_json(), "data/seed/002_seed.sql"
        ),
        physical_rag_build_id=rag.build_id,
        ontology_version_id=ontology.ontology_version_id,
        bundle_hash=ontology.compiled_bundle_hash or calculate_bundle_hash(ontology.bundle),
        sql_asset_build_id=sql_build.build_id if sql_build else None,
    )
    return EvaluationRuntime(
        engine=engine,
        service=service,
        strategies=strategies,
        executor=executor,
        ontology=ontology,
        physical_rag=rag,
    )
