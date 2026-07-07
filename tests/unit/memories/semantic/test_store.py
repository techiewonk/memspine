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
