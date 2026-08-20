"""需求理解、Tool路由和可信运行上下文。"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

from .task_requests import CommonContext


@dataclass(frozen=True, slots=True)
class RunContext:
    request_id: str
    task_id: str
    trace_id: str
    conversation_id: str
    user_id: str
    timestamp: str
    original_query: str
    common_context: CommonContext
    result_version: int = 1
    correction_context: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def create(
        cls,
        *,
        conversation_id: str,
        original_query: str,
        user_id: str,
        common_context: CommonContext,
        task_id: str | None = None,
        result_version: int = 1,
        correction_context: list[dict[str, Any]] | None = None,
    ) -> "RunContext":
        return cls(
            request_id=f"req_{uuid4().hex}",
            task_id=task_id or f"task_{uuid4().hex}",
            trace_id=f"trace_{uuid4().hex}",
            conversation_id=conversation_id,
            user_id=user_id,
            timestamp=datetime.now().astimezone().isoformat(timespec="seconds"),
            original_query=original_query,
            common_context=common_context,
            result_version=result_version,
            correction_context=correction_context or [],
        )


@dataclass(frozen=True, slots=True)
class ToolSelection:
    tool_name: str | None
    arguments: dict[str, Any]
    assistant_message: str
