"""在调用专业服务前后校验统一OpenAPI契约。"""

from __future__ import annotations

import yaml
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_PATH = REPOSITORY_ROOT / "contracts" / "openapi.yaml"


class ContractValidationError(ValueError):
    """请求或响应不符合统一接口契约。"""


@lru_cache(maxsize=1)
def _load_openapi() -> dict[str, Any]:
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def _validator(schema_name: str) -> Draft202012Validator:
    document = _load_openapi()
    schemas = document["components"]["schemas"]
    if schema_name not in schemas:
        raise KeyError(f"OpenAPI中不存在Schema: {schema_name}")
    root_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$ref": f"#/components/schemas/{schema_name}",
        "components": document["components"],
    }
    return Draft202012Validator(root_schema)


def validate_contract(schema_name: str, instance: Any) -> None:
    errors = sorted(
        _validator(schema_name).iter_errors(instance),
        key=lambda item: list(item.path),
    )
    if not errors:
        return
    messages = [
        f"{'.'.join(str(part) for part in error.path) or '<root>'}: {error.message}"
        for error in errors
    ]
    raise ContractValidationError("; ".join(messages))
