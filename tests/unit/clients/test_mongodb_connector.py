"""MongoDB connector (RM-112): identity, locality and the replica-set requirement, no server."""

from __future__ import annotations

import pytest

from memspine.clients.mongodb import MongoConnector, MongoSpec, StandaloneServerError
from memspine.exceptions import StorageError


def test_identity_is_credential_free_and_host_order_independent() -> None:
    a = MongoSpec("mongodb://user:secret@h2:27017,h1:27017/?replicaSet=rs0&w=majority")
    b = MongoSpec("mongodb://user:secret@H1:27017,h2:27017/?w=majority&replicaSet=rs0")
    other = MongoSpec("mongodb://user:other@h1:27017,h2:27017/?replicaSet=rs0&w=majority")
    assert MongoConnector.identity(a) == MongoConnector.identity(b)
    assert MongoConnector.identity(a) != MongoConnector.identity(other)  # auth digest differs
    assert "secret" not in repr(MongoConnector.identity(a))
    assert MongoConnector.locality(a) == "cluster"


def test_a_malformed_url_fails_at_construction() -> None:
    with pytest.raises(StorageError, match="mongodb://"):
        MongoConnector(MongoSpec("postgresql://localhost/x"))


def test_a_standalone_server_is_refused_with_the_reason() -> None:
    connector = MongoConnector(MongoSpec("mongodb://localhost:27017"))
    with pytest.raises(StandaloneServerError, match="replica set"):
        connector._check_topology({"isWritablePrimary": True})
    assert connector._check_topology({"setName": "rs0"}) == "replica_set:rs0"
    assert connector._check_topology({"msg": "isdbgrid"}) == "sharded"
    relaxed = MongoConnector(MongoSpec("mongodb://localhost:27017", require_transactions=False))
    assert relaxed._check_topology({"isWritablePrimary": True}) == "standalone"


async def test_handles_need_an_open_connector() -> None:
    connector = MongoConnector(MongoSpec("mongodb://localhost:27017"))
    assert await connector.health() is False
    with pytest.raises(StorageError, match="open"):
        connector.handle  # noqa: B018
    await connector.close()  # idempotent on a never-opened connector
