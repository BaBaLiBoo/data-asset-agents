"""LangChain Tool抽象及两个专业任务实现。"""

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from ..agents.task_agents import TaskAgentPort
from ..models.routing import RunContext
from ..models.task_requests import (
    AssetDuplicateCheckRequest,
    AssetDuplicateToolInput,
    LineageJobRequest,
    LineageToolInput,
    SqlGenerationRequest,
    SqlGenerationToolInput,
    TaskType,
)


class ToolInputError(ValueError):
    """模型生成的Tool参数未通过数据契约校验。"""


class TaskTool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    task_type: ClassVar[TaskType]
    input_model: ClassVar[type[BaseModel]]

    def __init__(self, target_agent: TaskAgentPort) -> None:
        self._target_agent = target_agent

    def llm_definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_model.model_json_schema(by_alias=True),
            },
        }

    async def execute(
        self,
        arguments: dict[str, Any],
        context: RunContext,
    ) -> dict[str, Any]:
        try:
            tool_input = self.input_model.model_validate(arguments)
        except ValidationError as exc:
            raise ToolInputError(str(exc)) from exc
        request = self.build_request(tool_input, context)
        return await self._target_agent.execute(request)

    @abstractmethod
    def build_request(self, tool_input: BaseModel, context: RunContext) -> BaseModel:
        """把模型字段和可信运行上下文组装为专业服务请求。"""


class AssetDuplicateCheckTool(TaskTool):
    name = "check_asset_duplicate"
    description = (
        "当用户准备新增数据资产，希望检索可能重复或可复用的已有资产时使用。"
        "当前能力返回待复核候选，不把检索相似度当作正式重复结论。"
        "只提取用户明确提供的资产名称、描述、口径、粒度、SQL、字段和血缘。"
    )
    task_type = TaskType.ASSET_DUPLICATE
    input_model = AssetDuplicateToolInput

    def build_request(
        self,
        tool_input: AssetDuplicateToolInput,
        context: RunContext,
    ) -> AssetDuplicateCheckRequest:
        return AssetDuplicateCheckRequest(
            request_id=context.request_id,
            task_id=context.task_id,
            trace_id=context.trace_id,
            conversation_id=context.conversation_id,
            user_id=context.user_id,
            timestamp=context.timestamp,
            original_query=context.original_query,
            context=context.common_context,
            correction_context=context.correction_context,
            draft_asset=tool_input.draft_asset,
            search_options=tool_input.search_options,
        )


class GenerateSqlTool(TaskTool):
    name = "generate_sql"
    description = (
        "当用户希望依据自然语言需求推荐表字段并生成SQL时使用。"
        "只提取指标、维度、过滤条件、时间范围、粒度和明确的数据范围。"
    )
    task_type = TaskType.SQL_GENERATION
    input_model = SqlGenerationToolInput

    def build_request(
        self,
        tool_input: SqlGenerationToolInput,
        context: RunContext,
    ) -> SqlGenerationRequest:
        return SqlGenerationRequest(
            request_id=context.request_id,
            task_id=context.task_id,
            trace_id=context.trace_id,
            conversation_id=context.conversation_id,
            user_id=context.user_id,
            timestamp=context.timestamp,
            original_query=context.original_query,
            context=context.common_context,
            correction_context=context.correction_context,
            requirement=tool_input.requirement,
            execution_context=tool_input.execution_context,
            constraints=tool_input.constraints,
        )


class AnalyzeLineageTool(TaskTool):
    name = "parse_regulatory_lineage"
    description = (
        "当用户上传监管报送SQL/TXT脚本后，需要解析某目标字段的算子级血缘时使用。"
        "只提取脚本ID、目标字段、SQL方言和解析参数，不代用户上传或改写脚本。"
    )
    task_type = TaskType.LINEAGE_PARSING
    input_model = LineageToolInput

    def build_request(
        self,
        tool_input: LineageToolInput,
        context: RunContext,
    ) -> LineageJobRequest:
        return LineageJobRequest(
            request_id=context.request_id,
            task_id=context.task_id,
            trace_id=context.trace_id,
            conversation_id=context.conversation_id,
            user_id=context.user_id,
            timestamp=context.timestamp,
            original_query=context.original_query,
            context=context.common_context,
            correction_context=context.correction_context,
            script_id=tool_input.script_id,
            target_field=tool_input.target_field,
            sql_dialect=tool_input.sql_dialect,
            options=tool_input.options,
        )
