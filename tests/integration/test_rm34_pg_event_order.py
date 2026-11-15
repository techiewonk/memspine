"""RM-34: owned PostgreSQL proof that a scoped head cannot pass an uncommitted lower seq."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from memspine.clients.postgres import PostgresClient, PostgresSpec
from memspine.services.storage.postgres.engine import PostgresStorage
from memspine.store.events import EventKind, MemoryEvent

pytest.importorskip("psycopg")


@dataclass(frozen=True)
class PgTarget:
    url: str = field(repr=False)


def _owned_state(
    expected: tuple[str, str, int] | None = None,
) -> tuple[int, str, str, str]:
    path = Path(__file__).resolve().parents[2] / "scripts" / "local_services.py"
    spec = importlib.util.spec_from_file_location("local_services", path)
    assert spec is not None and spec.loader is not None
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    state = launcher.load_state()
    if state is None:
        pytest.skip("owned RM-136 PostgreSQL stack is absent")
    port = launcher.ports_for(state)["POSTGRES"]
    project, owner = str(state["project"]), str(state["owner"])
    if expected is not None and (project, owner, port) != expected:
        raise RuntimeError("owned PostgreSQL target changed during scratch database lifetime")
    ids = launcher.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            "label=com.docker.compose.service=postgres",
            "--format",
            "{{.ID}}",
        ]
    ).stdout.split()
    if len(ids) != 1:
        raise RuntimeError("owned PostgreSQL container is missing or ambiguous")
    inspected = (
        launcher.run(
            [
                "docker",
                "inspect",
                "--format",
                '{{index .Config.Labels "com.docker.compose.project"}}|'
                '{{index .Config.Labels "com.docker.compose.service"}}|'
                '{{index .Config.Labels "memspine.rm136.owner"}}|'
                "{{.State.Status}}|{{json .NetworkSettings.Ports}}",
                ids[0],
            ]
        )
        .stdout.strip()
        .split("|", 4)
    )
    if len(inspected) != 5:
        raise RuntimeError("owned PostgreSQL container inspection was invalid")
    bound = json.loads(inspected[4]).get("5432/tcp")
    if inspected[:4] != [project, "postgres", owner, "running"] or bound != [
        {"HostIp": "127.0.0.1", "HostPort": str(port)}
    ]:
        raise RuntimeError("owned PostgreSQL container does not match its loopback port")
    return port, str(state["password"]), project, owner


@pytest.fixture
def scratch_postgres() -> Iterator[PgTarget]:
    """Create/drop only this test's unique database on the owned RM-136 server."""
    import psycopg

    port, password, project, owner = _owned_state()
    name = f"rm34_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(
        host="127.0.0.1",
        port=port,
        user="memspine_local",
        password=password,
        dbname="postgres",
        connect_timeout=3,
        autocommit=True,
    ) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        yield PgTarget(f"postgresql+psycopg://memspine_local:{password}@127.0.0.1:{port}/{name}")
    finally:
        _, current_password, _, _ = _owned_state((project, owner, port))
        with psycopg.connect(
            host="127.0.0.1",
            port=port,
            user="memspine_local",
            password=current_password,
            dbname="postgres",
            connect_timeout=3,
            autocommit=True,
        ) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@asynccontextmanager
async def _independent_stores(
    target: PgTarget,
) -> AsyncIterator[tuple[PostgresStorage, PostgresStorage]]:
    first_client = PostgresClient(PostgresSpec(target.url, connect_timeout_s=3))
    second_client = PostgresClient(PostgresSpec(target.url, connect_timeout_s=3))
    first, second = PostgresStorage(first_client), PostgresStorage(second_client)
    try:
        await first_client.open()
        await second_client.open()
        await first.start()
        yield first, second
    finally:
        try:
            await first.stop()
            await second.stop()
        finally:
            await first_client.close()
            await second_client.close()


async def _ordering_case(target: PgTarget, monkeypatch: pytest.MonkeyPatch) -> None:
    async with _independent_stores(target) as (first, second):
        allocated, release = asyncio.Event(), asyncio.Event()
        held_seq: int | None = None
        first_engine = first._client.engine
        original_begin = AsyncEngine.begin

        @asynccontextmanager
        async def hold_after_insert() -> AsyncIterator[Any]:
            nonlocal held_seq
            async with original_begin(first_engine) as conn:
                yield conn
                held_seq = int(
                    await conn.scalar(
                        text("SELECT currval(pg_get_serial_sequence('memory_events','seq'))")
                    )
                )
                allocated.set()
                await asyncio.wait_for(release.wait(), 8)

        def wrapped_begin(engine: AsyncEngine) -> Any:
            return hold_after_insert() if engine is first_engine else original_begin(engine)

        monkeypatch.setattr(AsyncEngine, "begin", wrapped_begin)
        older = MemoryEvent(kind=EventKind.FORGET, namespace="a", payload={"record_id": "older"})
        newer = MemoryEvent(kind=EventKind.FORGET, namespace="a", payload={"record_id": "newer"})
        task_a = asyncio.create_task(first.append_event(older))
        task_b: asyncio.Task[MemoryEvent] | None = None
        try:
            await asyncio.wait_for(allocated.wait(), 8)
            task_b = asyncio.create_task(second.append_event(newer))
            try:
                await asyncio.wait_for(asyncio.shield(task_b), 1.5)
            except TimeoutError:
                pass  # correct: B waits for A's transaction-wide append guard
            visible, head = await second.read_scoped_events(
                "a", [EventKind.FORGET], after_seq=0, limit=3
            )
            assert visible == [] and head == 0  # B cannot certify beyond A's open seq
        finally:
            release.set()
            await asyncio.wait_for(task_a, 8)
            if task_b is not None:
                await asyncio.wait_for(task_b, 8)
        assert held_seq == task_a.result().seq
        assert task_b is not None
        a_seq, b_seq = task_a.result().seq, task_b.result().seq
        assert a_seq is not None and b_seq is not None
        assert a_seq < b_seq
        visible, head = await second.read_scoped_events(
            "a", [EventKind.FORGET], after_seq=0, limit=3
        )
        assert [event.payload["record_id"] for event in visible] == ["older", "newer"]
        assert head == task_b.result().seq


def test_pg_global_commit_order_precedes_scoped_head(
    scratch_postgres: PgTarget, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(
        _ordering_case(scratch_postgres, monkeypatch), loop_factory=asyncio.SelectorEventLoop
    )


async def _mixed_path_case(target: PgTarget, monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    async with _independent_stores(target) as (first, second):
        allocated, release = asyncio.Event(), asyncio.Event()
        first_engine = first._client.engine
        original_begin = AsyncEngine.begin

        @asynccontextmanager
        async def hold_after_insert() -> AsyncIterator[Any]:
            async with original_begin(first_engine) as conn:
                yield conn
                allocated.set()
                await asyncio.wait_for(release.wait(), 8)

        def wrapped_begin(engine: AsyncEngine) -> Any:
            return hold_after_insert() if engine is first_engine else original_begin(engine)

        monkeypatch.setattr(AsyncEngine, "begin", wrapped_begin)
        task_a = asyncio.create_task(
            first.append_event(
                MemoryEvent(kind=EventKind.FORGET, namespace="a", payload={"record_id": "first"})
            )
        )
        task_b: asyncio.Task[Any] | None = None
        try:
            await asyncio.wait_for(allocated.wait(), 8)
            if path == "outbox":
                task_b = asyncio.create_task(
                    second.append_with_outbox(
                        MemoryEvent(
                            kind=EventKind.FORGET, namespace="a", payload={"record_id": "outbox"}
                        ),
                        [],
                    )
                )
            elif path == "registry":
                task_b = asyncio.create_task(
                    second.append_registry_marker(
                        MemoryEvent(
                            kind=EventKind.MARKER,
                            namespace="a",
                            payload={"marker": "entity.pending"},
                        )
                    )
                )
            elif path == "fence":
                task_b = asyncio.create_task(second.begin_registry_erasure("a", ["source-id"]))
            else:
                task_b = asyncio.create_task(
                    second._append_replayed_event(
                        MemoryEvent(
                            kind=EventKind.FORGET, namespace="a", payload={"record_id": "replay"}
                        )
                    )
                )
            try:
                await asyncio.wait_for(asyncio.shield(task_b), 1.0)
            except TimeoutError:
                pass
            assert not task_b.done(), f"{path} allocated past an uncommitted lower seq"
            events, head = await second.read_scoped_events(
                "a", [EventKind.FORGET, EventKind.MARKER], after_seq=0, limit=3
            )
            assert events == [] and head == 0
        finally:
            release.set()
            await asyncio.wait_for(task_a, 8)
            if task_b is not None:
                await asyncio.wait_for(task_b, 8)
        events, head = await second.read_scoped_events(
            "a",
            [EventKind.FORGET, EventKind.MARKER, EventKind.REGISTRY_ERASURE_FENCE],
            after_seq=0,
            limit=3,
        )
        assert [event.seq for event in events] == [task_a.result().seq, head]


@pytest.mark.parametrize("path", ["outbox", "registry", "fence", "replay"])
def test_pg_other_append_paths_obey_global_commit_order(
    scratch_postgres: PgTarget, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    asyncio.run(
        _mixed_path_case(scratch_postgres, monkeypatch, path),
        loop_factory=asyncio.SelectorEventLoop,
    )


async def _rollback_case(target: PgTarget, monkeypatch: pytest.MonkeyPatch) -> None:
    async with _independent_stores(target) as (first, second):
        allocated, release = asyncio.Event(), asyncio.Event()
        held_seq: int | None = None
        first_engine = first._client.engine
        original_begin = AsyncEngine.begin

        @asynccontextmanager
        async def hold_after_insert() -> AsyncIterator[Any]:
            nonlocal held_seq
            async with original_begin(first_engine) as conn:
                yield conn
                held_seq = int(
                    await conn.scalar(
                        text("SELECT currval(pg_get_serial_sequence('memory_events','seq'))")
                    )
                )
                allocated.set()
                await release.wait()

        def wrapped_begin(engine: AsyncEngine) -> Any:
            return hold_after_insert() if engine is first_engine else original_begin(engine)

        monkeypatch.setattr(AsyncEngine, "begin", wrapped_begin)
        task_a = asyncio.create_task(
            first.append_event(
                MemoryEvent(
                    kind=EventKind.FORGET, namespace="a", payload={"record_id": "rolled-back"}
                )
            )
        )
        task_b: asyncio.Task[MemoryEvent] | None = None
        try:
            await asyncio.wait_for(allocated.wait(), 8)
            task_b = asyncio.create_task(
                second.append_event(
                    MemoryEvent(
                        kind=EventKind.FORGET, namespace="a", payload={"record_id": "survived"}
                    )
                )
            )
            try:
                await asyncio.wait_for(asyncio.shield(task_b), 1.0)
            except TimeoutError:
                pass
            assert not task_b.done()
            task_a.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task_a, 8)
            committed = await asyncio.wait_for(task_b, 8)
            assert held_seq is not None and committed.seq is not None
            assert committed.seq > held_seq  # rolled-back sequence gap is lawful
            events, head = await second.read_scoped_events(
                "a", [EventKind.FORGET], after_seq=0, limit=3
            )
            assert [event.payload["record_id"] for event in events] == ["survived"]
            assert head == committed.seq
        finally:
            release.set()
            if not task_a.done():
                task_a.cancel()
            await asyncio.gather(task_a, return_exceptions=True)
            if task_b is not None:
                if not task_b.done():
                    task_b.cancel()
                await asyncio.gather(task_b, return_exceptions=True)

    # A fresh client sees the same committed head after both original connections close.
    reopened_client = PostgresClient(PostgresSpec(target.url, connect_timeout_s=3))
    reopened = PostgresStorage(reopened_client)
    try:
        await reopened_client.open()
        await reopened.start()
        events, head = await reopened.read_scoped_events(
            "a", [EventKind.FORGET], after_seq=0, limit=3
        )
        assert len(events) == 1 and events[0].payload["record_id"] == "survived"
        assert head == events[0].seq
    finally:
        try:
            await reopened.stop()
        finally:
            await reopened_client.close()


def test_pg_cancel_rolls_back_and_reopened_head_is_safe(
    scratch_postgres: PgTarget, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(
        _rollback_case(scratch_postgres, monkeypatch), loop_factory=asyncio.SelectorEventLoop
    )
