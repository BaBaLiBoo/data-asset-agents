from pathlib import Path

from data_asset_agents.ontology.models import (
    CandidateConcept,
    CandidateEnvelope,
    CandidateMapping,
    PhysicalMapping,
    ReviewStatus,
)
from data_asset_agents.ontology.repository import YamlOntologyRepository
from data_asset_agents.ontology.validation import OntologyContractValidator

ROOT = Path(__file__).parents[1]


def _bundle():
    return YamlOntologyRepository(ROOT / "ontology/retail_banking").load()


def _schema(bundle):
    return {table.name: set(table.columns) for table in bundle.tables}


def _codes(report):
    return {check.code for check in report.checks if not check.passed}


def test_valid_seed_contract_passes() -> None:
    bundle = _bundle()
    report = OntologyContractValidator().validate(
        bundle,
        snapshot_id="snapshot-one",
        source_candidates=[],
        schema_columns=_schema(bundle),
    )
    assert report.valid


def test_incomplete_metric_mapping_is_rejected() -> None:
    bundle = _bundle()
    mappings = [
        item.model_copy(
            update={"column_bindings": {"amount": "txn_amount_cny"}}
        )
        if item.concept_id == "metric:credit_card_transaction_amount"
        else item
        for item in bundle.mappings
    ]
    report = OntologyContractValidator().validate(
        bundle.model_copy(update={"mappings": mappings}),
        snapshot_id="snapshot-one",
        source_candidates=[],
        schema_columns=_schema(bundle),
    )
    assert "METRIC_BINDING_INCOMPLETE" in _codes(report)


def test_deprecated_mapping_table_is_rejected() -> None:
    bundle = _bundle()
    mappings = [
        item.model_copy(update={"table": "legacy_card_transaction"})
        if item.concept_id == "transaction"
        else item
        for item in bundle.mappings
    ]
    report = OntologyContractValidator().validate(
        bundle.model_copy(update={"mappings": mappings}),
        snapshot_id="snapshot-one",
        source_candidates=[],
        schema_columns=_schema(bundle),
    )
    assert "MAPPING_TABLE_NOT_SELECTABLE" in _codes(report)


def test_missing_mapping_column_is_rejected() -> None:
    bundle = _bundle()
    mappings = [
        PhysicalMapping(
            concept_id=item.concept_id,
            table=item.table,
            column_bindings={"value": "missing_column"},
        )
        if item.concept_id == "dimension:branch"
        else item
        for item in bundle.mappings
    ]
    report = OntologyContractValidator().validate(
        bundle.model_copy(update={"mappings": mappings}),
        snapshot_id="snapshot-one",
        source_candidates=[],
        schema_columns=_schema(bundle),
    )
    assert "MAPPING_COLUMN_NOT_FOUND" in _codes(report)


def test_unreviewed_and_mixed_snapshot_candidates_are_rejected() -> None:
    concept = CandidateConcept(
        id="candidate-concept",
        snapshot_id="snapshot-two",
        table_name="dim_branch",
        column_name="branch_name",
        semantic_id="dimension:branch",
        business_name="分行",
        business_object="分行",
        semantic_property="分行名称",
        role="dimension",
        confidence=0.9,
    )
    envelope = CandidateEnvelope(
        id=concept.id,
        candidate_type="concept",
        status=ReviewStatus.CANDIDATE,
        payload=concept.model_dump(mode="json"),
    )
    bundle = _bundle()
    report = OntologyContractValidator().validate(
        bundle,
        snapshot_id="snapshot-one",
        source_candidates=[envelope],
        schema_columns=_schema(bundle),
    )
    assert {"UNVERIFIED_CANDIDATE", "MIXED_METADATA_SNAPSHOT"} <= _codes(report)


def test_mapping_requires_verified_concept_candidate() -> None:
    mapping = CandidateMapping(
        id="candidate-mapping",
        snapshot_id="snapshot-one",
        candidate_concept_id="missing-concept",
        concept_id="dimension:branch",
        table_name="dim_branch",
        columns=["branch_name"],
        column_bindings={"value": "branch_name"},
        confidence=0.9,
        status=ReviewStatus.VERIFIED,
    )
    envelope = CandidateEnvelope(
        id=mapping.id,
        candidate_type="mapping",
        status=ReviewStatus.VERIFIED,
        payload=mapping.model_dump(mode="json"),
    )
    bundle = _bundle()
    report = OntologyContractValidator().validate(
        bundle,
        snapshot_id="snapshot-one",
        source_candidates=[envelope],
        schema_columns=_schema(bundle),
    )
    assert "MAPPING_CONCEPT_NOT_VERIFIED" in _codes(report)
