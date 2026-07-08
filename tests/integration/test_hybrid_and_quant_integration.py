"""L1 + L2 tied into a real file-backed lifecycle.

Part A (L1, hybrid — core, no extra): write with hybrid retrieval OFF, then
reopen the SAME db with ``read.hybrid=true``. The lexical projector is now
registered for the first time at offset 0, so ``start()`` catch-up backfills the
FTS5 index from the persisted event log; hybrid search then fuses and returns a
record the lexical leg surfaces. This proves the lexical projector caught up
from the persisted log (not from any in-memory state).

Part B (L2, quantization): write with quantization OFF (codes NULL), reopen with
``vector.quantization=int8`` (codes STILL NULL — catch-up does not re-project
already-applied events), then ``rebuild()`` — which resets the vector projector
and replays the log, populating the quantized codes. The two-stage rescore then
returns the correct exact-cosine top-1.

Both paths are core (FTS5 + the default hash embedder + the SQLite vector
store); nothing here needs an optional extra, so there is nothing to skip.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from memspine import Engine
from memspine.services.storage.sqlite.schema import memory_embeddings

_SEMANTIC: dict[str, Any] = {"memories": {"semantic": {"enabled": True}}}


async def _codes(engine: Engine) -> list[bytes | None]:
    client = engine._client
    assert client is not None
    async with client.engine.connect() as conn:
        rows = (await conn.execute(select(memory_embeddings.c.quantized_vec))).all()
    return [row[0] for row in rows]


async def test_hybrid_lexical_index_backfills_from_log_on_reopen(make_file_engine: Any) -> None:
    # ── write with hybrid OFF: no lexical projector, no FTS index ─────────────
    engine = make_file_engine(**_SEMANTIC)
    await engine.start()
    try:
        target = await engine.write("the quick brown fox jumps over the lazy dog")
        await engine.write("an essay concerning oceans and deep water currents")
        assert "lexical" not in engine.describe()["projectors"]
    finally:
        await engine.stop()

    # ── reopen with hybrid ON: catch-up backfills FTS5 from the persisted log ─
    hybrid = make_file_engine(**_SEMANTIC, read={"hybrid": True})
    await hybrid.start()
    try:
        assert "lexical" in hybrid.describe()["projectors"]
        lexical = hybrid._lexical
        assert lexical is not None
        # The index was empty at construction; only a replay from seq 0 could
        # have populated it — the lexical projector caught up from the log.
        assert await lexical.exists(target.record_id)
        direct = await lexical.search("default", "brown fox")
        assert target.record_id in {hit.record_id for hit in direct}
        # ...and hybrid search fuses the lexical leg into the returned results.
        fused = {r.record_id for r, _ in await hybrid.search("brown fox")}
        assert target.record_id in fused
    finally:
        await hybrid.stop()


async def test_rebuild_populates_quantized_codes_and_rescores(make_file_engine: Any) -> None:
    # ── write with quantization OFF: float32 rows, codes NULL ─────────────────
    engine = make_file_engine(**_SEMANTIC)
    await engine.start()
    try:
        assert engine._rescore_active is False
        for content in ("the sky is blue today", "green grass grows", "red apples fall"):
            await engine.write(content)
        assert all(code is None for code in await _codes(engine))
    finally:
        await engine.stop()

    # ── reopen with int8: rescore is active, but codes are still NULL until a
    #     rebuild re-projects the log (catch-up skips already-applied events) ──
    quant = make_file_engine(**_SEMANTIC, vector={"quantization": "int8"})
    await quant.start()
    try:
        assert quant._rescore_active is True
        assert all(code is None for code in await _codes(quant)), (
            "catch-up must not re-project already-applied WRITE events"
        )

        counts = await quant.rebuild()
        assert counts["vectors"] == 3
        codes = await _codes(quant)
        assert codes and all(code is not None for code in codes), (
            "rebuild must backfill quantized codes from the persisted log"
        )

        # The two-stage rescore over the freshly-coded rows returns the exact
        # float32 top-1 (small corpus < oversample window => exact).
        results = await quant.search("the sky is blue today")
        assert results[0][0].content == "the sky is blue today"
    finally:
        await quant.stop()
