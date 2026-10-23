"""ADR-083 / RM-119: ``settings.launch`` of the mongodb providers (validation, locality, wiring).

No server: the launcher and the connector opens are replaced by recorders; the launcher itself
is covered by tests/unit/clients/test_mongodb_launcher.py and the live suite by
tests/integration/test_mongodb_managed_live.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from memspine.clients.mongodb import MongoConnector, MongodLauncher
from memspine.config.schema import MemspineConfig
from memspine.config.schema.providers import (
    MongoDocumentsSettings,
    MongoLaunchSettings,
    MongoRecordsSettings,
)
from memspine.exceptions import ConfigError
from memspine.providers.builtin import mongodb as factory
from memspine.providers.locality import (
    DeploymentLocalityError,
    slot_localities,
    validate_deployment_locality,
)

MANAGED = {"mode": "managed", "version": "9.0.4", "port": 57017, "download": True}
URL = "mongodb://127.0.0.1:57017/?directConnection=true&replicaSet=rs0"


# ------------------------------------------------------------------------ validation
def test_external_is_the_default_and_needs_a_url() -> None:
    assert MongoRecordsSettings(url="mongodb://db/?replicaSet=rs0").launch.mode == "external"
    with pytest.raises(ValidationError, match="settings.url is required"):
        MongoRecordsSettings()


def test_managed_derives_the_loopback_url() -> None:
    settings = MongoRecordsSettings.model_validate({"launch": MANAGED})
    assert settings.url is None and settings.effective_url == URL
    assert factory.mongo_spec(settings).url == URL


@pytest.mark.parametrize(
    ("launch", "needle"),
    [
        ({"mode": "managed", "port": 57017, "download": True}, "launch.version is required"),
        ({"mode": "managed", "version": "latest", "port": 1, "download": True}, "version"),
        ({"mode": "managed", "version": "9.0", "port": 1, "download": True}, "version"),
        ({"mode": "managed", "version": "9.0.4", "download": True}, "launch.port is required"),
        ({"mode": "managed", "version": "9.0.4", "port": 57017}, "launch.download: true"),
        ({"mode": "managed", "version": "9.0.4", "port": 0, "download": True}, "port"),
        ({**MANAGED, "cache_size_gb": 0.1}, "cache_size_gb"),
        ({**MANAGED, "start_timeout_s": 0}, "start_timeout_s"),
        ({**MANAGED, "bind_ip": "0.0.0.0"}, "bind_ip"),  # loopback only: no such key
        ({"mode": "external", "port": 57017}, "launch.port applies only to launch.mode: managed"),
        ({"version": "9.0.4"}, "launch.version applies only"),
        ({"download": True}, "launch.download applies only"),
    ],
)
def test_launch_validation_names_the_key(launch: dict[str, Any], needle: str) -> None:
    with pytest.raises(ValidationError, match=needle):
        MongoLaunchSettings.model_validate(launch)


def test_a_binary_replaces_the_download_consent() -> None:
    launch = MongoLaunchSettings.model_validate(
        {"mode": "managed", "version": "9.0.4", "port": 57017, "binary": "/opt/mongod"}
    )
    assert launch.download is False and launch.binary == "/opt/mongod"


def test_managed_with_a_url_is_refused() -> None:
    with pytest.raises(ValidationError, match="settings.url must be unset"):
        MongoRecordsSettings.model_validate({"url": URL, "launch": MANAGED})
    with pytest.raises(ValidationError, match="documents settings.url must be unset"):
        MongoDocumentsSettings.model_validate({"url": URL, "launch": MANAGED})
    with pytest.raises(ValidationError, match="external needs settings.url"):
        MongoDocumentsSettings.model_validate({"launch": {"mode": "external"}})


def test_the_dbpath_defaults_under_deployment_data_dir(tmp_path: Path) -> None:
    launch = MongoLaunchSettings.model_validate(MANAGED)
    spec = factory.mongod_spec(launch, str(tmp_path))
    assert Path(spec.data_dir) == tmp_path / "mongod" / "57017"
    assert spec.url == URL and spec.download and spec.version == "9.0.4"
    explicit = MongoLaunchSettings.model_validate({**MANAGED, "data_dir": str(tmp_path / "x")})
    assert factory.mongod_spec(explicit, None).data_dir == str(tmp_path / "x")
    with pytest.raises(ConfigError, match=r"launch.data_dir or deployment.data_dir"):
        factory.mongod_spec(launch, None)


def test_documents_inherit_the_records_launcher() -> None:
    records = ("mongodb", {"launch": MANAGED, "database": "m"})
    docs = MongoDocumentsSettings()
    inherited = factory.documents_launch(docs, records)
    assert inherited is not None and inherited[1] == "storage.records"
    assert factory.documents_spec(docs, records).url == URL
    assert factory.documents_spec(docs, records).database == "m"
    own = MongoDocumentsSettings.model_validate({"launch": MANAGED})
    assert factory.documents_launch(own, None) == (own.launch, "storage.documents")
    external = MongoDocumentsSettings(url="mongodb://db/?replicaSet=rs0")
    assert factory.documents_launch(external, records) is None
    ext_records = ("mongodb", {"url": "mongodb://db/?replicaSet=rs0"})
    assert factory.documents_launch(docs, ext_records) is None


# -------------------------------------------------------------------------- locality
_RUNTIME = {
    "provider_services": {
        "health_timeout_s": 5.0,
        "close_timeout_s": 10.0,
        "startup_timeout_s": None,
    },
}
PG = {
    "provider": "postgres",
    "settings": {
        "url": "postgresql://u:p@db.internal:5432/m",
        "connect_timeout_s": 5.0,
        "statement_timeout_s": 30.0,
    },
}


def _config(
    locality: str, records: Any, documents: Any = None, **deployment: Any
) -> MemspineConfig:
    storage = {
        "documents": documents,
        "records": records,
        "vector": {"provider": "pgvector"},
        "lexical": {
            "provider": "opensearch",
            "settings": {"connect_timeout_s": 2.0, "request_timeout_s": 5.0},
        },
        "graph": {"provider": "sql_adjacency"},
        "cache": {"provider": "memory"},
    }
    return MemspineConfig.model_validate(
        {
            "runtime": _RUNTIME,
            "deployment": {"locality": locality, "id": "d", **deployment},
            "storage": storage,
        }
    )


MANAGED_RECORDS = {"provider": "mongodb", "settings": {"launch": MANAGED}}
EXTERNAL_RECORDS = {"provider": "mongodb", "settings": {"url": "mongodb://db/?replicaSet=rs0"}}


def _records(config: MemspineConfig) -> tuple[str, str | None]:
    found = {s.slot: (s.provider, s.locality) for s in slot_localities(config)}
    return found["records"]


def test_a_managed_slot_is_host_local_and_an_external_one_a_cluster() -> None:
    assert _records(_config("single_process", MANAGED_RECORDS)) == ("mongodb", "host")
    assert _records(_config("single_process", EXTERNAL_RECORDS)) == ("mongodb", "cluster")


def test_multi_node_rejects_a_managed_slot() -> None:
    with pytest.raises(DeploymentLocalityError) as raised:
        validate_deployment_locality(_config("multi_node", MANAGED_RECORDS))
    message = str(raised.value)
    assert "storage.records provider 'mongodb' is host-local" in message
    assert "storage.records.settings.launch.mode: external" in message
    validate_deployment_locality(_config("multi_node", EXTERNAL_RECORDS))


def test_multi_process_allows_managed_only_in_the_writer() -> None:
    validate_deployment_locality(_config("multi_process", MANAGED_RECORDS, writer=True))
    with pytest.raises(DeploymentLocalityError) as raised:
        validate_deployment_locality(_config("multi_process", MANAGED_RECORDS))
    message = str(raised.value)
    assert "not the designated writer" in message and "deployment.writer: true" in message
    assert f"launch.mode: external with storage.records.settings.url {URL}" in message
    validate_deployment_locality(_config("multi_process", EXTERNAL_RECORDS))


def test_a_managed_documents_slot_follows_the_same_rules() -> None:
    documents = {"provider": "mongodb", "settings": {"launch": MANAGED}}
    with pytest.raises(DeploymentLocalityError, match="storage.documents provider 'mongodb'"):
        validate_deployment_locality(_config("multi_node", PG, documents))
    with pytest.raises(DeploymentLocalityError, match="storage.documents.settings.launch.mode"):
        validate_deployment_locality(_config("multi_process", PG, documents))
    validate_deployment_locality(_config("multi_process", PG, documents, writer=True))


# ---------------------------------------------------------------------------- wiring
@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def launcher_open(self: MongodLauncher) -> None:
        calls.append(f"mongod.open:{self.spec.port}")
        self._opened = True

    async def launcher_close(self: MongodLauncher) -> None:
        calls.append("mongod.close")

    async def connector_open(self: MongoConnector) -> None:
        calls.append(f"connector.open:{self.spec.url}")
        self._client = object()  # type: ignore[assignment]

    async def connector_close(self: MongoConnector) -> None:
        calls.append("connector.close")

    monkeypatch.setattr(MongodLauncher, "open", launcher_open)
    monkeypatch.setattr(MongodLauncher, "close", launcher_close)
    monkeypatch.setattr(MongoConnector, "open", connector_open)
    monkeypatch.setattr(MongoConnector, "close", connector_close)
    return calls


async def test_records_and_documents_share_one_launcher_closed_after_the_connector(
    tmp_path: Path, recorded: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("pymongo")
    from memspine.config.schema.storage import EventLogConfig
    from memspine.providers.contexts import (
        DocumentsBuildContext,
        SharedBuildContext,
        StorageBuildContext,
    )
    from memspine.providers.contracts import ProjectionPlacement
    from memspine.providers.lifecycle import ProviderLifecycleManager
    from memspine.providers.registry import get_registry, validate_settings
    from memspine.providers.shared import ResourceRegistry
    from memspine.services.documents.mongodb import MongoDocumentStore

    async def no_start(self: MongoDocumentStore) -> None:
        return None

    monkeypatch.setattr(MongoDocumentStore, "start", no_start)
    resources = ProviderLifecycleManager(close_timeout_s=5.0)
    shared = SharedBuildContext(resources, "owner", 5.0, registry=ResourceRegistry())
    records_raw: dict[str, Any] = {"launch": MANAGED}
    records = get_registry("storage_backends").get("mongodb")
    built = await records.builder.build(
        validate_settings(records, records_raw),
        StorageBuildContext(shared, EventLogConfig(), data_dir=str(tmp_path)),
    )
    assert [r.resource_id for r in built.resources] == ["records.mongod", "records"]
    documents = get_registry("document_stores").get("mongodb")
    docs = await documents.builder.build(
        validate_settings(documents, {}),
        DocumentsBuildContext(
            shared,
            ProjectionPlacement(None, False, "owner"),
            records=("mongodb", records_raw),
            data_dir=str(tmp_path),
        ),
    )
    assert [r.resource_id for r in docs.resources] == ["documents.mongod", "documents"]
    assert recorded == ["mongod.open:57017", f"connector.open:{URL}"]  # one server, one client
    assert docs.service._connector is built.service._connector
    assert (await resources.close_all()).complete
    assert recorded[2:] == ["connector.close", "mongod.close"]


async def test_an_external_slot_never_builds_a_launcher(
    recorded: list[str],
) -> None:
    pytest.importorskip("pymongo")
    from memspine.config.schema.storage import EventLogConfig
    from memspine.providers.contexts import SharedBuildContext, StorageBuildContext
    from memspine.providers.lifecycle import ProviderLifecycleManager
    from memspine.providers.registry import get_registry, validate_settings
    from memspine.providers.shared import ResourceRegistry

    resources = ProviderLifecycleManager(close_timeout_s=5.0)
    shared = SharedBuildContext(resources, "owner", 5.0, registry=ResourceRegistry())
    records = get_registry("storage_backends").get("mongodb")
    built = await records.builder.build(
        validate_settings(records, {"url": "mongodb://db:27017/?replicaSet=rs0"}),
        StorageBuildContext(shared, EventLogConfig()),
    )
    assert [r.resource_id for r in built.resources] == ["records"]
    assert not [c for c in recorded if c.startswith("mongod")]
    await resources.close_all()
