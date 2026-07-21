"""Publication-grade validation for an atomic object ontology draft."""

from __future__ import annotations

from collections import Counter, defaultdict

import sqlglot
from sqlalchemy import Engine, inspect

from data_asset_agents.core.errors import OntologyError
from data_asset_agents.execution.executor import QueryExecutor
from data_asset_agents.ontology.models import MetricAggregation, OntologyBundle
from data_asset_agents.ontology.validation import OntologyContractValidator

from .compiler import ObjectSemanticCompiler
from .dry_run import DraftSemanticDryRun
from .models import (
    DraftResources,
    DraftValidationReport,
    LifecycleStatus,
    PropertyDataType,
    SemanticRole,
    ValidationIssue,
)

BENCHMARK_SQL = """SELECT b.branch_name,
       SUM(t.txn_amount_cny) AS credit_card_transaction_amount,
       COUNT(DISTINCT t.transaction_id) AS credit_card_transaction_count
FROM dwd_card_transaction AS t
JOIN dim_branch AS b ON t.branch_id = b.branch_id
WHERE t.card_type = 'CREDIT'
  AND t.transaction_status = 'POSTED'
  AND t.transaction_date >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY b.branch_name"""


class OntologyDraftValidator:
    """Validate business, physical, projection, SQL and EXPLAIN contracts."""

    def __init__(
        self,
        bundle: OntologyBundle,
        *,
        engine: Engine | None = None,
        executor: QueryExecutor | None = None,
    ) -> None:
        self.bundle = bundle
        self.engine = engine
        self.executor = executor

    @staticmethod
    def _issue(
        issues: list[ValidationIssue],
        code: str,
        resource_type: str,
        resource_id: str,
        message: str,
        fix: str,
        field: str | None = None,
    ) -> None:
        issues.append(
            ValidationIssue(
                code=code,
                resource_type=resource_type,
                resource_id=resource_id,
                field=field,
                message=message,
                suggested_fix=fix,
            )
        )

    def _schema(self) -> tuple[dict[str, dict[str, str]], dict[str, set[str]], dict[str, set[str]]]:
        if self.engine is None:
            columns = {
                table.name: {column: "unknown" for column in table.columns}
                for table in self.bundle.tables
            }
            return columns, {}, {}
        inspector = inspect(self.engine)
        result: dict[str, dict[str, str]] = {}
        primary_keys: dict[str, set[str]] = {}
        unique_keys: dict[str, set[str]] = {}
        for table in inspector.get_table_names(schema="public"):
            result[table] = {
                str(column["name"]): str(column["type"]).lower()
                for column in inspector.get_columns(table, schema="public")
            }
            primary_keys[table] = set(
                inspector.get_pk_constraint(table, schema="public").get("constrained_columns") or []
            )
            unique_keys[table] = {
                column
                for index in inspector.get_indexes(table, schema="public")
                if index.get("unique")
                for column in (index.get("column_names") or [])
            }
        return result, primary_keys, unique_keys

    @staticmethod
    def _type_compatible(data_type: PropertyDataType, physical: str) -> bool:
        if physical == "unknown":
            return True
        families = {
            PropertyDataType.STRING: ("char", "text", "uuid"),
            PropertyDataType.INTEGER: ("int", "serial"),
            PropertyDataType.DECIMAL: ("numeric", "decimal", "real", "double", "money", "int"),
            PropertyDataType.BOOLEAN: ("bool",),
            PropertyDataType.DATE: ("date",),
            PropertyDataType.DATETIME: ("timestamp", "date"),
        }
        return any(token in physical for token in families[data_type])

    def validate(
        self, resources: DraftResources, source_snapshot_id: str | None
    ) -> DraftValidationReport:
        issues: list[ValidationIssue] = []
        objects = {item.id: item for item in resources.object_types}
        properties = {item.id: item for item in resources.properties}
        dimensions = {item.id: item for item in resources.dimensions}
        bindings = {item.id: item for item in resources.bindings}
        joins = {item.id: item for item in resources.physical_joins}
        assets = {item.name: item for item in self.bundle.tables}
        schema, primary_keys, unique_keys = self._schema()

        if not resources.object_types:
            self._issue(
                issues,
                "OBJECT_TYPE_REQUIRED",
                "draft",
                "object-model",
                "Draft has no ObjectType resources",
                "Create at least one governed ObjectType before review",
            )

        for kind, values in (
            ("object_type", [x.id for x in resources.object_types]),
            ("property", [x.id for x in resources.properties]),
            ("link_type", [x.id for x in resources.link_types]),
            ("physical_join", [x.id for x in resources.physical_joins]),
            ("metric", [x.id for x in resources.metrics]),
            ("dimension", [x.id for x in resources.dimensions]),
        ):
            for identifier, count in Counter(values).items():
                if count > 1:
                    self._issue(
                        issues,
                        "DUPLICATE_ID",
                        kind,
                        identifier,
                        "ID is duplicated",
                        "Use a unique stable ID",
                        "id",
                    )

        for obj in resources.object_types:
            owned = {prop.id for prop in resources.properties if prop.object_type_id == obj.id}
            if obj.lifecycle_status == LifecycleStatus.ACTIVE and not obj.primary_key_property_id:
                self._issue(
                    issues,
                    "OBJECT_PRIMARY_KEY_REQUIRED",
                    "object_type",
                    obj.id,
                    "ACTIVE object has no primary key property",
                    "Select a non-null IDENTIFIER property",
                    "primary_key_property_id",
                )
            if obj.primary_key_property_id:
                prop = properties.get(obj.primary_key_property_id)
                if prop is None or prop.object_type_id != obj.id:
                    self._issue(
                        issues,
                        "OBJECT_PRIMARY_KEY_INVALID",
                        "object_type",
                        obj.id,
                        "Primary key property does not belong to the object",
                        "Choose one of the object's properties",
                        "primary_key_property_id",
                    )
                elif prop.semantic_role != SemanticRole.IDENTIFIER or prop.nullable:
                    self._issue(
                        issues,
                        "OBJECT_PRIMARY_KEY_CONTRACT",
                        "property",
                        prop.id,
                        "Primary key must be a non-null IDENTIFIER",
                        "Set role=IDENTIFIER and nullable=false",
                        "semantic_role",
                    )
            if obj.title_property_id and obj.title_property_id not in owned:
                self._issue(
                    issues,
                    "OBJECT_TITLE_INVALID",
                    "object_type",
                    obj.id,
                    "Title property does not belong to the object",
                    "Choose an owned property",
                    "title_property_id",
                )
            unknown = set(obj.property_ids) - owned
            if unknown:
                self._issue(
                    issues,
                    "OBJECT_PROPERTY_LIST_INVALID",
                    "object_type",
                    obj.id,
                    f"Unknown property IDs: {sorted(unknown)}",
                    "Remove unknown IDs or create the properties",
                    "property_ids",
                )

        for prop in resources.properties:
            if prop.object_type_id not in objects:
                self._issue(
                    issues,
                    "PROPERTY_OBJECT_NOT_FOUND",
                    "property",
                    prop.id,
                    "Property references a missing object",
                    "Select an existing object",
                    "object_type_id",
                )

        if resources.object_types and not resources.metrics and not resources.dimensions:
            self._issue(
                issues,
                "ANALYTICAL_SEMANTICS_REQUIRED",
                "draft",
                "analytical-semantics",
                "Object Draft has no governed Metric or Dimension resources",
                "Create analysis semantics in this Draft or import them through legacy migration",
            )

        for dimension in resources.dimensions:
            prop = properties.get(dimension.property_id)
            if prop is None:
                self._issue(
                    issues,
                    "DIMENSION_PROPERTY_NOT_FOUND",
                    "dimension",
                    dimension.id,
                    f"Dimension references missing Property {dimension.property_id}",
                    "Select an existing governed Property",
                    "property_id",
                )
                continue
            if prop.lifecycle_status != LifecycleStatus.ACTIVE or not prop.groupable:
                self._issue(
                    issues,
                    "DIMENSION_PROPERTY_NOT_GROUPABLE",
                    "dimension",
                    dimension.id,
                    "Dimension Property must be ACTIVE and groupable",
                    "Activate the Property and set groupable=true",
                    "property_id",
                )
            if prop.semantic_role not in {
                SemanticRole.DIMENSION,
                SemanticRole.STATUS,
                SemanticRole.TIME,
            }:
                self._issue(
                    issues,
                    "DIMENSION_PROPERTY_ROLE_INVALID",
                    "dimension",
                    dimension.id,
                    f"Property role {prop.semantic_role} cannot define a Dimension",
                    "Use a DIMENSION, STATUS, or TIME Property",
                    "property_id",
                )

        for metric in resources.metrics:
            measure = properties.get(metric.measure_property_id)
            if measure is None or measure.lifecycle_status != LifecycleStatus.ACTIVE:
                self._issue(
                    issues,
                    "METRIC_MEASURE_PROPERTY_INVALID",
                    "metric",
                    metric.id,
                    f"Metric measure Property {metric.measure_property_id} is missing or inactive",
                    "Select an ACTIVE governed Property",
                    "measure_property_id",
                )
            elif (
                metric.aggregation
                in {
                    MetricAggregation.SUM,
                    MetricAggregation.AVG,
                    MetricAggregation.MIN,
                    MetricAggregation.MAX,
                }
                and measure.semantic_role != SemanticRole.MEASURE
            ):
                self._issue(
                    issues,
                    "METRIC_AGGREGATION_ROLE_INVALID",
                    "metric",
                    metric.id,
                    f"{metric.aggregation} requires a MEASURE Property",
                    "Choose a MEASURE Property or a count aggregation",
                    "aggregation",
                )
            for predicate in metric.filter_predicates:
                prop = properties.get(predicate.property_id)
                if prop is None or prop.lifecycle_status != LifecycleStatus.ACTIVE:
                    self._issue(
                        issues,
                        "METRIC_FILTER_PROPERTY_INVALID",
                        "metric",
                        metric.id,
                        f"Filter Property {predicate.property_id} is missing or inactive",
                        "Select an ACTIVE filterable Property",
                        "filter_predicates",
                    )
                elif not prop.filterable:
                    self._issue(
                        issues,
                        "METRIC_FILTER_PROPERTY_NOT_FILTERABLE",
                        "metric",
                        metric.id,
                        f"Property {predicate.property_id} is not filterable",
                        "Set filterable=true or remove the predicate",
                        "filter_predicates",
                    )
                if predicate.operator != "EQ" or isinstance(predicate.value, list):
                    self._issue(
                        issues,
                        "METRIC_FILTER_RUNTIME_UNSUPPORTED",
                        "metric",
                        metric.id,
                        "Runtime compatibility projection currently supports EQ scalar filters",
                        "Use an EQ scalar predicate until the runtime contract is expanded",
                        "filter_predicates",
                    )
            if metric.time_property_id:
                time_property = properties.get(metric.time_property_id)
                if (
                    time_property is None
                    or time_property.lifecycle_status != LifecycleStatus.ACTIVE
                    or time_property.semantic_role != SemanticRole.TIME
                ):
                    self._issue(
                        issues,
                        "METRIC_TIME_PROPERTY_INVALID",
                        "metric",
                        metric.id,
                        f"Time Property {metric.time_property_id} is missing, inactive, "
                        "or not TIME",
                        "Select an ACTIVE TIME Property",
                        "time_property_id",
                    )
                elif not any(
                    dimension.property_id == metric.time_property_id
                    and dimension.id in metric.supported_dimension_ids
                    for dimension in resources.dimensions
                ):
                    self._issue(
                        issues,
                        "METRIC_TIME_DIMENSION_REQUIRED",
                        "metric",
                        metric.id,
                        "Time Property must have a supported Dimension in the same Draft",
                        "Create and select the Dimension that exposes the Time Property",
                        "supported_dimension_ids",
                    )
            missing_dimensions = set(metric.supported_dimension_ids) - dimensions.keys()
            if missing_dimensions:
                self._issue(
                    issues,
                    "METRIC_DIMENSION_NOT_FOUND",
                    "metric",
                    metric.id,
                    f"Missing supported Dimensions: {sorted(missing_dimensions)}",
                    "Select Dimensions from the same Draft",
                    "supported_dimension_ids",
                )

        active_binding_count: defaultdict[str, int] = defaultdict(int)
        snapshot_ids = {b.latest_snapshot_id for b in resources.bindings if b.latest_snapshot_id}
        if source_snapshot_id and snapshot_ids - {source_snapshot_id}:
            self._issue(
                issues,
                "MIXED_METADATA_SNAPSHOT",
                "draft",
                source_snapshot_id,
                "Bindings mix different metadata snapshots",
                "Re-inspect all bindings from one snapshot",
                "source_snapshot_id",
            )
        for binding in bindings.values():
            obj = objects.get(binding.object_type_id)
            if obj and obj.lifecycle_status == LifecycleStatus.ACTIVE:
                active_binding_count[obj.id] += 1
            asset = assets.get(binding.table_name)
            if binding.table_name not in schema:
                self._issue(
                    issues,
                    "BINDING_TABLE_NOT_FOUND",
                    "binding",
                    binding.id,
                    "Bound table does not exist",
                    "Choose an inspected table",
                    "table_name",
                )
                continue
            if asset is None or asset.status != "ACTIVE" or not asset.selectable:
                self._issue(
                    issues,
                    "BINDING_TABLE_NOT_SELECTABLE",
                    "binding",
                    binding.id,
                    "Bound table is not ACTIVE and selectable",
                    "Use a governed ACTIVE table",
                    "table_name",
                )
            if binding.primary_key_column not in schema[binding.table_name]:
                self._issue(
                    issues,
                    "BINDING_PRIMARY_KEY_NOT_FOUND",
                    "binding",
                    binding.id,
                    "Primary key column does not exist",
                    "Select an existing primary key",
                    "primary_key_column",
                )
            elif primary_keys and binding.primary_key_column not in primary_keys.get(
                binding.table_name, set()
            ) | unique_keys.get(binding.table_name, set()):
                self._issue(
                    issues,
                    "BINDING_PRIMARY_KEY_NOT_UNIQUE",
                    "binding",
                    binding.id,
                    "Primary key column is not a database primary/unique key",
                    "Use a reviewed primary or unique key",
                    "primary_key_column",
                )
            for property_id, column in binding.property_bindings.items():
                prop = properties.get(property_id)
                if prop is None or prop.object_type_id != binding.object_type_id:
                    self._issue(
                        issues,
                        "BINDING_PROPERTY_INVALID",
                        "binding",
                        binding.id,
                        f"Property {property_id} is missing or belongs to another object",
                        "Bind an owned property",
                        "property_bindings",
                    )
                    continue
                if column not in schema[binding.table_name]:
                    self._issue(
                        issues,
                        "BINDING_COLUMN_NOT_FOUND",
                        "binding",
                        binding.id,
                        f"Column {column} does not exist",
                        "Select an inspected column",
                        "property_bindings",
                    )
                elif not self._type_compatible(prop.data_type, schema[binding.table_name][column]):
                    self._issue(
                        issues,
                        "BINDING_TYPE_MISMATCH",
                        "property",
                        prop.id,
                        f"{prop.data_type} is incompatible with "
                        f"{schema[binding.table_name][column]}",
                        "Correct the property type or binding",
                        "data_type",
                    )
        for object_id, count in active_binding_count.items():
            if count > 1:
                self._issue(
                    issues,
                    "CONFLICTING_PRIMARY_BINDING",
                    "object_type",
                    object_id,
                    "ACTIVE object has multiple primary bindings",
                    "Keep one primary binding",
                    "bindings",
                )

        object_tables = {b.object_type_id: b.table_name for b in resources.bindings}
        for join in resources.physical_joins:
            if not join.evidence:
                self._issue(
                    issues,
                    "PHYSICAL_JOIN_EVIDENCE_REQUIRED",
                    "physical_join",
                    join.id,
                    "Physical Join has no review evidence",
                    "Record metadata, foreign-key, or reviewed SQL evidence",
                    "evidence",
                )
            if {
                join.left_data_source_id,
                join.right_data_source_id,
            } != {"minibank-postgres"}:
                self._issue(
                    issues,
                    "PHYSICAL_JOIN_DATA_SOURCE_INVALID",
                    "physical_join",
                    join.id,
                    "Only the managed PostgreSQL data source is supported",
                    "Use minibank-postgres on both endpoints",
                    "left_data_source_id",
                )
            for table, column in (
                (join.left_table, join.left_column),
                (join.right_table, join.right_column),
            ):
                if table not in schema or column not in schema[table]:
                    self._issue(
                        issues,
                        "PHYSICAL_JOIN_COLUMN_NOT_FOUND",
                        "physical_join",
                        join.id,
                        f"Join endpoint {table}.{column} does not exist",
                        "Use inspected join endpoints",
                    )
        for link in resources.link_types:
            if (
                link.source_object_type_id not in objects
                or link.target_object_type_id not in objects
            ):
                self._issue(
                    issues,
                    "LINK_OBJECT_NOT_FOUND",
                    "link_type",
                    link.id,
                    "Link references a missing object",
                    "Choose existing source and target objects",
                    "source_object_type_id",
                )
            for join_id in link.physical_join_ids:
                join = joins.get(join_id)
                if join is None or not join.enabled:
                    self._issue(
                        issues,
                        "LINK_PHYSICAL_JOIN_INVALID",
                        "link_type",
                        link.id,
                        f"Physical join {join_id} is missing or disabled",
                        "Choose an enabled reviewed join",
                        "physical_join_ids",
                    )
                    continue
                for table, column in (
                    (join.left_table, join.left_column),
                    (join.right_table, join.right_column),
                ):
                    if table not in schema or column not in schema[table]:
                        self._issue(
                            issues,
                            "PHYSICAL_JOIN_COLUMN_NOT_FOUND",
                            "physical_join",
                            join.id,
                            f"Join endpoint {table}.{column} does not exist",
                            "Use inspected join endpoints",
                            "physical_join_ids",
                        )
                expected = {
                    object_tables.get(link.source_object_type_id),
                    object_tables.get(link.target_object_type_id),
                }
                if {join.left_table, join.right_table} != expected:
                    self._issue(
                        issues,
                        "LINK_JOIN_TABLE_MISMATCH",
                        "link_type",
                        link.id,
                        "Physical join does not connect the two bound object tables",
                        "Select the matching physical join",
                        "physical_join_ids",
                    )

        try:
            compilation = ObjectSemanticCompiler(self.bundle).compile(resources)
            for conflict in compilation.conflicts:
                self._issue(
                    issues,
                    "OBJECT_LEGACY_SEMANTIC_CONFLICT",
                    "semantic_compilation",
                    "analytical-contract",
                    conflict,
                    "Update the object Property/Binding or align the compatibility fields",
                )
            projected = compilation.bundle
        except OntologyError as exc:
            self._issue(
                issues,
                "OBJECT_SEMANTIC_COMPILATION_FAILED",
                "semantic_compilation",
                "analytical-contract",
                str(exc),
                "Bind every referenced Property and align Metric/Dimension references",
            )
            projected = self.bundle
        contract = OntologyContractValidator(self.engine).validate(
            projected,
            snapshot_id=source_snapshot_id or "legacy-seed",
            source_candidates=[],
        )
        for check in contract.checks:
            if not check.passed:
                self._issue(
                    issues,
                    f"PROJECTION_{check.code}",
                    "projection",
                    "legacy-contract",
                    check.message,
                    "Correct the object projection or legacy bundle",
                )

        explain_passed: bool | None = None
        dry_run_cases: list[dict[str, object]] = []
        try:
            sqlglot.parse_one(BENCHMARK_SQL, read="postgres")
            if self.executor is not None:
                self.executor.explain(BENCHMARK_SQL, {"dwd_card_transaction", "dim_branch"})
                explain_passed = True
        except Exception as exc:
            explain_passed = False
            self._issue(
                issues,
                "DATABASE_HEALTH_PROBE_FAILED",
                "database",
                "read-only-explain",
                str(exc),
                "Repair projection, schema binding, or database health",
            )

        if self.executor is not None:
            reports = DraftSemanticDryRun(self.executor).run(resources, self.bundle)
            dry_run_cases = [item.model_dump(mode="json") for item in reports]
            for report in reports:
                if (
                    report.errors
                    or not report.sqlglot_valid
                    or not report.ontology_policy_valid
                    or not report.explain_passed
                ):
                    self._issue(
                        issues,
                        "DYNAMIC_SEMANTIC_DRY_RUN_FAILED",
                        "benchmark",
                        report.benchmark_case_id,
                        "; ".join(report.errors) or "A semantic Dry Run contract failed",
                        "Repair Property references, Binding, Link, or Physical Join",
                    )

        return DraftValidationReport(
            valid=not any(item.severity == "ERROR" for item in issues),
            issues=issues,
            benchmark_sql=BENCHMARK_SQL,
            explain_passed=explain_passed,
            dry_run_cases=dry_run_cases,
        )
