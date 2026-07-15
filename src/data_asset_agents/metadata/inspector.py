from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, MetaData, String, Table, cast, desc, func, inspect, select
from sqlalchemy.exc import SQLAlchemyError

from data_asset_agents.core.errors import OntologyError
from data_asset_agents.ontology.models import (
    ColumnMetadata,
    ColumnProfile,
    ForeignKeyMetadata,
    IndexMetadata,
    MetadataSnapshot,
    TableMetadata,
    ValueFrequency,
)

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SAFE_CATEGORY_TOKENS = (
    "status",
    "type",
    "channel",
    "category",
    "region",
    "currency",
)
SENSITIVE_TOKENS = ("name", "phone", "email", "address", "identity", "id_card")


def _stringify(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, (date, datetime)):
        return str(value)
    return str(value)


def mask_sample_value(column_name: str, value: object) -> str:
    """Mask direct identifiers while preserving bounded categorical evidence."""

    text_value = _stringify(value) or ""
    lowered = column_name.lower()
    if any(token in lowered for token in SAFE_CATEGORY_TOKENS):
        return text_value[:80]
    if lowered == "id" or lowered.endswith("_id"):
        return f"***{text_value[-2:]}" if text_value else "***"
    if any(token in lowered for token in SENSITIVE_TOKENS):
        return f"{text_value[:1]}***" if text_value else "***"
    if len(text_value) > 80:
        return text_value[:77] + "..."
    return text_value


class MetadataInspector:
    """Extract allowlisted metadata plus bounded and masked column profiles."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @staticmethod
    def _validate_identifier(identifier: str, kind: str) -> None:
        if not IDENTIFIER.fullmatch(identifier):
            raise OntologyError(f"Unsafe {kind} identifier: {identifier}")

    def _approved_tables(
        self,
        schema_name: str,
        allowed_tables: set[str],
        requested_tables: list[str] | None,
    ) -> list[str]:
        self._validate_identifier(schema_name, "schema")
        discovered = set(inspect(self.engine).get_table_names(schema=schema_name))
        requested = set(requested_tables or allowed_tables)
        unsafe = {name for name in requested if not IDENTIFIER.fullmatch(name)}
        if unsafe:
            raise OntologyError(
                f"Unsafe table identifier(s): {', '.join(sorted(unsafe))}"
            )
        outside_allowlist = requested - allowed_tables
        if outside_allowlist:
            raise OntologyError(
                "Table(s) are not in the profiling allowlist: "
                + ", ".join(sorted(outside_allowlist))
            )
        missing = requested - discovered
        if missing:
            raise OntologyError(
                f"Approved table(s) do not exist: {', '.join(sorted(missing))}"
            )
        return sorted(requested)

    def capture_snapshot(
        self,
        *,
        schema_name: str = "public",
        allowed_tables: set[str],
        requested_tables: list[str] | None = None,
        sample_limit: int = 5,
        top_value_limit: int = 5,
    ) -> MetadataSnapshot:
        """Capture schema details and profiles without interpolating untrusted SQL."""

        table_names = self._approved_tables(
            schema_name, allowed_tables, requested_tables
        )
        inspector = inspect(self.engine)
        metadata = MetaData()
        tables: list[TableMetadata] = []
        with self.engine.connect() as connection:
            for table_name in table_names:
                table = Table(
                    table_name,
                    metadata,
                    schema=schema_name,
                    autoload_with=connection,
                    extend_existing=True,
                )
                row_count = int(
                    connection.execute(select(func.count()).select_from(table)).scalar_one()
                )
                raw_columns = inspector.get_columns(table_name, schema=schema_name)
                columns = [
                    ColumnMetadata(
                        name=str(column["name"]),
                        data_type=str(column["type"]),
                        nullable=bool(column.get("nullable", True)),
                        comment=column.get("comment"),
                        default=(
                            str(column["default"])
                            if column.get("default") is not None
                            else None
                        ),
                    )
                    for column in raw_columns
                ]
                profiles = [
                    self._profile_column(
                        connection,
                        table,
                        column.name,
                        str(column.type),
                        row_count,
                        sample_limit,
                        top_value_limit,
                    )
                    for column in table.columns
                ]
                pk = inspector.get_pk_constraint(table_name, schema=schema_name)
                foreign_keys = [
                    ForeignKeyMetadata(
                        name=item.get("name"),
                        constrained_columns=list(item.get("constrained_columns") or []),
                        referred_schema=item.get("referred_schema"),
                        referred_table=str(item["referred_table"]),
                        referred_columns=list(item.get("referred_columns") or []),
                    )
                    for item in inspector.get_foreign_keys(table_name, schema=schema_name)
                ]
                indexes = [
                    IndexMetadata(
                        name=str(item["name"]),
                        columns=list(item.get("column_names") or []),
                        unique=bool(item.get("unique", False)),
                    )
                    for item in inspector.get_indexes(table_name, schema=schema_name)
                    if item.get("name")
                ]
                try:
                    table_comment = inspector.get_table_comment(
                        table_name, schema=schema_name
                    ).get("text")
                except NotImplementedError:
                    table_comment = None
                tables.append(
                    TableMetadata(
                        schema_name=schema_name,
                        table_name=table_name,
                        comment=table_comment,
                        columns=columns,
                        primary_key=list(pk.get("constrained_columns") or []),
                        foreign_keys=foreign_keys,
                        indexes=indexes,
                        profiles=profiles,
                    )
                )
        return MetadataSnapshot(schema_name=schema_name, tables=tables)

    def _profile_column(
        self,
        connection: Any,
        table: Table,
        column_name: str,
        data_type: str,
        row_count: int,
        sample_limit: int,
        top_value_limit: int,
    ) -> ColumnProfile:
        self._validate_identifier(column_name, "column")
        column = table.c[column_name]
        null_count, distinct_count = connection.execute(
            select(
                func.count().filter(column.is_(None)),
                func.count(func.distinct(column)),
            ).select_from(table)
        ).one()
        minimum: str | None = None
        maximum: str | None = None
        try:
            minimum_value, maximum_value = connection.execute(
                select(func.min(column), func.max(column)).select_from(table)
            ).one()
            minimum = _stringify(minimum_value)
            maximum = _stringify(maximum_value)
        except SQLAlchemyError:
            # Some database-specific types have no ordering operators.
            minimum = None
            maximum = None
        count_label = func.count().label("frequency")
        top_rows = connection.execute(
            select(column, count_label)
            .select_from(table)
            .where(column.is_not(None))
            .group_by(column)
            .order_by(desc(count_label), cast(column, String))
            .limit(top_value_limit)
        ).all()
        sample_rows = connection.execute(
            select(column)
            .select_from(table)
            .where(column.is_not(None))
            .order_by(cast(column, String))
            .distinct()
            .limit(sample_limit)
        ).scalars()
        denominator = row_count or 1
        return ColumnProfile(
            table_name=table.name,
            column_name=column_name,
            data_type=data_type,
            row_count=row_count,
            null_count=int(null_count or 0),
            null_rate=round(int(null_count or 0) / denominator, 6),
            distinct_count=int(distinct_count or 0),
            unique_rate=round(int(distinct_count or 0) / denominator, 6),
            minimum=minimum,
            maximum=maximum,
            top_values=[
                ValueFrequency(
                    value=mask_sample_value(column_name, row[0]),
                    count=int(row[1]),
                )
                for row in top_rows
            ],
            sample_values=[
                mask_sample_value(column_name, value) for value in sample_rows
            ],
        )

    def inspect_schema(self, schema: str = "public") -> list[dict[str, Any]]:
        """Compatibility metadata view without profiling arbitrary tables."""

        inspector = inspect(self.engine)
        self._validate_identifier(schema, "schema")
        assets: list[dict[str, Any]] = []
        for table_name in inspector.get_table_names(schema=schema):
            self._validate_identifier(table_name, "table")
            assets.append(
                {
                    "table": table_name,
                    "schema": schema,
                    "columns": [
                        {
                            "name": column["name"],
                            "type": str(column["type"]),
                            "nullable": column["nullable"],
                            "comment": column.get("comment"),
                        }
                        for column in inspector.get_columns(table_name, schema=schema)
                    ],
                }
            )
        return assets

    def basic_statistics(self, table: str, allowed_tables: set[str]) -> dict[str, int]:
        """Compatibility helper retaining strict allowlist validation."""

        snapshot = self.capture_snapshot(
            allowed_tables=allowed_tables,
            requested_tables=[table],
            sample_limit=1,
            top_value_limit=1,
        )
        row_count = snapshot.tables[0].profiles[0].row_count
        return {"row_count": row_count}
