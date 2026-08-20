"""PostgreSQL任务、版本、反馈和会话历史仓储。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy import create_engine

from ..db.schema import (
    app_user,
    conversation,
    feedback_event,
    message,
    task,
    task_result,
)
from ..models.api import (
    ChatRequest,
    ConversationMessageResponse,
    ConversationSummary,
    FeedbackRequest,
    PreferredScene,
    ResultStatus,
    TaskResultResponse,
)
from ..models.execution_plan import complete_execution_plan_after_review
from .base import (
    ConversationNotFoundError,
    FeedbackConflictError,
    TaskNotFoundError,
    TaskSnapshot,
)


class PostgresTaskRepository:
    """生产型PostgreSQL仓储；表结构由Alembic管理。"""

    def __init__(self, database_url: str, *, engine: Engine | None = None) -> None:
        self._engine = engine or create_engine(
            database_url,
            pool_pre_ping=True,
        )

    def save_initial(
        self,
        request: ChatRequest,
        user_id: str,
        result: TaskResultResponse,
    ) -> None:
        now = self._now()
        try:
            with self._engine.begin() as connection:
                self._ensure_conversation(connection, request, user_id, now)
                connection.execute(
                    task.insert().values(
                        task_id=result.task_id,
                        conversation_id=result.conversation_id,
                        user_id=user_id,
                        scene=result.scene.value,
                        status=result.status.value,
                        original_request_json=request.model_dump(
                            mode="json",
                            by_alias=True,
                        ),
                        current_result_id=result.result_id,
                        current_version=result.version,
                        created_at=now,
                        updated_at=now,
                    )
                )
                self._insert_result(connection, result)
                self._insert_message(
                    connection,
                    conversation_id=request.conversation_id,
                    role="USER",
                    content=request.message,
                    task_id=result.task_id,
                    result_id=None,
                    metadata={
                        "preferredScene": request.preferred_scene.value,
                        "context": request.context.model_dump(
                            mode="json",
                            by_alias=True,
                        ),
                    },
                    created_at=now,
                )
                self._insert_message(
                    connection,
                    conversation_id=request.conversation_id,
                    role="ASSISTANT",
                    content=result.summary,
                    task_id=result.task_id,
                    result_id=result.result_id,
                    metadata={
                        "response": result.model_dump(
                            mode="json",
                            by_alias=True,
                        )
                    },
                    created_at=self._as_datetime(result.created_at),
                )
        except IntegrityError as exc:
            raise FeedbackConflictError(
                f"任务或结果已存在: {result.task_id}"
            ) from exc

    def save_retry_result(self, result: TaskResultResponse) -> None:
        try:
            with self._engine.begin() as connection:
                row = self._task_row(
                    connection,
                    result.task_id,
                    for_update=True,
                )
                expected_version = int(row["current_version"]) + 1
                if result.version != expected_version:
                    raise FeedbackConflictError(
                        f"新结果版本必须为 {expected_version}，"
                        f"实际为 {result.version}"
                    )
                self._insert_result(connection, result)
                connection.execute(
                    update(task)
                    .where(task.c.task_id == result.task_id)
                    .values(
                        status=result.status.value,
                        current_result_id=result.result_id,
                        current_version=result.version,
                        summary_override=None,
                        accepted_content=None,
                        updated_at=self._now(),
                    )
                )
                self._insert_message(
                    connection,
                    conversation_id=result.conversation_id,
                    role="ASSISTANT",
                    content=result.summary,
                    task_id=result.task_id,
                    result_id=result.result_id,
                    metadata={
                        "response": result.model_dump(
                            mode="json",
                            by_alias=True,
                        )
                    },
                    created_at=self._as_datetime(result.created_at),
                )
                self._touch_conversation(connection, result.conversation_id)
        except IntegrityError as exc:
            raise FeedbackConflictError("新结果版本保存冲突") from exc

    def record_feedback(
        self,
        request: FeedbackRequest,
    ) -> tuple[str, str]:
        feedback_id = f"feedback_{uuid4().hex}"
        stored_at = self._now()
        try:
            with self._engine.begin() as connection:
                row = self._task_row(
                    connection,
                    request.task_id,
                    for_update=True,
                )
                if row["conversation_id"] != request.conversation_id:
                    raise FeedbackConflictError("会话与任务不匹配")
                if row["current_result_id"] != request.result_id:
                    raise FeedbackConflictError("反馈对应的结果版本已经过期")
                if row["scene"] != request.scene.value:
                    raise FeedbackConflictError("反馈场景与任务不匹配")
                if row["status"] != ResultStatus.REVIEW_REQUIRED.value:
                    raise FeedbackConflictError("当前任务状态不允许提交反馈")
                connection.execute(
                    feedback_event.insert().values(
                        feedback_id=feedback_id,
                        task_id=request.task_id,
                        result_id=request.result_id,
                        decision=request.decision.value,
                        rating=request.rating,
                        reason_codes_json=request.reason_codes,
                        comment=request.comment,
                        edited_content=request.edited_content,
                        retry=request.retry,
                        stored_at=stored_at,
                    )
                )
                feedback_text = request.comment or {
                    "ACCEPT": "采纳当前结果",
                    "EDIT_AND_ACCEPT": "修改后采纳当前结果",
                    "REJECT": "不满意当前结果",
                }[request.decision.value]
                self._insert_message(
                    connection,
                    conversation_id=request.conversation_id,
                    role="USER",
                    content=feedback_text,
                    task_id=request.task_id,
                    result_id=request.result_id,
                    metadata={
                        "feedback": request.model_dump(
                            mode="json",
                            by_alias=True,
                        )
                    },
                    created_at=stored_at,
                )
                self._touch_conversation(connection, request.conversation_id)
        except IntegrityError as exc:
            raise FeedbackConflictError("当前结果已经提交过反馈") from exc
        return feedback_id, self._iso(stored_at)

    def accept_task(
        self,
        task_id: str,
        *,
        summary: str,
        edited_content: str | None,
    ) -> TaskResultResponse:
        with self._engine.begin() as connection:
            row = self._task_row(connection, task_id, for_update=True)
            connection.execute(
                update(task)
                .where(task.c.task_id == task_id)
                .values(
                    status=ResultStatus.SUCCESS.value,
                    summary_override=summary,
                    accepted_content=edited_content,
                    updated_at=self._now(),
                )
            )
            conversation_id = row["conversation_id"]
        effective_result = self.get_task(task_id).effective_result
        with self._engine.begin() as connection:
            connection.execute(
                update(message)
                .where(
                    message.c.result_id == effective_result.result_id,
                    message.c.role == "ASSISTANT",
                )
                .values(
                    content=effective_result.summary,
                    metadata_json={
                        "response": effective_result.model_dump(
                            mode="json",
                            by_alias=True,
                        )
                    },
                )
            )
            self._touch_conversation(connection, conversation_id)
        return effective_result

    def get_task(self, task_id: str) -> TaskSnapshot:
        with self._engine.connect() as connection:
            row = self._task_row(connection, task_id)
            result_row = connection.execute(
                select(task_result.c.response_json).where(
                    task_result.c.result_id == row["current_result_id"]
                )
            ).mappings().first()
            if result_row is None:
                raise TaskNotFoundError(
                    f"任务结果不存在: {row['current_result_id']}"
                )
            return self._snapshot(row, result_row["response_json"])

    def list_results(self, task_id: str) -> list[TaskResultResponse]:
        with self._engine.connect() as connection:
            self._task_row(connection, task_id)
            rows = connection.execute(
                select(task_result.c.response_json)
                .where(task_result.c.task_id == task_id)
                .order_by(task_result.c.version.asc())
            ).mappings().all()
        return [
            TaskResultResponse.model_validate(row["response_json"])
            for row in rows
        ]

    def list_conversations(self, user_id: str) -> list[ConversationSummary]:
        message_count = func.count(message.c.message_id).label("message_count")
        statement = (
            select(
                conversation.c.conversation_id,
                conversation.c.user_id,
                conversation.c.title,
                conversation.c.preferred_scene,
                conversation.c.created_at,
                conversation.c.updated_at,
                message_count,
            )
            .outerjoin(
                message,
                message.c.conversation_id == conversation.c.conversation_id,
            )
            .where(conversation.c.user_id == user_id)
            .where(conversation.c.deleted_at.is_(None))
            .group_by(conversation.c.conversation_id)
            .order_by(conversation.c.updated_at.desc())
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [
            ConversationSummary(
                conversation_id=row["conversation_id"],
                user_id=row["user_id"],
                title=row["title"],
                preferred_scene=PreferredScene(row["preferred_scene"]),
                message_count=int(row["message_count"]),
                created_at=self._iso(row["created_at"]),
                updated_at=self._iso(row["updated_at"]),
            )
            for row in rows
        ]

    def list_messages(
        self,
        user_id: str,
        conversation_id: str,
    ) -> list[ConversationMessageResponse]:
        with self._engine.connect() as connection:
            owner = connection.execute(
                select(
                    conversation.c.user_id,
                    conversation.c.deleted_at,
                ).where(
                    conversation.c.conversation_id == conversation_id
                )
            ).mappings().first()
            if (
                owner is None
                or owner["user_id"] != user_id
                or owner["deleted_at"] is not None
            ):
                raise ConversationNotFoundError(
                    f"会话不存在: {conversation_id}"
                )
            rows = connection.execute(
                select(message)
                .where(message.c.conversation_id == conversation_id)
                .order_by(message.c.sequence_number.asc())
            ).mappings().all()
        return [
            ConversationMessageResponse(
                message_id=row["message_id"],
                conversation_id=row["conversation_id"],
                role=row["role"],
                content=row["content"],
                sequence_number=row["sequence_number"],
                task_id=row["task_id"],
                result_id=row["result_id"],
                metadata=row["metadata_json"],
                created_at=self._iso(row["created_at"]),
            )
            for row in rows
        ]

    def list_recent_messages(
        self,
        user_id: str,
        conversation_id: str,
        *,
        limit: int,
    ) -> list[ConversationMessageResponse]:
        if limit < 1:
            raise ValueError("limit 必须大于 0")
        with self._engine.connect() as connection:
            owner = connection.execute(
                select(
                    conversation.c.user_id,
                    conversation.c.deleted_at,
                ).where(
                    conversation.c.conversation_id == conversation_id
                )
            ).mappings().first()
            if owner is None:
                return []
            if owner["user_id"] != user_id or owner["deleted_at"] is not None:
                raise ConversationNotFoundError(
                    f"会话不存在: {conversation_id}"
                )
            rows = connection.execute(
                select(message)
                .where(message.c.conversation_id == conversation_id)
                .order_by(message.c.sequence_number.desc())
                .limit(limit)
            ).mappings().all()
        return [
            ConversationMessageResponse(
                message_id=row["message_id"],
                conversation_id=row["conversation_id"],
                role=row["role"],
                content=row["content"],
                sequence_number=row["sequence_number"],
                task_id=row["task_id"],
                result_id=row["result_id"],
                metadata=row["metadata_json"],
                created_at=self._iso(row["created_at"]),
            )
            for row in reversed(rows)
        ]

    def soft_delete_conversation(
        self,
        user_id: str,
        conversation_id: str,
        *,
        reason: str,
    ) -> str:
        deleted_at = self._now()
        with self._engine.begin() as connection:
            result = connection.execute(
                update(conversation)
                .where(
                    conversation.c.conversation_id == conversation_id,
                    conversation.c.user_id == user_id,
                    conversation.c.deleted_at.is_(None),
                )
                .values(
                    deleted_at=deleted_at,
                    deleted_by=user_id,
                    delete_reason=reason,
                    updated_at=deleted_at,
                )
            )
            if result.rowcount != 1:
                raise ConversationNotFoundError(
                    f"会话不存在: {conversation_id}"
                )
        return self._iso(deleted_at)

    def soft_delete_all_conversations(
        self,
        user_id: str,
        *,
        reason: str,
    ) -> tuple[int, str]:
        deleted_at = self._now()
        with self._engine.begin() as connection:
            result = connection.execute(
                update(conversation)
                .where(
                    conversation.c.user_id == user_id,
                    conversation.c.deleted_at.is_(None),
                )
                .values(
                    deleted_at=deleted_at,
                    deleted_by=user_id,
                    delete_reason=reason,
                    updated_at=deleted_at,
                )
            )
        return result.rowcount, self._iso(deleted_at)

    def close(self) -> None:
        self._engine.dispose()

    def _ensure_conversation(
        self,
        connection: Any,
        request: ChatRequest,
        user_id: str,
        now: datetime,
    ) -> None:
        connection.execute(
            pg_insert(app_user)
            .values(
                user_id=user_id,
                display_name=None,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=[app_user.c.user_id],
                set_={"updated_at": now},
            )
        )
        connection.execute(
            pg_insert(conversation)
            .values(
                conversation_id=request.conversation_id,
                user_id=user_id,
                title=request.message[:80],
                preferred_scene=request.preferred_scene.value,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=[conversation.c.conversation_id],
                set_={"updated_at": now},
            )
        )
        owner = connection.execute(
            select(
                conversation.c.user_id,
                conversation.c.deleted_at,
            ).where(
                conversation.c.conversation_id == request.conversation_id
            )
        ).mappings().one()
        if owner["user_id"] != user_id:
            raise FeedbackConflictError("会话已属于其他用户")
        if owner["deleted_at"] is not None:
            raise FeedbackConflictError("会话已删除，不能继续写入")

    def _insert_result(self, connection: Any, result: TaskResultResponse) -> None:
        connection.execute(
            task_result.insert().values(
                result_id=result.result_id,
                task_id=result.task_id,
                version=result.version,
                response_json=result.model_dump(mode="json", by_alias=True),
                created_at=self._as_datetime(result.created_at),
            )
        )

    def _insert_message(
        self,
        connection: Any,
        *,
        conversation_id: str,
        role: str,
        content: str,
        task_id: str | None,
        result_id: str | None,
        metadata: dict[str, Any],
        created_at: datetime,
    ) -> None:
        connection.execute(
            select(conversation.c.conversation_id)
            .where(conversation.c.conversation_id == conversation_id)
            .with_for_update()
        )
        sequence = connection.execute(
            select(func.coalesce(func.max(message.c.sequence_number), 0) + 1)
            .where(message.c.conversation_id == conversation_id)
        ).scalar_one()
        connection.execute(
            message.insert().values(
                message_id=f"message_{uuid4().hex}",
                conversation_id=conversation_id,
                role=role,
                content=content,
                sequence_number=sequence,
                task_id=task_id,
                result_id=result_id,
                metadata_json=metadata,
                created_at=created_at,
            )
        )

    @staticmethod
    def _touch_conversation(connection: Any, conversation_id: str) -> None:
        connection.execute(
            update(conversation)
            .where(conversation.c.conversation_id == conversation_id)
            .values(updated_at=PostgresTaskRepository._now())
        )

    @staticmethod
    def _task_row(
        connection: Any,
        task_id: str,
        *,
        for_update: bool = False,
    ) -> Any:
        statement = select(task).where(task.c.task_id == task_id)
        if for_update:
            statement = statement.with_for_update()
        row = connection.execute(statement).mappings().first()
        if row is None:
            raise TaskNotFoundError(f"任务不存在: {task_id}")
        return row

    @staticmethod
    def _snapshot(row: Any, response_json: dict[str, Any]) -> TaskSnapshot:
        raw_result = TaskResultResponse.model_validate(response_json)
        effective_result = raw_result.model_copy(deep=True)
        effective_result.status = ResultStatus(row["status"])
        if effective_result.status is ResultStatus.SUCCESS:
            effective_result.execution_plan = (
                complete_execution_plan_after_review(
                    effective_result.execution_plan
                )
            )
        if row["summary_override"]:
            effective_result.summary = row["summary_override"]
        if row["accepted_content"] and effective_result.result:
            if effective_result.result.get("kind") == "sql":
                effective_result.result["sql"] = row["accepted_content"]
        return TaskSnapshot(
            task_id=row["task_id"],
            user_id=row["user_id"],
            original_request=ChatRequest.model_validate(
                row["original_request_json"]
            ),
            raw_result=raw_result,
            effective_result=effective_result,
        )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _as_datetime(value: str) -> datetime:
        return datetime.fromisoformat(value)

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.isoformat(timespec="seconds")
