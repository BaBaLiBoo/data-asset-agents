from __future__ import annotations

from app.catalog import SQLiteAssetCatalog
from app.config import Settings
from app.evaluation import load_assets


def main() -> int:
    settings = Settings.from_env()
    settings.ensure_directories()
    assets = list(load_assets(settings.bootstrap_assets_path).values())
    catalog = SQLiteAssetCatalog(settings.asset_catalog_path)
    catalog.upsert_many(assets, replace_existing=True)
    print(f"Asset catalog synchronized: {len(assets)} assets; total={catalog.count()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
