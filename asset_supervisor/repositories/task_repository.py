"""SQLite回退仓储，同时支持任务闭环和会话历史测试。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import RLock
from uuid import uuid4

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


class SqliteTaskRepository:
    """本地持久化实现；接口保持简单，后续可替换为 PostgreSQL。"""

    def __init__(self, database_path: str | Path) -> None:
        database_value = str(database_path)
        if database_value != ":memory:":
            path = Path(database_value)
            path.parent.mkdir(parents=True, exist_ok=True)
            database_value = str(path)
        self._connection = sqlite3.connect(
            database_value,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        self._initialize()

    def _initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS app_user (
                    user_id TEXT PRIMARY KEY,
                    display_name TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation (
                    conversation_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    preferred_scene TEXT NOT NULL,
                    deleted_at TEXT,
                    deleted_by TEXT,
                    delete_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES app_user(user_id)
                )
                """
            )
            conversation_columns = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(conversation)"
                ).fetchall()
            }
            if "deleted_at" not in conversation_columns:
                self._connection.execute(
                    "ALTER TABLE conversation ADD COLUMN deleted_at TEXT"
                )
            if "deleted_by" not in conversation_columns:
                self._connection.execute(
                    "ALTER TABLE conversation ADD COLUMN deleted_by TEXT"
                )
            if "delete_reason" not in conversation_columns:
                self._connection.execute(
                    "ALTER TABLE conversation ADD COLUMN delete_reason TEXT"
                )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    ix_conversation_user_deleted_updated
                ON conversation(user_id, deleted_at, updated_at)
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS task (
                    task_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    scene TEXT NOT NULL,
                    status TEXT NOT NULL,
                    original_request_json TEXT NOT NULL,
                    current_result_id TEXT NOT NULL,
                    current_version INTEGER NOT NULL,
                    summary_override TEXT,
                    accepted_content TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS task_result (
                    result_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(task_id, version),
                    FOREIGN KEY(task_id) REFERENCES task(task_id)
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS message (
                    message_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    sequence_number INTEGER NOT NULL,
                    task_id TEXT,
                    result_id TEXT,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(conversation_id, sequence_number),
                    FOREIGN KEY(conversation_id)
                        REFERENCES conversation(conversation_id),
                    FOREIGN KEY(task_id) REFERENCES task(task_id)
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS feedback_event (
                    feedback_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    result_id TEXT NOT NULL UNIQUE,
                    decision TEXT NOT NULL,
                    rating INTEGER,
                    reason_codes_json TEXT NOT NULL,
                    comment TEXT,
                    edited_content TEXT,
                    retry INTEGER NOT NULL,
                    stored_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES task(task_id),
                    FOREIGN KEY(result_id) REFERENCES task_result(result_id)
                )
                """
            )

    def save_initial(
        self,
        request: ChatRequest,
        user_id: str,
        result: TaskResultResponse,
    ) -> None:
        now = self._now()
        request_json = request.model_dump_json(by_alias=True)
        response_json = result.model_dump_json(by_alias=True)
        try:
            with self._lock, self._connection:
                self._ensure_conversation(request, user_id, now)
                self._connection.execute(
                    """
                    INSERT INTO task (
                        task_id, conversation_id, user_id, scene, status,
                        original_request_json, current_result_id, current_version,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result.task_id,
                        result.conversation_id,
                        user_id,
                        result.scene.value,
                        result.status.value,
                        request_json,
                        result.result_id,
                        result.version,
                        now,
                        now,
                    ),
                )
                self._insert_result(result, response_json)
                self._insert_message(
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
                    created_at=result.created_at,
                )
        except sqlite3.IntegrityError as exc:
            raise FeedbackConflictError(
                f"任务或结果已存在: {result.task_id}"
            ) from exc

    def save_retry_result(self, result: TaskResultResponse) -> None:
        response_json = result.model_dump_json(by_alias=True)
        with self._lock, self._connection:
            row = self._task_row(result.task_id)
            expected_version = int(row["current_version"]) + 1
            if result.version != expected_version:
                raise FeedbackConflictError(
                    f"新结果版本必须为 {expected_version}，实际为 {result.version}"
                )
            self._insert_result(result, response_json)
            self._connection.execute(
                """
                UPDATE task
                SET status = ?, current_result_id = ?, current_version = ?,
                    summary_override = NULL, accepted_content = NULL, updated_at = ?
                WHERE task_id = ?
                """,
                (
                    result.status.value,
                    result.result_id,
                    result.version,
                    self._now(),
                    result.task_id,
                ),
            )
            self._insert_message(
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
                created_at=result.created_at,
            )
            self._touch_conversation(result.conversation_id)

    def record_feedback(
        self,
        request: FeedbackRequest,
    ) -> tuple[str, str]:
        feedback_id = f"feedback_{uuid4().hex}"
        stored_at = self._now()
        try:
            with self._lock, self._connection:
                row = self._task_row(request.task_id)
                if row["conversation_id"] != request.conversation_id:
                    raise FeedbackConflictError("会话与任务不匹配")
                if row["current_result_id"] != request.result_id:
                    raise FeedbackConflictError("反馈对应的结果版本已经过期")
                if row["scene"] != request.scene.value:
                    raise FeedbackConflictError("反馈场景与任务不匹配")
                if row["status"] != ResultStatus.REVIEW_REQUIRED.value:
                    raise FeedbackConflictError("当前任务状态不允许提交反馈")
                self._connection.execute(
                    """
                    INSERT INTO feedback_event (
                        feedback_id, task_id, result_id, decision, rating,
                        reason_codes_json, comment, edited_content, retry, stored_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        feedback_id,
                        request.task_id,
                        request.result_id,
                        request.decision.value,
                        request.rating,
                        json.dumps(request.reason_codes, ensure_ascii=False),
                        request.comment,
                        request.edited_content,
                        int(request.retry),
                        stored_at,
                    ),
                )
                feedback_text = request.comment or {
                    "ACCEPT": "采纳当前结果",
                    "EDIT_AND_ACCEPT": "修改后采纳当前结果",
                    "REJECT": "不满意当前结果",
                }[request.decision.value]
                self._insert_message(
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
                self._touch_conversation(request.conversation_id)
        except sqlite3.IntegrityError as exc:
            raise FeedbackConflictError("当前结果已经提交过反馈") from exc
        return feedback_id, stored_at

    def accept_task(
        self,
        task_id: str,
        *,
        summary: str,
        edited_content: str | None,
    ) -> TaskResultResponse:
        with self._lock, self._connection:
            self._task_row(task_id)
            self._connection.execute(
                """
                UPDATE task
                SET status = ?, summary_override = ?, accepted_content = ?,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (
                    ResultStatus.SUCCESS.value,
                    summary,
                    edited_content,
                    self._now(),
                    task_id,
                ),
            )
        effective_result = self.get_task(task_id).effective_result
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE message
                SET content = ?, metadata_json = ?
                WHERE result_id = ? AND role = 'ASSISTANT'
                """,
                (
                    effective_result.summary,
                    json.dumps(
                        {
                            "response": effective_result.model_dump(
                                mode="json",
                                by_alias=True,
                            )
                        },
                        ensure_ascii=False,
                    ),
                    effective_result.result_id,
                ),
            )
            self._touch_conversation(effective_result.conversation_id)
        return effective_result

    def get_task(self, task_id: str) -> TaskSnapshot:
        with self._lock:
            row = self._task_row(task_id)
            result_row = self._connection.execute(
                "SELECT response_json FROM task_result WHERE result_id = ?",
                (row["current_result_id"],),
            ).fetchone()
            if result_row is None:
                raise TaskNotFoundError(f"任务结果不存在: {row['current_result_id']}")

            raw_result = TaskResultResponse.model_validate_json(
                result_row["response_json"]
            )
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
                original_request=ChatRequest.model_validate_json(
                    row["original_request_json"]
                ),
                raw_result=raw_result,
                effective_result=effective_result,
            )

    def list_results(self, task_id: str) -> list[TaskResultResponse]:
        with self._lock:
            self._task_row(task_id)
            rows = self._connection.execute(
                """
                SELECT response_json
                FROM task_result
                WHERE task_id = ?
                ORDER BY version ASC
                """,
                (task_id,),
            ).fetchall()
        return [
            TaskResultResponse.model_validate_json(row["response_json"])
            for row in rows
        ]

    def list_conversations(self, user_id: str) -> list[ConversationSummary]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT c.*, COUNT(m.message_id) AS message_count
                FROM conversation c
                LEFT JOIN message m
                    ON m.conversation_id = c.conversation_id
                WHERE c.user_id = ? AND c.deleted_at IS NULL
                GROUP BY c.conversation_id
                ORDER BY c.updated_at DESC
                """,
                (user_id,),
            ).fetchall()
        return [
            ConversationSummary(
                conversation_id=row["conversation_id"],
                user_id=row["user_id"],
                title=row["title"],
                preferred_scene=PreferredScene(row["preferred_scene"]),
                message_count=int(row["message_count"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def list_messages(
        self,
        user_id: str,
        conversation_id: str,
    ) -> list[ConversationMessageResponse]:
        with self._lock:
            owner = self._connection.execute(
                """
                SELECT user_id, deleted_at
                FROM conversation
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()
            if (
                owner is None
                or owner["user_id"] != user_id
                or owner["deleted_at"] is not None
            ):
                raise ConversationNotFoundError(
                    f"会话不存在: {conversation_id}"
                )
            rows = self._connection.execute(
                """
                SELECT *
                FROM message
                WHERE conversation_id = ?
                ORDER BY sequence_number ASC
                """,
                (conversation_id,),
            ).fetchall()
        return [
            ConversationMessageResponse(
                message_id=row["message_id"],
                conversation_id=row["conversation_id"],
                role=row["role"],
                content=row["content"],
                sequence_number=row["sequence_number"],
                task_id=row["task_id"],
                result_id=row["result_id"],
                metadata=json.loads(row["metadata_json"]),
                created_at=row["created_at"],
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
        with self._lock:
            owner = self._connection.execute(
                """
                SELECT user_id, deleted_at
                FROM conversation
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()
            if owner is None:
                return []
            if owner["user_id"] != user_id or owner["deleted_at"] is not None:
                raise ConversationNotFoundError(
                    f"会话不存在: {conversation_id}"
                )
            rows = self._connection.execute(
                """
                SELECT *
                FROM message
                WHERE conversation_id = ?
                ORDER BY sequence_number DESC
                LIMIT ?
                """,
                (conversation_id, limit),
            ).fetchall()
        return [
            ConversationMessageResponse(
                message_id=row["message_id"],
                conversation_id=row["conversation_id"],
                role=row["role"],
                content=row["content"],
                sequence_number=row["sequence_number"],
                task_id=row["task_id"],
                result_id=row["result_id"],
                metadata=json.loads(row["metadata_json"]),
                created_at=row["created_at"],
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
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE conversation
                SET deleted_at = ?, deleted_by = ?, delete_reason = ?,
                    updated_at = ?
                WHERE conversation_id = ?
                    AND user_id = ?
                    AND deleted_at IS NULL
                """,
                (
                    deleted_at,
                    user_id,
                    reason,
                    deleted_at,
                    conversation_id,
                    user_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ConversationNotFoundError(
                    f"会话不存在: {conversation_id}"
                )
        return deleted_at

    def soft_delete_all_conversations(
        self,
        user_id: str,
        *,
        reason: str,
    ) -> tuple[int, str]:
        deleted_at = self._now()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE conversation
                SET deleted_at = ?, deleted_by = ?, delete_reason = ?,
                    updated_at = ?
                WHERE user_id = ? AND deleted_at IS NULL
                """,
                (
                    deleted_at,
                    user_id,
                    reason,
                    deleted_at,
                    user_id,
                ),
            )
        return cursor.rowcount, deleted_at

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _task_row(self, task_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM task WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise TaskNotFoundError(f"任务不存在: {task_id}")
        return row

    def _insert_result(
        self,
        result: TaskResultResponse,
        response_json: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO task_result (
                result_id, task_id, version, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                result.result_id,
                result.task_id,
                result.version,
                response_json,
                result.created_at,
            ),
        )

    def _ensure_conversation(
        self,
        request: ChatRequest,
        user_id: str,
        now: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO app_user (
                user_id, display_name, created_at, updated_at
            ) VALUES (?, NULL, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET updated_at = excluded.updated_at
            """,
            (user_id, now, now),
        )
        self._connection.execute(
            """
            INSERT INTO conversation (
                conversation_id, user_id, title, preferred_scene,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                updated_at = excluded.updated_at
            """,
            (
                request.conversation_id,
                user_id,
                request.message[:80],
                request.preferred_scene.value,
                now,
                now,
            ),
        )
        row = self._connection.execute(
            """
            SELECT user_id, deleted_at
            FROM conversation
            WHERE conversation_id = ?
            """,
            (request.conversation_id,),
        ).fetchone()
        if row is None or row["user_id"] != user_id:
            raise FeedbackConflictError("会话已属于其他用户")
        if row["deleted_at"] is not None:
            raise FeedbackConflictError("会话已删除，不能继续写入")

    def _insert_message(
        self,
        *,
        conversation_id: str,
        role: str,
        content: str,
        task_id: str | None,
        result_id: str | None,
        metadata: dict[str, object],
        created_at: str,
    ) -> None:
        sequence = self._connection.execute(
            """
            SELECT COALESCE(MAX(sequence_number), 0) + 1
            FROM message
            WHERE conversation_id = ?
            """,
            (conversation_id,),
        ).fetchone()[0]
        self._connection.execute(
            """
            INSERT INTO message (
                message_id, conversation_id, role, content,
                sequence_number, task_id, result_id, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"message_{uuid4().hex}",
                conversation_id,
                role,
                content,
                sequence,
                task_id,
                result_id,
                json.dumps(metadata, ensure_ascii=False),
                created_at,
            ),
        )

    def _touch_conversation(self, conversation_id: str) -> None:
        self._connection.execute(
            """
            UPDATE conversation
            SET updated_at = ?
            WHERE conversation_id = ?
            """,
            (self._now(), conversation_id),
        )

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")
