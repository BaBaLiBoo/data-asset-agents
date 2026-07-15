from typing import Any

from sqlalchemy import Engine, inspect, text


class MetadataInspector:
    """Extract physical metadata and bounded, non-sensitive statistics for offline building."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def inspect_schema(self, schema: str = "public") -> list[dict[str, Any]]:
        inspector = inspect(self.engine)
        assets: list[dict[str, Any]] = []
        for table_name in inspector.get_table_names(schema=schema):
            columns = [
                {
                    "name": column["name"],
                    "type": str(column["type"]),
                    "nullable": column["nullable"],
                }
                for column in inspector.get_columns(table_name, schema=schema)
            ]
            assets.append({"table": table_name, "schema": schema, "columns": columns})
        return assets

    def basic_statistics(self, table: str, allowed_tables: set[str]) -> dict[str, int]:
        if table not in allowed_tables:
            raise ValueError(f"Table is not approved for profiling: {table}")
        with self.engine.connect() as connection:
            count = connection.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one()
        return {"row_count": int(count)}
