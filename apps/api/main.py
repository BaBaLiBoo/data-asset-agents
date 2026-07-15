from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine

from data_asset_agents.core.config import get_settings
from data_asset_agents.core.errors import DataAssetAgentsError, UnsupportedQueryError
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
    ReviewStatus,
)
from data_asset_agents.ontology.repository import (
    PostgresOntologyRepository,
    YamlOntologyRepository,
)
from data_asset_agents.ontology.service import OntologyService
from data_asset_agents.sql_assets import HistoricalSQLParser
from data_asset_agents.text2sql.graph import GRAPH_EDGES, GRAPH_NODES, build_text2sql_graph
from data_asset_agents.text2sql.models import (
    QueryRequest,
    QueryResponse,
    SemanticResolveRequest,
    SemanticSearchRequest,
)


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
    ontology = OntologyService(
        YamlOntologyRepository(settings.ontology_path), runtime_repository
    )
    executor = QueryExecutor(settings, ontology.bundle, engine=engine)
    builder = OntologyBuildService(
        inspector=MetadataInspector(engine),
        sql_parser=HistoricalSQLParser(),
        generator=CandidateGenerator(settings, ModelFactory(settings)),
        repository=runtime_repository,
        seed_bundle=ontology.seed_bundle,
        historical_sql_path=settings.historical_sql_path,
    )
    app.state.ontology = ontology
    app.state.executor = executor
    app.state.ontology_repository = runtime_repository
    app.state.ontology_builder = builder
    app.state.graph = build_text2sql_graph(ontology, executor)
    yield
    executor.engine.dispose()


app = FastAPI(
    title="Data Asset Agents API",
    version="0.2.0",
    description="Ontology-first MiniBank Text-to-SQL and reviewed ontology builder",
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
        result = request.app.state.graph.invoke(
            {
                "question": payload.question,
                "query_mode": payload.query_mode,
                "retry_count": 0,
                "trace_steps": [],
            }
        )
        if result.get("status") == "unsupported":
            raise UnsupportedQueryError(
                result.get("unsupported_reason") or "该问题不在第一阶段支持范围内",
                result.get("error_code") or "UNSUPPORTED_QUERY",
            )
        return QueryResponse.model_validate(result)
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
    version = request.app.state.ontology_builder.publish(payload)
    if not request.app.state.ontology.reload_published():
        raise HTTPException(status_code=500, detail="Published ontology could not be loaded")
    request.app.state.executor.set_ontology(request.app.state.ontology.bundle)
    request.app.state.graph = build_text2sql_graph(
        request.app.state.ontology, request.app.state.executor
    )
    return version


@app.get("/api/v1/ontology/versions", response_model=list[OntologyVersion])
def ontology_versions(request: Request) -> list[OntologyVersion]:
    return request.app.state.ontology_builder.versions()


@app.get("/api/v1/graph")
def graph_definition() -> dict[str, Any]:
    return {
        "nodes": GRAPH_NODES,
        "edges": [{"source": source, "target": target} for source, target in GRAPH_EDGES],
    }
