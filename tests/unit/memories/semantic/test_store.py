"""Semantic write pipeline (M13.3): dedup merge, conflict verdicts, bi-temporal."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from memspine.clients.sqlite import SQLiteClient, SQLiteSpec
from memspine.core.policies.conflict import ConflictPolicy
from memspine.core.policies.dedup import DedupPolicy
from memspine.memories.semantic.facts import fact_at
from memspine.memories.semantic.store import SemanticMemory, _restated_support
from memspine.prompts.models import ExtractedFact
from memspine.schemas.durable.assessment import Assessment
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import Evidence, TurnRef
from memspine.schemas.durable.identity import AssertionKey
from memspine.schemas.durable.semantic import (
    Mention,
    PredicateTyping,
    RoleBinding,
    SemanticEnvelope,
)
from memspine.semantics.registry import load_builtin_registry
from memspine.services.embedding.hash import HashEmbedding
from memspine.services.storage.projector import RecordProjector
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord, RecordStatus, RestatementSupportProof
from memspine.trust.restatement import claim_digest, valid_proof

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
    client = SQLiteClient(SQLiteSpec(":memory:"))
    await client.open()
    storage = SQLiteStorage(client)
    await storage.start()
    projector = RecordProjector(storage)

    async def append_and_project(event: MemoryEvent) -> None:
        appended = await storage.append_event(event)
        assert appended.seq is not None
        await projector.apply(appended)

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
    forged = rec("alice prefers black coffee")
    forged = forged.model_copy(
        update={
            "fingerprint": forged.fingerprint.model_copy(
                update={
                    "restatement_support": RestatementSupportProof(
                        claim_digest="a" * 64, source_ids=["source-a", "source-b"]
                    )
                }
            )
        }
    )
    result = await semantic.write(forged)
    assert result.action == "added"
    stored = await semantic._storage.get_record(result.record.record_id)
    assert stored is not None
    assert stored.simhash is not None and stored.minhash_sig  # D-27 at rest
    assert stored.fingerprint.restatement_support is None  # caller proof is never accepted


def test_restatement_merge_carries_each_source_role_evidence() -> None:
    def claim(source_id: str, binding_id: str) -> MemoryRecord:
        turn = TurnRef(source_id=source_id, source_version=1, seg_index=0)
        role_evidence = Evidence(role="binding", turn=turn, source_family="Asha")
        env = SemanticEnvelope(
            write_id=f"write-{source_id}",
            assertion_key=AssertionKey(src="asha", rel="resides_in", dst="sweden", kind="event"),
            occurrence_id=f"occ-{source_id}",
            scope=OwnerScope(tenant="default", owner="agent/a"),
            text="Asha resides in Sweden",
            kind="event",
            subject=Mention(surface="Asha", mention_id=f"mention-{source_id}"),
            attribute="resides_in",
            value="Sweden",
            speaker="Asha",
            evidence=[Evidence(role="support", turn=turn, source_family="Asha")],
            assessment=Assessment(source_trust=0.8, binding="bound", modality="asserted"),
            src_class="user_stated",
            role_bindings=[
                RoleBinding(
                    binding_id=binding_id,
                    role_name="resident",
                    role_version=1,
                    value_kind="entity",
                    value="Sweden",
                    entity_type="place",
                    evidence=[role_evidence],
                )
            ],
        )
        return MemoryRecord(
            namespace="agent/a",
            memory_type="semantic",
            content="Asha resides in Sweden",
            source={"parents": [source_id]},
            fingerprint={"envelope": env.model_dump(mode="json")},
        )

    kept = claim("source-a", "binding-a")
    incoming = claim("source-b", "binding-b")
    update = _restated_support(kept, incoming)
    merged = kept.model_copy(update=update)
    role_binding = merged.fingerprint.envelope["role_bindings"][0]
    evidence_sources = {
        item["turn"]["source_id"] for item in role_binding["evidence"] if item.get("turn")
    }
    assert evidence_sources == {"source-a", "source-b"}
    assert set(valid_proof(merged)) == {"source-a", "source-b"}
    flagged_env = {**merged.fingerprint.envelope, "cardinality_conflict": True}
    flagged = merged.model_copy(
        update={"fingerprint": merged.fingerprint.model_copy(update={"envelope": flagged_env})}
    )
    assert set(valid_proof(flagged)) == {"source-a", "source-b"}
    changed_value = {**flagged_env, "value": "Norway"}
    changed = flagged.model_copy(
        update={"fingerprint": flagged.fingerprint.model_copy(update={"envelope": changed_value})}
    )
    assert valid_proof(changed) == ()
    changed_scope = {**flagged_env, "scope": {**flagged_env["scope"], "owner": "agent/b"}}
    scoped = flagged.model_copy(
        update={"fingerprint": flagged.fingerprint.model_copy(update={"envelope": changed_scope})}
    )
    assert valid_proof(scoped) == ()


def test_cardinality_flag_is_not_part_of_restatement_identity() -> None:
    semantic = load_builtin_registry()
    assert semantic.predicates["born_in"].cardinality == "one"
    raw = SemanticEnvelope(
        write_id="write-a",
        assertion_key=AssertionKey(src="mira", rel="born_in", dst="lisbon", kind="state"),
        occurrence_id="occ-a",
        scope=OwnerScope(tenant="default", owner="agent/a"),
        text="Mira was born in Lisbon",
        kind="state",
        subject=Mention(surface="Mira", mention_id="m-a"),
        attribute="born_in",
        value="Lisbon",
        evidence=[
            Evidence(
                role="support",
                turn=TurnRef(source_id="source-a", source_version=1, seg_index=0),
                source_family="Mira",
            )
        ],
        assessment=Assessment(source_trust=0.8, binding="bound", modality="asserted"),
        src_class="user_stated",
    ).model_dump(mode="json")
    assert claim_digest(raw) == claim_digest({**raw, "cardinality_conflict": True})


async def test_near_duplicate_merges_union_preserving() -> None:
    semantic = await make_semantic()
    from memspine.store.records import PiiTier

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


async def test_scoped_canonical_claims_keep_competing_supported_values() -> None:
    semantic = await make_semantic()
    registry = load_builtin_registry()

    def typed_claim(
        place: str,
        source_id: str,
        *,
        subject_id: str = "mira",
        precision: str = "city",
        trust: float = 0.5,
        tags: list[str] | None = None,
    ) -> MemoryRecord:
        env = SemanticEnvelope(
            write_id=f"write-{source_id}",
            assertion_key=AssertionKey(src=subject_id, rel="born_in", dst=place, kind="state"),
            occurrence_id=f"occ-{source_id}",
            scope=OwnerScope(tenant="default", owner="agent/a"),
            text=f"Mira was born in {place}",
            kind="state",
            subject=Mention(
                surface="Mira", mention_id=f"mention-{source_id}", entity_id=subject_id
            ),
            attribute="born_in",
            value=place,
            evidence=[
                Evidence(
                    role="support",
                    turn=TurnRef(source_id=source_id, source_version=1, seg_index=0),
                    source_family=source_id,
                )
            ],
            assessment=Assessment(source_trust=0.8, binding="bound", modality="asserted"),
            src_class="user_stated",
            registry=PredicateTyping(
                predicate="born_in",
                predicate_version=registry.predicates["born_in"].version,
                registry_digest=registry.digest,
                subject_type="person",
                object_kind="entity",
                object_entity_type="place",
                cardinality="one",
                cardinality_scope={"precision": precision},
            ),
        )
        return MemoryRecord(
            namespace="agent/a",
            memory_type="semantic",
            content="Mira birthplace fact",
            source={"parents": [source_id]},
            entity="Mira",
            attribute="born_in",
            fingerprint={"envelope": env.model_dump(mode="json")},
            valid_from=NOW,
            trust=trust,
            tags=tags or [],
        )

    a = await semantic.write(typed_claim("Lisbon", "source-lisbon"))
    b = await semantic.write(typed_claim("Porto", "source-porto"))
    assert {a.action, b.action} == {"added"}
    stored = [await semantic._storage.get_record(result.record.record_id) for result in (a, b)]
    assert all(record is not None and record.status is RecordStatus.ACTIVATED for record in stored)
    envelopes = [
        SemanticEnvelope.model_validate(record.fingerprint.envelope) for record in stored if record
    ]
    assert all(env.cardinality_conflict for env in envelopes)
    assert {env.value for env in envelopes} == {"Lisbon", "Porto"}
    assert {env.evidence[0].turn.source_id for env in envelopes if env.evidence[0].turn} == {
        "source-lisbon",
        "source-porto",
    }
    same = await semantic.write(typed_claim("Lisbon", "source-lisbon-again"))
    assert same.action == "merged" and same.record.record_id == a.record.record_id
    same_env = SemanticEnvelope.model_validate(same.record.fingerprint.envelope)
    assert {item.turn.source_id for item in same_env.evidence if item.turn} == {
        "source-lisbon",
        "source-lisbon-again",
    }
    correction = typed_claim("Madrid", "source-madrid")
    correction_env = SemanticEnvelope.model_validate(correction.fingerprint.envelope).model_copy(
        update={"change_hint": True}
    )
    correction = correction.model_copy(
        update={
            "fingerprint": correction.fingerprint.model_copy(
                update={"envelope": correction_env.model_dump(mode="json")}
            )
        }
    )
    corrected = await semantic.write(correction)
    assert corrected.action == "added"  # change hint cannot choose between two live predecessors
    assert corrected.record.status is RecordStatus.ACTIVATED
    for prior_id in (a.record.record_id, b.record.record_id):
        prior = await semantic._storage.get_record(prior_id)
        assert prior is not None and prior.status is RecordStatus.ACTIVATED

    other_scope = await semantic.write(typed_claim("Faro", "source-region", precision="region"))
    other_subject = await semantic.write(
        typed_claim("Braga", "source-other-subject", subject_id="other")
    )
    assert other_scope.action == other_subject.action == "added"
    for result in (other_scope, other_subject):
        stored_result = await semantic._storage.get_record(result.record.record_id)
        assert stored_result is not None and stored_result.status is RecordStatus.ACTIVATED

    # With one comparable predecessor, an explicit typed change still uses M4.
    unique = await make_semantic()
    old = await unique.write(typed_claim("Lisbon", "unique-old"))
    change = typed_claim("Porto", "unique-change")
    change_env = SemanticEnvelope.model_validate(change.fingerprint.envelope).model_copy(
        update={"change_hint": True}
    )
    change = change.model_copy(
        update={
            "fingerprint": change.fingerprint.model_copy(
                update={"envelope": change_env.model_dump(mode="json")}
            )
        }
    )
    changed = await unique.write(change)
    old_after = await unique._storage.get_record(old.record.record_id)
    assert changed.action == "updated"
    assert old_after is not None and old_after.status is RecordStatus.ARCHIVED
    low = typed_claim("Braga", "low-trust", trust=0.1)
    low_env = SemanticEnvelope.model_validate(low.fingerprint.envelope).model_copy(
        update={"change_hint": True}
    )
    low = low.model_copy(
        update={
            "fingerprint": low.fingerprint.model_copy(
                update={"envelope": low_env.model_dump(mode="json")}
            )
        }
    )
    assert (await unique.write(low)).action == "rejected"
    unique._conflict = ConflictPolicy.bind({"inferred_defers": True})
    inferred = typed_claim("Braga", "inferred", tags=["src:inferred"])
    inferred_env = SemanticEnvelope.model_validate(inferred.fingerprint.envelope).model_copy(
        update={"change_hint": True}
    )
    inferred = inferred.model_copy(
        update={
            "fingerprint": inferred.fingerprint.model_copy(
                update={"envelope": inferred_env.model_dump(mode="json")}
            )
        }
    )
    assert (await unique.write(inferred)).action == "rejected"
    unique._conflict = ConflictPolicy.bind()
    extras_only = typed_claim("Braga", "extras-only")
    extras_env = SemanticEnvelope.model_validate(extras_only.fingerprint.envelope).model_copy(
        update={"extras": {"replaces": changed.record.record_id}}
    )
    extras_only = extras_only.model_copy(
        update={
            "fingerprint": extras_only.fingerprint.model_copy(
                update={"envelope": extras_env.model_dump(mode="json")}
            )
        }
    )
    unsupported_hint = await unique.write(extras_only)
    still_current = await unique._storage.get_record(changed.record.record_id)
    assert unsupported_hint.action == "added"
    assert still_current is not None and still_current.status is RecordStatus.ACTIVATED

    for precision, value, expected in (
        ("country", "Porto", "added"),
        ("city", "Porto", "added"),
        ("city", "Lisbon", "invalidated"),
    ):
        retractions = await make_semantic()
        prior = await retractions.write(typed_claim("Lisbon", "retract-prior"))
        incoming = typed_claim(
            value, f"retract-{precision}-{value}", precision=precision, tags=["retract"]
        )
        result = await retractions.write(incoming)
        prior_after = await retractions._storage.get_record(prior.record.record_id)
        assert result.action == expected
        assert prior_after is not None
        assert (prior_after.status is RecordStatus.ARCHIVED) == (expected == "invalidated")

    negation = await make_semantic()
    prior = await negation.write(typed_claim("Lisbon", "negated-prior"))
    denied = typed_claim("Porto", "negated-other-value")
    denied_env = SemanticEnvelope.model_validate(denied.fingerprint.envelope)
    denied_env = denied_env.model_copy(
        update={
            "change_hint": True,
            "assessment": denied_env.assessment.model_copy(update={"modality": "negated"}),
        }
    )
    denied = denied.model_copy(
        update={
            "fingerprint": denied.fingerprint.model_copy(
                update={"envelope": denied_env.model_dump(mode="json")}
            )
        }
    )
    assert (await negation.write(denied)).action == "added"
    prior_after = await negation._storage.get_record(prior.record.record_id)
    assert prior_after is not None and prior_after.status is RecordStatus.ACTIVATED

    for trust, action in ((0.5, "invalidated"), (0.1, "rejected")):
        matching = await make_semantic()
        prior = await matching.write(typed_claim("Lisbon", f"negated-prior-{trust}"))
        denied = typed_claim("Lisbon", f"negated-same-{trust}", trust=trust)
        denied_env = SemanticEnvelope.model_validate(denied.fingerprint.envelope)
        denied_env = denied_env.model_copy(
            update={
                "change_hint": True,
                "assessment": denied_env.assessment.model_copy(update={"modality": "negated"}),
            }
        )
        denied = denied.model_copy(
            update={
                "fingerprint": denied.fingerprint.model_copy(
                    update={"envelope": denied_env.model_dump(mode="json")}
                )
            }
        )
        assert (await matching.write(denied)).action == action
        prior_after = await matching._storage.get_record(prior.record.record_id)
        assert prior_after is not None
        assert (prior_after.status is RecordStatus.ARCHIVED) == (action == "invalidated")

    for tags in ([], ["retract"]):
        mixed = await make_semantic()
        prior = await mixed.write(typed_claim("Lisbon", "typed-prior"))
        untyped = MemoryRecord(
            namespace="agent/a",
            memory_type="semantic",
            content="Mira was born in Porto",
            entity="Mira",
            attribute="born_in",
            tags=tags,
            valid_from=NOW,
        )
        result = await mixed.write(untyped)
        prior_after = await mixed._storage.get_record(prior.record.record_id)
        assert result.action == "added"
        assert prior_after is not None and prior_after.status is RecordStatus.ACTIVATED

    interleaved = await make_semantic()
    berlin = await interleaved.write(
        MemoryRecord(
            namespace="agent/a",
            memory_type="semantic",
            content="Mira was born in Berlin",
            entity="Mira",
            attribute="born_in",
            valid_from=NOW,
        )
    )
    lisbon = await interleaved.write(typed_claim("Lisbon", "typed-middle"))
    porto = await interleaved.write(
        MemoryRecord(
            namespace="agent/a",
            memory_type="semantic",
            content="Mira was born in Porto",
            entity="Mira",
            attribute="born_in",
            valid_from=NOW,
        )
    )
    berlin_after = await interleaved._storage.get_record(berlin.record.record_id)
    lisbon_after = await interleaved._storage.get_record(lisbon.record.record_id)
    assert porto.action == "updated"
    assert berlin_after is not None and berlin_after.status is RecordStatus.ARCHIVED
    assert lisbon_after is not None and lisbon_after.status is RecordStatus.ACTIVATED


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


def _claim_record(text: str, record_id: str) -> MemoryRecord:
    """A rules claim (canonical envelope) mined from one message ``record_id``."""
    from memspine.derive.non_generative_facts import compile_grounded_facts, mine_grounded_facts
    from memspine.schemas.durable.common import OwnerScope
    from memspine.store.records import RecordFingerprint, SourceInfo

    scope = OwnerScope(tenant="t", owner="agent/a")
    source = MemoryRecord(
        record_id=record_id,
        namespace="agent/a",
        memory_type="episodic",
        content=text,
        source=SourceInfo(
            role="user",
            channel="messages",
            message={"message_id": record_id, "sender": {"id": "asha"}},
        ),
    )
    [env] = compile_grounded_facts(mine_grounded_facts(source, scope), [source], scope).envelopes
    return MemoryRecord(
        namespace="agent/a",
        memory_type="semantic",
        content=env.text,
        valid_from=NOW,
        source=SourceInfo(role="assistant", channel="mining", parents=[record_id]),
        fingerprint=RecordFingerprint(envelope=env.model_dump(mode="json")),
    )


async def test_restated_claim_keeps_every_supporting_source() -> None:
    """RM-22: a restatement merged into a canonical claim adds its source to the claim's
    parents and evidence, so the claim stays supported when one source is erased."""
    semantic = await make_semantic()
    first = await semantic.write(_claim_record("I live in Sweden.", "src-june"))
    result = await semantic.write(_claim_record("Yes, I live in Sweden.", "src-july"))
    assert result.action == "merged" and result.record.record_id == first.record.record_id
    assert result.record.source.parents == ["src-june", "src-july"]
    envelope = result.record.fingerprint.envelope
    assert envelope is not None
    assert [e["turn"]["source_id"] for e in envelope["evidence"]] == ["src-june", "src-july"]
    stored = await semantic._storage.get_record(first.record.record_id)
    assert stored is not None and stored.source.parents == ["src-june", "src-july"]


async def test_plain_record_merge_keeps_its_parents() -> None:
    semantic = await make_semantic()
    base = rec("x").source
    a = rec("alice prefers her coffee black in the morning")
    b = rec("alice prefers her coffee black in the morning !")
    await semantic.write(
        a.model_copy(update={"source": base.model_copy(update={"parents": ["p1"]})})
    )
    result = await semantic.write(
        b.model_copy(update={"source": base.model_copy(update={"parents": ["p2"]})})
    )
    assert result.action == "merged" and result.record.source.parents == ["p1"]
