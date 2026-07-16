import csv
import io
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine

from data_asset_agents.core.config import get_settings
from data_asset_agents.core.errors import DataAssetAgentsError, UnsupportedQueryError
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
    ontology_full = build_text2sql_graph(
        ontology, executor, sql_assets=sql_asset_service
    )
    strategy_router = StrategyRouter(
        {
            "schema": SchemaBaselineStrategy(
                business_catalog, executor, settings, model_factory
            ),
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
            "ontology_full": OntologyStrategy(
                ontology_full, sql_asset_enabled=True
            ),
        }
    )
    app.state.ontology = ontology
    app.state.executor = executor
    app.state.ontology_repository = runtime_repository
    app.state.ontology_builder = builder
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
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.exception_handler(DataAssetAgentsError)
async def domain_error_handler(_: Request, exc: DataAssetAgentsError):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=422, content={"detail": str(exc)})


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
            result.evaluation_policy_report = (
                request.app.state.evaluation_policy_inspector.inspect(
                    result.generated_sql,
                    semantic_query=result.semantic_query,
                )
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


@app.post("/api/v1/sql-assets/build", response_model=SQLAssetBuildReport)
def build_sql_assets(
    payload: SQLAssetBuildRequest, request: Request
) -> SQLAssetBuildReport:
    if payload.source_path is not None:
        configured = request.app.state.sql_asset_service.settings.historical_sql_path
        if Path(payload.source_path).resolve() != configured.resolve():
            raise HTTPException(
                status_code=422,
                detail="source_path must be the configured allowlisted historical SQL file",
            )
    return request.app.state.sql_asset_service.build(payload.source_path)


@app.get("/api/v1/sql-assets", response_model=list[SQLAsset])
def list_sql_assets(
    request: Request, limit: int = 100, offset: int = 0
) -> list[SQLAsset]:
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
def get_evaluation_cases(
    run_id: str, request: Request
) -> list[EvaluationCaseResult]:
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
    selected = run_id or [
        run.run_id
        for run in service.repository.list_runs(20)
        if run.status == "COMPLETED"
    ][:4]
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
def build_ontology(
    payload: OntologyBuildRequest, request: Request
) -> OntologyBuildResult:
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
def publish_ontology(
    payload: OntologyPublishRequest, request: Request
) -> OntologyVersion:
    previous = request.app.state.ontology_repository.get_current_version()
    version = request.app.state.ontology_builder.publish(payload)
    try:
        _activate_runtime(request.app, version)
    except Exception as exc:
        if previous is not None:
            request.app.state.ontology_repository.activate_version(previous.version)
            _activate_runtime(request.app, previous)
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


@app.post(
    "/api/v1/ontology/versions/{version}/activate",
    response_model=OntologyVersion,
)
def activate_ontology_version(version: str, request: Request) -> OntologyVersion:
    previous = request.app.state.ontology_repository.get_current_version()
    activated = request.app.state.ontology_builder.activate(version)
    try:
        _activate_runtime(request.app, activated)
    except Exception as exc:
        if previous is not None:
            request.app.state.ontology_repository.activate_version(previous.version)
            _activate_runtime(request.app, previous)
        raise HTTPException(
            status_code=500,
            detail=f"Ontology activation failed health checks and was rolled back: {exc}",
        ) from exc
    return activated


def _activate_runtime(app_instance: FastAPI, version: OntologyVersion) -> None:
    """Reload all online consumers and prove the target benchmark remains healthy."""

    if not app_instance.state.ontology.reload_version(version.version):
        raise RuntimeError(f"Ontology version could not be loaded: {version.version}")
    app_instance.state.executor.set_ontology(app_instance.state.ontology.bundle)
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
            "ontology_full": OntologyStrategy(
                candidate_graph, sql_asset_enabled=True
            ),
        }
    )
    app_instance.state.evaluation_policy_inspector = EvaluationPolicyInspector(
        app_instance.state.ontology.bundle,
        app_instance.state.ontology.ontology_version_id,
    )
    evaluation_service = app_instance.state.evaluation_service
    evaluation_service.strategies = app_instance.state.strategy_router
    evaluation_service.inspector = app_instance.state.evaluation_policy_inspector
    evaluation_service.ontology_version_id = (
        app_instance.state.ontology.ontology_version_id
    )
    latest_sql_build = app_instance.state.sql_asset_service.repository.latest_ready(
        app_instance.state.ontology.ontology_version_id
    )
    evaluation_service.sql_asset_build_id = (
        latest_sql_build.build_id if latest_sql_build else None
    )


@app.get("/api/v1/graph")
def graph_definition() -> dict[str, Any]:
    return {
        "nodes": GRAPH_NODES,
        "edges": [{"source": source, "target": target} for source, target in GRAPH_EDGES],
    }
