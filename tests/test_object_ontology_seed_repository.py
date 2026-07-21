from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from data_asset_agents.core.errors import OntologyError
from data_asset_agents.ontology.manager.compiler import ObjectSemanticCompiler
from data_asset_agents.ontology.manager.seed_repository import (
    ObjectOntologySeedRepository,
)
from data_asset_agents.ontology.repository import YamlOntologyRepository

SEED = Path("ontology/retail_banking/object_model")


def repository(path: Path = SEED) -> ObjectOntologySeedRepository:
    return ObjectOntologySeedRepository({"retail_banking": path})


def copied_seed(tmp_path: Path) -> Path:
    target = tmp_path / "object_model"
    shutil.copytree(SEED, target)
    return target


def mutate(path: Path, filename: str, callback) -> None:
    target = path / filename
    payload = yaml.safe_load(target.read_text(encoding="utf-8"))
    callback(payload)
    target.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")


def test_minibank_object_seed_loads_complete_and_stable() -> None:
    first = repository().load("retail_banking")
    second = repository().load("retail_banking")
    assert {item.id for item in first.object_types} == {
        "customer",
        "account",
        "card",
        "transaction",
        "branch",
        "merchant",
    }
    assert {item.id for item in first.properties} >= {
        "transaction.amount",
        "transaction.status",
        "branch.name",
    }
    assert {item.id for item in first.link_types} >= {
        "transaction_belongs_to_branch"
    }
    assert {item.id for item in first.physical_joins} >= {
        "transaction_branch_join"
    }
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_direct_seed_compiles_to_runtime_without_legacy_migration() -> None:
    resources = repository().load("retail_banking")
    fallback = YamlOntologyRepository("ontology/retail_banking").load()
    compilation = ObjectSemanticCompiler(fallback).compile(resources)
    assert not compilation.conflicts
    amount = next(
        item
        for item in compilation.bundle.metrics
        if item.id == "credit_card_transaction_amount"
    )
    branch = next(
        item for item in compilation.bundle.dimensions if item.id == "branch"
    )
    assert amount.expression == "SUM(dwd_card_transaction.txn_amount_cny)"
    assert branch.table == "dim_branch"
    assert branch.column == "branch_name"


def test_seed_rejects_duplicate_ids(tmp_path: Path) -> None:
    seed = copied_seed(tmp_path)
    mutate(seed, "objects.yaml", lambda rows: rows.append(dict(rows[0])))
    with pytest.raises(OntologyError, match="Duplicate Object"):
        repository(seed).load("retail_banking")


def test_seed_rejects_missing_object(tmp_path: Path) -> None:
    seed = copied_seed(tmp_path)
    mutate(
        seed,
        "properties.yaml",
        lambda rows: rows[0].update({"object_type_id": "ghost"}),
    )
    with pytest.raises(OntologyError, match="missing Object"):
        repository(seed).load("retail_banking")


def test_seed_rejects_missing_property(tmp_path: Path) -> None:
    seed = copied_seed(tmp_path)
    mutate(seed, "properties.yaml", lambda rows: rows.pop(0))
    with pytest.raises(OntologyError, match="property_ids do not match"):
        repository(seed).load("retail_banking")


def test_seed_rejects_unsafe_binding_identifier(tmp_path: Path) -> None:
    seed = copied_seed(tmp_path)
    mutate(
        seed,
        "bindings.yaml",
        lambda rows: rows[0].update({"table_name": "dim_customer;DROP"}),
    )
    with pytest.raises(OntologyError, match="Invalid object ontology seed"):
        repository(seed).load("retail_banking")


def test_seed_rejects_link_without_physical_join(tmp_path: Path) -> None:
    seed = copied_seed(tmp_path)
    mutate(
        seed,
        "links.yaml",
        lambda rows: rows[0].update({"physical_join_ids": ["missing_join"]}),
    )
    with pytest.raises(OntologyError, match="missing Physical Join"):
        repository(seed).load("retail_banking")


def test_seed_name_is_allowlisted_not_a_path() -> None:
    with pytest.raises(OntologyError, match="Unknown object ontology seed"):
        repository().load("../../untrusted")
