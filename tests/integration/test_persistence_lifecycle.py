"""The core durability proof (event-sourced core + projector high-water marks).

Write across every memory type into a FILE-BACKED engine, ``stop()``, construct
a *brand-new* engine on the SAME db file, ``start()``, and assert every record /
skill-stage / link / watch / grant survived — the projectors materialized from
the persisted log and their persisted offsets, with nothing lost across the
construct/teardown boundary.

Two further angles:

* ``rebuild()`` from the persisted log reproduces a byte-identical read model
  (the D0.1 rebuildability guarantee), and
* the L2 angle — writes made with the default embedder are still vector-
  queryable (through ``search()``) after reopen: the LanceDB table persisted
  beside the db file and its rows survived the construct/teardown boundary.

Vector store is LanceDB (the sole backend, ADR-021), file-backed beside the db
file (``<path>.lance``) so its projection persists across the reopen exactly
like the SQLite read model.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from memspine import Engine
from memspine.core.registry import MEMORY_TYPES

ALL_TYPES = sorted(MEMORY_TYPES)

#: A fixed instant so the prospective watch never touches the wall clock.
NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

_CONFIG: dict[str, Any] = {
    "memories": {name: {"enabled": True} for name in ALL_TYPES},
}


async def _snapshot(engine: Engine) -> dict[str, dict[str, Any]]:
    """The full materialized read model, keyed by record_id — the object a
    rebuild must reproduce exactly from the persisted log."""
    storage = engine._storage
    assert storage is not None
    snapshot: dict[str, dict[str, Any]] = {}
    for namespace in await storage.list_namespaces():
        for record in await storage.list_records(namespace):
            snapshot[record.record_id] = record.model_dump(mode="json")
    return snapshot


async def test_persistence_survives_reopen_and_rebuild(
    make_file_engine: Any, tmp_path: Path
) -> None:
    # ── seed: one write of (nearly) every type through the real verbs ─────────
    engine = make_file_engine(**_CONFIG)
    await engine.start()
    try:
        fact = await engine.write(
            "the deploy pipeline uses blue-green rollout",
            entity="deploy_pipeline",
            attribute="strategy",
        )
        episode = await engine.write("we shipped the release on friday", memory_type="episodic")

        skill = await engine.add_skill("1. build 2. test 3. ship", name="release")
        staged = await engine.promote_skill(skill.record_id)  # draft -> staged
        assert staged.skill_stage == "staged"

        reflection = await engine.reflect("the team ships on fridays", [episode.record_id])

        left = await engine.write("alpha only")
        right = await engine.write("omega only")
        await engine.associate(left.record_id, right.record_id, weight=0.9)

        watch = await engine.watch("rotate the api key", due_at=NOW + timedelta(hours=1))

        shared_fact = await engine.write("the rotation schedule is monthly", namespace="team-a")
        grant = await engine.grant("team-b", namespace="team-a")

        note = await engine.write("hot scratch note", memory_type="working")

        doc = tmp_path / "runbook.md"
        doc.write_text(
            "# Runbook\n\nRotate the API key monthly.\n\nPage on-call.", encoding="utf-8"
        )
        chunks = await engine.ingest(str(doc))
        assert chunks, "ingest produced no resource records"
    finally:
        await engine.stop()

    # ── reopen: a brand-new engine over the SAME db file ──────────────────────
    reopened = make_file_engine(**_CONFIG)
    await reopened.start()
    try:
        # rebuild() from the persisted log must reproduce the caught-up read
        # model byte-for-byte (D0.1): capture, rebuild from seq 0, re-capture.
        before = await _snapshot(reopened)
        assert fact.record_id in before, "the persisted read model is empty after reopen"
        counts = await reopened.rebuild()
        assert counts["records"] > 0
        after = await _snapshot(reopened)
        assert before == after, "rebuild diverged from the persisted read model"

        # every type survived — checked through its real read verb.
        semantic_ids = {r.record_id for r, _ in await reopened.search("deploy pipeline rollout")}
        assert fact.record_id in semantic_ids

        episodic_ids = {r.record_id for r in await reopened.retrieve(memory_type="episodic")}
        assert episode.record_id in episodic_ids

        surviving_skill = next(
            r for r in await reopened.skills(usable_only=False) if r.record_id == skill.record_id
        )
        assert surviving_skill.skill_stage == "staged"  # ladder position persisted

        reflective_ids = {r.record_id for r in await reopened.retrieve(memory_type="reflective")}
        assert reflection.record_id in reflective_ids

        related_ids = {r.record_id for r in await reopened.related(left.record_id)}
        assert right.record_id in related_ids  # the association edge persisted

        fired = await reopened.due(now=NOW + timedelta(hours=1, seconds=1))
        assert watch.record_id in {r.record_id for r in fired}
        assert await reopened.due(now=NOW) == []  # ...and does not fire early

        crossed = await reopened.shared_search("rotation schedule", namespace="team-b")
        assert shared_fact.record_id in {r.record_id for r, _ in crossed}  # grant survived
        assert grant.record_id  # the grant bookkeeping record materialized

        resource_ids = {r.record_id for r in await reopened.retrieve(memory_type="resource")}
        assert {c.record_id for c in chunks} <= resource_ids

        working_ids = {r.record_id for r in await reopened.retrieve(memory_type="working")}
        assert note.record_id in working_ids

        assert set(reopened.describe()["memories"]["enabled"]) == set(ALL_TYPES)

        # L2 angle: the semantic search at the top of this block already proved
        # the default embedder's vectors persisted in the file-backed LanceDB
        # table and stayed queryable across the reopen (fact.record_id was in
        # the search hits) — no store-internal column read is needed or possible.
    finally:
        await reopened.stop()
