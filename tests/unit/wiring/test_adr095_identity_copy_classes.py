"""ADR-095 copy-class gate for the entity name index, aliases and the message envelope.

A hard erase must leave no copy of an erased record's entity names, alias text or message
``metadata`` / sender name, nor any fingerprint of them, in the raw event log or in any table
of the records database. Each test erases, then scans the decoded log AND every table row.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import orjson
import pytest
import zstandard
from sqlalchemy import MetaData, Table, inspect, select

from memspine import Engine
from memspine.memories.associative.entities import name_fingerprint
from memspine.memories.associative.entity_index import ALIAS_MARKER, LINKS_KEY, entity_status
from memspine.services.storage.entity_index_tables import entity_index_store_of
from memspine.store.events import EventKind, fingerprint_payload

NS = "a"


async def _engine(path: Path) -> Engine:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"records": {"provider": "sqlite", "settings": {"path": str(path)}}},
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={
            "semantic": {"enabled": True},
            "associative": {"enabled": True, "policies": {"entity_nodes": True}},
        },
        read={"hybrid": True, "record_access": False},
    )
    await eng.start()
    return eng


def _text(value: Any) -> str:
    if isinstance(value, bytes | bytearray | memoryview):
        raw = bytes(value)
        try:
            raw = zstandard.ZstdDecompressor().decompress(raw)
        except zstandard.ZstdError:
            pass
        return raw.decode("utf-8", errors="replace")
    return str(value)


async def _scan(eng: Engine) -> str:
    """Everything the records database holds, as text: every decoded event payload, and every
    row of every table (blobs decompressed when they are zstd frames)."""
    assert eng._storage is not None and eng._sql is not None
    parts = [orjson.dumps(e.payload).decode() for e in await eng._storage.read_events(0, 1_000_000)]
    engine = eng._sql.engine
    async with engine.connect() as conn:
        names = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
        meta = MetaData()
        tables = await conn.run_sync(
            lambda sync: {n: Table(n, meta, autoload_with=sync) for n in names}
        )
        for name, table in tables.items():
            for row in (await conn.execute(select(table))).all():
                parts.append(name + ": " + " | ".join(_text(v) for v in row))
    return "\n".join(parts).lower()


def _absent(haystack: str, needles: list[str]) -> list[str]:
    return [n for n in needles if n and n.lower() in haystack]


async def test_hard_erase_leaves_no_entity_name_or_name_fingerprint(tmp_path: Path) -> None:
    eng = await _engine(tmp_path / "m.db")
    try:
        gone = await eng.write(
            "Zebulon Quartz lives in Vrbnik",
            namespace=NS,
            entity="Zebulon Quartz",
            tags=["dst:Vrbnik", "entity_type:person"],
        )
        kept = await eng.write("Dana bakes bread", namespace=NS, entity="Dana")
        assert eng._storage is not None
        [write] = [
            e
            for e in await eng._storage.read_events(0, 10_000)
            if e.kind is EventKind.WRITE and e.payload["record"]["record_id"] == gone.record_id
        ]
        links = write.payload[LINKS_KEY]
        assert {lk["name_fp"] for lk in links} == {
            name_fingerprint("zebulon quartz"),
            name_fingerprint("vrbnik"),
        }
        await eng.forget(gone.record_id, namespace=NS, hard=True)
        haystack = await _scan(eng)
        needles = [
            "zebulon",
            "vrbnik",
            name_fingerprint("zebulon quartz"),
            name_fingerprint("vrbnik"),
            *(lk["entity_id"] for lk in links),  # the erased record's entities are gone too
        ]
        assert _absent(haystack, needles) == []
        assert "dana" in haystack  # the scan sees live data
        # the redacted WRITE keeps its seq and kind, with no links left
        [after] = [
            e
            for e in await eng._storage.read_events(0, 10_000)
            if e.kind is EventKind.WRITE and e.payload["record"]["record_id"] == gone.record_id
        ]
        assert after.payload[LINKS_KEY] == [] and after.payload["redacted"] is True
        # rebuild stays deterministic: it reads ids from the live links only
        store = entity_index_store_of(eng._storage)
        before = [(r.kind, r.key, r.entity_id) for r in await store.select(NS, "entity")]
        await eng.rebuild()
        assert [(r.kind, r.key, r.entity_id) for r in await store.select(NS, "entity")] == before
        assert _absent(await _scan(eng), needles) == []
        assert kept.record_id
    finally:
        await eng.stop()


@pytest.mark.parametrize("second_evidence", [False, True])
async def test_hard_erase_of_alias_evidence_scrubs_the_alias(
    tmp_path: Path, second_evidence: bool
) -> None:
    eng = await _engine(tmp_path / "m.db")
    try:
        await eng.write("Melanie sings", namespace=NS, entity="Melanie")
        evidence = await eng.write("everyone calls Melanie Quetzalia", namespace=NS)
        other = await eng.write("her friends agree on that name", namespace=NS)
        assert eng._associative is not None and eng._storage is not None
        [melanie] = await (await eng._associative.entity_lookup(NS)).get("melanie")
        cited = [evidence.record_id, *([other.record_id] if second_evidence else [])]
        await eng._associative.add_entity_alias(NS, melanie, "Quetzalia", cited)
        [marker] = [
            e
            for e in await eng._storage.read_events(0, 10_000)
            if e.kind is EventKind.MARKER and e.payload.get("marker") == ALIAS_MARKER
        ]
        alias_id = marker.payload["alias_id"]
        # the alias id is opaque: no hash of the alias text
        assert alias_id != fingerprint_payload({"alias": [melanie, "quetzalia", sorted(cited)]})
        assert await (await eng._associative.entity_lookup(NS)).get("quetzalia") == [melanie]
        # erasing ONE cited record undoes the alias even while other evidence is live
        await eng.forget(evidence.record_id, namespace=NS, hard=True)
        assert await eng._storage.get_record(other.record_id) is not None
        haystack = await _scan(eng)
        assert _absent(haystack, ["quetzalia", name_fingerprint("quetzalia")]) == []
        assert "melanie" in haystack
        # the alias is undone (a hard erase of any cited record), the entity contested
        assert await (await eng._associative.entity_lookup(NS)).get("quetzalia") == []
        assert await entity_status(eng._storage, NS, melanie) == "contested"
        await eng.rebuild()  # the scrubbed marker replays to the same state
        assert await (await eng._associative.entity_lookup(NS)).get("quetzalia") == []
        assert await entity_status(eng._storage, NS, melanie) == "contested"
    finally:
        await eng.stop()


async def test_hard_erase_of_a_message_leaves_no_metadata_sender_name_or_input_hash(
    tmp_path: Path,
) -> None:
    eng = await _engine(tmp_path / "m.db")
    try:
        [row] = await eng.ingest_messages(
            [
                {
                    "message_id": "m1",
                    "role": "user",
                    "content": "the kettle is broken again",
                    "sender": {"id": "u1", "kind": "human", "name": "Xanthippe Moor"},
                    "metadata": {"crm_ticket": "TCK-778899", "note": "vip-hollowell"},
                }
            ],
            namespace=NS,
        )
        await eng.write("Dana bakes bread", namespace=NS, entity="Dana")
        envelope = row.source.message
        assert envelope is not None
        hashes = list(envelope["input_fingerprints"].values())
        assert hashes
        await eng.forget(row.record_id, namespace=NS, hard=True)
        haystack = await _scan(eng)
        needles = ["xanthippe", "tck-778899", "vip-hollowell", "kettle", *hashes]
        assert _absent(haystack, needles) == []
        assert "dana" in haystack
        # a reuse of the erased message id is still refused (the identity tombstone)
        with pytest.raises(ValueError, match="erased"):
            await eng.ingest_messages(
                [{"message_id": "m1", "role": "user", "content": "the kettle is broken again"}],
                namespace=NS,
            )
    finally:
        await eng.stop()


async def test_declared_provenance_is_typed_and_metadata_is_not_read(tmp_path: Path) -> None:
    """ADR-090: provenance axes come from the typed envelope field, never from metadata."""
    eng = await _engine(tmp_path / "m.db")
    try:
        typed, bagged = await eng.ingest_messages(
            [
                {
                    "message_id": "m1",
                    "role": "user",
                    "content": "forwarded note",
                    "provenance": {"origin": "email", "assertion_owner_id": "owner-1"},
                },
                {
                    "message_id": "m2",
                    "role": "user",
                    "content": "another note",
                    "metadata": {"source_origin": "email", "trust_tier": "reviewed"},
                },
            ],
            namespace=NS,
        )
        assert typed.source.message is not None and bagged.source.message is not None
        first = typed.source.message["source_provenance"]
        assert first["origin"] == "email" and first["assertion_owner_id"] == "owner-1"
        second = bagged.source.message["source_provenance"]
        assert second["origin"] == "raw_message" and second["trust_tier"] is None
        assert bagged.source.message["metadata"] == {
            "source_origin": "email",
            "trust_tier": "reviewed",
        }
        with pytest.raises(ValueError, match="trust_tier"):
            await eng.ingest_messages(
                [
                    {
                        "role": "user",
                        "content": "x",
                        "provenance": {"trust_tier": "reviewed"},
                    }
                ],
                namespace=NS,
            )
    finally:
        await eng.stop()
