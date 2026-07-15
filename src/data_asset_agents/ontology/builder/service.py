from __future__ import annotations

from pathlib import Path
from typing import Literal

from data_asset_agents.metadata import MetadataInspector
from data_asset_agents.ontology.builder.candidate_generator import CandidateGenerator
from data_asset_agents.ontology.models import (
    CandidateEnvelope,
    CandidateReviewRequest,
    OntologyBuildRequest,
    OntologyBuildResult,
    OntologyBundle,
    OntologyPublishRequest,
    OntologyVersion,
    ReviewStatus,
)
from data_asset_agents.ontology.repository.postgres_repository import (
    PostgresOntologyRepository,
)
from data_asset_agents.sql_assets import HistoricalSQLParser


class OntologyBuildService:
    """Orchestrate offline evidence capture, candidate review, and publication."""

    def __init__(
        self,
        *,
        inspector: MetadataInspector,
        sql_parser: HistoricalSQLParser,
        generator: CandidateGenerator,
        repository: PostgresOntologyRepository,
        seed_bundle: OntologyBundle,
        historical_sql_path: Path,
    ) -> None:
        self.inspector = inspector
        self.sql_parser = sql_parser
        self.generator = generator
        self.repository = repository
        self.seed_bundle = seed_bundle
        self.historical_sql_path = historical_sql_path

    def build(self, request: OntologyBuildRequest) -> OntologyBuildResult:
        allowed_tables = {asset.name for asset in self.seed_bundle.tables}
        snapshot = self.inspector.capture_snapshot(
            schema_name=request.schema_name,
            allowed_tables=allowed_tables,
            requested_tables=request.tables,
            sample_limit=request.sample_limit,
            top_value_limit=request.top_value_limit,
        )
        historical_sql = self.sql_parser.parse_file(self.historical_sql_path)
        summary = self.sql_parser.summarize(historical_sql)
        concepts, mappings, joins = self.generator.generate(
            snapshot, historical_sql, summary
        )
        result = OntologyBuildResult(
            snapshot=snapshot,
            historical_sql=historical_sql,
            historical_summary=summary,
            concepts=concepts,
            mappings=mappings,
            joins=joins,
        )
        self.repository.save_build(result)
        return result

    def list_candidates(
        self,
        *,
        status: ReviewStatus | None = None,
        candidate_type: Literal["concept", "mapping", "join"] | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[CandidateEnvelope]:
        return self.repository.list_candidates(
            status=status,
            candidate_type=candidate_type,
            limit=limit,
            offset=offset,
        )

    def get_candidate(self, candidate_id: str) -> CandidateEnvelope:
        return self.repository.get_candidate(candidate_id)

    def verify(
        self, candidate_id: str, request: CandidateReviewRequest
    ) -> CandidateEnvelope:
        return self.repository.review_candidate(
            candidate_id, ReviewStatus.VERIFIED, request
        )

    def reject(
        self, candidate_id: str, request: CandidateReviewRequest
    ) -> CandidateEnvelope:
        return self.repository.review_candidate(
            candidate_id, ReviewStatus.REJECTED, request
        )

    def publish(self, request: OntologyPublishRequest) -> OntologyVersion:
        return self.repository.publish(self.seed_bundle, request)

    def versions(self) -> list[OntologyVersion]:
        return self.repository.list_versions()
