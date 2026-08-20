"""模型、Supervisor、Tool和HTTP专业服务的对象装配。"""

import logging

from langchain.chat_models import init_chat_model

from .adapters import AssetHttpTaskAgent, LineageClient, SqlHttpTaskAgent
from .agents.requirement_agent import RequirementUnderstandingAgent
from .agents.result_reflection_agent import ResultReflectionAgent
from .config import Settings
from .services.supervisor import SupervisorService
from .tools.registry import ToolRegistry
from .tools.task_tools import (
    AnalyzeLineageTool,
    AssetDuplicateCheckTool,
    GenerateSqlTool,
)


def build_supervisor(settings: Settings | None = None) -> SupervisorService:
    settings = settings or Settings.from_env()

    task_one_agent = AssetHttpTaskAgent(
        base_url=settings.asset_service_url,
        timeout_seconds=settings.request_timeout_seconds,
        token=settings.professional_service_token,
    )
    task_two_agent = SqlHttpTaskAgent(
        base_url=settings.sql_service_url,
        timeout_seconds=settings.request_timeout_seconds,
        token=settings.professional_service_token,
    )
    lineage_client = LineageClient(
        base_url=settings.lineage_service_url,
        timeout_seconds=settings.request_timeout_seconds,
        token=settings.professional_service_token,
    )
    lineage_agent = AnalyzeLineageTool(lineage_client)
    registry = ToolRegistry(
        [
            AssetDuplicateCheckTool(task_one_agent),
            GenerateSqlTool(task_two_agent),
            lineage_agent,
        ]
    )

    requirement_agent = None
    reflection_agent = None
    if settings.model_enabled:
        requirement_model = init_chat_model(
            model=settings.model_name,
            model_provider="openai",
            api_key=settings.api_key,
            base_url=settings.base_url,
            temperature=0,
            streaming=False,
        )
        requirement_agent = RequirementUnderstandingAgent(
            requirement_model,
            registry.llm_definitions(),
        )
        try:
            reflection_model = init_chat_model(
                model=settings.reflection_model_name,
                model_provider="openai",
                api_key=settings.api_key,
                base_url=settings.base_url,
                temperature=0,
                streaming=False,
                extra_body={
                    "enable_thinking": settings.reflection_enable_thinking
                },
            )
            reflection_agent = ResultReflectionAgent(reflection_model)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "当前模型不支持结果反思结构化输出，将使用UNAVAILABLE降级: %s",
                exc,
            )

    return SupervisorService(
        requirement_agent,
        registry,
        reflection_agent=reflection_agent,
        lineage_client=lineage_client,
    )
