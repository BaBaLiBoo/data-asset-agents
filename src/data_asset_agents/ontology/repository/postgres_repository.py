from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from data_asset_agents.core.errors import OntologyError
from data_asset_agents.ontology.models import (
    BusinessConcept,
    CandidateConcept,
    CandidateEnvelope,
    CandidateJoin,
    CandidateMapping,
    CandidateReviewRequest,
    JoinDefinition,
    OntologyBuildResult,
    OntologyBundle,
    OntologyPublishRequest,
    OntologyVersion,
    PhysicalMapping,
    ReviewStatus,
)

CANDIDATE_MODELS = {
    "concept": CandidateConcept,
    "mapping": CandidateMapping,
    "join": CandidateJoin,
}
CANDIDATE_EDIT_FIELDS = {
    "concept": {
        "business_name",
        "business_object",
        "semantic_property",
        "role",
        "unit",
        "synonyms",
        "confidence",
        "evidence",
        "semantic_id",
    },
    "mapping": {
        "concept_id",
        "table_name",
        "columns",
        "condition",
        "confidence",
        "evidence",
    },
    "join": {
        "left_table",
        "right_table",
        "left_column",
        "right_column",
        "relationship",
        "confidence",
        "evidence",
    },
}


def _json(payload: Any) -> str:
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")
    return json.dumps(payload, ensure_ascii=False)


class PostgresOntologyRepository:
    """Persist build evidence, review state, and immutable published versions."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def save_build(self, result: OntologyBuildResult) -> None:
        snapshot = result.snapshot
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO metadata_snapshot
                        (snapshot_id, schema_name, source, captured_at, payload)
                    VALUES
                        (:id, :schema_name, :source, :captured_at, CAST(:payload AS jsonb))
                    """
                ),
                {
                    "id": snapshot.id,
                    "schema_name": snapshot.schema_name,
                    "source": snapshot.source,
                    "captured_at": snapshot.captured_at,
                    "payload": _json(
                        {
                            "snapshot": snapshot.model_dump(mode="json"),
                            "historical_summary": result.historical_summary.model_dump(
                                mode="json"
                            ),
                        }
                    ),
                },
            )
            for table_metadata in snapshot.tables:
                connection.execute(
                    text(
                        """
                        INSERT INTO metadata_table_snapshot
                            (snapshot_id, table_name, payload)
                        VALUES (:snapshot_id, :table_name, CAST(:payload AS jsonb))
                        """
                    ),
                    {
                        "snapshot_id": snapshot.id,
                        "table_name": table_metadata.table_name,
                        "payload": _json(table_metadata),
                    },
                )
                for profile in table_metadata.profiles:
                    connection.execute(
                        text(
                            """
                            INSERT INTO column_profile
                                (snapshot_id, table_name, column_name, payload)
                            VALUES
                                (:snapshot_id, :table_name, :column_name,
                                 CAST(:payload AS jsonb))
                            """
                        ),
                        {
                            "snapshot_id": snapshot.id,
                            "table_name": table_metadata.table_name,
                            "column_name": profile.column_name,
                            "payload": _json(profile),
                        },
                    )
            for analysis in result.historical_sql:
                connection.execute(
                    text(
                        """
                        INSERT INTO historical_sql_analysis
                            (analysis_id, snapshot_id, certified, payload)
                        VALUES
                            (:id, :snapshot_id, :certified, CAST(:payload AS jsonb))
                        """
                    ),
                    {
                        "id": analysis.id,
                        "snapshot_id": snapshot.id,
                        "certified": analysis.certified,
                        "payload": _json(analysis),
                    },
                )
            for candidate_type, candidates in (
                ("concept", result.concepts),
                ("mapping", result.mappings),
                ("join", result.joins),
            ):
                for candidate in candidates:
                    connection.execute(
                        text(
                            """
                            INSERT INTO ontology_candidate
                                (candidate_id, snapshot_id, candidate_type, status,
                                 payload, created_at)
                            VALUES
                                (:id, :snapshot_id, :candidate_type, :status,
                                 CAST(:payload AS jsonb), :created_at)
                            """
                        ),
                        {
                            "id": candidate.id,
                            "snapshot_id": snapshot.id,
                            "candidate_type": candidate_type,
                            "status": candidate.status.value,
                            "payload": _json(candidate),
                            "created_at": candidate.created_at,
                        },
                    )

    def list_candidates(
        self,
        *,
        status: ReviewStatus | None = None,
        candidate_type: Literal["concept", "mapping", "join"] | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[CandidateEnvelope]:
        clauses: list[str] = []
        parameters: dict[str, Any] = {"limit": limit, "offset": offset}
        if status is not None:
            clauses.append("status = :status")
            parameters["status"] = status.value
        if candidate_type is not None:
            clauses.append("candidate_type = :candidate_type")
            parameters["candidate_type"] = candidate_type
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT candidate_id, candidate_type, status, payload, "
                    "reviewed_at, reviewer, review_note FROM ontology_candidate"
                    + where
                    + " ORDER BY created_at, candidate_id LIMIT :limit OFFSET :offset"
                ),
                parameters,
            ).mappings()
            return [self._envelope(row) for row in rows]

    def get_candidate(self, candidate_id: str) -> CandidateEnvelope:
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT candidate_id, candidate_type, status, payload,
                           reviewed_at, reviewer, review_note
                    FROM ontology_candidate
                    WHERE candidate_id = :candidate_id
                    """
                ),
                {"candidate_id": candidate_id},
            ).mappings().one_or_none()
        if row is None:
            raise OntologyError(f"Ontology candidate not found: {candidate_id}")
        return self._envelope(row)

    def review_candidate(
        self,
        candidate_id: str,
        target_status: ReviewStatus,
        request: CandidateReviewRequest,
    ) -> CandidateEnvelope:
        if target_status not in {ReviewStatus.VERIFIED, ReviewStatus.REJECTED}:
            raise OntologyError(f"Invalid review target status: {target_status}")
        with self.engine.begin() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT candidate_type, status, payload
                    FROM ontology_candidate
                    WHERE candidate_id = :candidate_id
                    FOR UPDATE
                    """
                ),
                {"candidate_id": candidate_id},
            ).mappings().one_or_none()
            if row is None:
                raise OntologyError(f"Ontology candidate not found: {candidate_id}")
            if row["status"] != ReviewStatus.CANDIDATE.value:
                raise OntologyError(
                    f"Candidate {candidate_id} is already {row['status']} and is immutable"
                )
            candidate_type = str(row["candidate_type"])
            unexpected = set(request.edits) - CANDIDATE_EDIT_FIELDS[candidate_type]
            if unexpected:
                raise OntologyError(
                    "Unsupported candidate edit field(s): "
                    + ", ".join(sorted(unexpected))
                )
            payload = dict(row["payload"])
            payload.update(request.edits)
            payload["status"] = target_status.value
            try:
                model = CANDIDATE_MODELS[candidate_type].model_validate(payload)
            except ValueError as exc:
                raise OntologyError(f"Candidate edits are invalid: {exc}") from exc
            reviewed_at = datetime.now(UTC)
            connection.execute(
                text(
                    """
                    UPDATE ontology_candidate
                    SET status = :status,
                        payload = CAST(:payload AS jsonb),
                        reviewed_at = :reviewed_at,
                        reviewer = :reviewer,
                        review_note = :review_note
                    WHERE candidate_id = :candidate_id
                    """
                ),
                {
                    "status": target_status.value,
                    "payload": _json(model),
                    "reviewed_at": reviewed_at,
                    "reviewer": request.reviewer,
                    "review_note": request.note,
                    "candidate_id": candidate_id,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO candidate_review
                        (candidate_id, from_status, to_status, reviewer,
                         review_note, reviewed_at, reviewed_payload)
                    VALUES
                        (:candidate_id, :from_status, :to_status, :reviewer,
                         :review_note, :reviewed_at, CAST(:payload AS jsonb))
                    """
                ),
                {
                    "candidate_id": candidate_id,
                    "from_status": ReviewStatus.CANDIDATE.value,
                    "to_status": target_status.value,
                    "reviewer": request.reviewer,
                    "review_note": request.note,
                    "reviewed_at": reviewed_at,
                    "payload": _json(model),
                },
            )
        return self.get_candidate(candidate_id)

    def publish(
        self,
        seed_bundle: OntologyBundle,
        request: OntologyPublishRequest,
    ) -> OntologyVersion:
        verified = self.list_candidates(status=ReviewStatus.VERIFIED, limit=10_000)
        if not verified:
            raise OntologyError("At least one verified candidate is required to publish")
        bundle, attributes = self._merge_verified(seed_bundle, verified, request.version)
        version = OntologyVersion(
            version=request.version,
            description=request.description,
            source_snapshot_id=request.snapshot_id,
            concept_count=len(bundle.concepts) + len(bundle.metrics) + len(bundle.dimensions),
            mapping_count=len(bundle.mappings),
            join_count=len(bundle.joins),
            published_by=request.published_by,
        )
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    text("UPDATE ontology_version SET is_current = false WHERE is_current")
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO ontology_version
                            (version_id, version, description, source_snapshot_id, status,
                             concept_count, mapping_count, join_count, published_at,
                             published_by, is_current, bundle_json)
                        VALUES
                            (:id, :version, :description, :snapshot_id, :status,
                             :concept_count, :mapping_count, :join_count, :published_at,
                             :published_by, true, CAST(:bundle AS jsonb))
                        """
                    ),
                    {
                        "id": version.id,
                        "version": version.version,
                        "description": version.description,
                        "snapshot_id": version.source_snapshot_id,
                        "status": version.status,
                        "concept_count": version.concept_count,
                        "mapping_count": version.mapping_count,
                        "join_count": version.join_count,
                        "published_at": version.published_at,
                        "published_by": version.published_by,
                        "bundle": _json(bundle),
                    },
                )
                self._publish_rows(connection, version.id, bundle, attributes)
        except SQLAlchemyError as exc:
            raise OntologyError(f"Ontology publication failed: {exc}") from exc
        return version

    @staticmethod
    def _merge_verified(
        seed: OntologyBundle,
        verified: list[CandidateEnvelope],
        version_name: str,
    ) -> tuple[OntologyBundle, list[CandidateConcept]]:
        concepts_by_id = {concept.id: concept for concept in seed.concepts}
        mappings_by_id = {mapping.concept_id: mapping for mapping in seed.mappings}
        joins_by_key = {
            frozenset(
                {
                    (join.left_table, join.left_column),
                    (join.right_table, join.right_column),
                }
            ): join
            for join in seed.joins
        }
        verified_concepts: dict[str, CandidateConcept] = {}
        attributes: list[CandidateConcept] = []
        for item in verified:
            if item.candidate_type == "concept":
                concept = CandidateConcept.model_validate(item.payload)
                verified_concepts[concept.id] = concept
                attributes.append(concept)
                if concept.semantic_id not in concepts_by_id:
                    concepts_by_id[concept.semantic_id] = BusinessConcept(
                        id=concept.semantic_id,
                        name=concept.business_name,
                        kind="term",
                        description=concept.semantic_property,
                        synonyms=concept.synonyms,
                    )
            elif item.candidate_type == "join":
                join = CandidateJoin.model_validate(item.payload)
                definition = JoinDefinition(
                    left_table=join.left_table,
                    right_table=join.right_table,
                    left_column=join.left_column,
                    right_column=join.right_column,
                    relationship=join.relationship,
                )
                joins_by_key[
                    frozenset(
                        {
                            (join.left_table, join.left_column),
                            (join.right_table, join.right_column),
                        }
                    )
                ] = definition
        for item in verified:
            if item.candidate_type != "mapping":
                continue
            mapping = CandidateMapping.model_validate(item.payload)
            if mapping.candidate_concept_id not in verified_concepts:
                continue
            mappings_by_id[mapping.concept_id] = PhysicalMapping(
                concept_id=mapping.concept_id,
                table=mapping.table_name,
                columns=mapping.columns,
                condition=mapping.condition,
            )
        domain = dict(seed.domain)
        domain["version"] = version_name
        return (
            seed.model_copy(
                update={
                    "domain": domain,
                    "concepts": list(concepts_by_id.values()),
                    "mappings": list(mappings_by_id.values()),
                    "joins": list(joins_by_key.values()),
                }
            ),
            attributes,
        )

    @staticmethod
    def _publish_rows(
        connection: Any,
        version_id: str,
        bundle: OntologyBundle,
        attributes: list[CandidateConcept],
    ) -> None:
        groups: list[tuple[str, str, list[tuple[str, Any]]]] = [
            (
                "published_semantic_concept",
                "concept_id",
                [(item.id, item) for item in bundle.concepts],
            ),
            (
                "published_metric",
                "metric_id",
                [(item.id, item) for item in bundle.metrics],
            ),
            (
                "published_dimension",
                "dimension_id",
                [(item.id, item) for item in bundle.dimensions],
            ),
            (
                "published_semantic_attribute",
                "attribute_id",
                [(item.semantic_id, item) for item in attributes],
            ),
            (
                "published_physical_mapping",
                "concept_id",
                [(item.concept_id, item) for item in bundle.mappings],
            ),
            (
                "published_semantic_join",
                "join_key",
                [
                    (
                        f"{item.left_table}.{item.left_column}="
                        f"{item.right_table}.{item.right_column}",
                        item,
                    )
                    for item in bundle.joins
                ],
            ),
            (
                "published_table_asset",
                "table_name",
                [(item.name, item) for item in bundle.tables],
            ),
            (
                "published_semantic_rule",
                "rule_key",
                [(key, {"value": value}) for key, value in bundle.policies.items()],
            ),
        ]
        allowed_tables = {table_name for table_name, _, _ in groups}
        for table_name, key_column, rows in groups:
            if not rows:
                continue
            if table_name not in allowed_tables:
                raise OntologyError(f"Invalid publication table: {table_name}")
            statement = text(
                f"INSERT INTO {table_name} (version_id, {key_column}, payload) "
                f"VALUES (:version_id, :key, CAST(:payload AS jsonb))"
            )
            connection.execute(
                statement,
                [
                    {"version_id": version_id, "key": key, "payload": _json(payload)}
                    for key, payload in rows
                ],
            )

    def load_latest_published_bundle(self) -> OntologyBundle | None:
        try:
            with self.engine.connect() as connection:
                payload = connection.execute(
                    text(
                        """
                        SELECT bundle_json
                        FROM ontology_version
                        WHERE is_current AND status = 'PUBLISHED'
                        ORDER BY published_at DESC
                        LIMIT 1
                        """
                    )
                ).scalar_one_or_none()
        except SQLAlchemyError:
            return None
        return OntologyBundle.model_validate(payload) if payload is not None else None

    def list_versions(self) -> list[OntologyVersion]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT version_id, version, description, source_snapshot_id,
                           status, concept_count, mapping_count, join_count,
                           published_at, published_by, is_current
                    FROM ontology_version
                    ORDER BY published_at DESC
                    """
                )
            ).mappings()
            return [
                OntologyVersion(
                    id=row["version_id"],
                    version=row["version"],
                    description=row["description"],
                    source_snapshot_id=row["source_snapshot_id"],
                    status=row["status"],
                    concept_count=row["concept_count"],
                    mapping_count=row["mapping_count"],
                    join_count=row["join_count"],
                    published_at=row["published_at"],
                    published_by=row["published_by"],
                    is_current=row["is_current"],
                )
                for row in rows
            ]

    @staticmethod
    def _envelope(row: Any) -> CandidateEnvelope:
        return CandidateEnvelope(
            id=row["candidate_id"],
            candidate_type=row["candidate_type"],
            status=row["status"],
            payload=dict(row["payload"]),
            reviewed_at=row["reviewed_at"],
            reviewer=row["reviewer"],
            review_note=row["review_note"],
        )
