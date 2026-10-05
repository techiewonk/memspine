"""#2 + #43: erasure scrubs every identifying field, cascades to derived records,
purges caches, erases per subject and per namespace, and leaves no bytes behind."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from memspine import Engine
from memspine.core import erasure
from memspine.core.records import SourceInfo
from memspine.exceptions import MemspineError


def _engine(**extra: Any) -> Engine:
    config: dict[str, Any] = {
        "template": "core",
        "dotenv_path": None,
        "storage": {"path": ":memory:"},
        "embedding": {"provider": "hash"},
    }
    config.update(extra)
    return Engine(**config)


@pytest.fixture
async def engine() -> AsyncIterator[Engine]:
    eng = _engine()
    await eng.start()
    try:
        yield eng
    finally:
        await eng.stop()


async def _log_text(eng: Engine) -> str:
    return " ".join(str(e.payload) for e in await eng._require_started().read_events())


async def test_hard_forget_scrubs_key_tags_and_fingerprint(engine: Engine) -> None:
    record = await engine.write(
        "Zelda Quixote lives at 12 Wren Lane",
        namespace="a",
        entity="ZeldaQuixote",
        attribute="home_address",
        tags=["subject-zelda-quixote"],
    )
    fingerprint = record.content_fingerprint
    await engine.forget(record.record_id, namespace="a", hard=True)

    text = await _log_text(engine)
    for needle in ("Wren Lane", "ZeldaQuixote", "home_address", "subject-zelda", fingerprint):
        assert needle not in text
    report = await engine.verify_forget(record.record_id, namespace="a")
    assert report["clean"] is True
    assert report["log_retained_fields"] == []


async def test_verify_is_clean_only_once_every_field_is_gone(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A content-only redactor (the pre-#2 behaviour) must not pass the proof."""
    record = await engine.write(
        "Zelda prefers tea", namespace="a", entity="Zelda", attribute="drink"
    )

    def content_only(node: Any, record_id: str) -> bool:
        changed = False
        if isinstance(node, dict):
            if node.get("record_id") == record_id and node.get("content"):
                node["content"] = ""
                changed = True
            for value in node.values():
                changed |= content_only(value, record_id)
        elif isinstance(node, list):
            for item in node:
                changed |= content_only(item, record_id)
        return changed

    from memspine.services.storage import sql_base

    monkeypatch.setattr(sql_base, "redact_record", content_only)
    await engine.forget(record.record_id, namespace="a", hard=True)
    report = await engine.verify_forget(record.record_id, namespace="a")
    assert report["clean"] is False
    assert {"entity", "attribute", "content_fingerprint"} <= set(report["log_retained_fields"])

    monkeypatch.setattr(sql_base, "redact_record", erasure.redact_record)
    await engine.forget(record.record_id, namespace="a", hard=True)  # idempotent retry
    report = await engine.verify_forget(record.record_id, namespace="a")
    assert report["clean"] is True


async def test_hard_forget_cascades_to_derived_records(engine: Engine) -> None:
    turn = await engine.write(
        "I moved to Lisbon last spring", namespace="a", memory_type="episodic"
    )
    fact = await engine.write("user lives in Lisbon", namespace="a", derived_from=[turn.record_id])
    grandchild = await engine.write(
        "user is near the sea", namespace="a", derived_from=[fact.record_id]
    )
    other = await engine.write("unrelated note about cats", namespace="a")

    await engine.forget(turn.record_id, namespace="a", hard=True)

    storage = engine._require_started()
    for gone in (turn, fact, grandchild):
        assert await storage.get_record(gone.record_id) is None
        assert (await engine.verify_forget(gone.record_id, namespace="a"))["clean"] is True
    assert await storage.get_record(other.record_id) is not None
    assert "Lisbon" not in await _log_text(engine)


async def test_cascade_off_leaves_descendants_and_verify_says_so(engine: Engine) -> None:
    turn = await engine.write("my badge number is 4471", namespace="a", memory_type="episodic")
    child = await engine.write("badge 4471", namespace="a", derived_from=[turn.record_id])
    await engine.forget(turn.record_id, namespace="a", hard=True, cascade=False)
    report = await engine.verify_forget(turn.record_id, namespace="a")
    assert report["descendants_remaining"] == [child.record_id]
    assert report["clean"] is False


async def test_soft_forget_does_not_cascade(engine: Engine) -> None:
    turn = await engine.write("soft parent", namespace="a", memory_type="episodic")
    child = await engine.write("soft child", namespace="a", derived_from=[turn.record_id])
    await engine.forget(turn.record_id, namespace="a")
    stored = await engine._require_started().get_record(child.record_id)
    assert stored is not None and stored.status.value == "activated"


async def test_hard_forget_purges_embedding_cache(engine: Engine) -> None:
    text = "cache me if you can, Zelda"
    record = await engine.write(text, namespace="a")
    embedder: Any = engine._embedder
    assert engine._cache is not None
    key = embedder._key(text, "emb")
    assert await engine._cache.get(key) is not None
    await engine.forget(record.record_id, namespace="a", hard=True)
    assert await engine._cache.get(key) is None


async def test_erase_subject(engine: Engine) -> None:
    a1 = await engine.write("Alice likes jazz", namespace="a", entity="Alice", attribute="music")
    a2 = await engine.write("Alice is 34", namespace="a", entity="alice", attribute="age")
    p1 = await engine.write(
        "note from alice's agent",
        namespace="a",
        source=SourceInfo(role="user", principal="Alice"),
    )
    bob = await engine.write("Bob likes rock", namespace="a", entity="Bob", attribute="music")
    derived = await engine.write("jazz fan", namespace="a", derived_from=[a1.record_id])

    erased = await engine.erase_subject("Alice", namespace="a")

    assert set(erased) == {a1.record_id, a2.record_id, p1.record_id, derived.record_id}
    storage = engine._require_started()
    assert await storage.get_record(bob.record_id) is not None
    for rid in erased:
        assert (await engine.verify_forget(rid, namespace="a"))["clean"] is True


async def test_erase_namespace(engine: Engine) -> None:
    mine = [await engine.write(f"private note {i}", namespace="tenant/x") for i in range(3)]
    keep = await engine.write("other tenant note", namespace="tenant/y")
    erased = await engine.erase_namespace("tenant/x")
    assert set(erased) == {r.record_id for r in mine}
    assert await engine.retrieve(namespace="tenant/x") == []
    assert len(await engine.retrieve(namespace="tenant/y")) == 1
    assert "private note" not in await _log_text(engine)
    assert keep.record_id not in erased


async def test_erase_namespace_refused_under_legal_hold() -> None:
    eng = _engine(
        memories={
            "semantic": {
                "enabled": True,
                "policies": {"retention": {"legal_hold_namespaces": ["held"]}},
            }
        }
    )
    await eng.start()
    try:
        record = await eng.write("held evidence", namespace="held")
        with pytest.raises(MemspineError):
            await eng.erase_namespace("held")
        assert await eng._require_started().get_record(record.record_id) is not None
    finally:
        await eng.stop()


def _file_bytes(db: Path) -> tuple[int, bytes]:
    """(WAL size, database bytes); a missing WAL counts as empty."""
    wal = Path(f"{db}-wal")
    return (wal.stat().st_size if wal.exists() else 0), db.read_bytes()


async def test_file_database_keeps_no_erased_bytes(tmp_path: Path) -> None:
    """secure_delete zeroes freed pages and the checkpoint empties the WAL."""
    db = tmp_path / "erase.db"
    eng = _engine(storage={"path": str(db)})
    await eng.start()
    try:
        sentinel = "SENTINEL-ERASE-7c1f9"
        record = await eng.write(f"the code is {sentinel}", namespace="a")
        await eng.forget(record.record_id, namespace="a", hard=True)
        wal_bytes, db_bytes = _file_bytes(db)
        assert wal_bytes == 0
        assert sentinel.encode() not in db_bytes
    finally:
        await eng.stop()
