"""Behavioral acceptance for the six memory types in PA-075..080.

These tests exercise the local Engine path: SQLite event/read models, hash
embeddings, LanceDB vector projection and Tantivy lexical projection. Both in-memory and disk cache provider configurations execute. No LLM
provider is configured; reflection text is caller supplied, so these tests do
not make claims about generated text quality.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from memspine import Engine
from memspine.exceptions import ConflictError
from memspine.memories.semantic.facts import fact_at
from memspine.store.records import RecordStatus, SourceInfo


def _engine(tmp_path: Path, *, page_size: int = 8, cache_provider: str = "memory") -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        deployment={"id": f"memory-acceptance-{uuid4().hex}"},
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": str(tmp_path / "events.sqlite"), "busy_timeout_s": 5.0},
            },
            "vector": {"provider": "lancedb", "settings": {"isolation": "shared"}},
            "cache": {
                "provider": cache_provider,
                "settings": ({"path": str(tmp_path / "cache")} if cache_provider == "disk" else {}),
            },
            "lexical": {"provider": "tantivy", "settings": {}},
        },
        model={"embedding": {"provider": "hash", "settings": {"dim": 64}}},
        read={"hybrid": True},
        memories={
            "semantic": {"enabled": True},
            "working": {"enabled": True, "policies": {"page_size": page_size}},
            "episodic": {"enabled": True},
            "resource": {"enabled": True},
            "procedural": {"enabled": True},
            "reflective": {"enabled": True},
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cache_provider", ["memory", "disk"])
async def test_semantic_grounding_dedup_conflict_and_event_time_history(
    tmp_path: Path, cache_provider: str
) -> None:
    engine = _engine(tmp_path, cache_provider=cache_provider)
    await engine.start()
    try:
        before = datetime(2024, 1, 1, tzinfo=UTC)
        changed = datetime(2025, 1, 1, tzinfo=UTC)
        source = SourceInfo(
            role="user",
            channel="conversation",
            principal="mira",
            message={"message_id": "msg-41", "session_id": "s-9", "conversation_id": "c-3"},
        )
        first = await engine.write_ex(
            "Mira lives in Oslo",
            namespace="tenant/mira",
            source=source,
            entity="mira",
            attribute="city",
            valid_from=before,
        )
        duplicate = await engine.write_ex(
            "Mira lives in Oslo",
            namespace="tenant/mira",
            source=source.model_copy(update={"message": {"message_id": "msg-42"}}),
            entity="mira",
            attribute="city",
            valid_from=before,
        )
        current = await engine.write_ex(
            "Mira lives in Kyoto",
            namespace="tenant/mira",
            source=SourceInfo(role="user", channel="conversation", message_id="msg-43"),
            entity="mira",
            attribute="city",
            valid_from=changed,
        )

        rows = await engine.retrieve("tenant/mira", "semantic")
        assert first.action == "added"
        assert duplicate.action == "merged"
        assert duplicate.record.record_id == first.record.record_id
        assert current.action == "updated"
        assert first.record.source.message["message_id"] == "msg-41"
        assert first.record.source.message["session_id"] == "s-9"
        assert len([r for r in rows if r.status is RecordStatus.ACTIVATED]) == 1
        archived = next(r for r in rows if r.record_id == first.record.record_id)
        assert archived.status is RecordStatus.ARCHIVED
        assert archived.valid_to == changed
        assert any(r.content == "Mira lives in Kyoto" and r.valid_from == changed for r in rows)
        assert engine._storage is not None
        historical = await fact_at(engine._storage, "tenant/mira", "mira", "city", before)
        present = await fact_at(engine._storage, "tenant/mira", "mira", "city", changed)
        assert historical is not None and historical.content == "Mira lives in Oslo"
        assert present is not None and present.content == "Mira lives in Kyoto"
        assert not await engine.search("Oslo", namespace="tenant/other", top_k=5)
    finally:
        await engine.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("cache_provider", ["memory", "disk"])
async def test_working_overflow_preserves_persona_and_namespace_boundaries(
    tmp_path: Path, cache_provider: str
) -> None:
    engine = _engine(tmp_path, page_size=2, cache_provider=cache_provider)
    await engine.start()
    try:
        alice_persona = await engine.set_persona("tenant/alice", "Alice persona")
        bob_persona = await engine.set_persona("tenant/bob", "Bob persona")
        turns = [
            await engine.write(f"alice turn {i}", namespace="tenant/alice", memory_type="working")
            for i in range(3)
        ]
        alice_hot = await engine.retrieve("tenant/alice", "working")
        alice_paged = await engine.retrieve("tenant/alice", "episodic")
        bob_hot = await engine.retrieve("tenant/bob", "working")

        assert {r.record_id for r in alice_hot} == {
            alice_persona.record_id,
            turns[1].record_id,
            turns[2].record_id,
        }
        assert [r.record_id for r in alice_paged] == [turns[0].record_id]
        assert alice_paged[0].version == turns[0].version + 1
        assert {r.record_id for r in bob_hot} == {bob_persona.record_id}
        assert all(r.namespace == "tenant/alice" for r in alice_hot + alice_paged)
    finally:
        await engine.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("cache_provider", ["memory", "disk"])
async def test_episodic_timeline_uses_event_time_and_retains_message_session_links(
    tmp_path: Path,
    cache_provider: str,
) -> None:
    engine = _engine(tmp_path, cache_provider=cache_provider)
    await engine.start()
    try:
        event_times = [
            datetime(2024, 4, 2, 9, tzinfo=UTC),
            datetime(2024, 4, 2, 12, tzinfo=UTC),
            datetime(2024, 4, 2, 9, 30, tzinfo=UTC),
        ]
        records = []
        for i, at in enumerate(event_times):
            records.append(
                await engine.write(
                    f"episode {i}",
                    namespace="tenant/episodic",
                    memory_type="episodic",
                    valid_from=at,
                    session_id="session-historical",
                    source=SourceInfo(
                        role="user",
                        channel="conversation",
                        message={
                            "message_id": f"event-{i}",
                            "session_id": "session-historical",
                            "conversation_id": "conversation-7",
                            "sender": {"id": "mira", "kind": "human", "name": "Mira"},
                        },
                    ),
                )
            )
        timeline = await engine.timeline("tenant/episodic")
        sessions = await engine.sessions("tenant/episodic", gap_minutes=60)

        assert [r.record_id for r in timeline] == [
            records[0].record_id,
            records[2].record_id,
            records[1].record_id,
        ]
        assert timeline[0].valid_from == event_times[0]
        assert timeline[0].source.message["session_id"] == "session-historical"
        assert timeline[1].source.message["sender"]["id"] == "mira"
        assert {r.source.message["conversation_id"] for r in timeline} == {"conversation-7"}
        assert len(sessions) == 2
        assert {r.record_id for r in timeline[:2]} == set(sessions[0].record_ids)
    finally:
        await engine.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("cache_provider", ["memory", "disk"])
async def test_resource_chunks_keep_lineage_search_scope_and_hard_erasure(
    tmp_path: Path, cache_provider: str
) -> None:
    engine = _engine(tmp_path, cache_provider=cache_provider)
    await engine.start()
    try:
        asset = tmp_path / "field-notes.txt"
        asset.write_text(
            "Cobalt observatory calibration protocol. " * 40,
            encoding="utf-8",
        )
        chunks = await engine.ingest_data(asset, namespace="tenant/docs")
        assert chunks
        assert all(r.source.doc_path == str(asset) for r in chunks)
        assert [r.source.message_id for r in chunks] == [f"chunk:{i}" for i in range(len(chunks))]
        assert await engine.ingest_data(asset, namespace="tenant/docs") == []
        found = await engine.search(
            "cobalt observatory calibration", namespace="tenant/docs", top_k=10
        )
        assert any(r.record_id in {chunk.record_id for chunk in chunks} for r, _ in found)
        assert not await engine.search(
            "cobalt observatory calibration", namespace="tenant/elsewhere", top_k=10
        )

        for chunk in chunks:
            await engine.forget(chunk.record_id, namespace="tenant/docs", hard=True, cascade=False)
        after = await engine.search(
            "cobalt observatory calibration", namespace="tenant/docs", top_k=10
        )
        assert all(r.record_id not in {chunk.record_id for chunk in chunks} for r, _ in after)
        remaining = await engine.retrieve("tenant/docs", "resource")
        assert not ({chunk.record_id for chunk in chunks} & {r.record_id for r in remaining})
    finally:
        await engine.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("cache_provider", ["memory", "disk"])
async def test_procedural_skills_are_gated_and_plan_recall_is_namespace_scoped(
    tmp_path: Path,
    cache_provider: str,
) -> None:
    engine = _engine(tmp_path, cache_provider=cache_provider)
    await engine.start()
    try:
        draft = await engine.add_skill("Use the local checksum tool", "checksum", "tenant/a")
        assert await engine.skills("tenant/a", usable_only=True) == []
        staged = await engine.promote_skill(draft.record_id, "tenant/a")
        verified = await engine.promote_skill(staged.record_id, "tenant/a")
        with pytest.raises(ConflictError, match="passing dry run"):
            await engine.promote_skill(verified.record_id, "tenant/a")
        active = await engine.promote_skill(verified.record_id, "tenant/a", dry_run_passed=True)
        assert [r.record_id for r in await engine.skills("tenant/a", usable_only=True)] == [
            active.record_id
        ]
        assert await engine.skills("tenant/b", usable_only=True) == []
        with pytest.raises(ConflictError, match="no such record"):
            await engine.promote_skill(active.record_id, "tenant/b")

        plan = await engine.record_plan("rotate signing keys", "1. Prepare\n2. Rotate", "tenant/a")
        assert await engine.recall_plan("rotate signing keys", "tenant/b") is None
        # Plans are cached by task embedding, but only after promotion to usable.
        assert await engine.recall_plan("rotate signing keys", "tenant/a") is None
        plan = await engine.promote_skill(plan.record_id, "tenant/a")
        plan = await engine.promote_skill(plan.record_id, "tenant/a", dry_run_passed=True)
        assert (
            await engine.recall_plan("rotate signing keys", "tenant/a")
        ).record_id == plan.record_id
    finally:
        await engine.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("cache_provider", ["memory", "disk"])
async def test_reflections_enforce_parent_namespace_depth_and_trust_lineage(
    tmp_path: Path, cache_provider: str
) -> None:
    engine = _engine(tmp_path, cache_provider=cache_provider)
    await engine.start()
    try:
        parent = await engine.write(
            "The archive key is stored in vault seven.",
            namespace="tenant/reflection",
            memory_type="episodic",
            source=SourceInfo(role="user", channel="external", principal="mira"),
        )
        foreign = await engine.write(
            "A foreign tenant's unrelated source.",
            namespace="tenant/foreign",
            memory_type="episodic",
        )
        reflection = await engine.reflect(
            "The archive key belongs in a protected vault.",
            [parent.record_id],
            namespace="tenant/reflection",
        )
        assert reflection.source.parents == [parent.record_id]
        assert reflection.trust <= parent.trust
        assert reflection.trust == parent.trust < 0.5
        with pytest.raises(ConflictError, match="does not exist"):
            await engine.reflect("cross tenant", [foreign.record_id], namespace="tenant/reflection")

        meta = await engine.reflect(
            "The general lesson is to protect sensitive keys.",
            [reflection.record_id],
            namespace="tenant/reflection",
        )
        assert meta.reflection_depth == reflection.reflection_depth + 1
        with pytest.raises(ConflictError):
            await engine.reflect("depth three", [meta.record_id], namespace="tenant/reflection")
        persisted = await engine.retrieve("tenant/reflection", "reflective")
        assert {reflection.record_id, meta.record_id} <= {r.record_id for r in persisted}
        assert not await engine.search(
            "The general lesson is to protect sensitive keys.",
            namespace="tenant/foreign",
            top_k=10,
            memory_types=["reflective"],
        )
    finally:
        await engine.stop()
