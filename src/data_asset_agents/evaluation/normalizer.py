from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from data_asset_agents.text2sql.models import ExecutionResult


class ResultNormalizer:
    """Create stable, type-aware result JSON and SHA-256 fingerprints."""

    def __init__(self, float_precision: int = 10) -> None:
        self.float_precision = float_precision

    def normalize(
        self,
        result: ExecutionResult,
        *,
        order_sensitive: bool,
    ) -> dict[str, Any]:
        columns = [str(column).strip().lower() for column in result.columns]
        rows = [
            [self._value(row.get(original)) for original in result.columns] for row in result.rows
        ]
        if not order_sensitive:
            rows.sort(
                key=lambda row: json.dumps(
                    row,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        return {"columns": columns, "rows": rows}

    def hash(
        self,
        result: ExecutionResult,
        *,
        order_sensitive: bool,
    ) -> str:
        payload = self.normalize(result, order_sensitive=order_sensitive)
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _value(self, value: Any) -> Any:
        if value is None or isinstance(value, (str, bool, int)):
            return value
        if isinstance(value, Decimal):
            return format(value.normalize(), "f")
        if isinstance(value, float):
            if not math.isfinite(value):
                return str(value)
            return format(value, f".{self.float_precision}g")
        if isinstance(value, (date, datetime, time)):
            return value.isoformat()
        if isinstance(value, dict):
            return {str(key): self._value(item) for key, item in sorted(value.items())}
        if isinstance(value, (list, tuple)):
            return [self._value(item) for item in value]
        return str(value)
