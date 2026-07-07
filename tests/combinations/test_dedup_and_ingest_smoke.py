"""C6 §6: dedup two-stage smoke (D-27) + multi-format ingest smoke (D-29).

Both are boot-level smokes, not the deep behavior suites (those live under
``tests/unit/memories/semantic`` and ``.../resource``). They confirm the
combination wiring: the MinHash-LSH -> cosine dedup path merges a near-dup, and
the ingest verb round-trips a document into resource records.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# ── dedup two-stage (D-27) ────────────────────────────────────────────────────


async def test_near_duplicate_semantic_writes_merge_via_two_stage_dedup(make_engine) -> None:
    # The proven merge thresholds under the hash embedder (dim 64), matching
    # tests/unit/memories/semantic/test_store.py: stage-1 MinHash-LSH surfaces
    # the candidate, stage-2 cosine confirms, and the merge preserves identity.
    engine = make_engine(
        memories={
            "semantic": {
                "enabled": True,
                "policies": {"dedup": {"lsh_threshold": 0.5, "cosine_threshold": 0.9}},
            }
        },
    )
    await engine.start()
    try:
        first = await engine.write("alice prefers her coffee black in the morning")
        dup = await engine.write("alice prefers her coffee black in the morning !")
        # Merged onto the incumbent: same record id, and only one active row.
        assert dup.record_id == first.record_id
        active = await engine.retrieve(memory_type="semantic")
        assert [r.record_id for r in active] == [first.record_id]
    finally:
        await engine.stop()


async def test_distinct_semantic_writes_do_not_merge(make_engine) -> None:
    # Control: disjoint facts stay two rows — dedup is not merging everything.
    engine = make_engine(
        memories={
            "semantic": {
                "enabled": True,
                "policies": {"dedup": {"lsh_threshold": 0.5, "cosine_threshold": 0.9}},
            }
        },
    )
    await engine.start()
    try:
        a = await engine.write("the deploy pipeline uses blue-green rollout")
        b = await engine.write("the office cat is named mochi")
        assert a.record_id != b.record_id
        ids = {r.record_id for r in await engine.retrieve(memory_type="semantic")}
        assert {a.record_id, b.record_id} <= ids
    finally:
        await engine.stop()


# ── ingest smoke (D-29) ───────────────────────────────────────────────────────


async def test_plaintext_ingest_round_trips_without_the_extra(tmp_path: Path, make_engine) -> None:
    # Plain text/markdown extraction is a core fallback (no [ingest] extra), so
    # this smoke always runs and exercises extract -> chunk -> resource records.
    engine = make_engine(memories={"resource": {"enabled": True}})
    await engine.start()
    try:
        doc = tmp_path / "report.md"
        doc.write_text(
            "# Incident Report\n\nThe cache filled up overnight.\n\nRotation now runs hourly.\n",
            encoding="utf-8",
        )
        chunks = await engine.ingest(str(doc))
        assert chunks, "ingest produced no resource records"
        assert all(c.memory_type == "resource" for c in chunks)
        stored = await engine.retrieve(memory_type="resource")
        assert {c.record_id for c in chunks} <= {r.record_id for r in stored}
    finally:
        await engine.stop()


async def test_rich_format_ingest_smoke_when_extra_present(tmp_path: Path, make_engine) -> None:
    # docx/pdf/pptx need markitdown + chonkie ([ingest]); skip cleanly if absent.
    if importlib.util.find_spec("markitdown") is None:
        pytest.skip("markitdown not installed — [ingest] extra absent (D-29)")
    from markitdown import MarkItDown  # noqa: F401  — present per the guard above

    engine = make_engine(memories={"resource": {"enabled": True}})
    await engine.start()
    try:
        doc = tmp_path / "brief.md"  # markitdown handles md too; keep the smoke cheap
        doc.write_text("# Brief\n\nOne paragraph of content.\n", encoding="utf-8")
        chunks = await engine.ingest(str(doc))
        assert chunks
    finally:
        await engine.stop()
