from __future__ import annotations

from datetime import UTC, datetime

import pytest

from memspine.providers.projection_backend import (
    EventLogProjectionBackend,
    graph_projector,
    lexical_projector,
    vector_projector,
)
from memspine.providers.switching import (
    CleanupEvidence,
    GenerationStatus,
    ProjectionGeneration,
    ProjectionVerificationEvidence,
)
from memspine.services.graph.base import GraphEdge, GraphNode
from memspine.services.lexical.base import LexicalHit
from memspine.services.vector.base import VectorHit
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.projections.projector import Projector
from memspine.store.records import MemoryRecord


class EventLog:
    can_rebuild = True

    def __init__(self, events: list[MemoryEvent]) -> None:
        self.events = events

    async def read_events(self, after_seq: int = 0, limit: int = 1000) -> list[MemoryEvent]:
        return [event for event in self.events if (event.seq or 0) > after_seq][:limit]


class RecordingProjector(Projector):
    name = "test"

    def __init__(self) -> None:
        self.sequences: list[int] = []

    async def apply(self, event: MemoryEvent) -> None:
        assert event.seq is not None
        self.sequences.append(event.seq)

    async def reset(self) -> None:
        self.sequences.clear()


class Handle:
    def __init__(self, projector: Projector) -> None:
        self.projector = projector
        self.closed = 0

    async def verify(self, *, source, target, source_head_seq, erase_checkpoint_seq):
        return ProjectionVerificationEvidence(
            report_id="verified",
            source_head_seq=source_head_seq,
            target_checkpoint_seq=source_head_seq,
            erase_checkpoint_seq=erase_checkpoint_seq,
            owner_id=target.owner_id,
            target_fingerprint=target.target_fingerprint,
            successful_probes=("backend-test",),
        )

    async def activate(self, **kwargs) -> None:
        return None

    async def freeze(self, generation) -> None:
        return None

    async def has_live_references(self, generation) -> bool:
        return False

    async def delete_owned(self, generation) -> CleanupEvidence:
        return CleanupEvidence(
            report_id="deleted",
            owner_id=generation.owner_id,
            generation_id=generation.generation_id,
            target_fingerprint=generation.target_fingerprint,
            deleted=True,
            remaining_references=0,
        )

    async def close(self) -> None:
        self.closed += 1


class SwitchStore:
    def __init__(self) -> None:
        self.generations: dict[tuple[str, str, str], ProjectionGeneration] = {}

    async def get_generation(
        self, deployment_id: str, logical_service_id: str, generation_id: str
    ) -> ProjectionGeneration:
        return self.generations[(deployment_id, logical_service_id, generation_id)]

    async def save_generation(
        self, generation: ProjectionGeneration, *, expected_revision: int | None
    ) -> None:
        key = (generation.deployment_id, generation.logical_service_id, generation.generation_id)
        previous = self.generations.get(key)
        revision = previous.revision if previous is not None else None
        if revision != expected_revision:
            raise RuntimeError("stale generation revision")
        self.generations[key] = generation.model_copy(
            update={"revision": 1 if revision is None else revision + 1}
        )


class ProjectionPorts:
    def __init__(self) -> None:
        self.graph_nodes: list[str] = []
        self.vector_rows: list[tuple[str, str, str, list[float]]] = []
        self.lexical_rows: list[str] = []
        self.deleted: list[str] = []
        self.vector_upserts: list[str] = []
        self.lexical_indexes: list[str] = []

    async def upsert_node(self, node_id, labels=(), properties=None, *, namespace=None) -> None:
        self.graph_nodes.append(node_id)

    async def upsert_edge(self, src, dst, rel_type, properties=None, *, namespace=None) -> None:
        return None

    async def neighbors(self, *args, **kwargs) -> list[GraphNode]:
        return []

    async def subgraph(self, *args, **kwargs) -> list[GraphEdge]:
        return []

    async def edges_of(self, node_id: str) -> list[GraphEdge]:
        return []

    async def delete_node(self, node_id: str) -> None:
        self.deleted.append(node_id)
        self.graph_nodes = [item for item in self.graph_nodes if item != node_id]

    async def edge_list(self, namespace=None) -> list[GraphEdge]:
        return []

    async def node_count(self) -> int:
        return len(self.graph_nodes)

    async def edge_count(self) -> int:
        return 0

    async def upsert(self, record_id, namespace, embedder_id, vector) -> None:
        self.vector_upserts.append(record_id)
        self.vector_rows.append((record_id, namespace, embedder_id, vector))

    async def query(self, *args, **kwargs) -> list[VectorHit]:
        return []

    async def search_rescore(self, *args, **kwargs) -> list[VectorHit]:
        return []

    async def delete(self, record_id: str) -> None:
        self.deleted.append(record_id)
        self.vector_rows = [row for row in self.vector_rows if row[0] != record_id]
        self.lexical_rows = [item for item in self.lexical_rows if item != record_id]

    async def delete_all(self) -> None:
        self.vector_rows.clear()

    async def exists(self, record_id: str) -> bool:
        return any(row[0] == record_id for row in self.vector_rows)

    async def index(self, record: MemoryRecord) -> None:
        self.lexical_indexes.append(record.record_id)
        self.lexical_rows.append(record.record_id)

    async def index_many(self, records) -> None:
        for record in records:
            await self.index(record)

    async def search(self, namespace, query, top_k=8) -> list[LexicalHit]:
        return []

    async def clear(self) -> None:
        self.graph_nodes.clear()
        self.vector_rows.clear()
        self.lexical_rows.clear()

    async def close(self) -> None:
        return None


class Embedder:
    embedder_id = "test-embedder"
    dim = 2
    manifest = None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, float(len(text))] for text in texts]


def _generation(generation_id: str = "g2") -> ProjectionGeneration:
    return ProjectionGeneration(
        deployment_id="deployment-a",
        logical_service_id="vector",
        generation_id=generation_id,
        provider="lancedb",
        target_fingerprint="config-v2",
        owner_id="deployment-a:vector",
        status=GenerationStatus.BUILDING,
        updated_at=datetime.now(UTC),
    )


def _events(count: int) -> list[MemoryEvent]:
    return [
        MemoryEvent(
            seq=seq,
            kind=EventKind.MARKER,
            namespace="default",
            payload={"marker": str(seq)},
        )
        for seq in range(1, count + 1)
    ]


def _write_and_forget() -> list[MemoryEvent]:
    record = MemoryRecord(namespace="tenant-a", memory_type="episodic", content="hello")
    return [
        MemoryEvent(
            seq=1,
            kind=EventKind.WRITE,
            namespace=record.namespace,
            payload={"record": record.model_dump(mode="json")},
        ),
        MemoryEvent(
            seq=2,
            kind=EventKind.FORGET,
            namespace=record.namespace,
            payload={"record_id": record.record_id, "hard": True},
        ),
    ]


@pytest.mark.asyncio
async def test_event_log_adapter_captures_full_head_and_replays_exact_batches() -> None:
    projector = RecordingProjector()
    handles: list[Handle] = []

    async def open_generation(generation: ProjectionGeneration) -> Handle:
        handle = Handle(projector)
        handles.append(handle)
        return handle

    stopped: list[tuple[str, int]] = []

    async def stop(deployment_id: str, epoch: int) -> None:
        stopped.append((deployment_id, epoch))

    backend = EventLogProjectionBackend(
        category="vector",
        event_log=EventLog(_events(5)),
        switch_store=SwitchStore(),
        open_generation=open_generation,
        stop_admission=stop,
        read_batch_size=2,
    )
    snapshot = await backend.capture_snapshot("deployment-a")
    assert snapshot.complete and snapshot.head_seq == 5 and snapshot.checkpoint_seq == 0
    await backend.stop_admission("deployment-a", 7)
    target = _generation()
    await backend.prepare_owned_target(target)
    assert await backend.restore_snapshot(snapshot.snapshot_id, target) == 0
    first = await backend.replay_batch(
        deployment_id="deployment-a",
        source_generation="g1",
        target=target,
        after_seq=0,
        through_seq=5,
        max_events=2,
    )
    second = await backend.replay_batch(
        deployment_id="deployment-a",
        source_generation="g1",
        target=target,
        after_seq=first.source_checkpoint_seq,
        through_seq=5,
        max_events=2,
    )
    third = await backend.replay_batch(
        deployment_id="deployment-a",
        source_generation="g1",
        target=target,
        after_seq=second.source_checkpoint_seq,
        through_seq=5,
        max_events=2,
    )
    assert projector.sequences == [1, 2, 3, 4, 5]
    assert [first.done, second.done, third.done] == [False, False, True]
    assert stopped == [("deployment-a", 7)]
    assert all(handle.closed == 1 for handle in handles)


@pytest.mark.asyncio
async def test_event_log_adapter_rejects_pruned_and_gapped_source_history() -> None:
    backend = EventLogProjectionBackend(
        category="graph",
        event_log=EventLog([_events(3)[1]]),
        switch_store=SwitchStore(),
        open_generation=lambda _generation: pytest.fail("must not open a target"),
        stop_admission=lambda _deployment, _epoch: pytest.fail("not called"),
        read_batch_size=10,
    )
    pruned = await backend.capture_snapshot("deployment-a")
    assert not pruned.complete
    assert pruned.missing_ranges == ("1..1",)

    backend = EventLogProjectionBackend(
        category="lexical",
        event_log=EventLog([_events(3)[0], _events(3)[2]]),
        switch_store=SwitchStore(),
        open_generation=lambda _generation: pytest.fail("must not open a target"),
        stop_admission=lambda _deployment, _epoch: pytest.fail("not called"),
        read_batch_size=10,
    )
    gap = await backend.capture_snapshot("deployment-a")
    assert not gap.complete
    assert gap.evidence_id == "event-log-sequence-gap"


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["graph", "vector", "lexical"])
async def test_adapter_replays_canonical_category_projector_write_and_erasure(
    category: str,
) -> None:
    ports = ProjectionPorts()
    embedder = Embedder()
    if category == "graph":
        projector = graph_projector(ports)  # type: ignore[arg-type]
    elif category == "vector":
        projector = vector_projector(ports, embedder)  # type: ignore[arg-type]
    else:
        projector = lexical_projector(ports)  # type: ignore[arg-type]
    handles: list[Handle] = []

    async def open_generation(_generation: ProjectionGeneration) -> Handle:
        handle = Handle(projector)
        handles.append(handle)
        return handle

    async def stop(_deployment: str, _epoch: int) -> None:
        return None

    backend = EventLogProjectionBackend(
        category=category,  # type: ignore[arg-type]
        event_log=EventLog(_write_and_forget()),
        switch_store=SwitchStore(),
        open_generation=open_generation,
        stop_admission=stop,
        read_batch_size=10,
    )
    target = _generation()
    snapshot = await backend.capture_snapshot("deployment-a")
    assert snapshot.complete
    await backend.prepare_owned_target(target)
    await backend.restore_snapshot(snapshot.snapshot_id, target)
    replay = await backend.replay_batch(
        deployment_id="deployment-a",
        source_generation="g1",
        target=target,
        after_seq=0,
        through_seq=snapshot.head_seq,
        max_events=10,
    )

    assert replay.done and replay.target_checkpoint_seq == 2
    assert len(ports.deleted) == 1
    if category == "graph":
        assert ports.graph_nodes == []
    elif category == "vector":
        assert len(ports.vector_upserts) == 1
        assert ports.vector_rows == []
    else:
        assert len(ports.lexical_indexes) == 1
        assert ports.lexical_rows == []
    assert all(handle.closed == 1 for handle in handles)


@pytest.mark.asyncio
async def test_retained_source_erasure_is_idempotent_and_durably_checkpointed() -> None:
    projector = RecordingProjector()
    handles: list[Handle] = []

    async def open_generation(generation: ProjectionGeneration) -> Handle:
        handle = Handle(projector)
        handles.append(handle)
        return handle

    store = SwitchStore()
    source = _generation("g1").model_copy(
        update={"status": GenerationStatus.RETAINED, "frozen": True}
    )
    await store.save_generation(source, expected_revision=None)
    erasure = MemoryEvent(
        seq=9,
        kind=EventKind.FORGET,
        namespace="default",
        payload={"record_id": "record-1", "hard": True},
    )
    backend = EventLogProjectionBackend(
        category="graph",
        event_log=EventLog([*_events(8), erasure]),
        switch_store=store,
        open_generation=open_generation,
        stop_admission=lambda _deployment, _epoch: pytest.fail("not called"),
        read_batch_size=10,
    )
    after = await backend.apply_retained_erasure(source, erasure)
    duplicate = await backend.apply_retained_erasure(source, erasure)
    assert after.erase_checkpoint_seq == 9
    assert duplicate.erase_checkpoint_seq == 9
    assert duplicate.revision == 2
    assert len(projector.sequences) == 1
    assert projector.sequences == [9]


@pytest.mark.asyncio
async def test_retained_source_erasure_cannot_skip_an_unapplied_forget() -> None:
    projector = RecordingProjector()
    source = _generation("g1").model_copy(
        update={"status": GenerationStatus.RETAINED, "frozen": True}
    )
    store = SwitchStore()
    await store.save_generation(source, expected_revision=None)
    first_forget = MemoryEvent(
        seq=2,
        kind=EventKind.FORGET,
        namespace="default",
        payload={"record_id": "first", "hard": True},
    )
    second_forget = MemoryEvent(
        seq=4,
        kind=EventKind.FORGET,
        namespace="default",
        payload={"record_id": "second", "hard": True},
    )
    event_log = EventLog([_events(1)[0], first_forget, _events(3)[2], second_forget])

    async def open_generation(_generation: ProjectionGeneration) -> Handle:
        return Handle(projector)

    backend = EventLogProjectionBackend(
        category="graph",
        event_log=event_log,
        switch_store=store,
        open_generation=open_generation,
        stop_admission=lambda _deployment, _epoch: pytest.fail("not called"),
        read_batch_size=2,
    )
    with pytest.raises(ValueError, match="next unapplied FORGET"):
        await backend.apply_retained_erasure(source, second_forget)
    assert projector.sequences == []

    await backend.apply_retained_erasure(source, first_forget)
    after = await backend.apply_retained_erasure(source, second_forget)
    assert after.erase_checkpoint_seq == 4
    assert projector.sequences == [2, 4]


def test_category_helpers_return_the_existing_canonical_projectors() -> None:
    class Graph:
        pass

    class Vector:
        pass

    class Lexical:
        pass

    class Embedder:
        pass

    assert graph_projector(Graph()).name == "graph"  # type: ignore[arg-type]
    assert vector_projector(Vector(), Embedder()).name == "vectors"  # type: ignore[arg-type]
    assert lexical_projector(Lexical()).name == "lexical"  # type: ignore[arg-type]
