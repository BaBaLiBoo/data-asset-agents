"""任务、结果、反馈和会话历史仓储接口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..models.api import (
    ChatRequest,
    ConversationMessageResponse,
    ConversationSummary,
    FeedbackRequest,
    TaskResultResponse,
)


class TaskNotFoundError(LookupError):
    """任务不存在。"""


class ConversationNotFoundError(LookupError):
    """会话不存在或不属于当前用户。"""


class FeedbackConflictError(RuntimeError):
    """反馈针对过期结果、重复提交或非法任务状态。"""


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    task_id: str
    user_id: str
    original_request: ChatRequest
    raw_result: TaskResultResponse
    effective_result: TaskResultResponse


class TaskRepository(Protocol):
    def save_initial(
        self,
        request: ChatRequest,
        user_id: str,
        result: TaskResultResponse,
    ) -> None: ...

    def save_retry_result(self, result: TaskResultResponse) -> None: ...

    def record_feedback(
        self,
        request: FeedbackRequest,
    ) -> tuple[str, str]: ...

    def accept_task(
        self,
        task_id: str,
        *,
        summary: str,
        edited_content: str | None,
    ) -> TaskResultResponse: ...

    def get_task(self, task_id: str) -> TaskSnapshot: ...

    def list_results(self, task_id: str) -> list[TaskResultResponse]: ...

    def list_conversations(self, user_id: str) -> list[ConversationSummary]: ...

    def list_messages(
        self,
        user_id: str,
        conversation_id: str,
    ) -> list[ConversationMessageResponse]: ...

    def list_recent_messages(
        self,
        user_id: str,
        conversation_id: str,
        *,
        limit: int,
    ) -> list[ConversationMessageResponse]: ...

    def soft_delete_conversation(
        self,
        user_id: str,
        conversation_id: str,
        *,
        reason: str,
    ) -> str: ...

    def soft_delete_all_conversations(
        self,
        user_id: str,
        *,
        reason: str,
    ) -> tuple[int, str]: ...

    def close(self) -> None: ...
