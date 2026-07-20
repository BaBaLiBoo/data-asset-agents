"""Command-line entry points for safe legacy object-model migration."""

from __future__ import annotations

import argparse
import json

from sqlalchemy import create_engine

from data_asset_agents.core.config import get_settings
from data_asset_agents.ontology.repository import YamlOntologyRepository

from .migration import LegacyOntologyObjectMigrator
from .models import MigrateLegacyRequest
from .repository import PostgresOntologyManagerRepository
from .service import OntologyManagerService
from .validator import OntologyDraftValidator


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ontology Manager maintenance")
    subcommands = parser.add_subparsers(dest="command", required=True)
    migrate = subcommands.add_parser("migrate-legacy")
    migrate.add_argument("--dry-run", action="store_true")
    migrate.add_argument("--created-by", default="legacy-migrator")
    migrate.add_argument("--draft-name", default="MiniBank legacy object migration")
    return parser


def main() -> None:
    args = _parser().parse_args()
    settings = get_settings()
    bundle = YamlOntologyRepository(settings.ontology_path).load()
    if args.dry_run:
        resources = LegacyOntologyObjectMigrator(bundle).migrate()
        print(json.dumps(resources.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    repository = PostgresOntologyManagerRepository(engine)
    service = OntologyManagerService(
        repository,
        bundle,
        OntologyDraftValidator(bundle, engine=engine),
        engine=engine,
    )
    result = service.migrate_legacy(
        MigrateLegacyRequest(
            draft_name=args.draft_name,
            created_by=args.created_by,
        )
    )
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
