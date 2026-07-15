from __future__ import annotations

import re
from collections.abc import Mapping

import networkx as nx
import sqlglot
from sqlalchemy import Engine, inspect

from data_asset_agents.ontology.models import (
    CandidateConcept,
    CandidateEnvelope,
    CandidateMapping,
    ContractCheck,
    OntologyBundle,
    OntologyContractReport,
    ReviewStatus,
)


class OntologyContractValidator:
    """Validate a proposed ontology against live schema and review provenance."""

    def __init__(self, engine: Engine | None = None, schema_name: str = "public") -> None:
        self.engine = engine
        self.schema_name = schema_name

    def _schema_columns(self, bundle: OntologyBundle) -> dict[str, set[str]]:
        if self.engine is None:
            return {table.name: set(table.columns) for table in bundle.tables}
        inspector = inspect(self.engine)
        discovered = set(inspector.get_table_names(schema=self.schema_name))
        return {
            table_name: {
                str(column["name"])
                for column in inspector.get_columns(table_name, schema=self.schema_name)
            }
            for table_name in discovered
        }

    @staticmethod
    def _error(
        checks: list[ContractCheck],
        code: str,
        message: str,
        *,
        candidate_id: str | None = None,
        table: str | None = None,
        column: str | None = None,
    ) -> None:
        checks.append(
            ContractCheck(
                code=code,
                passed=False,
                message=message,
                candidate_id=candidate_id,
                table=table,
                column=column,
            )
        )

    def validate(
        self,
        bundle: OntologyBundle,
        *,
        snapshot_id: str,
        source_candidates: list[CandidateEnvelope],
        schema_columns: Mapping[str, set[str]] | None = None,
    ) -> OntologyContractReport:
        checks: list[ContractCheck] = []
        physical_schema = dict(schema_columns or self._schema_columns(bundle))
        assets = {table.name: table for table in bundle.tables}

        candidate_snapshots = {
            str(item.payload.get("snapshot_id", "")) for item in source_candidates
        }
        if candidate_snapshots - {snapshot_id}:
            self._error(
                checks,
                "MIXED_METADATA_SNAPSHOT",
                "Publication candidates must come from exactly one MetadataSnapshot",
            )
        for candidate in source_candidates:
            if candidate.status != ReviewStatus.VERIFIED:
                self._error(
                    checks,
                    "UNVERIFIED_CANDIDATE",
                    f"Candidate {candidate.id} is {candidate.status} and cannot be published",
                    candidate_id=candidate.id,
                )

        verified_concepts = {
            CandidateConcept.model_validate(item.payload).id
            for item in source_candidates
            if item.candidate_type == "concept" and item.status == ReviewStatus.VERIFIED
        }
        for candidate in source_candidates:
            if candidate.candidate_type != "mapping":
                continue
            mapping_candidate = CandidateMapping.model_validate(candidate.payload)
            if mapping_candidate.candidate_concept_id not in verified_concepts:
                self._error(
                    checks,
                    "MAPPING_CONCEPT_NOT_VERIFIED",
                    f"Mapping {candidate.id} does not reference a VERIFIED concept candidate",
                    candidate_id=candidate.id,
                )

        for mapping in bundle.mappings:
            asset = assets.get(mapping.table)
            if mapping.table not in physical_schema:
                self._error(
                    checks,
                    "MAPPING_TABLE_NOT_FOUND",
                    f"Mapping {mapping.concept_id} references missing table {mapping.table}",
                    table=mapping.table,
                )
                continue
            if asset is None or asset.status != "ACTIVE" or not asset.selectable:
                self._error(
                    checks,
                    "MAPPING_TABLE_NOT_SELECTABLE",
                    f"Mapping {mapping.concept_id} references non-selectable {mapping.table}",
                    table=mapping.table,
                )
            for column in mapping.column_bindings.values():
                if column not in physical_schema[mapping.table]:
                    self._error(
                        checks,
                        "MAPPING_COLUMN_NOT_FOUND",
                        f"Mapping {mapping.concept_id} references missing column "
                        f"{mapping.table}.{column}",
                        table=mapping.table,
                        column=column,
                    )

        mappings = {mapping.concept_id: mapping for mapping in bundle.mappings}
        for metric in bundle.metrics:
            mapping = mappings.get(f"metric:{metric.id}")
            if mapping is None:
                self._error(
                    checks,
                    "METRIC_MAPPING_MISSING",
                    f"Metric {metric.id} has no PhysicalMapping",
                )
                continue
            roles = set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", metric.expression))
            required_roles = roles | set(metric.required_filters)
            missing_roles = required_roles - set(mapping.column_bindings)
            for role in sorted(missing_roles):
                self._error(
                    checks,
                    "METRIC_BINDING_INCOMPLETE",
                    f"Metric {metric.id} is missing required binding {role}",
                )
            if metric.time_dimension:
                time_mapping = mappings.get(f"dimension:{metric.time_dimension}")
                if time_mapping is None or time_mapping.column_for("value") is None:
                    self._error(
                        checks,
                        "METRIC_TIME_BINDING_MISSING",
                        f"Metric {metric.id} has no complete time-dimension binding",
                    )
            if not missing_roles:
                expression = metric.expression
                for role in roles:
                    expression = expression.replace(
                        f"{{{role}}}",
                        f"{mapping.table}.{mapping.column_bindings[role]}",
                    )
                try:
                    sqlglot.parse_one(f"SELECT {expression}", read="postgres")
                except sqlglot.errors.ParseError as exc:
                    self._error(
                        checks,
                        "METRIC_EXPRESSION_INVALID",
                        f"Metric {metric.id} expression is invalid: {exc}",
                    )

        for dimension in bundle.dimensions:
            mapping = mappings.get(f"dimension:{dimension.id}")
            if mapping is None or set(mapping.column_bindings) != {"value"}:
                self._error(
                    checks,
                    "DIMENSION_BINDING_AMBIGUOUS",
                    f"Dimension {dimension.id} must bind exactly one value column",
                )

        join_graph = nx.Graph()
        for join in bundle.joins:
            for table, column in (
                (join.left_table, join.left_column),
                (join.right_table, join.right_column),
            ):
                if table not in physical_schema:
                    self._error(
                        checks,
                        "JOIN_TABLE_NOT_FOUND",
                        f"Join references missing table {table}",
                        table=table,
                    )
                elif column not in physical_schema[table]:
                    self._error(
                        checks,
                        "JOIN_COLUMN_NOT_FOUND",
                        f"Join references missing column {table}.{column}",
                        table=table,
                        column=column,
                    )
            if join.enabled:
                join_graph.add_edge(join.left_table, join.right_table)

        mapped_tables = sorted({mapping.table for mapping in bundle.mappings})
        if len(mapped_tables) > 1:
            disconnected = [
                table
                for table in mapped_tables[1:]
                if mapped_tables[0] not in join_graph
                or table not in join_graph
                or not nx.has_path(join_graph, mapped_tables[0], table)
            ]
            if disconnected:
                self._error(
                    checks,
                    "JOIN_GRAPH_DISCONNECTED",
                    "Published mapping tables are disconnected: "
                    + ", ".join(disconnected),
                )

        if not checks:
            checks.append(
                ContractCheck(
                    code="ONTOLOGY_CONTRACT_VALID",
                    passed=True,
                    message="All publication contract checks passed",
                )
            )
        return OntologyContractReport(
            valid=all(check.passed for check in checks),
            checks=checks,
        )
