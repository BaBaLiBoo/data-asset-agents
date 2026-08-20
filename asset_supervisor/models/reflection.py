"""专业结果的结构化语义反思意见。"""

from enum import StrEnum

from pydantic import Field

from .base import ApiModel


class ReflectionStatus(StrEnum):
    PASSED = "PASSED"
    WARNING = "WARNING"
    UNAVAILABLE = "UNAVAILABLE"


class ReflectionItemStatus(StrEnum):
    PASSED = "PASSED"
    WARNING = "WARNING"
    UNKNOWN = "UNKNOWN"


class ReflectionCategory(StrEnum):
    METRIC = "METRIC"
    DIMENSION = "DIMENSION"
    GRAIN = "GRAIN"
    TIME_RANGE = "TIME_RANGE"
    FILTER = "FILTER"
    TABLE_FIELD = "TABLE_FIELD"
    ASSET_EVIDENCE = "ASSET_EVIDENCE"
    RISK = "RISK"


class ReflectionRiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ReflectionItem(ApiModel):
    category: ReflectionCategory
    status: ReflectionItemStatus
    requirement: str = Field(min_length=1, max_length=1000)
    observation: str = Field(min_length=1, max_length=2000)
    suggestion: str = Field(default="", max_length=1000)


class ResultReflection(ApiModel):
    status: ReflectionStatus
    summary: str = Field(min_length=1, max_length=2000)
    items: list[ReflectionItem] = Field(default_factory=list, max_length=20)
    risk_level: ReflectionRiskLevel


def unavailable_reflection(reason: str) -> ResultReflection:
    """模型未配置或调用失败时返回不阻断专业结果的检查状态。"""

    return ResultReflection(
        status=ReflectionStatus.UNAVAILABLE,
        summary=reason,
        items=[],
        risk_level=ReflectionRiskLevel.MEDIUM,
    )
