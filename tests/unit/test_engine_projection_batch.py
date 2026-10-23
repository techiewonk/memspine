"""ingest_messages shares one projection batch across its turns.

The batch may hold the lexical commit and the projector checkpoints until the
end of the call, but reads inside it must see every applied turn, and a
checkpoint must never run ahead of what its projection holds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memspine.api.engine import Engine
from memspine.clients.tantivy import TantivyConnector
from memspine.services.lexical.projector import LexicalProjector
from memspine.services.lexical.tantivy import TantivyLexical, tantivy_spec
from memspine.store.records import MemoryRecord
from memspine.store.replay import catch_up

_TURNS = [
    {"role": "user", "content": "I adopted a beagle named Pepper last spring."},
    {"role": "assistant", "content": "Pepper sounds lovely, how old is she?"},
    {"role": "user", "content": "She turns three in October and loves the lake."},
]


async def _engine() -> Engine:
    engine = Engine(
        template="base",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
    )
    await engine.start()
    return engine


async def _offsets(engine: Engine) -> dict[str, int]:
    assert engine._storage is not None
    return {p.name: await engine._storage.get_offset(p.name) for p in engine._projectors}


async def _head(engine: Engine) -> int:
    assert engine._storage is not None
    events = await engine._storage.read_events(after_seq=0, limit=1_000_000)
    return max(event.seq or 0 for event in events)


async def test_ingest_messages_checkpoints_every_projector_at_the_head() -> None:
    engine = await _engine()
    try:
        assert any(isinstance(p, LexicalProjector) for p in engine._projectors)
        await engine.ingest_messages(_TURNS, namespace="chat")
        head = await _head(engine)
        assert set((await _offsets(engine)).values()) == {head}
        assert engine._batch_offsets is None
        assert engine._lexical is not None
        hits = await engine._lexical.search("chat", "beagle Pepper", top_k=5)
        assert len(hits) == 2
    finally:
        await engine.stop()


async def test_reads_inside_the_batch_see_every_applied_turn() -> None:
    engine = await _engine()
    try:
        lexical = engine._lexical
        assert isinstance(lexical, TantivyLexical)
        async with engine._projection_batch():
            await engine.write("Pepper chased a heron at the lake.", namespace="chat")
            # Held commit: the lexical read commits first, so the turn is visible.
            assert lexical.connector.uncommitted
            hits = await lexical.search("chat", "heron", top_k=5)
            assert len(hits) == 1
            assert await lexical.exists(hits[0].record_id)
            # Checkpoints wait for the flush.
            assert set((await _offsets(engine)).values()) == {0}
        head = await _head(engine)
        assert set((await _offsets(engine)).values()) == {head}
    finally:
        await engine.stop()


async def test_a_failed_flush_keeps_only_that_checkpoint_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await _engine()
    try:
        lexical = next(p for p in engine._projectors if isinstance(p, LexicalProjector))

        async def broken_flush() -> None:
            raise RuntimeError("index unavailable")

        monkeypatch.setattr(lexical, "flush", broken_flush)
        with pytest.raises(RuntimeError, match="index unavailable"):
            await engine.ingest_messages(_TURNS, namespace="chat")
        head = await _head(engine)
        offsets = await _offsets(engine)
        assert offsets.pop("lexical") == 0
        assert set(offsets.values()) == {head}

        # Catch-up re-applies the lexical tail (applies are idempotent).
        monkeypatch.undo()
        assert engine._storage is not None
        await catch_up(engine._storage, [lexical])
        assert (await _offsets(engine))["lexical"] == head
        assert engine._lexical is not None
        assert len(await engine._lexical.search("chat", "beagle Pepper", top_k=5)) == 2
    finally:
        await engine.stop()


async def test_tantivy_close_commits_held_documents(tmp_path: Path) -> None:
    path = tmp_path / "lexical"
    store = TantivyLexical(TantivyConnector(tantivy_spec(path)))
    store.defer_commits()
    await store.index(
        MemoryRecord(namespace="n", memory_type="episodic", content="kayak on the river")
    )
    await store.connector.close()  # the owner's close commits held documents
    reopened = TantivyLexical(TantivyConnector(tantivy_spec(path)))
    try:
        assert len(await reopened.search("n", "kayak", top_k=3)) == 1
    finally:
        await reopened.connector.close()
