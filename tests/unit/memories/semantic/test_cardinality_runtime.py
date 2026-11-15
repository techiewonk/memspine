"""RM-45 canonical identity checks before the storage write door."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from memspine.clients.sqlite import SQLiteClient, SQLiteSpec
from memspine.read.pipeline.cardinality_overlay import overlay_result
from memspine.read.timeview import TimeView
from memspine.read.timeview import apply as timeview_apply
from memspine.schemas.durable.assessment import Assessment
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import Evidence, TurnRef
from memspine.schemas.durable.identity import AssertionKey
from memspine.schemas.durable.semantic import Mention, PredicateTyping, SemanticEnvelope
from memspine.schemas.durable.temporal import TimeAxes
from memspine.semantics.registry import load_builtin_registry
from memspine.services.storage.projector import RecordProjector
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.store.claim_cardinality import (
    UnknownRegistryDigest,
    group_of,
    record_group_of,
    value_token,
)
from memspine.store.claim_index import claim_cardinality_flag, claim_predicate, claim_subject_token
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord, RecordStatus
from memspine.store.replay import catch_up
from memspine.store.visibility_policy import audience_key
from memspine.trust.visibility import visible_to


def _claim(
    *,
    value: str = "Lisbon",
    owner: str = "person-a",
    subject_id: str | None = "entity-a",
    precision: str = "city",
) -> SemanticEnvelope:
    registry = load_builtin_registry()
    return SemanticEnvelope(
        write_id=f"write-{value}",
        assertion_key=AssertionKey(src="mira", rel="born_in", dst=value, kind="state"),
        occurrence_id=f"occ-{value}",
        scope=OwnerScope(tenant="tenant-a", owner=owner),
        text=f"Mira was born in {value}",
        kind="state",
        subject=Mention(surface="Mira", mention_id="mira-mention", entity_id=subject_id),
        attribute="born_in",
        value=value,
        evidence=[
            Evidence(
                role="support",
                turn=TurnRef(source_id="source-a", source_version=1, seg_index=0),
                source_family="Mira",
            )
        ],
        assessment=Assessment(source_trust=0.8, binding="bound", modality="asserted"),
        src_class="user_stated",
        registry=PredicateTyping(
            predicate="born_in",
            predicate_version=registry.predicates["born_in"].version,
            registry_digest=registry.digest,
            subject_type="person",
            object_kind="entity",
            object_entity_type="place",
            cardinality="one",
            cardinality_scope={"precision": precision},
        ),
    )


def _group(env: SemanticEnvelope, namespace: str):
    return group_of(env, namespace, audience=("default",))


def test_declared_scope_and_owner_boundaries_are_exact() -> None:
    a = _claim()
    group = _group(a, "ns-a")
    assert group is not None
    assert group == _group(_claim(value="Porto"), "ns-a")
    assert group != _group(_claim(owner="person-b"), "ns-a")
    assert group != _group(_claim(precision="country"), "ns-a")
    assert group != _group(
        a.model_copy(
            update={
                "registry": a.registry.model_copy(
                    update={"cardinality_scope": {"precision": "city", "context": "fiction"}}
                )
            }
        ),
        "ns-a",
    )
    assert group != _group(_claim(subject_id=None), "ns-a")
    assert group != _group(a, "ns-b")
    assert _group(a.model_copy(update={"status": "hidden"}), "ns-a") is None
    assert (
        _group(
            a.model_copy(
                update={"assessment": a.assessment.model_copy(update={"modality": "reported"})}
            ),
            "ns-a",
        )
        is None
    )
    assert (
        _group(
            a.model_copy(
                update={"registry": a.registry.model_copy(update={"cardinality_scope": {}})}
            ),
            "ns-a",
        )
        is None
    )
    with pytest.raises(UnknownRegistryDigest):
        _group(
            a.model_copy(
                update={"registry": a.registry.model_copy(update={"registry_digest": "f" * 64})}
            ),
            "ns-a",
        )
    assert (
        _group(
            a.model_copy(
                update={"registry": a.registry.model_copy(update={"subject_type": "place"})}
            ),
            "ns-a",
        )
        is None
    )
    many = a.model_copy(
        update={
            "attribute": "resides_in",
            "registry": a.registry.model_copy(
                update={"predicate": "resides_in", "cardinality": "many", "cardinality_scope": {}}
            ),
        }
    )
    assert _group(many, "ns-a") is None


def test_typed_values_keep_entity_and_property_units_distinct() -> None:
    a = _claim()
    assert value_token(a) == value_token(_claim(value="lisbon"))
    assert value_token(a) != value_token(_claim(value="Porto"))
    resolved = a.model_copy(
        update={"object": Mention(surface="Lisbon", mention_id="place-m", entity_id="place-id")}
    )
    assert value_token(resolved) != value_token(a)
    assert a.registry is not None
    numerical = a.model_copy(
        update={
            "registry": a.registry.model_copy(
                update={
                    "object_kind": "number",
                    "attribute_key": "height",
                    "amount": Decimal("2.0"),
                    "unit": "m",
                }
            )
        }
    )
    assert value_token(numerical) == value_token(
        numerical.model_copy(
            update={"registry": numerical.registry.model_copy(update={"amount": Decimal("2.00")})}
        )
    )
    assert value_token(numerical) != value_token(
        numerical.model_copy(
            update={"registry": numerical.registry.model_copy(update={"unit": "cm"})}
        )
    )
    assert value_token(numerical) != value_token(
        numerical.model_copy(
            update={"registry": numerical.registry.model_copy(update={"attribute_key": "width"})}
        )
    )


def test_projection_tokens_rebuild_from_the_canonical_envelope() -> None:
    env = _claim()
    raw = env.model_dump(mode="json")
    assert claim_subject_token(raw) == _group(env, "ns-a").subject
    assert claim_predicate(raw) == "born_in"
    assert not claim_cardinality_flag(raw)
    assert claim_cardinality_flag({**raw, "cardinality_conflict": True})


def test_visibility_audience_is_part_of_comparable_group() -> None:
    base = _record("Lisbon")
    private = base.model_copy(
        update={
            "tags": ["visibility:private"],
            "axes": base.axes.model_copy(update={"participants": ["Alice", "Bob"]}),
        }
    )
    participants = base.model_copy(
        update={
            "tags": ["visibility:participants"],
            "axes": base.axes.model_copy(update={"participants": ["Alice", "Bob"]}),
        }
    )
    owner_shared = base.model_copy(update={"tags": ["visibility:owner_shared"]})
    assert visible_to(private, "Alice") and not visible_to(private, "Bob")
    assert visible_to(participants, "Alice") and visible_to(participants, "Bob")
    assert visible_to(owner_shared, "Carla")
    assert audience_key(private) == ("private", "alice")
    assert audience_key(participants) == ("participants", "alice", "bob")
    assert len({record_group_of(record) for record in (private, participants, owner_shared)}) == 3


def test_only_current_affirmative_supported_records_enter_a_group() -> None:
    base = _record("Lisbon")
    assert record_group_of(base) is not None
    assert record_group_of(base.model_copy(update={"tier": "cold"})) is not None
    assert record_group_of(base.model_copy(update={"status": RecordStatus.ARCHIVED})) is None
    assert record_group_of(base.model_copy(update={"quarantined": True})) is None
    assert record_group_of(base.model_copy(update={"instruction_flag": True})) is None
    assert (
        record_group_of(base.model_copy(update={"valid_to": datetime(2026, 1, 1, tzinfo=UTC)}))
        is None
    )
    assert (
        record_group_of(base.model_copy(update={"superseded_at": datetime(2026, 1, 1, tzinfo=UTC)}))
        is None
    )
    assert record_group_of(base.model_copy(update={"tags": ["retract"]})) is None
    env = _claim()
    negated = env.model_copy(
        update={"assessment": env.assessment.model_copy(update={"modality": "negated"})}
    )
    assert group_of(negated, "ns-a", audience=("default",)) is None


def test_known_time_view_does_not_reveal_later_cardinality_rival() -> None:
    early = _claim(value="Lisbon").model_copy(
        update={
            "cardinality_conflict": True,
            "time": TimeAxes(recorded_at=datetime(2026, 1, 1, tzinfo=UTC)),
        }
    )
    late = _claim(value="Porto").model_copy(
        update={
            "cardinality_conflict": True,
            "time": TimeAxes(recorded_at=datetime(2026, 2, 1, tzinfo=UTC)),
        }
    )
    result = timeview_apply(TimeView.at_known(datetime(2026, 1, 15, tzinfo=UTC)), [early, late])
    assert len(result.hits) == 1
    assert result.hits[0].assertion.value == "Lisbon"
    assert not result.hits[0].assertion.cardinality_conflict

    def rederive(known: list[SemanticEnvelope]) -> list[SemanticEnvelope]:
        # The caller supplies the complete visibility-filtered known population.
        values = {
            value_token(env)
            for env in known
            if group_of(env, "ns-a", audience=("default",)) is not None
        }
        conflict = len(values) > 1
        return [env.model_copy(update={"cardinality_conflict": conflict}) for env in known]

    both_known = timeview_apply(
        TimeView.at_known(datetime(2026, 2, 15, tzinfo=UTC)), [early, late], rederive
    )
    assert len(both_known.hits) == 2
    assert all(hit.assertion.cardinality_conflict for hit in both_known.hits)


def _record(value: str) -> MemoryRecord:
    env = _claim(value=value)
    return MemoryRecord(
        namespace="ns-a",
        memory_type="semantic",
        content=env.text,
        entity="Mira",
        attribute="born_in",
        fingerprint={"envelope": env.model_dump(mode="json")},
    )


async def test_public_group_overlay_uses_one_snapshot_over_256_rows() -> None:
    # A page-at-a-time read could see 256 Lisbon rows, then one Porto row after
    # an A→B group rewrite even though the two states never coexisted. The
    # public overlay must request one coherent native group snapshot instead.
    old_state = [_record("Lisbon") for _ in range(257)]
    later_state = [_record("Porto")]

    class Store:
        snapshot_calls = 0
        page_calls = 0

        async def claim_group_snapshot(self, namespace: str, group_key: str):
            self.snapshot_calls += 1
            return old_state

        async def claim_group_page(self, namespace: str, group_key: str, **kwargs):
            self.page_calls += 1
            return old_state[:256] if self.page_calls == 1 else later_state

    class EngineView:
        def __init__(self) -> None:
            self.storage = Store()

        def _require_started(self):
            return self.storage

        def _consent_ok(self, record: MemoryRecord) -> bool:
            return True

    eng = EngineView()
    public = await overlay_result(eng, old_state[0], viewer=None, cache={})
    assert not public.fingerprint.envelope["cardinality_conflict"]
    assert eng.storage.snapshot_calls == 1 and eng.storage.page_calls == 0


async def test_claim_flag_rebuilds_after_append_before_projection() -> None:
    client = SQLiteClient(SQLiteSpec(":memory:"))
    await client.open()
    storage = SQLiteStorage(client)
    await storage.start()
    projector = RecordProjector(storage)
    try:
        records = [_record("Lisbon"), _record("Porto")]
        for record in records:
            event = await storage.append_event(
                MemoryEvent(
                    kind=EventKind.WRITE,
                    namespace="ns-a",
                    actor="system",
                    payload={"record": record.model_dump(mode="json")},
                )
            )
            if record is records[0]:
                await projector.apply(event)
        # Simulate cancellation after the second canonical append. Catch-up, not
        # the original writer, must derive the visible conflict on both claims.
        assert await catch_up(storage, [projector]) == {"records": 1}
        stored = [await storage.get_record(record.record_id) for record in records]
        assert all(
            row is not None and row.fingerprint.envelope["cardinality_conflict"] for row in stored
        )
    finally:
        await storage.stop()
        await client.close()


async def test_same_typed_value_from_distinct_sources_corroborates_without_conflict() -> None:
    client = SQLiteClient(SQLiteSpec(":memory:"))
    await client.open()
    storage = SQLiteStorage(client)
    await storage.start()
    projector = RecordProjector(storage)
    try:
        first, second = _record("Lisbon"), _record("Lisbon")
        raw = dict(second.fingerprint.envelope)
        raw["evidence"] = [
            {
                **raw["evidence"][0],
                "source_family": "other-family",
                "turn": {**raw["evidence"][0]["turn"], "source_id": "other-source"},
            }
        ]
        second = second.model_copy(
            update={"fingerprint": second.fingerprint.model_copy(update={"envelope": raw})}
        )
        for row in (first, second):
            event = await storage.append_event(
                MemoryEvent(
                    kind=EventKind.WRITE,
                    namespace="ns-a",
                    actor="system",
                    payload={"record": row.model_dump(mode="json")},
                )
            )
            await projector.apply(event)
        stored = [await storage.get_record(row.record_id) for row in (first, second)]
        assert all(
            row is not None and not row.fingerprint.envelope["cardinality_conflict"]
            for row in stored
        )
        assert {
            row.fingerprint.envelope["evidence"][0]["turn"]["source_id"]
            for row in stored
            if row is not None
        } == {"source-a", "other-source"}
    finally:
        await storage.stop()
        await client.close()


async def test_claim_projection_converges_when_peer_engines_apply_out_of_order() -> None:
    client = SQLiteClient(SQLiteSpec(":memory:"))
    await client.open()
    storage = SQLiteStorage(client)
    await storage.start()
    projector = RecordProjector(storage)
    try:
        records = [_record("Lisbon"), _record("Porto")]
        events = [
            await storage.append_event(
                MemoryEvent(
                    kind=EventKind.WRITE,
                    namespace="ns-a",
                    actor="system",
                    payload={"record": record.model_dump(mode="json")},
                )
            )
            for record in records
        ]
        for event in reversed(events):
            await projector.apply(event)
        stored = [await storage.get_record(record.record_id) for record in records]
        assert all(
            row is not None and row.fingerprint.envelope["cardinality_conflict"] for row in stored
        )
    finally:
        await storage.stop()
        await client.close()


async def test_claim_projection_clears_only_current_flag_after_forget() -> None:
    client = SQLiteClient(SQLiteSpec(":memory:"))
    await client.open()
    storage = SQLiteStorage(client)
    await storage.start()
    projector = RecordProjector(storage)
    try:
        first, second = _record("Lisbon"), _record("Porto")
        for record in (first, second):
            event = await storage.append_event(
                MemoryEvent(
                    kind=EventKind.WRITE,
                    namespace="ns-a",
                    actor="system",
                    payload={"record": record.model_dump(mode="json")},
                )
            )
            await projector.apply(event)
        stored_first = await storage.get_record(first.record_id)
        assert (
            stored_first is not None and stored_first.fingerprint.envelope["cardinality_conflict"]
        )
        forgotten = await storage.append_event(
            MemoryEvent(
                kind=EventKind.FORGET,
                namespace="ns-a",
                actor="user",
                payload={"record_id": second.record_id, "hard": True},
            )
        )
        await projector.apply(forgotten)
        survivor = await storage.get_record(first.record_id)
        assert survivor is not None and not survivor.fingerprint.envelope["cardinality_conflict"]
        assert await storage.get_record(second.record_id) is None
    finally:
        await storage.stop()
        await client.close()


@pytest.mark.parametrize(
    "delta",
    [
        {"status": "archived"},
        {"quarantined": True},
        {"valid_to": "2026-01-01T00:00:00Z"},
    ],
)
async def test_departed_claim_and_survivor_clear_derived_flag(delta: dict[str, object]) -> None:
    client = SQLiteClient(SQLiteSpec(":memory:"))
    await client.open()
    storage = SQLiteStorage(client)
    await storage.start()
    projector = RecordProjector(storage)
    try:
        first, second = _record("Lisbon"), _record("Porto")
        for record in (first, second):
            event = await storage.append_event(
                MemoryEvent(
                    kind=EventKind.WRITE,
                    namespace="ns-a",
                    actor="system",
                    payload={"record": record.model_dump(mode="json")},
                )
            )
            await projector.apply(event)
        assert (await storage.get_record(first.record_id)).fingerprint.envelope[  # type: ignore[union-attr,index]
            "cardinality_conflict"
        ]
        transition = await storage.append_event(
            MemoryEvent(
                kind=EventKind.DECAY_TRANSITION,
                namespace="ns-a",
                actor="system",
                payload={"record_id": second.record_id, "set": delta},
            )
        )
        await projector.apply(transition)
        stored = [await storage.get_record(record.record_id) for record in (first, second)]
        assert all(
            row is not None and not row.fingerprint.envelope["cardinality_conflict"]
            for row in stored
        )
    finally:
        await storage.stop()
        await client.close()


async def test_claim_projection_clears_flag_after_supported_correction() -> None:
    client = SQLiteClient(SQLiteSpec(":memory:"))
    await client.open()
    storage = SQLiteStorage(client)
    await storage.start()
    projector = RecordProjector(storage)
    try:
        first, second = _record("Lisbon"), _record("Porto")
        for record in (first, second):
            event = await storage.append_event(
                MemoryEvent(
                    kind=EventKind.WRITE,
                    namespace="ns-a",
                    actor="system",
                    payload={"record": record.model_dump(mode="json")},
                )
            )
            await projector.apply(event)
        current = await storage.get_record(second.record_id)
        assert current is not None and current.fingerprint.envelope["cardinality_conflict"]
        closed = current.model_copy(
            update={"status": RecordStatus.ARCHIVED, "valid_to": current.valid_from}
        )
        event = await storage.append_event(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace="ns-a",
                actor="system",
                payload={"record": closed.model_dump(mode="json")},
            )
        )
        await projector.apply(event)
        survivor = await storage.get_record(first.record_id)
        archived = await storage.get_record(second.record_id)
        assert survivor is not None and not survivor.fingerprint.envelope["cardinality_conflict"]
        assert archived is not None and not archived.fingerprint.envelope["cardinality_conflict"]
    finally:
        await storage.stop()
        await client.close()


async def test_reordered_same_version_write_keeps_newer_claim_snapshot() -> None:
    client = SQLiteClient(SQLiteSpec(":memory:"))
    await client.open()
    storage = SQLiteStorage(client)
    await storage.start()
    projector = RecordProjector(storage)
    try:
        old = _record("Lisbon")
        newer = old.model_copy(
            update={
                "content": "Mira was born in Porto",
                "fingerprint": old.fingerprint.model_copy(
                    update={"envelope": _claim(value="Porto").model_dump(mode="json")}
                ),
            }
        )
        assert old.version == newer.version  # metadata/lifecycle rewrites may share a version
        first = await storage.append_event(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace="ns-a",
                actor="system",
                payload={"record": old.model_dump(mode="json")},
            )
        )
        second = await storage.append_event(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace="ns-a",
                actor="system",
                payload={"record": newer.model_dump(mode="json")},
            )
        )
        await projector.apply(second)
        await projector.apply(first)
        stored = await storage.get_record(old.record_id)
        assert stored is not None and stored.content == newer.content
        assert stored.fingerprint.envelope["value"] == "Porto"
    finally:
        await storage.stop()
        await client.close()


async def test_replayed_old_write_cannot_resurrect_hard_forgotten_claim() -> None:
    client = SQLiteClient(SQLiteSpec(":memory:"))
    await client.open()
    storage = SQLiteStorage(client)
    await storage.start()
    projector = RecordProjector(storage)
    try:
        record = _record("Lisbon")
        written = await storage.append_event(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace="ns-a",
                actor="system",
                payload={"record": record.model_dump(mode="json")},
            )
        )
        forgotten = await storage.append_event(
            MemoryEvent(
                kind=EventKind.FORGET,
                namespace="ns-a",
                actor="user",
                payload={"record_id": record.record_id, "hard": True},
            )
        )
        await projector.apply(written)
        await projector.apply(forgotten)
        await projector.apply(written)  # stale peer Engine or retry after the hard FORGET
        assert await storage.get_record(record.record_id) is None
    finally:
        await storage.stop()
        await client.close()
