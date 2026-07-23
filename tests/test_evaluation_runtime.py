from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from data_asset_agents.core.config import Settings
from data_asset_agents.core.errors import DataAssetAgentsError, OntologyGovernanceError
from data_asset_agents.evaluation.runtime import (
    _activate_evaluation_ontology,
    _select_sql_asset_build,
)
from data_asset_agents.ontology.repository import YamlOntologyRepository
from data_asset_agents.ontology.service import OntologyService

ROOT = Path(__file__).parents[1]


class FakeRuntimeRepository:
    def __init__(self, version_id: str | None) -> None:
        self.version = (
            SimpleNamespace(id=version_id, status="PUBLISHED")
            if version_id is not None
            else None
        )

    def get_current_version(self):
        return self.version


class FakeManagerRepository:
    def __init__(self, artifact: object | None) -> None:
        self.artifact = artifact

    def get_compiled_artifact(self, _version_id: str):
        return self.artifact


class FakeManager:
    def __init__(
        self,
        bundle,
        artifact: object | None,
        *,
        legacy_fallback: bool = False,
        load_error: Exception | None = None,
    ) -> None:
        self.bundle = bundle
        self.repository = FakeManagerRepository(artifact)
        self.legacy_fallback = legacy_fallback
        self.load_error = load_error

    def load_runtime_bundle(self, _version_id: str, _legacy_bundle):
        if self.load_error is not None:
            raise self.load_error
        return self.bundle, self.legacy_fallback, self.repository.artifact


class FakeIndexService:
    def __init__(self, ready: bool) -> None:
        self.ready = ready

    def current(self, _version_id: str, _index_type):
        return SimpleNamespace(build_id="ontology-index-ready") if self.ready else None


class FakeSQLAssetRepository:
    def __init__(self, ready: object | None) -> None:
        self.ready = ready
        self.calls = 0

    def latest_ready(self, _version_id: str, _bundle_hash: str):
        self.calls += 1
        return self.ready


class FakeSQLAssetService:
    def __init__(self, initialized: object | None = None) -> None:
        self.initialized = initialized
        self.calls = 0

    def initialize_if_needed(self):
        self.calls += 1
        return self.initialized


def _ontology() -> OntologyService:
    return OntologyService(
        YamlOntologyRepository(ROOT / "ontology/retail_banking"),
        settings=Settings(llm_mode="mock"),
    )


def _artifact(
    *,
    bundle_hash: str = "b" * 64,
    compiler_version: str = "object-semantic-v1",
) -> SimpleNamespace:
    return SimpleNamespace(
        bundle_hash=bundle_hash,
        compiler_version=compiler_version,
        status="READY",
    )


def test_mock_without_published_version_uses_yaml_seed() -> None:
    ontology = _ontology()

    artifact = _activate_evaluation_ontology(
        Settings(llm_mode="mock"),
        ontology,
        FakeRuntimeRepository(None),  # type: ignore[arg-type]
        FakeManager(ontology.bundle, None),  # type: ignore[arg-type]
        FakeIndexService(False),  # type: ignore[arg-type]
    )

    assert artifact is None
    assert ontology.ontology_version_id.startswith("yaml-seed-")


def test_live_without_published_version_fails() -> None:
    ontology = _ontology()
    with pytest.raises(
        DataAssetAgentsError,
        match="Live evaluation requires a PUBLISHED ontology version",
    ):
        _activate_evaluation_ontology(
            Settings(llm_mode="live"),
            ontology,
            FakeRuntimeRepository(None),  # type: ignore[arg-type]
            FakeManager(ontology.bundle, None),  # type: ignore[arg-type]
            FakeIndexService(False),  # type: ignore[arg-type]
        )


def test_live_published_version_without_artifact_fails() -> None:
    ontology = _ontology()
    with pytest.raises(
        DataAssetAgentsError,
        match="Live evaluation requires a PUBLISHED ontology version",
    ):
        _activate_evaluation_ontology(
            Settings(llm_mode="live"),
            ontology,
            FakeRuntimeRepository("version-formal"),  # type: ignore[arg-type]
            FakeManager(ontology.bundle, None),  # type: ignore[arg-type]
            FakeIndexService(True),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("error_code", "message"),
    [
        ("ONTOLOGY_ARTIFACT_NOT_READY", "Compiled artifact is not READY"),
        ("ONTOLOGY_ARTIFACT_BUNDLE_HASH_MISMATCH", "Compiled artifact bundle hash mismatch"),
    ],
)
def test_live_invalid_artifact_is_rejected(error_code: str, message: str) -> None:
    ontology = _ontology()
    error = OntologyGovernanceError(message, error_code)
    with pytest.raises(OntologyGovernanceError, match=message):
        _activate_evaluation_ontology(
            Settings(llm_mode="live"),
            ontology,
            FakeRuntimeRepository("version-formal"),  # type: ignore[arg-type]
            FakeManager(
                ontology.bundle,
                _artifact(),
                load_error=error,
            ),  # type: ignore[arg-type]
            FakeIndexService(True),  # type: ignore[arg-type]
        )


def test_live_formal_version_requires_ready_index_and_loads_artifact() -> None:
    ontology = _ontology()
    settings = Settings(llm_mode="live")
    runtime_repository = FakeRuntimeRepository("version-formal")
    manager = FakeManager(ontology.bundle, _artifact())

    with pytest.raises(DataAssetAgentsError, match="READY ontology index"):
        _activate_evaluation_ontology(
            settings,
            ontology,
            runtime_repository,  # type: ignore[arg-type]
            manager,  # type: ignore[arg-type]
            FakeIndexService(False),  # type: ignore[arg-type]
        )

    loaded = _activate_evaluation_ontology(
        settings,
        ontology,
        runtime_repository,  # type: ignore[arg-type]
        manager,  # type: ignore[arg-type]
        FakeIndexService(True),  # type: ignore[arg-type]
    )

    assert loaded is manager.repository.artifact
    assert ontology.ontology_version_id == "version-formal"
    assert ontology.compiled_bundle_hash == "b" * 64
    assert ontology.compiler_version == "object-semantic-v1"


@pytest.mark.parametrize(
    "artifact",
    [
        _artifact(bundle_hash=""),
        _artifact(compiler_version=""),
    ],
)
def test_live_artifact_requires_bundle_hash_and_compiler_version(
    artifact: SimpleNamespace,
) -> None:
    ontology = _ontology()
    with pytest.raises(
        DataAssetAgentsError,
        match="Live evaluation requires a PUBLISHED ontology version",
    ):
        _activate_evaluation_ontology(
            Settings(llm_mode="live"),
            ontology,
            FakeRuntimeRepository("version-formal"),  # type: ignore[arg-type]
            FakeManager(ontology.bundle, artifact),  # type: ignore[arg-type]
            FakeIndexService(True),  # type: ignore[arg-type]
        )


def test_sql_assets_disabled_does_not_access_repository_or_service() -> None:
    ontology = _ontology()
    repository = FakeSQLAssetRepository(None)
    service = FakeSQLAssetService()

    selected = _select_sql_asset_build(
        Settings(llm_mode="live"),
        enabled=False,
        repository=repository,  # type: ignore[arg-type]
        service=service,  # type: ignore[arg-type]
        ontology=ontology,
    )

    assert selected is None
    assert repository.calls == 0
    assert service.calls == 0


def test_live_sql_assets_enabled_requires_ready_build() -> None:
    ontology = _ontology()
    with pytest.raises(DataAssetAgentsError, match="requires a READY SQLAssetBuild"):
        _select_sql_asset_build(
            Settings(llm_mode="live"),
            enabled=True,
            repository=FakeSQLAssetRepository(None),  # type: ignore[arg-type]
            service=FakeSQLAssetService(),  # type: ignore[arg-type]
            ontology=ontology,
        )


def test_live_sql_assets_enabled_uses_ready_build_without_initializing() -> None:
    ontology = _ontology()
    ready = SimpleNamespace(build_id="sqlbuild-ready")
    repository = FakeSQLAssetRepository(ready)
    service = FakeSQLAssetService()

    selected = _select_sql_asset_build(
        Settings(llm_mode="live"),
        enabled=True,
        repository=repository,  # type: ignore[arg-type]
        service=service,  # type: ignore[arg-type]
        ontology=ontology,
    )

    assert selected is ready
    assert repository.calls == 1
    assert service.calls == 0
