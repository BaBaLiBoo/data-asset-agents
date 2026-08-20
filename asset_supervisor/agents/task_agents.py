"""任务一、任务二专业服务端口。"""

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel


class TaskAgentPort(ABC):
    @abstractmethod
    async def execute(self, request: BaseModel) -> dict[str, Any]:
        """提交结构化请求并返回专业服务结构化结果。"""
