"""Semantic write pipeline (M13.3): dedup merge, conflict verdicts, bi-temporal."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from memspine.clients.sqlite import SQLiteClient
from memspine.core.events import EventKind, MemoryEvent
from memspine.core.policies.conflict import ConflictPolicy
from memspine.core.policies.dedup import DedupPolicy
from memspine.core.records import MemoryRecord, RecordStatus
from memspine.memories.semantic.facts import fact_at
from memspine.memories.semantic.store import SemanticMemory
from memspine.prompts.models import ExtractedFact
from memspine.services.embedding.hash_local import HashEmbedding
from memspine.services.storage.projector import RecordProjector
from memspine.services.storage.sqlite.engine import SQLiteStorage

NOW = datetime(2026, 7, 7, tzinfo=UTC)


class StubExtractor:
    """Deterministic extractor: 'entity=X attribute=Y' patterns in content."""

    prompt_version = "extract@test"

    async def extract(self, content: str) -> list[ExtractedFact]:
        parts = dict(token.split("=", 1) for token in content.split() if "=" in token)
        if "entity" not in parts:
            return []
        return [
            ExtractedFact(
                entity=parts["entity"], attribute=parts.get("attribute", "fact"), value=content
            )
        ]


async def make_semantic(extractor: StubExtractor | None = None) -> SemanticMemory:
    client = SQLiteClient(":memory:")
    await client.connect()
    storage = SQLiteStorage(client)
    await storage.start()
    projector = RecordProjector(storage)

    async def append_and_project(event: MemoryEvent) -> None:
        appended = await storage.append_event(event)
        assert appended.seq is not None
        await projector.apply(appended)
        await storage.set_offset(projector.name, appended.seq)

    return SemanticMemory(
        storage=storage,
        embedder=HashEmbedding(dim=64),
        append_event=append_and_project,
        conflict=ConflictPolicy.bind(),
        dedup=DedupPolicy.bind({"lsh_threshold": 0.5, "cosine_threshold": 0.9}),
        extractor=extractor,
    )


def rec(
    content: str, days_ago: float = 0.0, trust: float = 0.5, ns: str = "agent/a"
) -> MemoryRecord:
    return MemoryRecord(
        namespace=ns,
        memory_type="semantic",
        content=content,
        valid_from=NOW - timedelta(days=days_ago),
        trust=trust,
    )


async def test_plain_add_annotates_sketches() -> None:
    semantic = await make_semantic()
    result = await semantic.write(rec("alice prefers black coffee"))
    assert result.action == "added"
    stored = await semantic._storage.get_record(result.record.record_id)
    assert stored is not None
    assert stored.simhash is not None and stored.minhash_sig  # D-27 at rest


async def test_near_duplicate_merges_union_preserving() -> None:
    semantic = await make_semantic()
    from memspine.core.records import PiiTier

    first = await semantic.write(rec("alice prefers her coffee black in the morning"))
    dup = rec("alice prefers her coffee black in the morning !")
    dup = dup.model_copy(update={"consent_tags": ["chat"], "pii_tier": PiiTier.LOW})

    result = await semantic.write(dup)
    assert result.action == "merged"
    assert result.record.record_id == first.record.record_id  # kept identity
    assert result.record.consent_tags == ["chat"]  # union preserved
    assert result.record.pii_tier.value == "low"  # governance maxes upward
    assert result.record.scoring.importance > 0.0  # reinforcement

    events = await semantic._storage.read_events()
    assert EventKind.MERGE in {event.kind for event in events}  # audit trail


async def test_conflicting_fact_updates_bi_temporally() -> None:
    semantic = await make_semantic(StubExtractor())
    old = await semantic.write(rec("entity=alice attribute=city value Berlin", days_ago=30))
    new = await semantic.write(rec("entity=alice attribute=city value Paris", days_ago=0))
    assert new.action == "updated"

    old_stored = await semantic._storage.get_record(old.record.record_id)
    assert old_stored is not None
    assert old_stored.status is RecordStatus.ARCHIVED
    assert old_stored.valid_to == new.record.valid_from  # closed interval (M4)
    assert old_stored.evolve_to == new.record.record_id  # lifecycle chain (D-42)

    active = await semantic._storage.find_active_fact("agent/a", "alice", "city")
    assert active is not None and active.record_id == new.record.record_id

    # Point-in-time (bi-temporal): two weeks ago the city was still Berlin.
    then = await fact_at(semantic._storage, "agent/a", "alice", "city", NOW - timedelta(days=14))
    assert then is not None and then.record_id == old.record.record_id

    events = await semantic._storage.read_events()
    assert EventKind.CONFLICT in {event.kind for event in events}


async def test_untrusted_write_is_rejected_by_r1() -> None:
    semantic = await make_semantic(StubExtractor())
    trusted = await semantic.write(
        rec("entity=alice attribute=city value Berlin", days_ago=30, trust=0.9)
    )
    attack = await semantic.write(rec("entity=alice attribute=city value Attackerville", trust=0.1))
    assert attack.action == "rejected"
    assert attack.record.record_id == trusted.record.record_id  # existing survives
    active = await semantic._storage.find_active_fact("agent/a", "alice", "city")
    assert active is not None and "Berlin" in active.content


async def test_historical_backfill_gets_closed_validity() -> None:
    semantic = await make_semantic(StubExtractor())
    current = await semantic.write(rec("entity=alice attribute=city value Berlin", days_ago=10))
    backfill = await semantic.write(
        rec("entity=alice attribute=city value Munich decades earlier", days_ago=365)
    )
    assert backfill.action == "added"
    assert backfill.record.valid_to == current.record.valid_from  # closed history
    active = await semantic._storage.find_active_fact("agent/a", "alice", "city")
    assert active is not None and active.record_id == current.record.record_id


async def test_extractor_provenance_lands_in_source() -> None:
    semantic = await make_semantic(StubExtractor())
    result = await semantic.write(rec("entity=bob attribute=role value engineer"))
    assert result.record.source.prompt_version == "extract@test"  # E1 audit trail


async def test_reasserted_fact_reaches_the_ladder_not_the_tombstone() -> None:
    """Regression (empirically found): a re-asserted old fact used to merge
    into its ARCHIVED tombstone, silently vanishing instead of superseding."""
    semantic = await make_semantic(StubExtractor())
    paris1 = await semantic.write(rec("entity=alice attribute=city lives in paris", days_ago=60))
    await semantic.write(rec("entity=alice attribute=city lives in london", days_ago=30))
    # Re-assert the old fact, newer than everything: must UPDATE, not merge.
    back = await semantic.write(rec("entity=alice attribute=city lives in paris", days_ago=0))
    assert back.action == "updated"
    assert back.record.record_id != paris1.record.record_id  # not the tombstone
    active = await semantic._storage.find_active_fact("agent/a", "alice", "city")
    assert active is not None and active.record_id == back.record.record_id


async def test_invalidate_verdict_archives_without_successor() -> None:
    """INVALIDATE (M4): existing fact archived with no evolve_to; the negation
    itself lands archived with a closed validity interval."""
    from memspine.core.policies.conflict import ConflictVerdict

    semantic = await make_semantic(StubExtractor())
    current = await semantic.write(rec("entity=alice attribute=city value Berlin", days_ago=30))

    class AlwaysInvalidate:
        def resolve(self, incoming: MemoryRecord, existing: MemoryRecord) -> ConflictVerdict:
            return ConflictVerdict.INVALIDATE

    semantic._conflict = AlwaysInvalidate()  # type: ignore[assignment]
    negation = await semantic.write(rec("entity=alice attribute=city no longer known"))
    assert negation.action == "invalidated"
    assert negation.record.status is RecordStatus.ARCHIVED
    assert negation.record.valid_to == negation.record.valid_from  # asserts an end

    old = await semantic._storage.get_record(current.record.record_id)
    assert old is not None and old.status is RecordStatus.ARCHIVED
    assert old.evolve_to is None  # no successor: the fact simply ended
    assert await semantic._storage.find_active_fact("agent/a", "alice", "city") is None


async def test_concurrent_same_key_writes_leave_one_active_fact() -> None:
    """Regression: the find-active-fact -> write sequence is serialized per
    namespace, so racing writers cannot both create active facts."""
    import asyncio

    semantic = await make_semantic(StubExtractor())
    await asyncio.gather(
        semantic.write(rec("entity=alice attribute=city first value one two three")),
        semantic.write(rec("entity=alice attribute=city second value four five six")),
        semantic.write(rec("entity=alice attribute=city third value seven eight nine")),
    )
    records = await semantic._storage.list_records("agent/a", "semantic")
    active = [r for r in records if r.valid_to is None and r.status is RecordStatus.ACTIVATED]
    assert len(active) == 1  # exactly one open-validity fact per key


async def test_bias_oldest_never_leaves_two_active_facts() -> None:
    """Regression (empirically found): bias='oldest' + newer incoming used to
    leave BOTH records active, breaking the single-active-fact invariant."""
    semantic = await make_semantic(StubExtractor())
    semantic._conflict = ConflictPolicy.bind({"bias": "oldest"})
    await semantic.write(rec("entity=alice attribute=city value Berlin", days_ago=30))
    newer = await semantic.write(rec("entity=alice attribute=city value Paris", days_ago=0))
    assert newer.action == "added"
    assert newer.record.valid_to is not None  # recorded, never current

    records = await semantic._storage.list_records("agent/a", "semantic")
    active = [r for r in records if r.valid_to is None and r.status is RecordStatus.ACTIVATED]
    assert len(active) == 1 and "Berlin" in active[0].content  # oldest wins


async def test_stage2_embeds_candidates_in_one_batch() -> None:
    """Regression: stage-2 confirm used N+1 embed calls; must be one batch."""
    calls: list[int] = []

    class CountingEmbedder(HashEmbedding):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            calls.append(len(texts))
            return await super().embed(texts)

    semantic = await make_semantic()
    semantic._embedder = CountingEmbedder(dim=64)
    await semantic.write(rec("alice prefers her coffee black in the morning"))
    calls.clear()
    result = await semantic.write(rec("alice prefers her coffee black in the morning !!"))
    assert result.action == "merged"
    assert len(calls) == 1  # incoming + all candidates in ONE embed call


async def test_audit_events_carry_full_rejected_snapshot() -> None:
    """E1: a rejected/dropped write must be recoverable from the log alone."""
    semantic = await make_semantic(StubExtractor())
    await semantic.write(rec("entity=alice attribute=city value Berlin", days_ago=30, trust=0.9))
    rejected = rec("entity=alice attribute=city value Attackerville", trust=0.1)
    await semantic.write(rejected)

    events = await semantic._storage.read_events()
    conflict_events = [e for e in events if e.kind is EventKind.CONFLICT]
    assert conflict_events, "conflict audit event missing"
    snapshot = conflict_events[-1].payload["incoming_record"]
    assert "Attackerville" in snapshot["content"]
    assert snapshot["trust"] == 0.1  # provenance + trust recoverable (E1)
