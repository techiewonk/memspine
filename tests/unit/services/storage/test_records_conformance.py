"""Records store conformance suite (ADR-081, RM-111): the SQL-independent ``storage.records``
contract, run against every provider that claims it.

The suite talks to the store only through :class:`~memspine.services.storage.records_port.
RecordsStore`. To run it against a new provider (MongoDB, RM-112), add a case to
:data:`PROVIDERS` and a branch to :func:`_open` that returns a started store plus, when the
store can be opened twice (a second process on the same data), a factory for a second handle.
The contract text is ``docs/RECORDS_STORE_CONTRACT.md``.

Providers: ``sqlite_memory`` (``create_all``), ``sqlite_file`` (Alembic), and with
``MEMSPINE_TEST_PG_URL`` (a scratch server; each test gets its own throwaway database)
``postgres_alembic`` and ``postgres_create_all``; with ``MEMSPINE_TEST_MONGO_URL`` (a replica set,
e.g. ``mongodb://127.0.0.1:57017/?directConnection=true``; each test gets its own throwaway
database) ``mongodb`` (RM-112).
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import orjson
import pytest

from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.lineage import LineageEdge
from memspine.services.storage.records_port import (
    LeasePort,
    OutboxItem,
    RecordsStore,
)
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord, SourceInfo

PG_URL = os.environ.get("MEMSPINE_TEST_PG_URL")
MONGO_URL = os.environ.get("MEMSPINE_TEST_MONGO_URL")
PROVIDERS = ["sqlite_memory", "sqlite_file", "postgres_alembic", "postgres_create_all", "mongodb"]
SCOPE = OwnerScope(tenant="t1", owner="alice")
OTHER = OwnerScope(tenant="t1", owner="bob")


@dataclass
class Handle:
    kind: str
    store: RecordsStore
    #: opens a second, independent handle on the same data (None: process-local store)
    reopen: Callable[[], Awaitable[RecordsStore]] | None
    closers: list[Callable[[], Awaitable[None]]]


# ------------------------------------------------------------------------------- providers
@pytest.fixture
def pg_database() -> Iterator[str]:
    """A throwaway database on the scratch server, dropped afterwards."""
    if not PG_URL or importlib.util.find_spec("psycopg") is None:
        pytest.skip("records conformance on Postgres: set MEMSPINE_TEST_PG_URL (scratch server)")
    import psycopg
    from sqlalchemy.engine import make_url

    name = f"rc_{uuid.uuid4().hex[:12]}"
    base = make_url(PG_URL)
    admin = base.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        yield base.set(database=name).render_as_string(hide_password=False)
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def mongo_database() -> Iterator[str]:
    """A throwaway database name on the scratch replica set, dropped afterwards."""
    if not MONGO_URL or importlib.util.find_spec("pymongo") is None:
        pytest.skip("records conformance on MongoDB: set MEMSPINE_TEST_MONGO_URL (replica set)")
    from pymongo import MongoClient

    name = f"rc_{uuid.uuid4().hex[:12]}"
    try:
        yield name
    finally:
        with MongoClient(MONGO_URL) as admin:
            admin.drop_database(name)


async def _open_mongo(database: str) -> tuple[RecordsStore, Callable[[], Awaitable[None]]]:
    from memspine.clients.mongodb import MongoConnector, MongoSpec
    from memspine.services.storage.mongodb import MongoRecordsStore

    assert MONGO_URL is not None
    connector = MongoConnector(MongoSpec(MONGO_URL, database=database))
    await connector.open()
    store = MongoRecordsStore(connector)
    await store.start()

    async def close() -> None:
        await store.stop()
        await connector.close()

    return store, close


async def _open_sqlite(path: str) -> tuple[RecordsStore, Callable[[], Awaitable[None]]]:
    from memspine.clients.sqlite import SQLiteClient
    from memspine.services.storage.sqlite.engine import SQLiteStorage

    client = SQLiteClient(path, busy_timeout_s=10.0)
    await client.connect()
    store = SQLiteStorage(client)
    await store.start()

    async def close() -> None:
        await store.stop()
        await client.close()

    return store, close


async def _open_pg(url: str, alembic: bool) -> tuple[RecordsStore, Callable[[], Awaitable[None]]]:
    from memspine.clients.postgres import PostgresClient
    from memspine.services.storage.postgres.engine import PostgresStorage

    client = PostgresClient(url)
    await client.connect()
    store = PostgresStorage(client, use_alembic=alembic)
    await store.start()

    async def close() -> None:
        await store.stop()
        await client.close()

    return store, close


@pytest.fixture(params=PROVIDERS)
async def handle(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Handle]:
    kind = request.param
    closers: list[Callable[[], Awaitable[None]]] = []
    opener: Callable[[], Awaitable[tuple[RecordsStore, Callable[[], Awaitable[None]]]]]
    reopenable = True
    if kind == "sqlite_memory":
        path = ":memory:"
        reopenable = False

        def opener() -> Awaitable[tuple[RecordsStore, Callable[[], Awaitable[None]]]]:
            return _open_sqlite(path)
    elif kind == "sqlite_file":
        path = str(tmp_path / "records.db")

        def opener() -> Awaitable[tuple[RecordsStore, Callable[[], Awaitable[None]]]]:
            return _open_sqlite(path)
    elif kind == "mongodb":
        database = request.getfixturevalue("mongo_database")

        def opener() -> Awaitable[tuple[RecordsStore, Callable[[], Awaitable[None]]]]:
            return _open_mongo(database)
    else:
        url = request.getfixturevalue("pg_database")
        alembic = kind == "postgres_alembic"

        def opener() -> Awaitable[tuple[RecordsStore, Callable[[], Awaitable[None]]]]:
            return _open_pg(url, alembic)

    store, close = await opener()
    closers.append(close)

    async def reopen() -> RecordsStore:
        other, other_close = await opener()
        closers.append(other_close)
        return other

    try:
        yield Handle(kind, store, reopen if reopenable else None, closers)
    finally:
        for closer in reversed(closers):
            await closer()


def ev(
    namespace: str = "ns/a", kind: EventKind = EventKind.WRITE, **payload: object
) -> MemoryEvent:
    return MemoryEvent(kind=kind, namespace=namespace, payload=dict(payload))


def _record(record_id: str, content: str, **kw: Any) -> MemoryRecord:
    return MemoryRecord(
        record_id=record_id,
        namespace=kw.pop("namespace", "ns/a"),
        memory_type=kw.pop("memory_type", "semantic"),
        content=content,
        source=kw.pop("source", SourceInfo(role="user", channel="chat", message_id="m-1")),
        **kw,
    )


#: Known gap (found by this suite, RM-111): a `:memory:` SQLite store is a shared-cache
#: database, whose table locks (SQLITE_LOCKED) are not retried by `busy_timeout`, so two
#: write transactions in flight at once fail with "database table is locked" instead of
#: waiting. File databases (WAL + busy timeout) and Postgres serialise correctly.
MEMORY_CONCURRENCY_GAP = (
    "sqlite :memory: (shared cache) raises 'database table is locked' for concurrent writers"
)


def _concurrent_writers(request: pytest.FixtureRequest, handle: Handle) -> None:
    if handle.kind == "sqlite_memory":
        request.applymarker(pytest.mark.xfail(strict=True, reason=MEMORY_CONCURRENCY_GAP))


# ------------------------------------------------------------------------------- schema
async def test_the_store_satisfies_the_port_and_its_schema_is_versioned(handle: Handle) -> None:
    store = handle.store
    assert isinstance(store, RecordsStore)
    assert isinstance(store.leases(), LeasePort)
    revision = await store.schema_revision()
    assert revision
    await store.start()  # setup is idempotent and keeps the version
    assert await store.schema_revision() == revision
    assert store.can_rebuild


# ------------------------------------------------------------------------------- event log
async def test_sequence_is_monotonic_under_concurrent_appenders(
    request: pytest.FixtureRequest, handle: Handle
) -> None:
    _concurrent_writers(request, handle)
    stores = [handle.store]
    if handle.reopen is not None:
        stores.append(await handle.reopen())  # a second "process" on the same data
    per_store = 25
    results = await asyncio.gather(
        *(
            store.append_event(ev(content=f"{i}:{n}"))
            for i, store in enumerate(stores)
            for n in range(per_store)
        )
    )
    seqs = [e.seq for e in results]
    assert all(isinstance(s, int) and s > 0 for s in seqs)
    assert len(set(seqs)) == len(seqs)  # never reused
    replay = await handle.store.read_events(limit=10_000)
    assert [e.seq for e in replay] == sorted(seqs)  # replay is ascending seq
    assert {e.event_id for e in replay} == {e.event_id for e in results}
    later = await handle.store.append_event(ev(content="after"))
    assert later.seq is not None and later.seq > max(s for s in seqs if s is not None)


async def test_replay_is_ordered_and_pages_by_seq(handle: Handle) -> None:
    store = handle.store
    written = [await store.append_event(ev(content=f"e{i}", i=i)) for i in range(23)]
    paged: list[MemoryEvent] = []
    cursor = 0
    while page := await store.read_events(after_seq=cursor, limit=7):
        assert len(page) <= 7
        paged.extend(page)
        cursor = page[-1].seq or 0
    assert [e.event_id for e in paged] == [e.event_id for e in written]
    assert [e.payload["i"] for e in paged] == list(range(23))
    assert all(a.fingerprint == b.fingerprint for a, b in zip(paged, written, strict=True))
    middle = written[10].seq or 0
    assert [e.seq for e in await store.read_events(after_seq=middle, limit=3)] == [
        w.seq for w in written[11:14]
    ]


# ------------------------------------------------------------------------------- outbox
async def test_append_plus_outbox_commits_event_and_items_together(handle: Handle) -> None:
    store = handle.store
    first, offsets = await store.append_with_outbox(
        ev(content="queued"),
        [
            OutboxItem(SCOPE, "windows", {"turn": 1}),
            OutboxItem(SCOPE, "windows", {"turn": 2}),
            OutboxItem(OTHER, "windows", {"turn": 1}),
        ],
    )
    assert first.seq is not None and offsets == [0, 1, 0]
    second, more = await store.append_with_outbox(
        ev(content="queued again"), [OutboxItem(SCOPE, "windows", {"turn": 3})]
    )
    assert second.seq is not None and second.seq > first.seq and more == [2]
    assert await store.outbox_items(SCOPE, "windows") == [
        (0, {"turn": 1}),
        (1, {"turn": 2}),
        (2, {"turn": 3}),
    ]
    assert await store.outbox_items(SCOPE, "windows", after=1) == [(2, {"turn": 3})]
    assert await store.outbox_items(OTHER, "windows") == [(0, {"turn": 1})]
    assert [e.event_id for e in await store.read_events()] == [first.event_id, second.event_id]


class _Unencodable(Mapping[str, Any]):
    """An item payload that fails while the transaction is open."""

    def __getitem__(self, key: str) -> Any:
        raise RuntimeError("injected outbox fault")

    def __iter__(self) -> Iterator[str]:
        raise RuntimeError("injected outbox fault")

    def __len__(self) -> int:
        return 1


@pytest.mark.parametrize("fault", ["encode", "constraint"])
async def test_an_outbox_failure_rolls_the_event_back(handle: Handle, fault: str) -> None:
    store = handle.store
    before, _ = await store.append_with_outbox(
        ev(content="kept"), [OutboxItem(SCOPE, "windows", {"n": 0})]
    )
    bad = (
        OutboxItem(SCOPE, "windows", _Unencodable())
        if fault == "encode"
        else OutboxItem(SCOPE, None, {"n": 2})  # type: ignore[arg-type]  # NOT NULL violated
    )
    with pytest.raises(Exception):  # noqa: B017 - any failure, as long as nothing commits
        await store.append_with_outbox(
            ev(content="rolled back"), [OutboxItem(SCOPE, "windows", {"n": 1}), bad]
        )
    events = await store.read_events()
    assert [e.event_id for e in events] == [before.event_id]  # the event did not commit
    assert await store.outbox_items(SCOPE, "windows") == [(0, {"n": 0})]  # nor any item
    after, offsets = await store.append_with_outbox(
        ev(content="next"), [OutboxItem(SCOPE, "windows", {"n": 1})]
    )
    assert after.seq is not None and before.seq is not None and after.seq > before.seq
    assert offsets == [1]  # the failed item left no offset behind


# ------------------------------------------------------------------------------- offsets
async def test_projector_offsets_advance_only(handle: Handle) -> None:
    store = handle.store
    assert await store.get_offset("vectors") == 0
    await store.set_offset("vectors", 5)
    await store.set_offset("vectors", 3)  # never backwards
    assert await store.get_offset("vectors") == 5
    await store.set_offset("graph", 2)
    for n in (7, 12, 9, 11):
        await store.set_offset("vectors", n)
    assert await store.get_offset("vectors") == 12
    assert await store.get_offset("graph") == 2
    await store.reset_offset("vectors")  # the one sanctioned move back
    assert await store.get_offset("vectors") == 0
    assert await store.get_offset("graph") == 2
    if handle.reopen is not None:  # durable and shared by every handle
        assert await (await handle.reopen()).get_offset("graph") == 2


async def test_concurrent_checkpoints_keep_the_highest_mark(
    request: pytest.FixtureRequest, handle: Handle
) -> None:
    _concurrent_writers(request, handle)
    store = handle.store
    await asyncio.gather(*(store.set_offset("vectors", n) for n in (7, 12, 9, 11, 3)))
    assert await store.get_offset("vectors") == 12


# ------------------------------------------------------------------------------- leases
async def test_leases_are_exclusive_expiring_and_fenced(handle: Handle) -> None:
    leases = handle.store.leases()
    a = leases.claim("ns/a", "proc-a", "job-a", ttl=30.0)
    assert a is not None
    assert leases.claim("ns/a", "proc-b", "job-b", ttl=30.0) is None  # one live holder
    assert leases.claim("ns/other", "proc-b", "job-b", ttl=30.0) is not None  # per key
    beat = leases.heartbeat(a, ttl=30.0)
    assert beat is not None and beat.epoch == a.epoch
    assert leases.release(beat)
    b = leases.claim("ns/a", "proc-b", "job-b", ttl=0.3)
    assert b is not None and b.epoch == a.epoch + 1  # every takeover bumps the epoch
    assert leases.heartbeat(a, ttl=30.0) is None  # the stale holder is fenced
    assert leases.release(a) is False
    await asyncio.sleep(0.6)
    c = leases.claim("ns/a", "proc-c", "job-c", ttl=30.0)  # an expired lease is free
    assert c is not None and c.epoch == b.epoch + 1
    assert leases.heartbeat(b, ttl=30.0) is None
    held = leases.get_lease("ns/a")
    assert held is not None and (held.owner, held.epoch) == ("proc-c", c.epoch)
    if handle.reopen is not None:  # the lease table lives in the store (RM-111 WP5 side tables)
        other = (await handle.reopen()).leases()
        seen = other.get_lease("ns/a")
        assert seen is not None and (seen.owner, seen.epoch) == ("proc-c", c.epoch)
        assert other.claim("ns/a", "proc-d", "job-d", ttl=30.0) is None  # exclusive across handles


async def test_concurrent_claims_have_exactly_one_winner(handle: Handle) -> None:
    if handle.reopen is None:
        pytest.skip("a process-local store has a single-threaded lease table")
    leases = handle.store.leases()
    with ThreadPoolExecutor(max_workers=8) as pool:
        won = list(
            pool.map(
                lambda n: leases.claim("ns/race", f"proc-{n}", f"job-{n}", ttl=30.0), range(16)
            )
        )
    winners = [lease for lease in won if lease is not None]
    assert len(winners) == 1 and winners[0].epoch == 1


# ------------------------------------------------------------------------------- read model
async def test_the_record_read_model_round_trips(handle: Handle) -> None:
    store = handle.store
    fact = _record("r-fact", "Alice lives in Paris", entity="alice", attribute="city")
    note = _record(
        "r-note",
        "a passing note",
        memory_type="episodic",
        source=SourceInfo(role="assistant", channel="chat", message_id="m-1"),
    )
    other = _record("r-other", "elsewhere", namespace="ns/b")
    for record in (fact, note, other):
        await store.upsert_record(record)
    loaded = await store.get_record("r-fact")
    assert loaded is not None and loaded.content == fact.content
    assert await store.list_namespaces() == ["ns/a", "ns/b"]
    assert {r.record_id for r in await store.list_records("ns/a")} == {"r-fact", "r-note"}
    assert [r.record_id for r in await store.list_records("ns/a", "episodic")] == ["r-note"]
    active = await store.find_active_fact("ns/a", "alice", "city")
    assert active is not None and active.record_id == "r-fact"
    session = await store.list_session_records("ns/a", "m-1", roles=["assistant"])
    assert [r.record_id for r in session] == ["r-note"]
    await store.upsert_record(fact.model_copy(update={"content": "Alice lives in Rome"}))
    updated = await store.get_record("r-fact")
    assert updated is not None and updated.content == "Alice lives in Rome"  # upsert, not dup
    await store.delete_all_records()  # rebuild drops the read model, keeps the log
    assert await store.list_namespaces() == []


# ------------------------------------------------------------------------------- lineage
async def test_lineage_edges_and_queries(handle: Handle) -> None:
    store = handle.store
    edges = [
        LineageEdge(child="fact-1", parent="turn-1", role="support"),
        LineageEdge(child="fact-1", parent="turn-2", role="context"),
        LineageEdge(child="summary-1", parent="fact-1", role="member"),
    ]
    assert await store.add_lineage(edges, namespace="ns/a") == 3
    assert await store.add_lineage(edges[:1], namespace="ns/a") == 0  # idempotent per edge
    parents = await store.lineage_parents(["fact-1"])
    assert [(e.parent, e.role) for e in parents["fact-1"]] == [
        ("turn-1", "support"),
        ("turn-2", "context"),
    ]  # insertion order
    assert [e.parent for e in (await store.lineage_parents(["fact-1"], ["context"]))["fact-1"]] == [
        "turn-2"
    ]
    children = await store.lineage_children(["turn-1", "fact-1"])
    assert [e.child for e in children["turn-1"]] == ["fact-1"]
    assert [e.child for e in children["fact-1"]] == ["summary-1"]
    assert await store.delete_lineage(edges[2:]) == 1
    assert await store.lineage_children(["fact-1"]) == {}


# ------------------------------------------------------------------------------- erasure
async def test_erasure_removes_content_and_keeps_order(handle: Handle) -> None:
    store = handle.store
    secret = "my passport number is X1234567"
    target = _record("r-secret", secret, entity="alice", attribute="passport")
    child = _record(
        "r-child",
        "derived",
        source=SourceInfo(role="user", channel="mining", message_id="m-1", parents=["r-secret"]),
    )
    await store.upsert_record(target)
    await store.upsert_record(child)
    kept = await store.append_event(ev(content="unrelated"))
    written = await store.append_event(ev(record=target.model_dump(mode="json")))
    later = await store.append_event(ev(content="later"))
    assert [r.record_id for r in await store.list_children("ns/a", ["r-secret"])] == ["r-child"]

    redacted = await store.redact_event_payloads("r-secret")
    await store.delete_record("r-secret")

    assert redacted == [written.seq]
    events = await store.read_events()
    assert [e.seq for e in events] == [kept.seq, written.seq, later.seq]  # order survives
    erased = events[1]
    assert erased.payload.get("redacted") is True
    assert (erased.kind, erased.fingerprint) == (written.kind, written.fingerprint)
    assert secret not in orjson.dumps(erased.payload).decode()  # content is gone
    assert "X1234567" not in orjson.dumps([e.payload for e in events]).decode()
    assert events[0].payload == {"content": "unrelated"}  # others untouched
    assert await store.get_record("r-secret") is None
    assert await store.redact_event_payloads("r-secret") == []  # idempotent


async def test_retention_never_prunes_unapplied_events(handle: Handle) -> None:
    store = handle.store
    await store.append_event(ev(content="old"))
    # full mode (the default): the audit log is never pruned
    assert await store.prune_events(datetime.now(UTC) + timedelta(days=1)) == 0
    assert len(await store.read_events()) == 1
