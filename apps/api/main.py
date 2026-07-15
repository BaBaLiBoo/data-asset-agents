from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware

from data_asset_agents.core.config import get_settings
from data_asset_agents.core.errors import DataAssetAgentsError
from data_asset_agents.execution.executor import QueryExecutor
from data_asset_agents.ontology.repository import YamlOntologyRepository
from data_asset_agents.ontology.service import OntologyService
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
    ontology = OntologyService(YamlOntologyRepository(settings.ontology_path))
    executor = QueryExecutor(settings)
    app.state.ontology = ontology
    app.state.executor = executor
    app.state.graph = build_text2sql_graph(ontology, executor)
    yield
    executor.engine.dispose()


app = FastAPI(
    title="Data Asset Agents API",
    version="0.1.0",
    description="Ontology-first MiniBank Text-to-SQL MVP",
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


@app.get("/health")
def health(request: Request) -> dict[str, str]:
    database = "up" if request.app.state.executor.ping() else "down"
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


@app.get("/api/v1/graph")
def graph_definition() -> dict[str, Any]:
    return {
        "nodes": GRAPH_NODES,
        "edges": [{"source": source, "target": target} for source, target in GRAPH_EDGES],
    }
