"""Data-source-driven ontology construction orchestration."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from data_asset_agents.core.errors import OntologyConflictError, OntologyError
from data_asset_agents.evaluation.ontology_construction import (
    GoldOntologyLoader,
    OntologyConstructionEvaluator,
)

from .candidate_generator import ObjectFirstCandidateGenerator
from .construction_repository import ConstructionRepository
from .models import (
    CandidateReviewDecision,
    ConstructionCandidate,
    ConstructionCandidateStatus,
    ConstructionEvaluationReport,
    ConstructionEvidenceMode,
    ConstructionMode,
    ConstructionRunStatus,
    CreateConstructionRunRequest,
    DimensionDefinition,
    DraftResources,
    LifecycleStatus,
    LinkType,
    MetricDefinition,
    ObjectDataSourceBinding,
    ObjectType,
    OntologyConstructionRun,
    OntologyDraft,
    OntologyDraftAggregate,
    PhysicalJoinDefinition,
    PromoteConstructionRunRequest,
    PropertyDefinition,
    ReviewConstructionCandidateRequest,
)
from .repository import OntologyManagerRepository

RESOURCE_MODELS = {
    "object_type": ("object_types", ObjectType),
    "property": ("properties", PropertyDefinition),
    "binding": ("bindings", ObjectDataSourceBinding),
    "link_type": ("link_types", LinkType),
    "physical_join": ("physical_joins", PhysicalJoinDefinition),
    "dimension": ("dimensions", DimensionDefinition),
    "metric": ("metrics", MetricDefinition),
}


def _hash_model(value: object) -> str:
    def normalize(item: object) -> object:
        if isinstance(item, BaseModel):
            return item.model_dump(mode="json")
        if isinstance(item, dict):
            return {str(key): normalize(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [normalize(child) for child in item]
        return item

    payload = normalize(value)
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


class OntologyConstructionService:
    def __init__(
        self,
        repository: ConstructionRepository,
        ontology_repository: OntologyManagerRepository,
        evidence_repository: object,
        generator: ObjectFirstCandidateGenerator,
        gold_loader: GoldOntologyLoader | None = None,
        evaluator: OntologyConstructionEvaluator | None = None,
    ) -> None:
        self.repository = repository
        self.ontology_repository = ontology_repository
        self.evidence_repository = evidence_repository
        self.generator = generator
        self.gold_loader = gold_loader
        self.evaluator = evaluator or OntologyConstructionEvaluator()

    def create_run(
        self, request: CreateConstructionRunRequest
    ) -> OntologyConstructionRun:
        snapshot = self.evidence_repository.get_metadata_snapshot(
            request.source_snapshot_id
        )
        historical_sql = self.evidence_repository.list_historical_sql(snapshot.id)
        run = OntologyConstructionRun(
            run_id=f"construction-{uuid4().hex}",
            source_snapshot_id=snapshot.id,
            source_snapshot_hash=_hash_model(snapshot),
            profiling_snapshot_hash=request.profiling_snapshot_hash
            or _hash_model([table.profiles for table in snapshot.tables]),
            historical_sql_snapshot_hash=request.historical_sql_snapshot_hash
            or _hash_model(historical_sql),
            catalog_mode=request.catalog_mode,
            construction_mode=request.construction_mode,
            evidence_mode=request.evidence_mode,
            llm_mode=request.llm_mode,
            provider=request.provider,
            model=request.model,
            temperature=request.temperature,
            random_seed=request.random_seed,
            git_sha=os.getenv("GITHUB_SHA", "unknown"),
            created_by=request.created_by,
        )
        self.repository.save_run(run)
        return run

    def list_runs(self) -> list[OntologyConstructionRun]:
        return self.repository.list_runs()

    def get_run(self, run_id: str) -> OntologyConstructionRun:
        run = self.repository.get_run(run_id)
        if run is None:
            raise OntologyError(f"Construction run not found: {run_id}")
        return run

    def generate(self, run_id: str) -> OntologyConstructionRun:
        run = self.get_run(run_id)
        if run.status != ConstructionRunStatus.CREATED:
            raise OntologyConflictError("Construction candidates can only be generated once")
        run.status = ConstructionRunStatus.RUNNING
        self.repository.save_run(run)
        try:
            snapshot = self.evidence_repository.get_metadata_snapshot(
                run.source_snapshot_id
            )
            historical_sql = self.evidence_repository.list_historical_sql(snapshot.id)
            if _hash_model(snapshot) != run.source_snapshot_hash:
                raise OntologyError("MetadataSnapshot changed after run creation")
            if _hash_model(historical_sql) != run.historical_sql_snapshot_hash:
                raise OntologyError("Historical SQL snapshot changed after run creation")
            include_profiles = run.evidence_mode != ConstructionEvidenceMode.O_A_SCHEMA_ONLY
            include_sql = run.evidence_mode in {
                ConstructionEvidenceMode.O_C_SCHEMA_PROFILING_SQL,
                ConstructionEvidenceMode.O_D_ALL_EVIDENCE_LLM,
            }
            evidence_snapshot = (
                snapshot
                if include_profiles
                else snapshot.model_copy(
                    update={
                        "tables": [
                            table.model_copy(update={"profiles": []})
                            for table in snapshot.tables
                        ]
                    }
                )
            )
            desired_llm_mode = (
                run.llm_mode
                if run.evidence_mode == ConstructionEvidenceMode.O_D_ALL_EVIDENCE_LLM
                else "mock"
            )
            generator = self.generator
            if generator.settings.llm_mode != desired_llm_mode:
                generator = ObjectFirstCandidateGenerator(
                    generator.settings.model_copy(
                        update={"llm_mode": desired_llm_mode}
                    ),
                    list(generator.assets.values()),
                )
            generated = generator.generate(
                evidence_snapshot,
                historical_sql if include_sql else [],
                DraftResources(),
                catalog_mode=run.catalog_mode,
            )
            candidates = self._flatten(run_id, generated)
            self.repository.save_candidates(candidates)
            run.candidate_counts = {
                resource_type: sum(
                    1 for item in candidates if item.resource_type == resource_type
                )
                for resource_type in RESOURCE_MODELS
            }
            run.excluded_tables = generated.excluded_tables
            run.llm_invocations = [
                {**item, "random_seed": run.random_seed}
                for item in generated.llm_invocations
            ]
            run.status = ConstructionRunStatus.CANDIDATES_READY
        except Exception as exc:
            run.status = ConstructionRunStatus.FAILED
            run.errors.append(f"{type(exc).__name__}: {exc}")
            run.completed_at = datetime.now(UTC)
            self.repository.save_run(run)
            raise
        self.repository.save_run(run)
        return run

    @staticmethod
    def _flatten(run_id: str, generated: Any) -> list[ConstructionCandidate]:
        groups = (
            ("object_type", generated.object_types, "object_type"),
            ("property", generated.properties, "property"),
            ("binding", generated.bindings, "binding"),
            ("link_type", generated.link_types, "link_type"),
            ("physical_join", generated.physical_joins, "physical_join"),
            ("dimension", generated.dimensions, "dimension"),
            ("metric", generated.metrics, "metric"),
        )
        result: list[ConstructionCandidate] = []
        for resource_type, candidates, payload_field in groups:
            for candidate in candidates:
                resource = getattr(candidate, payload_field)
                evidence = [
                    item.model_copy(
                        update={
                            "source_snapshot_id": item.source_snapshot_id
                            or generated.snapshot_id
                        }
                    )
                    for item in candidate.evidence
                ]
                payload = resource.model_dump(mode="json")
                result.append(
                    ConstructionCandidate(
                        candidate_id=candidate.candidate_id,
                        run_id=run_id,
                        resource_type=resource_type,
                        original_candidate=candidate.model_dump(mode="json"),
                        current_resource=payload,
                        evidence=evidence,
                    )
                )
        return result

    def list_candidates(
        self,
        run_id: str,
        *,
        resource_type: str | None = None,
        status: ConstructionCandidateStatus | None = None,
    ) -> list[ConstructionCandidate]:
        self.get_run(run_id)
        return [
            item
            for item in self.repository.list_candidates(run_id)
            if (resource_type is None or item.resource_type == resource_type)
            and (status is None or item.status == status)
        ]

    def review(
        self,
        run_id: str,
        candidate_id: str,
        request: ReviewConstructionCandidateRequest,
    ) -> ConstructionCandidate:
        run = self.get_run(run_id)
        if run.status not in {
            ConstructionRunStatus.CANDIDATES_READY,
            ConstructionRunStatus.UNDER_REVIEW,
        }:
            raise OntologyConflictError("Construction run is not reviewable")
        candidate = self.repository.get_candidate(run_id, candidate_id)
        if candidate is None:
            raise OntologyError(f"Construction candidate not found: {candidate_id}")
        if candidate.status not in {
            ConstructionCandidateStatus.PENDING,
            ConstructionCandidateStatus.DEFERRED,
        }:
            raise OntologyConflictError("Candidate already has a final review decision")
        expected_revision = candidate.revision
        resource = request.modified_resource or candidate.current_resource
        original_id = str(candidate.current_resource.get("id"))
        if str(resource.get("id")) != original_id:
            raise OntologyError("Human modification cannot change the stable resource ID")
        if request.decision == CandidateReviewDecision.MODIFY and not request.modified_resource:
            raise OntologyError("MODIFY requires modified_resource")
        if request.decision == CandidateReviewDecision.MERGE:
            if not request.merge_target_candidate_id:
                raise OntologyError("MERGE requires merge_target_candidate_id")
            target = self.repository.get_candidate(
                run_id, request.merge_target_candidate_id
            )
            if target is None or target.resource_type != candidate.resource_type:
                raise OntologyError("Merge target must exist and have the same resource type")
        statuses = {
            CandidateReviewDecision.ACCEPT: ConstructionCandidateStatus.ACCEPTED,
            CandidateReviewDecision.MODIFY: ConstructionCandidateStatus.MODIFIED,
            CandidateReviewDecision.REJECT: ConstructionCandidateStatus.REJECTED,
            CandidateReviewDecision.MERGE: ConstructionCandidateStatus.MERGED,
            CandidateReviewDecision.DEFER: ConstructionCandidateStatus.DEFERRED,
        }
        candidate.status = statuses[request.decision]
        candidate.current_resource = resource
        candidate.reviewer = request.reviewer
        candidate.reviewed_at = datetime.now(UTC)
        candidate.decision = request.decision
        candidate.comment = request.comment
        candidate.merge_target_candidate_id = request.merge_target_candidate_id
        candidate.revision += 1
        self.repository.save_review(candidate, expected_revision=expected_revision)
        run.status = ConstructionRunStatus.UNDER_REVIEW
        self.repository.save_run(run)
        return candidate

    def promote_to_draft(
        self, run_id: str, request: PromoteConstructionRunRequest
    ) -> OntologyDraftAggregate:
        run = self.get_run(run_id)
        candidates = self.repository.list_candidates(run_id)
        unfinished = [
            item.candidate_id
            for item in candidates
            if item.status
            in {
                ConstructionCandidateStatus.PENDING,
                ConstructionCandidateStatus.DEFERRED,
            }
        ]
        if unfinished:
            raise OntologyConflictError(
                "All candidates must be accepted, modified, rejected, or merged before promotion"
            )
        resources = DraftResources()
        for candidate in candidates:
            if candidate.status not in {
                ConstructionCandidateStatus.ACCEPTED,
                ConstructionCandidateStatus.MODIFIED,
            }:
                continue
            collection_name, model = RESOURCE_MODELS[candidate.resource_type]
            resource = model.model_validate(candidate.current_resource)
            if hasattr(resource, "lifecycle_status"):
                resource = resource.model_copy(
                    update={"lifecycle_status": LifecycleStatus.ACTIVE}
                )
            getattr(resources, collection_name).append(resource)
        self._validate_candidate_dependencies(resources)
        draft = OntologyDraft(
            id=f"draft-construction-{uuid4().hex}",
            name=request.draft_name,
            description=f"Reviewed candidates from {run_id}",
            source_snapshot_id=run.source_snapshot_id,
            construction_run_id=run_id,
            created_by=request.actor,
        )
        aggregate = self.ontology_repository.create_draft(draft, resources)
        run.promoted_draft_id = draft.id
        run.status = ConstructionRunStatus.PROMOTED_TO_DRAFT
        run.completed_at = datetime.now(UTC)
        self.repository.save_run(run)
        return aggregate

    def evaluate(self, run_id: str) -> ConstructionEvaluationReport:
        run = self.get_run(run_id)
        if self.gold_loader is None:
            raise OntologyError("Gold ontology loader is not configured")
        if not run.promoted_draft_id:
            raise OntologyConflictError("Promote reviewed candidates before evaluation")
        aggregate = self.ontology_repository.get_draft(run.promoted_draft_id)
        if aggregate is None:
            raise OntologyError("Promoted Draft no longer exists")
        gold, gold_hash = self.gold_loader.load()
        raw_report = (
            ConstructionEvaluationReport.model_validate(run.raw_evaluation)
            if run.raw_evaluation is not None
            else self.evaluator.evaluate_raw(
                run,
                self.repository.list_candidates(run_id),
                gold,
                gold_hash,
            )
        )
        report = self.evaluator.evaluate(
            run,
            aggregate.resources,
            self.repository.list_candidates(run_id),
            gold,
            gold_hash,
            raw_report=raw_report,
        )
        run.raw_evaluation = raw_report.model_dump(mode="json")
        run.evaluation = report.model_dump(mode="json")
        run.status = ConstructionRunStatus.EVALUATED
        self.repository.save_run(run)
        return report

    def evaluate_raw(self, run_id: str) -> ConstructionEvaluationReport:
        """Score the immutable generation snapshot before any review mutation."""

        run = self.get_run(run_id)
        if self.gold_loader is None:
            raise OntologyError("Gold ontology loader is not configured")
        if run.status in {
            ConstructionRunStatus.CREATED,
            ConstructionRunStatus.RUNNING,
            ConstructionRunStatus.FAILED,
        }:
            raise OntologyConflictError("Generate candidates before raw evaluation")
        if run.raw_evaluation is not None:
            return ConstructionEvaluationReport.model_validate(run.raw_evaluation)
        gold, gold_hash = self.gold_loader.load()
        report = self.evaluator.evaluate_raw(
            run,
            self.repository.list_candidates(run_id),
            gold,
            gold_hash,
        )
        run.raw_evaluation = report.model_dump(mode="json")
        self.repository.save_run(run)
        return report

    def get_reviewed_evaluation(self, run_id: str) -> ConstructionEvaluationReport:
        run = self.get_run(run_id)
        if run.evaluation is None:
            raise OntologyError("Reviewed construction evaluation not found")
        return ConstructionEvaluationReport.model_validate(run.evaluation)

    def record_publication(
        self,
        run_id: str,
        version_id: str,
        *,
        runtime_activation_succeeded: bool,
    ) -> OntologyConstructionRun:
        run = self.get_run(run_id)
        if not run.promoted_draft_id:
            raise OntologyConflictError("Construction run has no promoted Draft")
        aggregate = self.ontology_repository.get_draft(run.promoted_draft_id)
        if aggregate is None:
            raise OntologyError("Promoted construction Draft no longer exists")
        report = aggregate.draft.validation_report
        artifact = self.ontology_repository.get_compiled_artifact(version_id)
        run.strict_validation_passed = bool(
            report
            and report.valid
            and report.construction_mode == ConstructionMode.STRICT_CONSTRUCTION
        )
        if report is not None:
            run.seed_accessed = report.seed_accessed
            run.fallback_used = report.fallback_used
            run.legacy_ontology_accessed = report.legacy_ontology_accessed
        if artifact is not None:
            run.seed_accessed = run.seed_accessed or artifact.seed_accessed
            run.fallback_used = run.fallback_used or artifact.fallback_used
            run.legacy_ontology_accessed = (
                run.legacy_ontology_accessed
                or artifact.legacy_ontology_accessed
            )
            run.compiled_artifact_hash = artifact.bundle_hash
        run.publication_succeeded = artifact is not None
        run.runtime_activation_succeeded = runtime_activation_succeeded
        run.published_version_id = version_id
        self.repository.save_run(run)
        return run

    @staticmethod
    def _validate_candidate_dependencies(resources: DraftResources) -> None:
        object_ids = {item.id for item in resources.object_types}
        property_ids = {item.id for item in resources.properties}
        dimension_ids = {item.id for item in resources.dimensions}
        missing: list[str] = []
        for prop in resources.properties:
            if prop.object_type_id not in object_ids:
                missing.append(f"{prop.id}:object={prop.object_type_id}")
        for binding in resources.bindings:
            if binding.object_type_id not in object_ids:
                missing.append(f"{binding.id}:object={binding.object_type_id}")
            missing.extend(
                f"{binding.id}:property={property_id}"
                for property_id in binding.property_bindings
                if property_id not in property_ids
            )
        for dimension in resources.dimensions:
            if dimension.property_id not in property_ids:
                missing.append(f"{dimension.id}:property={dimension.property_id}")
        for metric in resources.metrics:
            if metric.measure_property_id not in property_ids:
                missing.append(
                    f"{metric.id}:measure={metric.measure_property_id}"
                )
            missing.extend(
                f"{metric.id}:dimension={dimension_id}"
                for dimension_id in metric.supported_dimension_ids
                if dimension_id not in dimension_ids
            )
        if missing:
            raise OntologyError(
                "Accepted construction candidates have missing dependencies: "
                + ", ".join(sorted(missing))
            )
