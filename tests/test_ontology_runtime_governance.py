from copy import deepcopy

import pytest
from sqlalchemy import create_engine, text

from data_asset_agents.core.config import Settings
from data_asset_agents.core.errors import OntologyConflictError, OntologyError
from data_asset_agents.llm.factory import ModelFactory
from data_asset_agents.ontology.manager.change_analysis import OntologyChangeAnalyzer
from data_asset_agents.ontology.manager.compiler import ObjectSemanticCompiler
from data_asset_agents.ontology.manager.drift import MetadataDriftService
from data_asset_agents.ontology.manager.governance_models import (
    BreakingLevel,
    DriftSeverity,
    ObjectFilter,
    ObjectFilterOperator,
    OntologyIndexBuildRequest,
    OntologyIndexStatus,
)
from data_asset_agents.ontology.manager.indexing import OntologyIndexService
from data_asset_agents.ontology.manager.migration import LegacyOntologyObjectMigrator
from data_asset_agents.ontology.manager.models import (
    ActorRequest,
    CreateDraftRequest,
    MigrateLegacyRequest,
    OntologyDraft,
    OntologyDraftAggregate,
    PublishDraftRequest,
    SemanticRole,
)
from data_asset_agents.ontology.manager.object_query import ObjectQueryService
from data_asset_agents.ontology.manager.repository import (
    MemoryOntologyManagerRepository,
)
from data_asset_agents.ontology.manager.service import OntologyManagerService
from data_asset_agents.ontology.manager.validator import OntologyDraftValidator
from data_asset_agents.ontology.repository import YamlOntologyRepository


@pytest.fixture
def bundle():
    return YamlOntologyRepository("ontology/retail_banking").load()


def test_draft_metric_is_authoritative_over_legacy_physical_fields(bundle) -> None:
    resources = LegacyOntologyObjectMigrator(bundle).migrate("snapshot-a")
    transaction = next(
        item for item in resources.bindings if item.object_type_id == "transaction"
    )
    transaction.property_bindings["transaction.amount"] = "txn_amount"

    compilation = ObjectSemanticCompiler(bundle).compile(
        resources, strict_compatibility=True
    )
    metric = next(
        item for item in compilation.bundle.metrics if item.id == "transaction_amount"
    )
    assert metric.expression == "SUM(dwd_card_transaction.txn_amount)"
    assert not compilation.conflicts


def test_object_compiler_enforces_measure_role_and_bound_properties(bundle) -> None:
    resources = LegacyOntologyObjectMigrator(bundle).migrate("snapshot-a")
    amount = next(
        item for item in resources.properties if item.id == "transaction.amount"
    )
    amount.semantic_role = SemanticRole.ATTRIBUTE
    with pytest.raises(OntologyError, match="requires a MEASURE property"):
        ObjectSemanticCompiler(bundle).compile(resources)

    resources = LegacyOntologyObjectMigrator(bundle).migrate("snapshot-a")
    transaction = next(
        item for item in resources.bindings if item.object_type_id == "transaction"
    )
    transaction.property_bindings.pop("transaction.event_time")
    with pytest.raises(OntologyError, match="unbound property"):
        ObjectSemanticCompiler(bundle).compile(resources)


def test_diff_classifies_physical_contract_changes_as_breaking(bundle) -> None:
    repository = MemoryOntologyManagerRepository()
    base = LegacyOntologyObjectMigrator(bundle).migrate("snapshot-a")
    repository.current = ("version-a", deepcopy(base))
    repository.versions["version-a"] = deepcopy(base)
    changed = deepcopy(base)
    changed.physical_joins[0].description = "Reviewed description changed"
    aggregate = OntologyDraftAggregate(
        draft=OntologyDraft(
            id="draft-change",
            name="change",
            base_version_id="version-a",
            created_by="test",
        ),
        resources=changed,
    )
    analyzer = OntologyChangeAnalyzer(repository, bundle)
    changes = analyzer.diff(aggregate)
    impact = analyzer.impact(aggregate, changes)
    assert changes.changed_physical_joins[0].breaking_level == BreakingLevel.BREAKING
    assert not impact.automatic_publish_allowed
    assert impact.rebuild_sql_assets


def test_property_description_change_is_non_breaking(bundle) -> None:
    repository = MemoryOntologyManagerRepository()
    base = LegacyOntologyObjectMigrator(bundle).migrate("snapshot-a")
    repository.current = ("version-a", deepcopy(base))
    repository.versions["version-a"] = deepcopy(base)
    changed = deepcopy(base)
    changed.properties[0].description = "Clarified fictional business description"
    aggregate = OntologyDraftAggregate(
        draft=OntologyDraft(
            id="draft-description",
            name="description",
            base_version_id="version-a",
            created_by="test",
        ),
        resources=changed,
    )
    analyzer = OntologyChangeAnalyzer(repository, bundle)
    changes = analyzer.diff(aggregate)
    impact = analyzer.impact(aggregate, changes)
    assert changes.modified_properties[0].breaking_level == BreakingLevel.NON_BREAKING
    assert impact.automatic_publish_allowed


def test_metric_and_dimension_changes_are_versioned_and_impact_runtime(bundle) -> None:
    repository = MemoryOntologyManagerRepository()
    base = LegacyOntologyObjectMigrator(bundle).migrate("snapshot-a")
    repository.current = ("version-a", deepcopy(base))
    repository.versions["version-a"] = deepcopy(base)
    changed = deepcopy(base)
    changed.metrics[0].aggregation = "AVG"
    changed.dimensions[0].description = "Clarified analytical grouping boundary"
    aggregate = OntologyDraftAggregate(
        draft=OntologyDraft(
            id="draft-analysis-change",
            name="analysis change",
            base_version_id="version-a",
            created_by="test",
        ),
        resources=changed,
    )

    analyzer = OntologyChangeAnalyzer(repository, bundle)
    changes = analyzer.diff(aggregate)
    impact = analyzer.impact(aggregate, changes)
    assert changes.modified_metrics[0].breaking_level == BreakingLevel.BREAKING
    assert changes.modified_dimensions[0].breaking_level == BreakingLevel.NON_BREAKING
    assert changed.metrics[0].id in impact.affected_metrics
    assert changed.dimensions[0].id in impact.affected_dimensions
    assert impact.rebuild_sql_assets
    assert impact.rerun_gold_hashes
    assert not impact.automatic_publish_allowed


def test_breaking_publish_requires_acknowledgement_and_ticket(bundle) -> None:
    repository = MemoryOntologyManagerRepository()
    service = OntologyManagerService(
        repository, bundle, OntologyDraftValidator(bundle)
    )
    first = service.migrate_legacy(MigrateLegacyRequest())
    service.validate(first.draft.id)
    service.submit(first.draft.id, ActorRequest())
    service.approve(first.draft.id, ActorRequest())
    version = service.publish(
        first.draft.id, PublishDraftRequest(version="object-base")
    )
    draft = service.create_draft(
        CreateDraftRequest(name="breaking", base_version_id=version.id)
    )
    physical_join = draft.resources.physical_joins[0].model_copy(
        update={"description": "Reviewed description changed"}
    )
    service.save_resource(draft.draft.id, physical_join)
    service.validate(draft.draft.id)
    service.submit(draft.draft.id, ActorRequest())
    service.approve(draft.draft.id, ActorRequest())
    with pytest.raises(OntologyConflictError, match="acknowledge"):
        service.publish(
            draft.draft.id, PublishDraftRequest(version="object-breaking")
        )
    with pytest.raises(OntologyConflictError, match="change_ticket"):
        service.publish(
            draft.draft.id,
            PublishDraftRequest(
                version="object-breaking", acknowledge_breaking_changes=True
            ),
        )


def test_versioned_mock_index_build_is_stable_and_atomically_current(bundle) -> None:
    repository = MemoryOntologyManagerRepository()
    settings = Settings(llm_mode="mock")
    service = OntologyIndexService(
        None,
        repository,
        settings,
        ModelFactory(settings),
        lambda: bundle,
        lambda: "version-a",
    )
    first = service.build(OntologyIndexBuildRequest())
    second = service.build(OntologyIndexBuildRequest())
    assert first.status == OntologyIndexStatus.READY
    assert second.status == OntologyIndexStatus.READY
    assert first.source_hash == second.source_hash
    assert not first.is_current
    assert second.is_current
    assert second.document_count > 0
    bundle.domain["description"] = "changed compiled bundle identity"
    assert service.current("version-a", second.index_type) is None


def test_metadata_drift_classifies_additive_and_breaking(monkeypatch, bundle) -> None:
    repository = MemoryOntologyManagerRepository()
    resources = LegacyOntologyObjectMigrator(bundle).migrate("snapshot-a")
    binding = resources.bindings[0]
    binding.schema_columns = {
        column: "integer" for column in binding.property_bindings.values()
    }
    binding.primary_key_columns = [binding.primary_key_column]
    repository.current = ("version-a", resources)
    repository.versions["version-a"] = deepcopy(resources)

    class Inspector:
        columns = [
            {"name": column, "type": "INTEGER"}
            for column in [*binding.schema_columns, "new_optional_column"]
        ]

        def get_columns(self, table_name, schema):
            return self.columns

        def get_pk_constraint(self, table_name, schema):
            return {"constrained_columns": [binding.primary_key_column]}

    inspector = Inspector()
    monkeypatch.setattr(
        "data_asset_agents.ontology.manager.drift.inspect", lambda engine: inspector
    )
    service = MetadataDriftService(
        repository, bundle, object(), lambda: "version-a"  # type: ignore[arg-type]
    )
    monkeypatch.setattr(service, "_persist_report", lambda report, run_id: None)
    additive = service.inspect_binding(binding.id)
    assert additive.severity == DriftSeverity.ADDITIVE
    assert additive.added_columns == ["new_optional_column"]

    removed_column = next(iter(binding.property_bindings.values()))
    inspector.columns = [
        item for item in inspector.columns if item["name"] != removed_column
    ]
    breaking = service.inspect_binding(binding.id)
    assert breaking.severity == DriftSeverity.BREAKING
    assert breaking.affected_properties


def test_object_explorer_masks_sensitive_values_and_rejects_unknown_filters(bundle) -> None:
    repository = MemoryOntologyManagerRepository()
    resources = LegacyOntologyObjectMigrator(bundle).migrate("snapshot-a")
    transaction = next(
        item for item in resources.bindings if item.object_type_id == "transaction"
    )
    transaction.schema_name = "main"
    repository.current = ("version-a", resources)
    repository.versions["version-a"] = deepcopy(resources)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    columns = ", ".join(
        f"{column} TEXT" for column in dict.fromkeys(transaction.property_bindings.values())
    )
    with engine.begin() as connection:
        connection.execute(text(f"CREATE TABLE dwd_card_transaction ({columns})"))
        values = {
            column: f"value-{index}"
            for index, column in enumerate(transaction.property_bindings.values())
        }
        placeholders = ", ".join(f":{column}" for column in values)
        connection.execute(
            text(
                "INSERT INTO dwd_card_transaction "
                f"({', '.join(values)}) VALUES ({placeholders})"
            ),
            values,
        )
    service = ObjectQueryService(engine, repository)
    rows = service.list_objects("transaction", limit=1)
    assert len(rows) == 1
    assert "***MASKED***" in rows[0].properties.values()
    with pytest.raises(OntologyError, match="not filterable"):
        service.list_objects(
            "transaction",
            [
                ObjectFilter(
                    property_id="transaction.unknown",
                    operator=ObjectFilterOperator.EQ,
                    value="x",
                )
            ],
        )
