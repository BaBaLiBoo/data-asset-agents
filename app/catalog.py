from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import AssetInput


SCHEMA = """
CREATE TABLE IF NOT EXISTS asset_catalog (
    asset_id TEXT PRIMARY KEY,
    asset_name TEXT NOT NULL,
    business_domain TEXT NOT NULL,
    asset_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_asset_catalog_domain
ON asset_catalog(business_domain);
"""


class AssetCatalogError(RuntimeError):
    pass


class SQLiteAssetCatalog:
    """Small-demo asset catalog.

    Assets are stored as validated JSON contracts. Retrieval remains independent
    from this persistence implementation, so a production metadata catalog can
    replace SQLite without changing the three-layer comparator.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def upsert(self, asset: AssetInput, *, replace_existing: bool = True) -> bool:
        return self.upsert_many([asset], replace_existing=replace_existing) == 1

    def upsert_many(
        self,
        assets: list[AssetInput],
        *,
        replace_existing: bool = True,
    ) -> int:
        if not assets:
            return 0
        ids = [asset.asset_id for asset in assets]
        if len(ids) != len(set(ids)):
            raise AssetCatalogError("batch contains duplicate asset_id")
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            (
                asset.asset_id,
                asset.asset_name,
                asset.business_domain,
                asset.model_dump_json(),
                now,
                now,
            )
            for asset in assets
        ]
        statement = (
            """
            INSERT INTO asset_catalog(
                asset_id, asset_name, business_domain,
                asset_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_id) DO UPDATE SET
                asset_name = excluded.asset_name,
                business_domain = excluded.business_domain,
                asset_json = excluded.asset_json,
                updated_at = excluded.updated_at
            """
            if replace_existing
            else """
            INSERT OR IGNORE INTO asset_catalog(
                asset_id, asset_name, business_domain,
                asset_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """
        )
        with self._connect() as connection:
            before = connection.total_changes
            connection.executemany(statement, rows)
            changed = connection.total_changes - before
        return min(changed, len(rows))

    def get(self, asset_id: str) -> AssetInput | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT asset_json FROM asset_catalog WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
        return AssetInput.model_validate_json(row["asset_json"]) if row else None

    def require(self, asset_id: str) -> AssetInput:
        asset = self.get(asset_id)
        if asset is None:
            raise AssetCatalogError(f"asset not found: {asset_id}")
        return asset

    def list(
        self,
        *,
        offset: int = 0,
        limit: int | None = None,
        business_domain: str | None = None,
    ) -> list[AssetInput]:
        where = ""
        parameters: list[object] = []
        if business_domain is not None:
            where = " WHERE business_domain = ?"
            parameters.append(business_domain)
        sql = f"SELECT asset_json FROM asset_catalog{where} ORDER BY asset_id"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            parameters.extend([limit, offset])
        with self._connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [AssetInput.model_validate_json(row["asset_json"]) for row in rows]

    def count(self, *, business_domain: str | None = None) -> int:
        if business_domain is None:
            sql = "SELECT COUNT(*) AS count FROM asset_catalog"
            parameters: tuple[object, ...] = ()
        else:
            sql = "SELECT COUNT(*) AS count FROM asset_catalog WHERE business_domain = ?"
            parameters = (business_domain,)
        with self._connect() as connection:
            row = connection.execute(sql, parameters).fetchone()
        return int(row["count"])

    def bootstrap_jsonl(self, path: Path, *, only_when_empty: bool = True) -> int:
        source = Path(path)
        if not source.exists() or (only_when_empty and self.count() > 0):
            return 0
        assets = [
            AssetInput.model_validate_json(line)
            for line in source.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.upsert_many(assets, replace_existing=True)
        return len(assets)
