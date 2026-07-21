"""Deterministic, domain-neutral identifiers and column semantic inference."""

from __future__ import annotations

import hashlib
import re

from .models import PropertyDataType, SemanticRole


def stable_resource_id(*parts: str) -> str:
    """Create a stable safe identifier from evidence-controlled strings."""

    normalized = "_".join(
        re.sub(r"[^a-z0-9]+", "_", part.lower()).strip("_") for part in parts if part
    ).strip("_")
    if len(normalized) <= 120:
        return normalized
    digest = hashlib.sha256(normalized.encode()).hexdigest()[:12]
    return f"{normalized[:107]}_{digest}"


def infer_object_id(table_name: str) -> str:
    """Infer a review candidate ID without treating a table as the object itself."""

    name = re.sub(r"^(dim|dwd)_", "", table_name.lower())
    if name == "card_transaction":
        return "transaction"
    return stable_resource_id(name)


def infer_property_name(column_name: str) -> str:
    """Map common physical naming patterns to stable semantic property roles."""

    lowered = column_name.lower()
    aliases = {
        "txn_amount_cny": "amount",
        "transaction_status": "status",
        "transaction_channel": "channel",
        "transaction_date": "event_time",
        "branch_name": "name",
        "merchant_name": "name",
        "merchant_category": "category",
    }
    return aliases.get(lowered, stable_resource_id(lowered))


def infer_property_contract(
    column_name: str, physical_type: str, *, primary_key: bool = False
) -> tuple[SemanticRole, PropertyDataType]:
    """Infer review-only semantic role and portable type from metadata."""

    lowered = column_name.lower()
    type_name = physical_type.lower()
    if primary_key:
        role = SemanticRole.IDENTIFIER
    elif "amount" in lowered:
        role = SemanticRole.MEASURE
    elif "status" in lowered:
        role = SemanticRole.STATUS
    elif any(token in lowered for token in ("date", "time", "month")):
        role = SemanticRole.TIME
    elif any(token in lowered for token in ("type", "category", "channel", "name", "region")):
        role = SemanticRole.DIMENSION
    else:
        role = SemanticRole.ATTRIBUTE

    if any(token in type_name for token in ("int", "serial")) or (
        not type_name and lowered.endswith("_id")
    ):
        data_type = PropertyDataType.INTEGER
    elif any(token in type_name for token in ("numeric", "decimal", "real", "double", "money")) or (
        not type_name and "amount" in lowered
    ):
        data_type = PropertyDataType.DECIMAL
    elif "bool" in type_name:
        data_type = PropertyDataType.BOOLEAN
    elif "timestamp" in type_name:
        data_type = PropertyDataType.DATETIME
    elif "date" in type_name or (not type_name and any(x in lowered for x in ("date", "time"))):
        data_type = PropertyDataType.DATE
    else:
        data_type = PropertyDataType.STRING
    return role, data_type
