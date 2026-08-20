"""FastAPI 总控入口。"""

import json
import logging
from functools import lru_cache

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from .adapters import DownstreamServiceError, LineageClient
from .bootstrap import build_supervisor
from .config import Settings
from .models.api import (
    ChatRequest,
    ConversationClearResponse,
    ConversationDeleteResponse,
    ConversationMessageResponse,
    ConversationSummary,
    FeedbackRequest,
    FeedbackResponse,
    TaskResultResponse,
)
from .repositories import (
    ConversationNotFoundError,
    FeedbackConflictError,
    PostgresTaskRepository,
    SqliteTaskRepository,
    TaskRepository,
    TaskNotFoundError,
)
from .services.conversation_memory import ConversationMemoryBuilder
from .services.feedback import FeedbackService
from .services.supervisor import SupervisorService


@lru_cache(maxsize=1)
def get_supervisor() -> SupervisorService:
    return build_supervisor()


@lru_cache(maxsize=1)
def get_task_repository() -> TaskRepository:
    settings = Settings.from_env()
    if settings.database_url:
        return PostgresTaskRepository(settings.database_url)
    return SqliteTaskRepository(settings.database_path)


@lru_cache(maxsize=1)
def get_lineage_client() -> LineageClient:
    settings = Settings.from_env()
    return LineageClient(
        base_url=settings.lineage_service_url,
        timeout_seconds=settings.request_timeout_seconds,
        token=settings.professional_service_token,
    )


@lru_cache(maxsize=1)
def get_conversation_memory_builder() -> ConversationMemoryBuilder:
    settings = Settings.from_env()
    return ConversationMemoryBuilder(
        max_messages=settings.memory_max_messages,
        max_chars=settings.memory_max_chars,
    )


def get_feedback_service(
    supervisor: SupervisorService = Depends(get_supervisor),
    repository: TaskRepository = Depends(get_task_repository),
) -> FeedbackService:
    return FeedbackService(repository, supervisor)


def create_app() -> FastAPI:
    application = FastAPI(
        title="数据中台资产智能研发总控智能体",
        version="0.9.0",
        description="受控显式任务计划、只读结果反思、多轮会话记忆、PostgreSQL历史、逻辑删除、结果版本和反馈闭环。",
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "UP", "service": "asset-supervisor"}

    @application.post("/api/v1/chat", response_model=TaskResultResponse)
    async def chat(
        request: ChatRequest,
        x_user_id: str = Header(default="anonymous"),
        supervisor: SupervisorService = Depends(get_supervisor),
        repository: TaskRepository = Depends(get_task_repository),
        memory_builder: ConversationMemoryBuilder = Depends(
            get_conversation_memory_builder
        ),
    ) -> TaskResultResponse:
        try:
            recent_messages = repository.list_recent_messages(
                x_user_id,
                request.conversation_id,
                limit=memory_builder.max_messages,
            )
        except ConversationNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        conversation_memory = memory_builder.build(recent_messages)
        result = await supervisor.handle(
            request,
            user_id=x_user_id,
            conversation_memory=conversation_memory,
        )
        try:
            repository.save_initial(request, x_user_id, result)
        except FeedbackConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return result

    @application.post(
        "/api/v1/feedback",
        response_model=FeedbackResponse,
    )
    async def submit_feedback(
        request: FeedbackRequest,
        service: FeedbackService = Depends(get_feedback_service),
    ) -> FeedbackResponse:
        try:
            return await service.submit(request)
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except FeedbackConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @application.get(
        "/api/v1/tasks/{task_id}",
        response_model=TaskResultResponse,
    )
    async def get_task(
        task_id: str,
        repository: TaskRepository = Depends(get_task_repository),
    ) -> TaskResultResponse:
        try:
            return repository.get_task(task_id).effective_result
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @application.get(
        "/api/v1/tasks/{task_id}/results",
        response_model=list[TaskResultResponse],
    )
    async def list_task_results(
        task_id: str,
        repository: TaskRepository = Depends(get_task_repository),
    ) -> list[TaskResultResponse]:
        try:
            return repository.list_results(task_id)
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @application.get(
        "/api/v1/conversations",
        response_model=list[ConversationSummary],
    )
    async def list_conversations(
        x_user_id: str = Header(default="anonymous"),
        repository: TaskRepository = Depends(get_task_repository),
    ) -> list[ConversationSummary]:
        return repository.list_conversations(x_user_id)

    @application.delete(
        "/api/v1/conversations",
        response_model=ConversationClearResponse,
    )
    async def clear_conversations(
        x_user_id: str = Header(default="anonymous"),
        repository: TaskRepository = Depends(get_task_repository),
    ) -> ConversationClearResponse:
        deleted_count, deleted_at = (
            repository.soft_delete_all_conversations(
                x_user_id,
                reason="USER_CLEAR_HISTORY",
            )
        )
        return ConversationClearResponse(
            deleted_count=deleted_count,
            deleted_at=deleted_at,
        )

    @application.delete(
        "/api/v1/conversations/{conversation_id}",
        response_model=ConversationDeleteResponse,
    )
    async def delete_conversation(
        conversation_id: str,
        x_user_id: str = Header(default="anonymous"),
        repository: TaskRepository = Depends(get_task_repository),
    ) -> ConversationDeleteResponse:
        try:
            deleted_at = repository.soft_delete_conversation(
                x_user_id,
                conversation_id,
                reason="USER_DELETE",
            )
            return ConversationDeleteResponse(
                conversation_id=conversation_id,
                deleted=True,
                deleted_at=deleted_at,
            )
        except ConversationNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @application.get(
        "/api/v1/conversations/{conversation_id}/messages",
        response_model=list[ConversationMessageResponse],
    )
    async def list_conversation_messages(
        conversation_id: str,
        x_user_id: str = Header(default="anonymous"),
        repository: TaskRepository = Depends(get_task_repository),
    ) -> list[ConversationMessageResponse]:
        try:
            return repository.list_messages(x_user_id, conversation_id)
        except ConversationNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @application.post("/api/v1/lineage/scripts")
    async def upload_lineage_script(
        request: Request,
        client: LineageClient = Depends(get_lineage_client),
    ) -> JSONResponse:
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="请求体必须是JSON") from exc
        try:
            return JSONResponse(content=await client.upload_script(payload))
        except DownstreamServiceError as exc:
            raise HTTPException(
                status_code=exc.http_status,
                detail=exc.message,
            ) from exc

    @application.get("/api/v1/lineage/jobs/{job_id}")
    async def get_lineage_job(
        job_id: str,
        client: LineageClient = Depends(get_lineage_client),
    ) -> JSONResponse:
        try:
            return JSONResponse(content=await client.get_job(job_id))
        except DownstreamServiceError as exc:
            raise HTTPException(
                status_code=exc.http_status,
                detail=exc.message,
            ) from exc

    @application.get("/api/v1/lineage/jobs/{job_id}/graph")
    async def get_lineage_graph(
        job_id: str,
        cursor: str | None = None,
        client: LineageClient = Depends(get_lineage_client),
    ) -> JSONResponse:
        try:
            return JSONResponse(
                content=await client.get_graph(job_id, cursor=cursor),
            )
        except DownstreamServiceError as exc:
            raise HTTPException(
                status_code=exc.http_status,
                detail=exc.message,
            ) from exc

    @application.post("/api/v1/chat/stream")
    async def chat_stream(
        request: ChatRequest,
        x_user_id: str = Header(default="anonymous"),
        supervisor: SupervisorService = Depends(get_supervisor),
        repository: TaskRepository = Depends(get_task_repository),
        memory_builder: ConversationMemoryBuilder = Depends(
            get_conversation_memory_builder
        ),
    ) -> StreamingResponse:
        try:
            recent_messages = repository.list_recent_messages(
                x_user_id,
                request.conversation_id,
                limit=memory_builder.max_messages,
            )
        except ConversationNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        conversation_memory = memory_builder.build(recent_messages)

        async def event_stream():
            async for name, data in supervisor.stream_chat(
                request,
                user_id=x_user_id,
                conversation_memory=conversation_memory,
            ):
                if name == "result":
                    try:
                        repository.save_initial(
                            request,
                            x_user_id,
                            TaskResultResponse.model_validate(data),
                        )
                    except Exception as exc:
                        logging.getLogger(__name__).warning(
                            "流式结果持久化失败: %s",
                            exc,
                        )
                yield (
                    f"event: {name}\n"
                    f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                )

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
            },
        )

    return application
