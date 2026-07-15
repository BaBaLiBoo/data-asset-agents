from __future__ import annotations

from pathlib import Path
from typing import Literal

from data_asset_agents.core.errors import DataAssetAgentsError, OntologyError
from data_asset_agents.execution.executor import QueryExecutor
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
    PublishDryRunReport,
    ReviewStatus,
)
from data_asset_agents.ontology.repository.postgres_repository import (
    PostgresOntologyRepository,
)
from data_asset_agents.ontology.repository.yaml_repository import YamlOntologyRepository
from data_asset_agents.ontology.service import OntologyService
from data_asset_agents.ontology.validation import OntologyContractValidator
from data_asset_agents.sql_assets import HistoricalSQLParser
from data_asset_agents.text2sql.models import QueryFilter, SemanticQuery, TimeRange
from data_asset_agents.text2sql.tools import JoinPlanner, build_select_sql
from data_asset_agents.validation import SQLValidator


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
        executor: QueryExecutor | None = None,
        yaml_repository: YamlOntologyRepository | None = None,
    ) -> None:
        self.inspector = inspector
        self.sql_parser = sql_parser
        self.generator = generator
        self.repository = repository
        self.seed_bundle = seed_bundle
        self.historical_sql_path = historical_sql_path
        self.executor = executor
        self.yaml_repository = yaml_repository
        self.contract_validator = OntologyContractValidator(repository.engine)

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
        snapshot_id: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[CandidateEnvelope]:
        return self.repository.list_candidates(
            status=status,
            candidate_type=candidate_type,
            snapshot_id=snapshot_id,
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
        report = self.validate_publish(request)
        if not report.valid:
            raise OntologyError(
                "Ontology publish dry run failed: " + "; ".join(report.errors)
            )
        return self.repository.publish(self.seed_bundle, request)

    def validate_publish(self, request: OntologyPublishRequest) -> PublishDryRunReport:
        if request.snapshot_id is None:
            raise OntologyError("snapshot_id is required for publication validation")
        bundle, _, candidates = self.repository.prepare_publication(
            self.seed_bundle, request
        )
        contract = self.contract_validator.validate(
            bundle,
            snapshot_id=request.snapshot_id,
            source_candidates=candidates,
        )
        errors = list(contract.errors)
        generated_sql: str | None = None
        sqlglot_valid = False
        explain_passed = False
        explain_plan: list[str] = []
        if contract.valid:
            try:
                yaml_repository = self.yaml_repository or YamlOntologyRepository(
                    self.generator.settings.ontology_path
                )
                proposed = OntologyService(
                    yaml_repository,
                    settings=self.generator.settings,
                    model_factory=self.generator.factory,
                    bundle_override=bundle,
                )
                semantic = SemanticQuery(
                    metric_ids=[
                        "credit_card_transaction_amount",
                        "credit_card_transaction_count",
                    ],
                    dimension_ids=["branch"],
                    metric_names=["信用卡交易金额", "信用卡交易笔数"],
                    dimension_names=["分行"],
                    time_range=TimeRange(kind="relative_days", days=30),
                    intent="aggregate",
                    confidence=1.0,
                )
                resolved = proposed.resolve(semantic)
                metrics = proposed.get_metrics(semantic)
                dimensions = proposed.get_dimensions(semantic)
                selected_tables = list(resolved["selected_tables"])
                join_plan = JoinPlanner(bundle.joins).plan(selected_tables)
                resolved_filters = [
                    QueryFilter.model_validate(item)
                    for item in resolved["filters"]  # type: ignore[union-attr]
                ]
                generated_sql = build_select_sql(
                    proposed,
                    semantic,
                    metrics,
                    dimensions,
                    join_plan,
                    resolved_filters,
                )
                validation = SQLValidator(bundle).validate(
                    generated_sql,
                    set(selected_tables),
                    resolved_filters,
                )
                sqlglot_valid = validation.valid
                if not validation.valid:
                    errors.extend(validation.errors)
                else:
                    dry_executor = QueryExecutor(
                        self.generator.settings,
                        bundle,
                        engine=self.repository.engine,
                    )
                    explain_plan = dry_executor.explain(
                        generated_sql, set(selected_tables)
                    )
                    explain_passed = True
            except DataAssetAgentsError as exc:
                errors.append(str(exc))
            except Exception as exc:
                errors.append(f"Unexpected dry-run failure: {exc}")
        return PublishDryRunReport(
            valid=contract.valid and sqlglot_valid and explain_passed and not errors,
            version=request.version,
            snapshot_id=request.snapshot_id,
            candidate_ids=[item.id for item in candidates],
            contract=contract,
            generated_sql=generated_sql,
            sqlglot_valid=sqlglot_valid,
            explain_passed=explain_passed,
            explain_plan=explain_plan,
            errors=errors,
        )

    def activate(self, version: str) -> OntologyVersion:
        return self.repository.activate_version(version)

    def versions(self) -> list[OntologyVersion]:
        return self.repository.list_versions()
