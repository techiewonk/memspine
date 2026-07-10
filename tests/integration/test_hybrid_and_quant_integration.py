"""L1 + L2 tied into a real file-backed lifecycle.

Part A (L1, hybrid — core, no extra): write with hybrid retrieval OFF, then
reopen the SAME db with ``read.hybrid=true``. The lexical projector is now
registered for the first time at offset 0, so ``start()`` catch-up backfills the
FTS5 index from the persisted event log; hybrid search then fuses and returns a
record the lexical leg surfaces. This proves the lexical projector caught up
from the persisted log (not from any in-memory state).

Part B (L2, quantization): write with quantization OFF, reopen with
``vector.quantization=int8`` (the vector projector is already caught up, so
catch-up re-projects nothing), then ``rebuild()`` — which resets the vector
projector and replays the log into a freshly int8-indexed LanceDB table. The
native two-stage rescore then returns the correct exact-cosine top-1.

The vector store is LanceDB (the sole backend, ADR-021) — file-backed beside
the db file so the projection persists across the reopen. Unlike the removed
SQLite brute-force store, LanceDB owns quantization inside its own table
(no ``memory_embeddings.quantized_vec`` column to read), so Part B asserts the
behavioral outcome (rebuild re-projects every record + the int8 rescore returns
the exact top-1); the native rescore's recall is proven directly in
tests/unit/services/vector/test_lancedb_rescore.py.
"""

from __future__ import annotations

from typing import Any

_SEMANTIC: dict[str, Any] = {
    "memories": {"semantic": {"enabled": True}},
}


async def test_hybrid_lexical_index_backfills_from_log_on_reopen(make_file_engine: Any) -> None:
    # ── write with hybrid OFF: no lexical projector, no FTS index ─────────────
    # (hybrid is the v0.2 A3 default; opt out explicitly to stage the reopen.)
    engine = make_file_engine(**_SEMANTIC, read={"hybrid": False})
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


async def test_rebuild_reprojects_and_int8_rescore_returns_exact_top1(
    make_file_engine: Any,
) -> None:
    # ── write with quantization OFF: the exact float32 query() path ───────────
    engine = make_file_engine(**_SEMANTIC)
    await engine.start()
    try:
        assert engine._rescore_active is False
        for content in ("the sky is blue today", "green grass grows", "red apples fall"):
            await engine.write(content)
    finally:
        await engine.stop()

    # ── reopen with int8: rescore is active. The vector projector is already
    #     caught up (its offset persisted), so start() re-projects nothing;
    #     rebuild() resets it and replays the log into an int8-indexed table ───
    quant = make_file_engine(memories=_SEMANTIC["memories"], vector={"quantization": "int8"})
    await quant.start()
    try:
        assert quant._rescore_active is True

        counts = await quant.rebuild()
        assert counts["vectors"] == 3, "rebuild must re-project every record from the log"

        # The native two-stage rescore over the freshly int8-indexed rows returns
        # the exact float32 top-1 (small corpus < oversample window => exact).
        results = await quant.search("the sky is blue today")
        assert results[0][0].content == "the sky is blue today"
    finally:
        await quant.stop()
