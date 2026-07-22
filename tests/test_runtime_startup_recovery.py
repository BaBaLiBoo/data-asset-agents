from types import SimpleNamespace

import pytest

from apps.api.main import _load_startup_runtime
from data_asset_agents.core.errors import OntologyGovernanceError
from data_asset_agents.ontology.manager.models import OntologyAuditAction
from data_asset_agents.ontology.repository import YamlOntologyRepository


@pytest.fixture
def bundle():
    return YamlOntologyRepository("ontology/retail_banking").load()


def test_startup_damage_records_failure_and_rolls_back_to_healthy_version(bundle) -> None:
    damaged = SimpleNamespace(id="damaged-version", version="damaged-v1")
    healthy = SimpleNamespace(id="healthy-version", version="healthy-v1")

    class RuntimeRepository:
        activated: list[str] = []

        @staticmethod
        def get_current_version():
            return damaged

        @staticmethod
        def list_versions():
            return [damaged, healthy]

        @staticmethod
        def load_published_bundle(version):
            return bundle if version == healthy.version else None

        def activate_version(self, version):
            self.activated.append(version)

    class Manager:
        events: list[tuple[OntologyAuditAction, str]] = []

        @staticmethod
        def load_runtime_bundle(version_id, candidate_bundle):
            if version_id == damaged.id:
                raise OntologyGovernanceError("damaged artifact", "DAMAGED")
            return candidate_bundle, False, SimpleNamespace(bundle_hash="b" * 64)

        def record_runtime_event(self, action, version_id, *_args, **_kwargs):
            self.events.append((action, version_id))

    repository = RuntimeRepository()
    manager = Manager()
    loaded = _load_startup_runtime(repository, manager, bundle)  # type: ignore[arg-type]
    assert loaded is not None
    assert loaded[0].id == healthy.id
    assert repository.activated == [healthy.version]
    assert manager.events == [
        (OntologyAuditAction.ACTIVATION_FAILED, damaged.id),
        (OntologyAuditAction.ROLLED_BACK, healthy.id),
    ]


def test_startup_fails_when_no_healthy_version_exists(bundle) -> None:
    damaged = SimpleNamespace(id="only-damaged-version", version="damaged-v1")

    class RuntimeRepository:
        @staticmethod
        def get_current_version():
            return damaged

        @staticmethod
        def list_versions():
            return [damaged]

    class Manager:
        events: list[OntologyAuditAction] = []

        @staticmethod
        def load_runtime_bundle(*_args):
            raise OntologyGovernanceError("damaged artifact", "DAMAGED")

        def record_runtime_event(self, action, *_args, **_kwargs):
            self.events.append(action)

    manager = Manager()
    with pytest.raises(OntologyGovernanceError, match="damaged artifact"):
        _load_startup_runtime(RuntimeRepository(), manager, bundle)  # type: ignore[arg-type]
    assert manager.events == [OntologyAuditAction.ACTIVATION_FAILED]
