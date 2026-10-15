"""F-02a: the record envelope (layer, type tag, provenance class, axes, fingerprint), its derivation
mapping, the 0007 migration (up, down, backfill), the write path and the envelope round trip."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest
from alembic import command

from memspine import Engine
from memspine.schemas.durable.assertion import AssertionEnvelope
from memspine.schemas.versioning import latest_version, upcast
from memspine.services.storage.sqlite.migrations import alembic_config, upgrade_to_head
from memspine.store.envelope import from_envelope, to_envelope, upcast_record_payload
from memspine.store.layers import derive_axes, derive_layer, derive_provenance_class
from memspine.store.records import MemoryRecord, RecordAxes, RecordStatus, SourceInfo

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)


def _rec(
    memory_type: str = "episodic",
    role: str = "user",
    channel: str = "message",
    parents: list[str] | None = None,
    **kw: object,
) -> MemoryRecord:
    return MemoryRecord(
        namespace="org/u1",
        memory_type=memory_type,
        content=f"{memory_type} content",
        source=SourceInfo(role=role, channel=channel, message_id="s1", parents=parents or []),
        **kw,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("memory_type", "channel", "layer"),
    [
        ("working", "internal", "L0"),
        ("episodic", "message", "L0"),
        ("resource", "ingest", "L0"),
        ("shared", "grant", "L0"),
        ("semantic", "message", "L1"),
        ("semantic", "mining", "L1"),
        ("semantic", "calibration", "L1"),
        ("semantic", "consolidation", "L2"),
        ("semantic", "window_episode", "L2"),
        ("semantic", "extract_graph", "L3"),
        ("semantic", "rule_edges", "L3"),
        ("semantic", "reorganize", "L3"),
        ("semantic", "entity_summary", "L3"),
        ("associative", "internal", "L3"),
        ("reflective", "reflection", "L4"),
        ("procedural", "internal", "L5"),
        ("prospective", "internal", "L5"),
        ("custom_type", "internal", "L0"),
    ],
)
def test_layer_mapping(memory_type: str, channel: str, layer: str) -> None:
    assert derive_layer(memory_type, channel) == layer
    record = _rec(memory_type, channel=channel)
    assert record.layer == layer
    assert record.type_tag == memory_type


@pytest.mark.parametrize(
    ("role", "channel", "parents", "expected"),
    [
        ("user", "message", False, "user_stated"),
        ("operator", "rest", False, "user_stated"),
        ("assistant", "message", False, "assistant_proposed"),
        ("tool", "message", False, "tool"),
        ("system", "internal", False, "tool"),
        ("document", "ingest", False, "document"),
        ("external", "ingest", False, "external"),
        ("mystery", "internal", False, "external"),
        ("assistant", "mining", False, "derived"),
        ("assistant", "extract_graph", True, "derived"),
        ("assistant", "message", True, "derived"),
    ],
)
def test_provenance_class_mapping(role: str, channel: str, parents: bool, expected: str) -> None:
    assert derive_provenance_class(role, channel, parents) == expected
    record = _rec("semantic", role, channel, parents=["p1"] if parents else [])
    assert record.provenance_class == expected


def test_axes_from_perspective_tags() -> None:
    axes = derive_axes(
        ["x", "spk:alice", "spk:bob", "addr:carol", "sub:dave", "sub:dave", "ask:erin"]
    )
    assert axes == {"speaker": "alice", "subject": ["dave"], "addressee": "carol", "asker": "erin"}
    record = _rec(tags=["spk:alice", "sub:dave", "ask:erin"])
    assert record.axes == RecordAxes(speaker="alice", subject=["dave"], asker="erin")


def test_fingerprint_defaults_and_explicit_values_survive() -> None:
    record = _rec()
    assert record.fingerprint.content == record.content_fingerprint
    assert record.schema_version == 1
    explicit = MemoryRecord(
        namespace="u",
        memory_type="semantic",
        content="c",
        layer="L2",
        provenance_class="user_confirmed",
        fingerprint={"model_id": "m1", "prompt_hash": "ph"},  # type: ignore[arg-type]
    )
    assert (explicit.layer, explicit.provenance_class) == ("L2", "user_confirmed")
    dumped = explicit.model_dump(mode="json")
    assert dumped["layer"] == "L2" and dumped["provenance_class"] == "user_confirmed"
    assert MemoryRecord.model_validate_json(explicit.model_dump_json()) == explicit


def test_dump_of_a_default_record_is_unchanged() -> None:
    """Derived envelope fields are left out of a dump, except ``layer``: MF-05 always writes it so a
    replay never depends on what ``derive_layer`` returns later."""
    dumped = _rec(tags=["spk:alice"]).model_dump(mode="json")
    for key in ("type_tag", "provenance_class", "axes", "fingerprint", "schema_version"):
        assert key not in dumped
    assert dumped["layer"] == "L0"
    revived = MemoryRecord.model_validate(dumped)
    assert revived.axes.speaker == "alice" and revived.layer == "L0"


# --------------------------------------------------------------------------- envelope round trip


@pytest.mark.parametrize(
    "record",
    [
        _rec(tags=["spk:alice", "ask:bob"]),
        _rec("semantic", "assistant", "mining", entity="alice", attribute="city", parents=["p1"]),
        _rec("reflective", "assistant", "reflection", parents=["p1", "p2"], trust=0.3),
        _rec("semantic", status=RecordStatus.ARCHIVED, corroborations=2),
    ],
)
def test_envelope_round_trip(record: MemoryRecord) -> None:
    envelope = to_envelope(record)
    assert isinstance(envelope, AssertionEnvelope)
    assert envelope.src_class == record.provenance_class
    assert envelope.record_id == record.record_id
    assert envelope.scope.owner == record.namespace and envelope.scope.tenant == "org"
    assert envelope.kind == record.type_tag
    again = AssertionEnvelope.model_validate_json(envelope.model_dump_json())
    assert from_envelope(again).model_dump() == record.model_dump()
    assert to_envelope(record).write_id == envelope.write_id  # deterministic


def test_from_envelope_without_the_lossless_copy() -> None:
    envelope = to_envelope(_rec("semantic", entity="alice", attribute="city"))
    bare = envelope.model_copy(update={"extras": {}})
    record = from_envelope(bare)
    assert (record.content, record.entity, record.attribute) == (envelope.text, "alice", "city")
    assert record.namespace == "org/u1" and record.provenance_class == envelope.src_class


# --------------------------------------------------------------------------------- upcaster


def test_record_kind_is_registered_at_v1() -> None:
    assert latest_version("memory_record") == 1
    assert upcast("memory_record", {"schema_version": 1, "a": 1}) == {"schema_version": 1, "a": 1}
    stamped = upcast_record_payload({"namespace": "u"})  # a pre-F-02a payload has no version
    assert stamped["schema_version"] == 1 and stamped["namespace"] == "u"


# ------------------------------------------------------------------- migration and write path


def _engine(path: Path) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": str(path)},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
    )


async def _seed(path: Path) -> None:
    eng = _engine(path)
    await eng.start()
    try:
        await eng.write_messages(
            [
                {"role": "user", "content": "we went camping by the lake"},
                {"role": "assistant", "content": "camping sounds fun"},
            ],
            namespace="u",
            session_id="s1",
            valid_from=T0,
        )
    finally:
        await eng.stop()


def _rows(path: Path, sql: str) -> list[tuple]:
    with sqlite3.connect(path) as db:
        return db.execute(sql + " ORDER BY content").fetchall()


def _columns(path: Path) -> set[str]:
    with sqlite3.connect(path) as db:
        return {row[1] for row in db.execute("PRAGMA table_info(memory_records)")}


_NEW_COLUMNS = {
    "layer",
    "type_tag",
    "provenance_class",
    "spk",
    "addr",
    "ask",
    "sub",
    "fingerprint",
    "schema_version",
}


async def test_new_records_carry_the_envelope_columns(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    await _seed(path)
    rows = _rows(
        path, "SELECT layer, type_tag, provenance_class, schema_version FROM memory_records"
    )
    assert rows and all(r[0] == "L0" and r[1] == "episodic" and r[3] == 1 for r in rows)
    assert {r[2] for r in rows} == {"user_stated", "assistant_proposed"}


async def test_reads_return_envelope_fields(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    await _seed(path)
    eng = _engine(path)
    await eng.start()
    try:
        turns = await eng.conversation("u", "s1")
    finally:
        await eng.stop()
    assert [(t.layer, t.type_tag, t.provenance_class) for t in turns] == [
        ("L0", "episodic", "user_stated"),
        ("L0", "episodic", "assistant_proposed"),
    ]


async def test_migration_down_up_and_backfill(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    await _seed(path)
    assert _columns(path) >= _NEW_COLUMNS
    command.downgrade(alembic_config(path), "0006_lineage")
    assert not _NEW_COLUMNS & _columns(path)
    # Turn the two rows into a graph fact and a reflection, with perspective tags, behind the app's back.
    with sqlite3.connect(path) as db:
        ids = [r[0] for r in db.execute("SELECT record_id FROM memory_records ORDER BY content")]
        db.execute(
            "UPDATE memory_records SET memory_type='semantic', source=?, tags=? WHERE record_id=?",
            (
                orjson.dumps({"role": "assistant", "channel": "extract_graph", "parents": ["p"]}),
                orjson.dumps(["spk:alice", "sub:bob", "ask:erin"]),
                ids[0],
            ),
        )
        db.execute(
            "UPDATE memory_records SET memory_type='reflective', source=? WHERE record_id=?",
            (orjson.dumps({"role": "assistant", "channel": "reflection"}), ids[1]),
        )
    upgrade_to_head(path)
    assert _columns(path) >= _NEW_COLUMNS
    got = {
        r[0]: r[1:]
        for r in _rows(
            path,
            "SELECT record_id, layer, type_tag, provenance_class, spk, ask, sub, schema_version "
            "FROM memory_records",
        )
    }
    assert got[ids[0]] == ("L3", "semantic", "derived", "alice", "erin", b'["bob"]', 1)
    assert got[ids[1]] == ("L4", "reflective", "derived", None, None, b"[]", 1)
    upgrade_to_head(path)  # a second upgrade is a no-op
    eng = _engine(path)
    await eng.start()
    try:
        record = await eng._storage.get_record(ids[0])  # type: ignore[attr-defined]
    finally:
        await eng.stop()
    assert record is not None
    assert (record.layer, record.axes.speaker, record.axes.subject) == ("L3", "alice", ["bob"])
