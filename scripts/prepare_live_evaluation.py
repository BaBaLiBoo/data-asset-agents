"""Prepare governed, versioned ontology artifacts required by live evaluation."""

from __future__ import annotations

import json

from sqlalchemy import create_engine, text

from data_asset_agents.core.config import get_settings
from data_asset_agents.core.errors import DataAssetAgentsError
from data_asset_agents.execution.executor import QueryExecutor
from data_asset_agents.llm.factory import ModelFactory
from data_asset_agents.ontology.manager.governance_models import (
    OntologyIndexBuildRequest,
    OntologyIndexStatus,
    OntologyIndexType,
)
from data_asset_agents.ontology.manager.indexing import OntologyIndexService
from data_asset_agents.ontology.manager.models import (
    ActorRequest,
    CompiledArtifactStatus,
    CreateDraftFromSeedRequest,
    PublishDraftRequest,
)
from data_asset_agents.ontology.manager.repository import (
    PostgresOntologyManagerRepository,
)
from data_asset_agents.ontology.manager.seed_repository import (
    ObjectOntologySeedRepository,
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
    SQLAssetBuildStatus,
    SQLAssetService,
)


def _require(value: object, message: str) -> None:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise DataAssetAgentsError(message)


def main() -> None:
    settings = get_settings()
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_timeout=settings.database_connect_timeout,
        connect_args={"connect_timeout": settings.database_connect_timeout},
    )
    with engine.connect() as connection:
        connection.execute(text("SELECT 1")).scalar_one()

    model_factory = ModelFactory(settings)
    runtime_repository = PostgresOntologyRepository(engine)
    manager_repository = PostgresOntologyManagerRepository(engine)
    ontology = OntologyService(
        YamlOntologyRepository(settings.ontology_path),
        runtime_repository,
        settings=settings,
        model_factory=model_factory,
    )
    executor = QueryExecutor(settings, ontology.bundle, engine=engine)
    manager = OntologyManagerService(
        manager_repository,
        ontology.bundle,
        OntologyDraftValidator(
            ontology.bundle,
            engine=engine,
            executor=executor,
        ),
        engine=engine,
        seed_repository=ObjectOntologySeedRepository(
            {"retail_banking": settings.ontology_path / "object_model"}
        ),
    )

    current_version = runtime_repository.get_current_version()
    if current_version is None:
        draft = manager.create_draft_from_seed(
            CreateDraftFromSeedRequest(
                draft_name="MiniBank live evaluation object model",
                created_by="live-evaluation-preparer",
                seed_name="retail_banking",
            )
        )
        validated = manager.validate(
            draft.draft.id,
            expected_revision=draft.draft.resource_revision,
            actor="live-evaluation-validator",
        )
        if (
            validated.draft.validation_report is None
            or not validated.draft.validation_report.valid
        ):
            raise DataAssetAgentsError(
                "Direct object seed failed publication validation"
            )
        submitted = manager.submit(
            draft.draft.id,
            ActorRequest(actor="live-evaluation-submitter"),
            expected_revision=validated.draft.resource_revision,
        )
        approved = manager.approve(
            draft.draft.id,
            ActorRequest(actor="live-evaluation-reviewer"),
            expected_revision=submitted.draft.resource_revision,
        )
        current_version = manager.publish(
            draft.draft.id,
            PublishDraftRequest(
                actor="live-evaluation-reviewer",
                version=f"live-evaluation-{approved.draft.resource_hash[:12]}",
                description="Governed fictional MiniBank live evaluation ontology",
            ),
            expected_revision=approved.draft.resource_revision,
        )

    if current_version.id.startswith("yaml-seed-"):
        raise DataAssetAgentsError("Live evaluation cannot use a YAML seed version")
    artifact = manager_repository.get_compiled_artifact(current_version.id)
    if artifact is None or artifact.status != CompiledArtifactStatus.READY:
        raise DataAssetAgentsError(
            "Live evaluation requires a PUBLISHED ontology version "
            "with a READY compiled artifact"
        )
    _require(artifact.source_resource_hash, "Compiled artifact resource_hash is missing")
    _require(artifact.bundle_hash, "Compiled artifact bundle_hash is missing")
    _require(artifact.compiler_version, "Compiled artifact compiler_version is missing")
    governed_bundle, legacy_fallback, loaded_artifact = manager.load_runtime_bundle(
        current_version.id, ontology.bundle
    )
    if legacy_fallback or loaded_artifact is None:
        raise DataAssetAgentsError("Live evaluation cannot use legacy ontology fallback")
    ontology.bundle = governed_bundle
    ontology.ontology_version_id = current_version.id
    ontology.compiled_bundle_hash = artifact.bundle_hash
    ontology.compiler_version = artifact.compiler_version
    ontology._refresh_indexes()
    executor.set_ontology(ontology.bundle)

    index_service = OntologyIndexService(
        engine,
        manager_repository,
        settings,
        model_factory,
        lambda: ontology.bundle,
        lambda: ontology.ontology_version_id,
    )
    ontology.index_service = index_service
    index_build = index_service.current(
        current_version.id, OntologyIndexType.BUSINESS_CONCEPT
    )
    if index_build is None:
        index_build = index_service.build(
            OntologyIndexBuildRequest(index_type=OntologyIndexType.BUSINESS_CONCEPT)
        )
    if index_build.status != OntologyIndexStatus.READY:
        raise DataAssetAgentsError(
            f"Ontology index build failed: {index_build.error_message or 'unknown error'}"
        )

    sql_repository = PostgresSQLAssetRepository(engine)
    sql_assets = SQLAssetService(
        sql_repository,
        ontology,
        executor,
        settings,
        model_factory,
    )
    sql_build = sql_repository.latest_ready(
        current_version.id,
        artifact.bundle_hash,
    )
    if sql_build is None:
        sql_build = sql_assets.build().build
    if sql_build.status != SQLAssetBuildStatus.READY:
        raise DataAssetAgentsError(
            f"SQLAsset build failed: {sql_build.error_message or 'unknown error'}"
        )
    if (
        sql_build.ontology_version_id != current_version.id
        or sql_build.bundle_hash != artifact.bundle_hash
    ):
        raise DataAssetAgentsError(
            "SQLAsset build provenance does not match the current ontology"
        )

    print(
        json.dumps(
            {
                "ontology_version_id": current_version.id,
                "resource_hash": artifact.source_resource_hash,
                "bundle_hash": artifact.bundle_hash,
                "compiler_version": artifact.compiler_version,
                "ontology_index_build_id": index_build.build_id,
                "sql_asset_build_id": sql_build.build_id,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
