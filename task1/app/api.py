from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request

from . import __version__
from .catalog import AssetCatalogError
from .config import Settings
from .embedding import EmbeddingServiceError
from .models import (
    AssetBatchImportRequest,
    AssetImportResult,
    AssetInput,
    AssetListResult,
    ComparisonRequest,
    ComparisonResult,
    FullScanRequest,
    FullScanResult,
    SearchDuplicatesRequest,
    SearchDuplicatesResult,
    TextAssetSearchRequest,
    TextAssetSearchResult,
)
from .service import Task1Service


def create_app(service: Task1Service | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if service is not None:
            app.state.task1_service = service
        else:
            app.state.task1_service = Task1Service.create(Settings.from_env())
        yield

    app = FastAPI(
        title="task1_v1 重复资产识别服务",
        version=__version__,
        lifespan=lifespan,
    )

    @app.get("/health")
    def health(request: Request) -> dict[str, str | int]:
        task1: Task1Service = request.app.state.task1_service
        return {
            "status": "ok",
            "version": __version__,
            "embedding_model": task1.embedding_model,
            "embedding_dimension": task1.embedding_dimension,
            "algorithm_version": task1.settings.algorithm_version,
            "asset_catalog_size": task1.catalog.count() if task1.catalog else 0,
        }

    @app.post("/api/v1/task1/compare", response_model=ComparisonResult)
    def compare(payload: ComparisonRequest, request: Request) -> ComparisonResult:
        task1: Task1Service = request.app.state.task1_service
        try:
            return task1.compare(payload)
        except EmbeddingServiceError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/task1/assets", response_model=AssetImportResult)
    def import_asset(payload: AssetInput, request: Request) -> AssetImportResult:
        task1: Task1Service = request.app.state.task1_service
        try:
            return task1.import_assets([payload])
        except EmbeddingServiceError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except (AssetCatalogError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/task1/assets/batch", response_model=AssetImportResult)
    def import_asset_batch(
        payload: AssetBatchImportRequest,
        request: Request,
    ) -> AssetImportResult:
        task1: Task1Service = request.app.state.task1_service
        try:
            return task1.import_assets(
                payload.assets,
                replace_existing=payload.replace_existing,
            )
        except EmbeddingServiceError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except (AssetCatalogError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/task1/assets", response_model=AssetListResult)
    def list_assets(
        request: Request,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=1_000),
        business_domain: str | None = None,
    ) -> AssetListResult:
        task1: Task1Service = request.app.state.task1_service
        if task1.catalog is None:
            raise HTTPException(status_code=503, detail="asset catalog is not configured")
        return AssetListResult(
            total=task1.catalog.count(business_domain=business_domain),
            offset=offset,
            limit=limit,
            assets=task1.catalog.list(
                offset=offset,
                limit=limit,
                business_domain=business_domain,
            ),
        )

    @app.get("/api/v1/task1/assets/{asset_id}", response_model=AssetInput)
    def get_asset(asset_id: str, request: Request) -> AssetInput:
        task1: Task1Service = request.app.state.task1_service
        if task1.catalog is None:
            raise HTTPException(status_code=503, detail="asset catalog is not configured")
        asset = task1.catalog.get(asset_id)
        if asset is None:
            raise HTTPException(status_code=404, detail=f"asset not found: {asset_id}")
        return asset

    @app.post(
        "/api/v1/task1/search-duplicates",
        response_model=SearchDuplicatesResult,
    )
    def search_duplicates(
        payload: SearchDuplicatesRequest,
        request: Request,
    ) -> SearchDuplicatesResult:
        task1: Task1Service = request.app.state.task1_service
        try:
            return task1.search_duplicates(payload)
        except EmbeddingServiceError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except AssetCatalogError as exc:
            status = 404 if "not found" in str(exc) else 503
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post(
        "/api/v1/task1/search-by-text",
        response_model=TextAssetSearchResult,
    )
    def search_by_text(
        payload: TextAssetSearchRequest,
        request: Request,
    ) -> TextAssetSearchResult:
        task1: Task1Service = request.app.state.task1_service
        try:
            return task1.search_by_text(payload)
        except EmbeddingServiceError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except AssetCatalogError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/task1/scan-all", response_model=FullScanResult)
    def scan_all(payload: FullScanRequest, request: Request) -> FullScanResult:
        task1: Task1Service = request.app.state.task1_service
        try:
            return task1.scan_all(payload)
        except EmbeddingServiceError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except AssetCatalogError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return app


app = create_app()
