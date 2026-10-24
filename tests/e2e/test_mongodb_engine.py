"""RM-112 end to end: an Engine whose ``storage.records`` (and ``storage.documents``) is MongoDB.

Needs a replica set (``MEMSPINE_TEST_MONGO_URL``, e.g. a disposable ``mongo:7 --replSet rs0``
container after ``rs.initiate()``; skipped otherwise) and pymongo. Each test uses a throwaway
database. The projections are the embedded ones (LanceDB, Tantivy, LadybugDB when installed)
under ``deployment.data_dir``; the embedder is the deterministic ``hash`` provider, so no model
downloads. Already on the ADR-081 shape (migrate_config_keys_adr081: skip).
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from memspine.exceptions import ConfigError

MONGO_URL = os.environ.get("MEMSPINE_TEST_MONGO_URL")
pytestmark = [
    pytest.mark.milestone,
    pytest.mark.skipif(
        not MONGO_URL or importlib.util.find_spec("pymongo") is None,
        reason="MongoDB engine e2e: set MEMSPINE_TEST_MONGO_URL (replica set)",
    ),
]


@pytest.fixture
def database() -> Iterator[str]:
    from pymongo import MongoClient

    name = f"e2e_{uuid.uuid4().hex[:12]}"
    try:
        yield name
    finally:
        with MongoClient(MONGO_URL) as admin:
            admin.drop_database(name)


def _engine(database: str, data_dir: Path, **overrides: Any) -> Any:
    from memspine import Engine

    storage = {
        "records": {"provider": "mongodb", "settings": {"url": MONGO_URL, "database": database}},
        **overrides.pop("storage", {}),
    }
    return Engine(
        dotenv_path=None,
        deployment={"id": f"dep-{database}", "data_dir": str(data_dir)},
        storage=storage,
        model={"embedding": {"provider": "hash", "settings": {}}},
        **overrides,
    )


def _collections(database: str) -> set[str]:
    from pymongo import MongoClient

    with MongoClient(MONGO_URL) as client:
        return set(client[database].list_collection_names())


async def test_write_search_read_forget_and_sleep_on_mongodb(database: str, tmp_path: Path) -> None:
    eng = _engine(database, tmp_path / "data")
    await eng.start()
    try:
        ns = "t1/alice"
        kept = await eng.write("Alice moved to Lisbon in 2024", namespace=ns)
        gone = await eng.write("Alice's locker code is 4417", namespace=ns)
        listed = {r.record_id for r in await eng.retrieve(ns)}
        assert {kept.record_id, gone.record_id} <= listed
        hits = await eng.search("Where does Alice live?", namespace=ns)
        assert kept.record_id in {record.record_id for record, _score in hits}
        context = await eng.read("Where does Alice live?", namespace=ns)
        assert "Lisbon" in str(context)
        await eng.forget(gone.record_id, namespace=ns, hard=True)
        assert gone.record_id not in {r.record_id for r in await eng.retrieve(ns)}
        assert all("4417" not in str(e.payload) for e in await eng._storage.read_events(0, 10_000))
        stats = await eng.sleep()
        assert isinstance(stats, dict) and stats
        assert not [n for n, s in stats.items() if s.get("status") == "error"], stats
    finally:
        await eng.stop()
    names = _collections(database)
    assert {"events", "records", "offsets", "meta", "projector_bindings"} <= names

    # A second start replays from the stored offsets and keeps the deployment identity.
    again = _engine(database, tmp_path / "data")
    await again.start()
    try:
        assert kept.record_id in {r.record_id for r in await again.retrieve("t1/alice")}
        assert await again._storage.claim_deployment_id("other") == f"dep-{database}"
    finally:
        await again.stop()


async def test_documents_slot_shares_the_records_deployment(database: str, tmp_path: Path) -> None:
    eng = _engine(
        database, tmp_path / "data", storage={"documents": {"provider": "mongodb", "settings": {}}}
    )
    await eng.start()
    try:
        record = await eng.write("an attachment holder", namespace="ns")
        head = await eng.documents.put("ns", b"%PDF-1.7 bytes", record_ids=[record.record_id])
        assert await eng.documents.get("ns", head.digest) == b"%PDF-1.7 bytes"
        await eng.forget(record.record_id, namespace="ns", hard=True)
        assert await eng.documents.get("ns", head.digest) is None  # the erasure hook ran
        connectors = {
            id(result.service._connector)
            for key, result in eng._provider_results.items()
            if key in {"documents"} or key.startswith("storage.")
        }
        assert len(connectors) == 1  # records and documents share one MongoDB connector
    finally:
        await eng.stop()
    assert {"documents.heads", "documents.files", "documents.chunks"} & _collections(database)


_ASSOCIATIVE = {"semantic": {"enabled": True}, "associative": {"enabled": True}}


async def test_graph_auto_resolves_to_ladybug_and_is_pinned(database: str, tmp_path: Path) -> None:
    """``storage.graph: auto`` on MongoDB records resolves to LadybugDB (never sql_adjacency)
    and is pinned in the MongoDB projector binding; erasure's children query runs on MongoDB."""
    if importlib.util.find_spec("ladybug") is None:
        pytest.skip("needs the [graph] extra (ladybug)")
    eng = _engine(database, tmp_path / "data", memories=_ASSOCIATIVE, write={"lineage_table": True})
    await eng.start()
    try:
        assert eng._wiring.graph_pin == "ladybug"
        fact = await eng.write(
            "Alice works at Acme", namespace="ns", entity="alice", attribute="employer"
        )
        derived = await eng.write(
            "Alice is an engineer at Acme", namespace="ns", derived_from=[fact.record_id]
        )
        stats = await eng.sleep()
        assert not [n for n, s in stats.items() if s.get("status") == "error"], stats
        bindings = eng._storage.projection_bindings()
        assert await bindings.bound_provider(f"dep-{database}", "graph") == "ladybug"
        children = await eng._storage.list_children("ns", [fact.record_id])
        assert derived.record_id in {r.record_id for r in children}
    finally:
        await eng.stop()


async def test_graph_auto_without_ladybug_is_refused_naming_the_slot(
    database: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memspine.providers import records_features

    monkeypatch.setattr(records_features, "_ladybug_installed", lambda: False)
    eng = _engine(database, tmp_path / "data", memories=_ASSOCIATIVE)
    with pytest.raises(ConfigError, match="storage.graph.provider 'auto'") as caught:
        await eng.start()
    assert "sql_adjacency" in str(caught.value) and "memspine[graph]" in str(caught.value)
    assert _collections(database) == set()


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"storage": {"vector": {"provider": "pgvector"}}}, "pgvector"),
        (
            {"storage": {"graph": {"provider": "sql_adjacency"}}, "memories": _ASSOCIATIVE},
            "sql_adjacency",
        ),
        ({"workers": {"outbox": True}}, "workers.outbox"),
        ({"offline": {"sleep": True}}, "offline.sleep"),
        ({"economy": {"ledger": True, "ledger_persist": True}}, "economy.ledger_persist"),
    ],
)
async def test_sql_only_features_are_refused_before_anything_opens(
    database: str, tmp_path: Path, overrides: dict[str, Any], needle: str
) -> None:
    eng = _engine(database, tmp_path / "data", **overrides)
    with pytest.raises(ConfigError, match=needle) as caught:
        await eng.start()
    assert "has no SQL access" in str(caught.value)
    assert _collections(database) == set()  # nothing was opened or created
