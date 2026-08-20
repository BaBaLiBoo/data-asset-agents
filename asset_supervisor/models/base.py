"""Pydantic 基础模型及统一命名规则。"""

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    """内部使用 snake_case，对外 JSON 统一使用 camelCase。"""

    # extra="forbid" 可尽早发现模型幻觉字段或前后端契约拼写错误。
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        str_strip_whitespace=True,
    )
