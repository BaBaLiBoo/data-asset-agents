import csv
import io
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from sqlalchemy import create_engine

from data_asset_agents.core.config import get_settings
from data_asset_agents.core.errors import (
    DataAssetAgentsError,
    OntologyConflictError,
    OntologyGovernanceError,
    UnsupportedQueryError,
)
from data_asset_agents.evaluation import (
    EvaluationComparison,
    EvaluationRun,
    EvaluationRunRequest,
    OntologyStrategy,
    PhysicalRAGIndex,
    PhysicalRAGStrategy,
    SchemaBaselineStrategy,
    StrategyRouter,
)
from data_asset_agents.evaluation.models import EvaluationCaseResult
from data_asset_agents.evaluation.repository import PostgresEvaluationRepository
from data_asset_agents.evaluation.service import EvaluationService, database_snapshot_hash
from data_asset_agents.execution.executor import QueryExecutor
from data_asset_agents.llm.factory import ModelFactory
from data_asset_agents.metadata import MetadataInspector
from data_asset_agents.ontology.builder import CandidateGenerator, OntologyBuildService
from data_asset_agents.ontology.manager.candidate_generator import (
    ObjectFirstCandidateGenerator,
)
from data_asset_agents.ontology.manager.drift import MetadataDriftService
from data_asset_agents.ontology.manager.governance_models import (
    ObjectFilter,
    ObjectFilterOperator,
    ObjectRecord,
    OntologyChangeSet,
    OntologyImpactReport,
    OntologyIndexBuild,
    OntologyIndexBuildRequest,
    OntologySyncRun,
    SchemaDriftReport,
)
from data_asset_agents.ontology.manager.indexing import OntologyIndexService
from data_asset_agents.ontology.manager.models import (
    ActorRequest,
    CompiledArtifactSummary,
    CreateDraftFromSeedRequest,
    CreateDraftRequest,
    DataSourceDefinition,
    DataSourceInspection,
    DimensionDefinition,
    ImportObjectCandidatesRequest,
    LinkType,
    MetricDefinition,
    MigrateLegacyRequest,
    ObjectCandidateSet,
    ObjectDataSourceBinding,
    ObjectGraph,
    ObjectType,
    OntologyAuditAction,
    OntologyAuditEvent,
    OntologyDraft,
    OntologyDraftAggregate,
    PhysicalJoinDefinition,
    PropertyDefinition,
    PublishDraftRequest,
    RejectDraftRequest,
)
from data_asset_agents.ontology.manager.object_query import ObjectQueryService
from data_asset_agents.ontology.manager.repository import (
    PostgresOntologyManagerRepository,
)
from data_asset_agents.ontology.manager.seed_repository import (
    ObjectOntologySeedRepository,
)
from data_asset_agents.ontology.manager.service import OntologyManagerService
from data_asset_agents.ontology.manager.validator import OntologyDraftValidator
from data_asset_agents.ontology.models import (
    CandidateEnvelope,
    CandidateReviewRequest,
    OntologyBuildRequest,
    OntologyBuildResult,
    OntologyPublishRequest,
    OntologyVersion,
    PublishDryRunReport,
    ReviewStatus,
)
from data_asset_agents.ontology.repository import (
    PostgresOntologyRepository,
    YamlOntologyRepository,
)
from data_asset_agents.ontology.service import OntologyService
from data_asset_agents.sql_assets import (
    HistoricalSQLParser,
    PostgresSQLAssetRepository,
    SQLAsset,
    SQLAssetBuildReport,
    SQLAssetBuildRequest,
    SQLAssetSearchRequest,
    SQLAssetSearchResult,
    SQLAssetService,
)
from data_asset_agents.text2sql.graph import GRAPH_EDGES, GRAPH_NODES, build_text2sql_graph
from data_asset_agents.text2sql.models import (
    QueryRequest,
    QueryResponse,
    SemanticResolveRequest,
    SemanticSearchRequest,
)
from data_asset_agents.text2sql.tools import JoinPlanner
from data_asset_agents.validation import EvaluationPolicyInspector


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_timeout=settings.database_connect_timeout,
        connect_args={"connect_timeout": settings.database_connect_timeout},
    )
    runtime_repository = PostgresOntologyRepository(engine)
    yaml_repository = YamlOntologyRepository(settings.ontology_path)
    model_factory = ModelFactory(settings)
    ontology = OntologyService(
        yaml_repository,
        runtime_repository,
        settings=settings,
        model_factory=model_factory,
    )
    executor = QueryExecutor(settings, ontology.bundle, engine=engine)
    ontology_manager_repository = PostgresOntologyManagerRepository(engine)
    object_seed_repository = ObjectOntologySeedRepository(
        {"retail_banking": settings.ontology_path / "object_model"}
    )
    object_candidate_generator = ObjectFirstCandidateGenerator(
        settings, ontology.bundle.tables, model_factory
    )
    ontology_manager = OntologyManagerService(
        ontology_manager_repository,
        ontology.bundle,
        OntologyDraftValidator(ontology.bundle, engine=engine, executor=executor),
        engine=engine,
        candidate_repository=runtime_repository,
        seed_repository=object_seed_repository,
        candidate_generator=object_candidate_generator,
    )
    current_version = runtime_repository.get_current_version()
    runtime_artifact = None
    legacy_fallback = True
    if current_version is not None:
        try:
            governed_bundle, legacy_fallback, runtime_artifact = (
                ontology_manager.load_runtime_bundle(current_version.id, ontology.bundle)
            )
        except DataAssetAgentsError as exc:
            ontology_manager.record_runtime_event(
                OntologyAuditAction.ACTIVATION_FAILED,
                current_version.id,
                "api-startup",
                metadata={"error_type": type(exc).__name__},
            )
            healthy = None
            for candidate in runtime_repository.list_versions():
                if candidate.id == current_version.id:
                    continue
                candidate_bundle = runtime_repository.load_published_bundle(candidate.version)
                if candidate_bundle is None:
                    continue
                try:
                    bundle, legacy, artifact = ontology_manager.load_runtime_bundle(
                        candidate.id, candidate_bundle
                    )
                except DataAssetAgentsError:
                    continue
                healthy = (candidate, bundle, legacy, artifact)
                break
            if healthy is None:
                raise
            current_version, governed_bundle, legacy_fallback, runtime_artifact = healthy
            runtime_repository.activate_version(current_version.version)
            ontology.ontology_version_id = current_version.id
            ontology_manager.record_runtime_event(
                OntologyAuditAction.ROLLED_BACK,
                current_version.id,
                "api-startup",
                metadata={
                    "legacy_fallback": legacy_fallback,
                    "bundle_hash": runtime_artifact.bundle_hash if runtime_artifact else None,
                },
            )
        else:
            ontology_manager.record_runtime_event(
                OntologyAuditAction.ACTIVATED,
                current_version.id,
                "api-startup",
                metadata={
                    "legacy_fallback": legacy_fallback,
                    "bundle_hash": runtime_artifact.bundle_hash if runtime_artifact else None,
                },
            )
        ontology.bundle = governed_bundle
        ontology.compiled_bundle_hash = (
            runtime_artifact.bundle_hash if runtime_artifact else ""
        )
        ontology.compiler_version = (
            runtime_artifact.compiler_version if runtime_artifact else "legacy"
        )
        ontology._refresh_indexes()
        executor.set_ontology(ontology.bundle)
    drift_service = MetadataDriftService(
        ontology_manager_repository,
        ontology.bundle,
        engine,
        lambda: ontology.ontology_version_id,
    )
    ontology_manager.drift_service = drift_service
    ontology_index_service = OntologyIndexService(
        engine,
        ontology_manager_repository,
        settings,
        model_factory,
        lambda: ontology.bundle,
        lambda: ontology.ontology_version_id,
    )
    ontology.index_service = ontology_index_service
    object_query_service = ObjectQueryService(engine, ontology_manager_repository)
    builder = OntologyBuildService(
        inspector=MetadataInspector(engine),
        sql_parser=HistoricalSQLParser(),
        generator=CandidateGenerator(settings, model_factory),
        repository=runtime_repository,
        seed_bundle=ontology.seed_bundle,
        historical_sql_path=settings.historical_sql_path,
        executor=executor,
        yaml_repository=yaml_repository,
    )
    sql_asset_repository = PostgresSQLAssetRepository(engine)
    sql_asset_service = SQLAssetService(
        sql_asset_repository,
        ontology,
        executor,
        settings,
        model_factory,
    )
    sql_asset_build = sql_asset_service.initialize_if_needed()
    physical_rag = PhysicalRAGIndex(dimensions=min(settings.embedding_dimensions, 128))
    business_catalog = executor.catalog.business_only()
    physical_rag.build(business_catalog, settings.historical_sql_path)
    ontology_without_assets = build_text2sql_graph(
        ontology,
        executor,
        sql_assets=None,
        history_enabled=False,
    )
    ontology_full = build_text2sql_graph(ontology, executor, sql_assets=sql_asset_service)
    strategy_router = StrategyRouter(
        {
            "schema": SchemaBaselineStrategy(business_catalog, executor, settings, model_factory),
            "rag": PhysicalRAGStrategy(
                business_catalog,
                physical_rag,
                executor,
                settings,
                model_factory,
            ),
            "ontology_no_sql_asset": OntologyStrategy(
                ontology_without_assets, sql_asset_enabled=False
            ),
            "ontology_full": OntologyStrategy(ontology_full, sql_asset_enabled=True),
        }
    )
    app.state.ontology = ontology
    app.state.executor = executor
    app.state.ontology_repository = runtime_repository
    app.state.ontology_builder = builder
    app.state.ontology_manager = ontology_manager
    app.state.ontology_drift_service = drift_service
    app.state.ontology_index_service = ontology_index_service
    app.state.object_query_service = object_query_service
    app.state.sql_asset_service = sql_asset_service
    app.state.physical_rag = physical_rag
    app.state.strategy_router = strategy_router
    app.state.evaluation_policy_inspector = EvaluationPolicyInspector(
        ontology.bundle, ontology.ontology_version_id
    )
    app.state.evaluation_service = EvaluationService(
        PostgresEvaluationRepository(engine),
        strategy_router,
        app.state.evaluation_policy_inspector,
        settings,
        database_snapshot_hash=database_snapshot_hash(
            business_catalog.model_dump_json(), "data/seed/002_seed.sql"
        ),
        physical_rag_build_id=physical_rag.build_id,
        ontology_version_id=ontology.ontology_version_id,
        sql_asset_build_id=(sql_asset_build.build_id if sql_asset_build else None),
    )
    app.state.graph = ontology_full
    app.state.ontology_runtime = {
        "legacy_fallback": legacy_fallback,
        "bundle_hash": runtime_artifact.bundle_hash if runtime_artifact else None,
        "compiler_version": runtime_artifact.compiler_version if runtime_artifact else None,
    }
    yield
    executor.engine.dispose()


app = FastAPI(
    title="Data Asset Agents API",
    version="0.3.0",
    description="Ontology-first Text-to-SQL with reviewed SQL asset retrieval",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8501"],
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)


def _governed_openapi() -> dict[str, Any]:
    if app.openapi_schema is not None:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    mutation_parts = {
        "object-types",
        "properties",
        "dimensions",
        "metrics",
        "link-types",
        "physical-joins",
        "bindings",
        "import-candidates",
        "validate",
        "submit",
        "approve",
        "reject",
        "publish",
    }
    for path, operations in schema.get("paths", {}).items():
        is_draft_delete = path.endswith("/ontology/drafts/{draft_id}")
        if "/ontology/drafts/{draft_id}/" not in path and not is_draft_delete:
            continue
        if not is_draft_delete and not any(f"/{part}" in path for part in mutation_parts):
            continue
        for method, operation in operations.items():
            if method.lower() not in {"post", "put", "delete"}:
                continue
            parameters = operation.setdefault("parameters", [])
            parameters.append(
                {
                    "name": "If-Match",
                    "in": "header",
                    "required": True,
                    "description": "Quoted resource_revision read from the Draft",
                    "schema": {"type": "string", "example": '"3"'},
                }
            )
    app.openapi_schema = schema
    return schema


app.openapi = _governed_openapi  # type: ignore[method-assign]


@app.exception_handler(DataAssetAgentsError)
async def domain_error_handler(_: Request, exc: DataAssetAgentsError):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=422,
        content={
            "error_code": getattr(exc, "code", "ONTOLOGY_ERROR"),
            "detail": str(exc),
            "current_revision": getattr(exc, "current_revision", None),
            "current_hash": getattr(exc, "current_hash", None),
        },
    )


@app.exception_handler(OntologyConflictError)
async def ontology_conflict_handler(_: Request, exc: OntologyConflictError):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=409,
        content={
            "error_code": exc.code,
            "detail": str(exc),
            "current_revision": exc.current_revision,
            "current_hash": exc.current_hash,
        },
    )


@app.exception_handler(OntologyGovernanceError)
async def ontology_governance_handler(_: Request, exc: OntologyGovernanceError):
    from fastapi.responses import JSONResponse

    conflict_codes = {
        "DRAFT_REVISION_CONFLICT",
        "DRAFT_CHANGED_DURING_VALIDATION",
        "REVIEW_SNAPSHOT_CHANGED",
    }
    return JSONResponse(
        status_code=409 if exc.code in conflict_codes else 422,
        content={
            "error_code": exc.code,
            "detail": str(exc),
            "current_revision": exc.current_revision,
            "current_hash": exc.current_hash,
        },
    )


@app.exception_handler(HTTPException)
async def http_error_handler(_: Request, exc: HTTPException):
    from fastapi.responses import JSONResponse

    if isinstance(exc.detail, dict) and "error_code" in exc.detail:
        content = exc.detail
    else:
        content = {
            "error_code": "HTTP_ERROR",
            "detail": exc.detail,
            "current_revision": None,
            "current_hash": None,
        }
    return JSONResponse(status_code=exc.status_code, content=content)


def _draft_revision(request: Request) -> int:
    raw = request.headers.get("if-match") or request.headers.get("x-draft-revision")
    if raw is None:
        raise HTTPException(
            status_code=428,
            detail={
                "error_code": "DRAFT_REVISION_REQUIRED",
                "detail": "If-Match or X-Draft-Revision is required",
                "current_revision": None,
                "current_hash": None,
            },
        )
    value = raw.strip().removeprefix("W/").strip('"')
    try:
        return int(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "DRAFT_REVISION_INVALID",
                "detail": "Invalid Draft revision header",
                "current_revision": None,
                "current_hash": None,
            },
        ) from exc


def _request_id(request: Request) -> str | None:
    return request.headers.get("x-request-id")


def _actor(request: Request) -> str:
    return request.headers.get("x-actor", "ontology-manager-api")


@app.exception_handler(UnsupportedQueryError)
async def unsupported_query_handler(_: Request, exc: UnsupportedQueryError):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={
            "status": "unsupported",
            "error_code": exc.code,
            "detail": str(exc),
        },
    )


@app.get("/health")
def health(request: Request, response: Response) -> dict[str, str]:
    database = "up" if request.app.state.executor.ping() else "down"
    if database == "down":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if database == "up" else "degraded", "database": database}


@app.post("/api/v1/query", response_model=QueryResponse)
def query(payload: QueryRequest, request: Request) -> QueryResponse:
    try:
        result = request.app.state.strategy_router.execute(
            payload.question,
            payload.query_mode,
            sql_asset_enabled=payload.sql_asset_enabled,
        )
        if result.generated_sql:
            result.evaluation_policy_report = request.app.state.evaluation_policy_inspector.inspect(
                result.generated_sql,
                semantic_query=result.semantic_query,
            )
        if result.status == "unsupported":
            raise UnsupportedQueryError(
                result.unsupported_reason or "该问题不在当前模式支持范围内",
                result.error_code or "UNSUPPORTED_QUERY",
            )
        encoded = jsonable_encoder(result)
        encoded["validation_report"] = encoded.get("ontology_policy_report") or encoded.get(
            "common_validation_report"
        )
        return QueryResponse.model_validate(encoded)
    except DataAssetAgentsError:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Text-to-SQL workflow failed: {exc}") from exc


@app.post("/api/v1/semantic/parse")
def parse_semantic(payload: QueryRequest, request: Request) -> dict[str, Any]:
    return request.app.state.ontology.parse(payload.question).model_dump(mode="json")


@app.post("/api/v1/semantic/search")
def search_semantic(payload: SemanticSearchRequest, request: Request) -> list[dict[str, Any]]:
    return [
        item.model_dump(mode="json")
        for item in request.app.state.ontology.search(payload.query, payload.limit)
    ]


@app.post("/api/v1/semantic/resolve")
def resolve_semantic(payload: SemanticResolveRequest, request: Request) -> dict[str, Any]:
    resolved = request.app.state.ontology.resolve(payload.semantic_query)
    return jsonable_encoder(resolved)


@app.get("/api/v1/ontology/concepts")
def ontology_concepts(request: Request) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in request.app.state.ontology.concepts()]


@app.get("/api/v1/ontology/metrics")
def ontology_metrics(request: Request) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in request.app.state.ontology.bundle.metrics]


@app.get("/api/v1/ontology/tables")
def ontology_tables(request: Request) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in request.app.state.ontology.tables()]


@app.post("/api/v1/ontology/drafts", response_model=OntologyDraftAggregate)
def create_ontology_draft(payload: CreateDraftRequest, request: Request) -> OntologyDraftAggregate:
    return request.app.state.ontology_manager.create_draft(payload)


@app.post(
    "/api/v1/ontology/drafts/from-seed",
    response_model=OntologyDraftAggregate,
)
def create_ontology_draft_from_seed(
    payload: CreateDraftFromSeedRequest, request: Request
) -> OntologyDraftAggregate:
    return request.app.state.ontology_manager.create_draft_from_seed(payload)


@app.get("/api/v1/ontology/drafts", response_model=list[OntologyDraft])
def list_ontology_drafts(request: Request) -> list[OntologyDraft]:
    return request.app.state.ontology_manager.list_drafts()


@app.get("/api/v1/ontology/drafts/{draft_id}", response_model=OntologyDraftAggregate)
def get_ontology_draft(draft_id: str, request: Request) -> OntologyDraftAggregate:
    return request.app.state.ontology_manager.get_draft(draft_id)


@app.get(
    "/api/v1/ontology/drafts/{draft_id}/audit-events",
    response_model=list[OntologyAuditEvent],
)
def draft_audit_events(
    draft_id: str, request: Request, limit: int = Query(default=100, ge=1, le=500)
) -> list[OntologyAuditEvent]:
    return request.app.state.ontology_manager.audit_events(
        draft_id=draft_id, limit=limit
    )


@app.delete("/api/v1/ontology/drafts/{draft_id}", status_code=204)
def delete_ontology_draft(draft_id: str, request: Request) -> Response:
    request.app.state.ontology_manager.delete_draft(
        draft_id, expected_revision=_draft_revision(request)
    )
    return Response(status_code=204)


@app.post(
    "/api/v1/ontology/drafts/{draft_id}/object-types",
    response_model=OntologyDraftAggregate,
)
def create_object_type(
    draft_id: str, payload: ObjectType, request: Request
) -> OntologyDraftAggregate:
    return request.app.state.ontology_manager.save_resource(
        draft_id,
        payload,
        expected_revision=_draft_revision(request),
        actor=_actor(request),
        request_id=_request_id(request),
    )


@app.put(
    "/api/v1/ontology/drafts/{draft_id}/object-types/{object_id}",
    response_model=OntologyDraftAggregate,
)
def update_object_type(
    draft_id: str, object_id: str, payload: ObjectType, request: Request
) -> OntologyDraftAggregate:
    if payload.id != object_id:
        raise HTTPException(status_code=422, detail="object_id must match payload.id")
    return request.app.state.ontology_manager.save_resource(
        draft_id, payload, expected_revision=_draft_revision(request), actor=_actor(request)
    )


@app.delete(
    "/api/v1/ontology/drafts/{draft_id}/object-types/{object_id}",
    response_model=OntologyDraftAggregate,
)
def delete_object_type(draft_id: str, object_id: str, request: Request) -> OntologyDraftAggregate:
    return request.app.state.ontology_manager.delete_resource(
        draft_id,
        "object_type",
        object_id,
        expected_revision=_draft_revision(request),
        actor=_actor(request),
    )


@app.post(
    "/api/v1/ontology/drafts/{draft_id}/properties",
    response_model=OntologyDraftAggregate,
)
def create_property(
    draft_id: str, payload: PropertyDefinition, request: Request
) -> OntologyDraftAggregate:
    return request.app.state.ontology_manager.save_resource(
        draft_id, payload, expected_revision=_draft_revision(request), actor=_actor(request)
    )


@app.put(
    "/api/v1/ontology/drafts/{draft_id}/properties/{property_id}",
    response_model=OntologyDraftAggregate,
)
def update_property(
    draft_id: str,
    property_id: str,
    payload: PropertyDefinition,
    request: Request,
) -> OntologyDraftAggregate:
    if payload.id != property_id:
        raise HTTPException(status_code=422, detail="property_id must match payload.id")
    return request.app.state.ontology_manager.save_resource(
        draft_id, payload, expected_revision=_draft_revision(request), actor=_actor(request)
    )


@app.delete(
    "/api/v1/ontology/drafts/{draft_id}/properties/{property_id}",
    response_model=OntologyDraftAggregate,
)
def delete_property(draft_id: str, property_id: str, request: Request) -> OntologyDraftAggregate:
    return request.app.state.ontology_manager.delete_resource(
        draft_id,
        "property",
        property_id,
        expected_revision=_draft_revision(request),
        actor=_actor(request),
    )


@app.post(
    "/api/v1/ontology/drafts/{draft_id}/dimensions",
    response_model=OntologyDraftAggregate,
)
def create_dimension(
    draft_id: str, payload: DimensionDefinition, request: Request
) -> OntologyDraftAggregate:
    return request.app.state.ontology_manager.save_resource(
        draft_id, payload, expected_revision=_draft_revision(request), actor=_actor(request)
    )


@app.put(
    "/api/v1/ontology/drafts/{draft_id}/dimensions/{dimension_id}",
    response_model=OntologyDraftAggregate,
)
def update_dimension(
    draft_id: str,
    dimension_id: str,
    payload: DimensionDefinition,
    request: Request,
) -> OntologyDraftAggregate:
    if payload.id != dimension_id:
        raise HTTPException(status_code=422, detail="dimension_id must match payload.id")
    return request.app.state.ontology_manager.save_resource(
        draft_id, payload, expected_revision=_draft_revision(request), actor=_actor(request)
    )


@app.delete(
    "/api/v1/ontology/drafts/{draft_id}/dimensions/{dimension_id}",
    response_model=OntologyDraftAggregate,
)
def delete_dimension(
    draft_id: str, dimension_id: str, request: Request
) -> OntologyDraftAggregate:
    return request.app.state.ontology_manager.delete_resource(
        draft_id,
        "dimension",
        dimension_id,
        expected_revision=_draft_revision(request),
        actor=_actor(request),
    )


@app.post(
    "/api/v1/ontology/drafts/{draft_id}/metrics",
    response_model=OntologyDraftAggregate,
)
def create_metric(
    draft_id: str, payload: MetricDefinition, request: Request
) -> OntologyDraftAggregate:
    return request.app.state.ontology_manager.save_resource(
        draft_id, payload, expected_revision=_draft_revision(request), actor=_actor(request)
    )


@app.put(
    "/api/v1/ontology/drafts/{draft_id}/metrics/{metric_id}",
    response_model=OntologyDraftAggregate,
)
def update_metric(
    draft_id: str,
    metric_id: str,
    payload: MetricDefinition,
    request: Request,
) -> OntologyDraftAggregate:
    if payload.id != metric_id:
        raise HTTPException(status_code=422, detail="metric_id must match payload.id")
    return request.app.state.ontology_manager.save_resource(
        draft_id, payload, expected_revision=_draft_revision(request), actor=_actor(request)
    )


@app.delete(
    "/api/v1/ontology/drafts/{draft_id}/metrics/{metric_id}",
    response_model=OntologyDraftAggregate,
)
def delete_metric(draft_id: str, metric_id: str, request: Request) -> OntologyDraftAggregate:
    return request.app.state.ontology_manager.delete_resource(
        draft_id,
        "metric",
        metric_id,
        expected_revision=_draft_revision(request),
        actor=_actor(request),
    )


@app.post(
    "/api/v1/ontology/drafts/{draft_id}/link-types",
    response_model=OntologyDraftAggregate,
)
def create_link_type(draft_id: str, payload: LinkType, request: Request) -> OntologyDraftAggregate:
    return request.app.state.ontology_manager.save_resource(
        draft_id, payload, expected_revision=_draft_revision(request), actor=_actor(request)
    )


@app.put(
    "/api/v1/ontology/drafts/{draft_id}/link-types/{link_id}",
    response_model=OntologyDraftAggregate,
)
def update_link_type(
    draft_id: str, link_id: str, payload: LinkType, request: Request
) -> OntologyDraftAggregate:
    if payload.id != link_id:
        raise HTTPException(status_code=422, detail="link_id must match payload.id")
    return request.app.state.ontology_manager.save_resource(
        draft_id, payload, expected_revision=_draft_revision(request), actor=_actor(request)
    )


@app.delete(
    "/api/v1/ontology/drafts/{draft_id}/link-types/{link_id}",
    response_model=OntologyDraftAggregate,
)
def delete_link_type(draft_id: str, link_id: str, request: Request) -> OntologyDraftAggregate:
    return request.app.state.ontology_manager.delete_resource(
        draft_id,
        "link_type",
        link_id,
        expected_revision=_draft_revision(request),
        actor=_actor(request),
    )


@app.post(
    "/api/v1/ontology/drafts/{draft_id}/physical-joins",
    response_model=OntologyDraftAggregate,
)
def create_physical_join(
    draft_id: str, payload: PhysicalJoinDefinition, request: Request
) -> OntologyDraftAggregate:
    return request.app.state.ontology_manager.save_resource(
        draft_id, payload, expected_revision=_draft_revision(request), actor=_actor(request)
    )


@app.put(
    "/api/v1/ontology/drafts/{draft_id}/physical-joins/{join_id}",
    response_model=OntologyDraftAggregate,
)
def update_physical_join(
    draft_id: str,
    join_id: str,
    payload: PhysicalJoinDefinition,
    request: Request,
) -> OntologyDraftAggregate:
    if payload.id != join_id:
        raise HTTPException(status_code=422, detail="join_id must match payload.id")
    return request.app.state.ontology_manager.save_resource(
        draft_id, payload, expected_revision=_draft_revision(request), actor=_actor(request)
    )


@app.delete(
    "/api/v1/ontology/drafts/{draft_id}/physical-joins/{join_id}",
    response_model=OntologyDraftAggregate,
)
def delete_physical_join(
    draft_id: str, join_id: str, request: Request
) -> OntologyDraftAggregate:
    return request.app.state.ontology_manager.delete_resource(
        draft_id,
        "physical_join",
        join_id,
        expected_revision=_draft_revision(request),
        actor=_actor(request),
    )


@app.get(
    "/api/v1/ontology/drafts/{draft_id}/diff",
    response_model=OntologyChangeSet,
)
def ontology_draft_diff(draft_id: str, request: Request) -> OntologyChangeSet:
    return request.app.state.ontology_manager.diff(draft_id)


@app.get(
    "/api/v1/ontology/drafts/{draft_id}/impact",
    response_model=OntologyImpactReport,
)
def ontology_draft_impact(draft_id: str, request: Request) -> OntologyImpactReport:
    return request.app.state.ontology_manager.impact(draft_id)


@app.get("/api/v1/ontology/data-sources", response_model=list[DataSourceDefinition])
def list_ontology_data_sources(request: Request) -> list[DataSourceDefinition]:
    return request.app.state.ontology_manager.repository.list_data_sources()


@app.post(
    "/api/v1/ontology/data-sources/{data_source_id}/inspect",
    response_model=DataSourceInspection,
)
def inspect_ontology_data_source(data_source_id: str, request: Request) -> DataSourceInspection:
    return request.app.state.ontology_manager.inspect_data_source(data_source_id)


@app.post(
    "/api/v1/ontology/drafts/{draft_id}/bindings",
    response_model=OntologyDraftAggregate,
)
def create_object_binding(
    draft_id: str, payload: ObjectDataSourceBinding, request: Request
) -> OntologyDraftAggregate:
    return request.app.state.ontology_manager.save_resource(
        draft_id, payload, expected_revision=_draft_revision(request), actor=_actor(request)
    )


@app.put(
    "/api/v1/ontology/drafts/{draft_id}/bindings/{binding_id}",
    response_model=OntologyDraftAggregate,
)
def update_object_binding(
    draft_id: str,
    binding_id: str,
    payload: ObjectDataSourceBinding,
    request: Request,
) -> OntologyDraftAggregate:
    if payload.id != binding_id:
        raise HTTPException(status_code=422, detail="binding_id must match payload.id")
    return request.app.state.ontology_manager.save_resource(
        draft_id, payload, expected_revision=_draft_revision(request), actor=_actor(request)
    )


@app.delete(
    "/api/v1/ontology/drafts/{draft_id}/bindings/{binding_id}",
    response_model=OntologyDraftAggregate,
)
def delete_object_binding(
    draft_id: str, binding_id: str, request: Request
) -> OntologyDraftAggregate:
    return request.app.state.ontology_manager.delete_resource(
        draft_id,
        "binding",
        binding_id,
        expected_revision=_draft_revision(request),
        actor=_actor(request),
    )


@app.post(
    "/api/v1/ontology/drafts/{draft_id}/candidates/generate",
    response_model=ObjectCandidateSet,
)
def generate_object_candidates(
    draft_id: str, request: Request
) -> ObjectCandidateSet:
    return request.app.state.ontology_manager.generate_candidates(draft_id)


@app.post(
    "/api/v1/ontology/drafts/{draft_id}/import-candidates",
    response_model=OntologyDraftAggregate,
)
def import_object_candidates(
    draft_id: str,
    payload: ImportObjectCandidatesRequest,
    request: Request,
) -> OntologyDraftAggregate:
    return request.app.state.ontology_manager.import_candidates(
        draft_id,
        payload,
        expected_revision=_draft_revision(request),
        request_id=_request_id(request),
    )


@app.post(
    "/api/v1/ontology/drafts/migrate-legacy",
    response_model=OntologyDraftAggregate,
)
def migrate_legacy_ontology(
    payload: MigrateLegacyRequest, request: Request
) -> OntologyDraftAggregate:
    return request.app.state.ontology_manager.migrate_legacy(payload)


@app.post(
    "/api/v1/ontology/drafts/{draft_id}/validate",
    response_model=OntologyDraftAggregate,
)
def validate_object_draft(draft_id: str, request: Request) -> OntologyDraftAggregate:
    return request.app.state.ontology_manager.validate(
        draft_id,
        expected_revision=_draft_revision(request),
        actor=_actor(request),
        request_id=_request_id(request),
    )


@app.post(
    "/api/v1/ontology/drafts/{draft_id}/submit",
    response_model=OntologyDraftAggregate,
)
def submit_object_draft(
    draft_id: str, payload: ActorRequest, request: Request
) -> OntologyDraftAggregate:
    return request.app.state.ontology_manager.submit(
        draft_id,
        payload,
        expected_revision=_draft_revision(request),
        request_id=_request_id(request),
    )


@app.post(
    "/api/v1/ontology/drafts/{draft_id}/approve",
    response_model=OntologyDraftAggregate,
)
def approve_object_draft(
    draft_id: str, payload: ActorRequest, request: Request
) -> OntologyDraftAggregate:
    return request.app.state.ontology_manager.approve(
        draft_id,
        payload,
        expected_revision=_draft_revision(request),
        request_id=_request_id(request),
    )


@app.post(
    "/api/v1/ontology/drafts/{draft_id}/reject",
    response_model=OntologyDraftAggregate,
)
def reject_object_draft(
    draft_id: str, payload: RejectDraftRequest, request: Request
) -> OntologyDraftAggregate:
    return request.app.state.ontology_manager.reject(
        draft_id,
        payload,
        expected_revision=_draft_revision(request),
        request_id=_request_id(request),
    )


@app.post(
    "/api/v1/ontology/drafts/{draft_id}/publish",
    response_model=OntologyVersion,
)
def publish_object_draft(
    draft_id: str, payload: PublishDraftRequest, request: Request
) -> OntologyVersion:
    previous = request.app.state.ontology_repository.get_current_version()
    version = request.app.state.ontology_manager.publish(
        draft_id, payload, expected_revision=_draft_revision(request)
    )
    try:
        _activate_runtime(request.app, version)
        request.app.state.ontology_manager.record_runtime_event(
            OntologyAuditAction.ACTIVATED, version.id, payload.actor
        )
    except Exception as exc:
        request.app.state.ontology_manager.record_runtime_event(
            OntologyAuditAction.ACTIVATION_FAILED,
            version.id,
            payload.actor,
            metadata={"error_type": type(exc).__name__},
        )
        if previous is not None:
            request.app.state.ontology_repository.activate_version(previous.version)
            _activate_runtime(request.app, previous)
            request.app.state.ontology_manager.record_runtime_event(
                OntologyAuditAction.ROLLED_BACK,
                previous.id,
                payload.actor,
                metadata={"failed_version_id": version.id},
            )
        raise HTTPException(
            status_code=500,
            detail=f"Object ontology activation failed and was rolled back: {exc}",
        ) from exc
    return version


@app.get("/api/v1/ontology/object-types", response_model=list[ObjectType])
def published_object_types(request: Request) -> list[ObjectType]:
    return request.app.state.ontology_manager.published()[1].object_types


@app.get("/api/v1/ontology/object-types/{object_id}", response_model=ObjectType)
def published_object_type(object_id: str, request: Request) -> ObjectType:
    objects = request.app.state.ontology_manager.published()[1].object_types
    found = next((item for item in objects if item.id == object_id), None)
    if found is None:
        raise HTTPException(status_code=404, detail="Published object type not found")
    return found


@app.get("/api/v1/ontology/link-types", response_model=list[LinkType])
def published_link_types(request: Request) -> list[LinkType]:
    return request.app.state.ontology_manager.published()[1].link_types


@app.get("/api/v1/ontology/object-graph", response_model=ObjectGraph)
def published_object_graph(request: Request) -> ObjectGraph:
    return request.app.state.ontology_manager.object_graph()


@app.post("/api/v1/ontology/sync-runs", response_model=OntologySyncRun)
def create_ontology_sync_run(request: Request) -> OntologySyncRun:
    return request.app.state.ontology_drift_service.create_run()


@app.get("/api/v1/ontology/sync-runs", response_model=list[OntologySyncRun])
def list_ontology_sync_runs(request: Request) -> list[OntologySyncRun]:
    return request.app.state.ontology_drift_service.list_runs()


@app.get("/api/v1/ontology/sync-runs/{run_id}", response_model=OntologySyncRun)
def get_ontology_sync_run(run_id: str, request: Request) -> OntologySyncRun:
    run = request.app.state.ontology_drift_service.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Ontology sync run not found")
    return run


@app.post(
    "/api/v1/ontology/bindings/{binding_id}/inspect",
    response_model=SchemaDriftReport,
)
def inspect_published_binding(binding_id: str, request: Request) -> SchemaDriftReport:
    return request.app.state.ontology_drift_service.inspect_binding(binding_id)


@app.get("/api/v1/ontology/drift", response_model=list[SchemaDriftReport])
def list_ontology_drift(request: Request) -> list[SchemaDriftReport]:
    return request.app.state.ontology_drift_service.list_reports()


@app.get("/api/v1/ontology/index-builds", response_model=list[OntologyIndexBuild])
def list_ontology_index_builds(request: Request) -> list[OntologyIndexBuild]:
    return request.app.state.ontology_index_service.list()


@app.post("/api/v1/ontology/index-builds", response_model=OntologyIndexBuild)
def create_ontology_index_build(
    payload: OntologyIndexBuildRequest, request: Request
) -> OntologyIndexBuild:
    return request.app.state.ontology_index_service.build(payload)


@app.get(
    "/api/v1/ontology/index-builds/{build_id}", response_model=OntologyIndexBuild
)
def get_ontology_index_build(build_id: str, request: Request) -> OntologyIndexBuild:
    build = request.app.state.ontology_index_service.get(build_id)
    if build is None:
        raise HTTPException(status_code=404, detail="Ontology index build not found")
    return build


def _reject_unknown_object_query_parameters(
    request: Request, allowed: set[str]
) -> None:
    unexpected = set(request.query_params) - allowed
    if unexpected:
        raise HTTPException(
            status_code=422,
            detail="Unsupported Object Explorer query parameter(s): "
            + ", ".join(sorted(unexpected)),
        )


@app.get("/api/v1/objects/{object_type_id}", response_model=list[ObjectRecord])
def list_objects(
    object_type_id: str,
    request: Request,
    property_id: str | None = None,
    operator: ObjectFilterOperator = ObjectFilterOperator.EQ,
    value: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[ObjectRecord]:
    _reject_unknown_object_query_parameters(
        request, {"property_id", "operator", "value", "limit"}
    )
    filters: list[ObjectFilter] = []
    if property_id is not None:
        if value is None:
            raise HTTPException(status_code=422, detail="Filtered property requires value")
        parsed_value: str | list[str] = (
            [item.strip() for item in value.split(",") if item.strip()]
            if operator == ObjectFilterOperator.IN
            else value
        )
        filters.append(
            ObjectFilter(property_id=property_id, operator=operator, value=parsed_value)
        )
    return request.app.state.object_query_service.list_objects(
        object_type_id, filters, limit
    )


@app.get("/api/v1/objects/{object_type_id}/{object_id}", response_model=ObjectRecord)
def get_object(object_type_id: str, object_id: str, request: Request) -> ObjectRecord:
    _reject_unknown_object_query_parameters(request, set())
    record = request.app.state.object_query_service.get_object(object_type_id, object_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Object not found")
    return record


@app.get(
    "/api/v1/objects/{object_type_id}/{object_id}/links/{link_type_id}",
    response_model=list[ObjectRecord],
)
def navigate_object_link(
    object_type_id: str,
    object_id: str,
    link_type_id: str,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[ObjectRecord]:
    _reject_unknown_object_query_parameters(request, {"limit"})
    return request.app.state.object_query_service.navigate(
        object_type_id, object_id, link_type_id, limit
    )


@app.post("/api/v1/sql-assets/build", response_model=SQLAssetBuildReport)
def build_sql_assets(payload: SQLAssetBuildRequest, request: Request) -> SQLAssetBuildReport:
    if payload.source_path is not None:
        configured = request.app.state.sql_asset_service.settings.historical_sql_path
        if Path(payload.source_path).resolve() != configured.resolve():
            raise HTTPException(
                status_code=422,
                detail="source_path must be the configured allowlisted historical SQL file",
            )
    return request.app.state.sql_asset_service.build(payload.source_path)


@app.get("/api/v1/sql-assets", response_model=list[SQLAsset])
def list_sql_assets(request: Request, limit: int = 100, offset: int = 0) -> list[SQLAsset]:
    if not 1 <= limit <= 1000 or offset < 0:
        raise HTTPException(status_code=422, detail="Invalid pagination")
    return request.app.state.sql_asset_service.list(limit, offset)


@app.get("/api/v1/sql-assets/{asset_id}", response_model=SQLAsset)
def get_sql_asset(asset_id: str, request: Request) -> SQLAsset:
    asset = request.app.state.sql_asset_service.get(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="SQL asset not found")
    return asset


@app.post("/api/v1/sql-assets/search", response_model=list[SQLAssetSearchResult])
def search_sql_assets(
    payload: SQLAssetSearchRequest, request: Request
) -> list[SQLAssetSearchResult]:
    if payload.semantic_query is None:
        ontology = request.app.state.ontology
        concepts = ontology.search(payload.question)
        semantic = ontology.parse(payload.question, concepts)
        if semantic.clarification_required or not semantic.metric_ids:
            return []
        resolved = ontology.resolve(semantic)
        plan = JoinPlanner(ontology.bundle.joins).plan(resolved["selected_tables"])
        payload = payload.model_copy(
            update={
                "semantic_query": semantic,
                "selected_tables": plan.tables,
                "selected_columns": resolved["selected_columns"],
                "join_conditions": [item.condition for item in plan.steps],
            }
        )
    return request.app.state.sql_asset_service.search(payload)


@app.post(
    "/api/v1/evaluation/runs",
    response_model=EvaluationRun,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_evaluation_run(
    payload: EvaluationRunRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> EvaluationRun:
    run = request.app.state.evaluation_service.create_run(payload)
    background_tasks.add_task(
        request.app.state.evaluation_service.execute_run,
        run.run_id,
        payload.benchmark_path,
    )
    return run


@app.get("/api/v1/evaluation/runs", response_model=list[EvaluationRun])
def list_evaluation_runs(request: Request, limit: int = 100) -> list[EvaluationRun]:
    if not 1 <= limit <= 1000:
        raise HTTPException(status_code=422, detail="Invalid limit")
    return request.app.state.evaluation_service.repository.list_runs(limit)


@app.get("/api/v1/evaluation/runs/{run_id}")
def get_evaluation_run(run_id: str, request: Request) -> dict[str, Any]:
    service = request.app.state.evaluation_service
    run = service.repository.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Evaluation run not found")
    response: dict[str, Any] = {"run": run.model_dump(mode="json"), "metrics": None}
    if run.status == "COMPLETED":
        response["metrics"] = service.metrics_for_run(run_id).model_dump(mode="json")
    return response


@app.get(
    "/api/v1/evaluation/runs/{run_id}/cases",
    response_model=list[EvaluationCaseResult],
)
def get_evaluation_cases(run_id: str, request: Request) -> list[EvaluationCaseResult]:
    if request.app.state.evaluation_service.repository.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Evaluation run not found")
    return request.app.state.evaluation_service.repository.list_cases(run_id)


@app.get("/api/v1/evaluation/compare", response_model=EvaluationComparison)
def compare_evaluation_runs(
    request: Request,
    run_id: Annotated[list[str] | None, Query()] = None,
    allow_mismatch: bool = False,
) -> EvaluationComparison:
    service = request.app.state.evaluation_service
    selected = (
        run_id
        or [run.run_id for run in service.repository.list_runs(20) if run.status == "COMPLETED"][:4]
    )
    if not selected:
        return EvaluationComparison(runs=[], warnings=["No completed runs"])
    return service.compare(
        selected,
        str(get_settings().evaluation_benchmark_path),
        allow_mismatch=allow_mismatch,
    )


@app.get("/api/v1/evaluation/runs/{run_id}/export")
def export_evaluation_run(
    run_id: str,
    request: Request,
    format: str = Query(default="json", pattern="^(json|csv)$"),
) -> Response:
    cases = request.app.state.evaluation_service.repository.list_cases(run_id)
    if not cases:
        raise HTTPException(status_code=404, detail="Evaluation cases not found")
    rows = [item.model_dump(mode="json") for item in cases]
    if format == "json":
        import json

        body = json.dumps(rows, ensure_ascii=False, indent=2)
        media_type = "application/json"
    else:
        output = io.StringIO()
        fieldnames = sorted({key for row in rows for key in row})
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: str(value) if isinstance(value, (dict, list)) else value
                    for key, value in row.items()
                }
            )
        body = output.getvalue()
        media_type = "text/csv"
    return Response(
        body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="evaluation-{run_id}.{format}"'},
    )


@app.post("/api/v1/ontology/build", response_model=OntologyBuildResult)
def build_ontology(payload: OntologyBuildRequest, request: Request) -> OntologyBuildResult:
    return request.app.state.ontology_builder.build(payload)


@app.get("/api/v1/ontology/candidates", response_model=list[CandidateEnvelope])
def ontology_candidates(
    request: Request,
    status_filter: ReviewStatus | None = None,
    candidate_type: str | None = None,
    snapshot_id: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[CandidateEnvelope]:
    if candidate_type not in {None, "concept", "mapping", "join"}:
        raise HTTPException(status_code=422, detail="Invalid candidate_type")
    if not 1 <= limit <= 1000 or offset < 0:
        raise HTTPException(status_code=422, detail="Invalid pagination")
    return request.app.state.ontology_builder.list_candidates(
        status=status_filter,
        candidate_type=candidate_type,
        snapshot_id=snapshot_id,
        limit=limit,
        offset=offset,
    )


@app.get(
    "/api/v1/ontology/candidates/{candidate_id}",
    response_model=CandidateEnvelope,
)
def ontology_candidate(candidate_id: str, request: Request) -> CandidateEnvelope:
    return request.app.state.ontology_builder.get_candidate(candidate_id)


@app.post(
    "/api/v1/ontology/candidates/{candidate_id}/verify",
    response_model=CandidateEnvelope,
)
def verify_ontology_candidate(
    candidate_id: str,
    payload: CandidateReviewRequest,
    request: Request,
) -> CandidateEnvelope:
    return request.app.state.ontology_builder.verify(candidate_id, payload)


@app.post(
    "/api/v1/ontology/candidates/{candidate_id}/reject",
    response_model=CandidateEnvelope,
)
def reject_ontology_candidate(
    candidate_id: str,
    payload: CandidateReviewRequest,
    request: Request,
) -> CandidateEnvelope:
    return request.app.state.ontology_builder.reject(candidate_id, payload)


@app.post("/api/v1/ontology/publish", response_model=OntologyVersion)
def publish_ontology(payload: OntologyPublishRequest, request: Request) -> OntologyVersion:
    previous = request.app.state.ontology_repository.get_current_version()
    version = request.app.state.ontology_builder.publish(payload)
    request.app.state.ontology_manager.record_runtime_event(
        OntologyAuditAction.PUBLISHED,
        version.id,
        payload.published_by,
        metadata={"legacy_fallback": True},
    )
    try:
        _activate_runtime(request.app, version)
        request.app.state.ontology_manager.record_runtime_event(
            OntologyAuditAction.ACTIVATED,
            version.id,
            payload.published_by,
            metadata=request.app.state.ontology_runtime,
        )
    except Exception as exc:
        request.app.state.ontology_manager.record_runtime_event(
            OntologyAuditAction.ACTIVATION_FAILED,
            version.id,
            payload.published_by,
            metadata={"error_type": type(exc).__name__},
        )
        if previous is not None:
            request.app.state.ontology_repository.activate_version(previous.version)
            _activate_runtime(request.app, previous)
            request.app.state.ontology_manager.record_runtime_event(
                OntologyAuditAction.ROLLED_BACK,
                previous.id,
                payload.published_by,
                metadata={"failed_version_id": version.id},
            )
        raise HTTPException(
            status_code=500,
            detail=f"Published version failed runtime activation and was rolled back: {exc}",
        ) from exc
    return version


@app.post(
    "/api/v1/ontology/publish/validate",
    response_model=PublishDryRunReport,
)
def validate_ontology_publish(
    payload: OntologyPublishRequest,
    request: Request,
) -> PublishDryRunReport:
    return request.app.state.ontology_builder.validate_publish(payload)


@app.get("/api/v1/ontology/versions", response_model=list[OntologyVersion])
def ontology_versions(request: Request) -> list[OntologyVersion]:
    return request.app.state.ontology_builder.versions()


@app.get(
    "/api/v1/ontology/versions/{version_id}/compiled-artifact",
    response_model=CompiledArtifactSummary,
)
def ontology_compiled_artifact(
    version_id: str,
    request: Request,
    include_bundle: bool = Query(default=False),
) -> CompiledArtifactSummary:
    return request.app.state.ontology_manager.compiled_artifact(
        version_id, include_bundle=include_bundle
    )


@app.get(
    "/api/v1/ontology/versions/{version_id}/audit-events",
    response_model=list[OntologyAuditEvent],
)
def version_audit_events(
    version_id: str, request: Request, limit: int = Query(default=100, ge=1, le=500)
) -> list[OntologyAuditEvent]:
    return request.app.state.ontology_manager.audit_events(
        version_id=version_id, limit=limit
    )


@app.post(
    "/api/v1/ontology/versions/{version}/activate",
    response_model=OntologyVersion,
)
def activate_ontology_version(version: str, request: Request) -> OntologyVersion:
    previous = request.app.state.ontology_repository.get_current_version()
    target = next(
        (
            item
            for item in request.app.state.ontology_repository.list_versions()
            if item.version == version
        ),
        None,
    )
    if target is None:
        raise OntologyGovernanceError(
            f"Published ontology version not found: {version}",
            "ONTOLOGY_VERSION_NOT_FOUND",
        )
    candidate_bundle = request.app.state.ontology_repository.load_published_bundle(version)
    if candidate_bundle is None:
        request.app.state.ontology_manager.record_runtime_event(
            OntologyAuditAction.ACTIVATION_FAILED,
            target.id,
            _actor(request),
            metadata={"error_code": "ONTOLOGY_BUNDLE_NOT_FOUND"},
        )
        raise OntologyGovernanceError(
            f"Ontology bundle not found: {version}", "ONTOLOGY_BUNDLE_NOT_FOUND"
        )
    try:
        request.app.state.ontology_manager.load_runtime_bundle(target.id, candidate_bundle)
    except Exception as exc:
        request.app.state.ontology_manager.record_runtime_event(
            OntologyAuditAction.ACTIVATION_FAILED,
            target.id,
            _actor(request),
            metadata={"error_type": type(exc).__name__},
        )
        raise
    activated = request.app.state.ontology_builder.activate(version)
    try:
        _activate_runtime(request.app, activated)
        activation_action = (
            OntologyAuditAction.ROLLED_BACK
            if previous is not None and activated.published_at < previous.published_at
            else OntologyAuditAction.ACTIVATED
        )
        request.app.state.ontology_manager.record_runtime_event(
            activation_action,
            activated.id,
            _actor(request),
            metadata=request.app.state.ontology_runtime,
        )
    except Exception as exc:
        request.app.state.ontology_manager.record_runtime_event(
            OntologyAuditAction.ACTIVATION_FAILED,
            activated.id,
            _actor(request),
            metadata={"error_type": type(exc).__name__},
        )
        if previous is not None:
            request.app.state.ontology_repository.activate_version(previous.version)
            _activate_runtime(request.app, previous)
            request.app.state.ontology_manager.record_runtime_event(
                OntologyAuditAction.ROLLED_BACK,
                previous.id,
                _actor(request),
                metadata={"failed_version_id": activated.id},
            )
        raise HTTPException(
            status_code=500,
            detail=f"Ontology activation failed health checks and was rolled back: {exc}",
        ) from exc
    return activated


def _activate_runtime(app_instance: FastAPI, version: OntologyVersion) -> None:
    """Reload all online consumers and prove the target benchmark remains healthy."""

    if not app_instance.state.ontology.reload_version(version.version):
        raise RuntimeError(f"Ontology version could not be loaded: {version.version}")
    governed_bundle, legacy_fallback, artifact = (
        app_instance.state.ontology_manager.load_runtime_bundle(
            version.id, app_instance.state.ontology.bundle
        )
    )
    app_instance.state.ontology.bundle = governed_bundle
    app_instance.state.ontology.ontology_version_id = version.id
    app_instance.state.ontology.compiled_bundle_hash = artifact.bundle_hash if artifact else ""
    app_instance.state.ontology.compiler_version = (
        artifact.compiler_version if artifact else "legacy"
    )
    app_instance.state.ontology._refresh_indexes()
    app_instance.state.ontology_runtime = {
        "legacy_fallback": legacy_fallback,
        "bundle_hash": artifact.bundle_hash if artifact else None,
        "compiler_version": artifact.compiler_version if artifact else None,
    }
    app_instance.state.executor.set_ontology(app_instance.state.ontology.bundle)
    manager = app_instance.state.ontology_manager
    manager.base_bundle = app_instance.state.ontology.bundle
    manager.migrator = manager.migrator.__class__(app_instance.state.ontology.bundle)
    manager.validator = OntologyDraftValidator(
        app_instance.state.ontology.bundle,
        engine=app_instance.state.executor.engine,
        executor=app_instance.state.executor,
    )
    manager.change_analyzer.bundle = app_instance.state.ontology.bundle
    app_instance.state.ontology_drift_service.bundle = app_instance.state.ontology.bundle
    app_instance.state.ontology.rebuild_search_index(version.id)
    app_instance.state.sql_asset_service.ontology = app_instance.state.ontology
    app_instance.state.sql_asset_service.parser = HistoricalSQLParser()
    app_instance.state.sql_asset_service.initialize_if_needed()
    candidate_graph = build_text2sql_graph(
        app_instance.state.ontology,
        app_instance.state.executor,
        sql_assets=app_instance.state.sql_asset_service,
    )
    candidate_no_asset_graph = build_text2sql_graph(
        app_instance.state.ontology,
        app_instance.state.executor,
        sql_assets=None,
        history_enabled=False,
    )
    if not app_instance.state.executor.ping():
        raise RuntimeError("Database health check failed after ontology activation")
    health_result = candidate_graph.invoke(
        {
            "question": "查询近30天各分行信用卡交易金额和交易笔数。",
            "query_mode": "ontology",
            "retry_count": 0,
            "trace_steps": [],
        }
    )
    if health_result.get("status") != "success":
        raise RuntimeError("Target Text-to-SQL health query failed after activation")
    app_instance.state.graph = candidate_graph
    app_instance.state.strategy_router.strategies.update(
        {
            "ontology_no_sql_asset": OntologyStrategy(
                candidate_no_asset_graph, sql_asset_enabled=False
            ),
            "ontology_full": OntologyStrategy(candidate_graph, sql_asset_enabled=True),
        }
    )
    app_instance.state.evaluation_policy_inspector = EvaluationPolicyInspector(
        app_instance.state.ontology.bundle,
        app_instance.state.ontology.ontology_version_id,
    )
    evaluation_service = app_instance.state.evaluation_service
    evaluation_service.strategies = app_instance.state.strategy_router
    evaluation_service.inspector = app_instance.state.evaluation_policy_inspector
    evaluation_service.ontology_version_id = app_instance.state.ontology.ontology_version_id
    latest_sql_build = app_instance.state.sql_asset_service.repository.latest_ready(
        app_instance.state.ontology.ontology_version_id
    )
    evaluation_service.sql_asset_build_id = latest_sql_build.build_id if latest_sql_build else None


@app.get("/api/v1/graph")
def graph_definition() -> dict[str, Any]:
    return {
        "nodes": GRAPH_NODES,
        "edges": [{"source": source, "target": target} for source, target in GRAPH_EDGES],
    }
