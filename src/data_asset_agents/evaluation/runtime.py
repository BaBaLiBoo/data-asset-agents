from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine, create_engine

from data_asset_agents.core.config import Settings
from data_asset_agents.core.errors import DataAssetAgentsError
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
from data_asset_agents.ontology.manager.governance_models import OntologyIndexType
from data_asset_agents.ontology.manager.hashing import calculate_bundle_hash
from data_asset_agents.ontology.manager.indexing import OntologyIndexService
from data_asset_agents.ontology.manager.models import CompiledOntologyArtifact
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
    SQLAssetBuild,
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


def _activate_evaluation_ontology(
    settings: Settings,
    ontology: OntologyService,
    runtime_repository: PostgresOntologyRepository,
    manager: OntologyManagerService,
    index_service: OntologyIndexService,
) -> CompiledOntologyArtifact | None:
    current_version = runtime_repository.get_current_version()
    if current_version is None:
        if settings.llm_mode == "live":
            raise DataAssetAgentsError(
                "Live evaluation requires a PUBLISHED ontology version "
                "with a READY compiled artifact"
            )
        return None
    artifact = manager.repository.get_compiled_artifact(current_version.id)
    if settings.llm_mode == "live" and artifact is None:
        raise DataAssetAgentsError(
            "Live evaluation requires a PUBLISHED ontology version "
            "with a READY compiled artifact"
        )
    governed_bundle, legacy_fallback, loaded_artifact = manager.load_runtime_bundle(
        current_version.id, ontology.bundle
    )
    if settings.llm_mode == "live" and (
        legacy_fallback
        or loaded_artifact is None
        or not loaded_artifact.bundle_hash
        or not loaded_artifact.compiler_version
        or current_version.id.startswith("yaml-seed-")
    ):
        raise DataAssetAgentsError(
            "Live evaluation requires a PUBLISHED ontology version "
            "with a READY compiled artifact"
        )
    ontology.bundle = governed_bundle
    ontology.ontology_version_id = current_version.id
    ontology.compiled_bundle_hash = loaded_artifact.bundle_hash if loaded_artifact else ""
    ontology.compiler_version = (
        loaded_artifact.compiler_version if loaded_artifact else "legacy"
    )
    ontology._refresh_indexes()
    if settings.llm_mode == "live" and index_service.current(
        current_version.id, OntologyIndexType.BUSINESS_CONCEPT
    ) is None:
        raise DataAssetAgentsError(
            "Live evaluation requires a READY ontology index build"
        )
    return loaded_artifact


def _select_sql_asset_build(
    settings: Settings,
    *,
    enabled: bool,
    repository: PostgresSQLAssetRepository,
    service: SQLAssetService,
    ontology: OntologyService,
) -> SQLAssetBuild | None:
    if not enabled:
        return None
    ready = repository.latest_ready(
        ontology.ontology_version_id,
        ontology.compiled_bundle_hash,
    )
    if settings.llm_mode == "live":
        if ready is None:
            raise DataAssetAgentsError(
                "ontology_full live evaluation requires a READY SQLAssetBuild"
            )
        return ready
    return ready or service.initialize_if_needed()


def build_evaluation_runtime(
    settings: Settings,
    *,
    sql_assets_enabled: bool = True,
) -> EvaluationRuntime:
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
    manager_repository = PostgresOntologyManagerRepository(engine)
    manager = OntologyManagerService(
        manager_repository,
        ontology.bundle,
        OntologyDraftValidator(ontology.bundle, engine=engine),
        engine=engine,
    )
    ontology_index = OntologyIndexService(
        engine,
        manager_repository,
        settings,
        model_factory,
        lambda: ontology.bundle,
        lambda: ontology.ontology_version_id,
    )
    _activate_evaluation_ontology(
        settings, ontology, runtime_repository, manager, ontology_index
    )
    ontology.index_service = ontology_index
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
    sql_build = _select_sql_asset_build(
        settings,
        enabled=sql_assets_enabled,
        repository=sql_repository,
        service=sql_assets,
        ontology=ontology,
    )
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
    full_graph = build_text2sql_graph(
        ontology,
        executor,
        sql_assets=sql_assets if sql_assets_enabled else None,
    )
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
