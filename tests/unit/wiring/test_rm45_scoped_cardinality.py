"""Canonical RM-45 claim flags through the public rules-only Engine path."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from memspine import Engine
from memspine.schemas.durable.assessment import Assessment
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import Evidence, TurnRef
from memspine.schemas.durable.identity import AssertionKey
from memspine.schemas.durable.semantic import Mention, PredicateTyping, SemanticEnvelope
from memspine.semantics.registry import load_builtin_registry
from memspine.store.claim_cardinality import UnknownRegistryDigest, record_group_of, value_token
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord, RecordStatus
from memspine.trust.privacy import read_scope


async def test_public_rules_path_keeps_two_supported_measurements() -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        deployment={"id": "rm45-test"},
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        write={
            "online_extractor": True,
            "window_extract": "facts",
            "window_async": False,
            "window_max_turns": 1,
            "window_idle_s": 0.0,
        },
        workers={"outbox": True},
    )
    await eng.start()
    try:
        for mid, text in (
            ("measure-1", "The hike is 5 km long."),
            ("measure-2", "The hike is 6 km long."),
        ):
            await eng.ingest_messages(
                [
                    {
                        "message_id": mid,
                        "role": "user",
                        "sender": {"id": "Mira"},
                        "content": text,
                        "created_at": "2026-10-24T10:00:00+00:00",
                    }
                ],
                namespace="a",
                session_id="s1",
                conversation_id="s1",
            )
            await eng.flush_windows("a")
        claims = [
            r
            for r in await eng.list_records("a", "semantic")
            if "non_generative" in r.tags and "predicate:has_measurement" in r.tags
        ]
        assert len(claims) == 2
        assert all(r.status is RecordStatus.ACTIVATED and r.valid_to is None for r in claims)
        envelopes = [await eng.get_envelope(r.record_id, "a") for r in claims]
        assert all(env is not None for env in envelopes)
        assert record_group_of(claims[0]) == record_group_of(claims[1])
        assert record_group_of(claims[0]) is not None
        assert value_token(envelopes[0]) != value_token(envelopes[1])
        assert all(env is not None and env.cardinality_conflict for env in envelopes)
        assert {env.registry.amount for env in envelopes if env and env.registry} == {5, 6}
        assert {
            env.evidence[0].turn.source_id for env in envelopes if env and env.evidence[0].turn
        } == {parent for r in claims for parent in r.source.parents}
        first, second = sorted(claims, key=lambda row: row.recorded_at)
        before = await eng.search(
            "hike length",
            namespace="a",
            memory_types=["semantic"],
            top_k=1,
            recorded_before=second.recorded_at,
        )
        assert before and before[0][0].record_id == first.record_id
        assert not before[0][0].fingerprint.envelope["cardinality_conflict"]
        after = await eng.search(
            "hike length",
            namespace="a",
            memory_types=["semantic"],
            top_k=1,
            recorded_before=datetime.now(UTC) + timedelta(days=1),
        )
        assert after and after[0][0].fingerprint.envelope["cardinality_conflict"]
    finally:
        await eng.stop()


def _born_in(place: str, purpose: str) -> MemoryRecord:
    registry = load_builtin_registry()
    env = SemanticEnvelope(
        write_id=f"write-{place}",
        assertion_key=AssertionKey(src="mira", rel="born_in", dst=place, kind="state"),
        occurrence_id=f"occ-{place}",
        scope=OwnerScope(tenant="a", owner="a"),
        text=f"Mira was born in {place}",
        kind="state",
        subject=Mention(surface="Mira", mention_id=f"m-{place}", entity_id="mira"),
        attribute="born_in",
        value=place,
        evidence=[
            Evidence(
                role="support",
                turn=TurnRef(source_id=f"source-{place}", source_version=1, seg_index=0),
                source_family=place,
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
            cardinality_scope={"precision": "city"},
        ),
    )
    return MemoryRecord(
        namespace="a",
        memory_type="semantic",
        content=env.text,
        entity="Mira",
        attribute="born_in",
        consent_tags=[purpose],
        fingerprint={"envelope": env.model_dump(mode="json")},
    )


async def test_public_conflict_flag_respects_current_consent_across_return_shapes() -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        deployment={"id": "rm45-consent"},
        storage={"records": {"provider": "sqlite", "settings": {"path": ":memory:"}}},
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"semantic": {"enabled": True}},
        consent={"enabled": True},
    )
    await eng.start()
    try:
        first, second = _born_in("Lisbon", "*"), _born_in("Porto", "diet")
        for record in (first, second):
            await eng._append_and_project(
                MemoryEvent(
                    kind=EventKind.WRITE,
                    namespace="a",
                    actor="system",
                    payload={"record": record.model_dump(mode="json")},
                )
            )
        with read_scope("diet"):
            before = await eng.get_envelope(first.record_id, "a")
            assert before is not None and before.cardinality_conflict
        await eng.consent_revoke("a", "diet")
        with read_scope(None):
            envelope = await eng.get_envelope(first.record_id, "a")
            record = await eng.get_record(first.record_id, "a")
            listed = await eng.list_records("a", "semantic")
            assert envelope is not None and not envelope.cardinality_conflict
            assert record is not None and not record.fingerprint.envelope["cardinality_conflict"]
            assert not next(
                r for r in listed if r.record_id == first.record_id
            ).fingerprint.envelope["cardinality_conflict"]
            assert (
                not record.model_dump(mode="json")["fingerprint"]["envelope"][  # REST shape
                    "cardinality_conflict"
                ]
            )
        search = await eng.search("Mira was born in Lisbon", namespace="a", top_k=1)
        assert search and search[0][0].record_id == first.record_id
        assert not search[0][0].fingerprint.envelope["cardinality_conflict"]
        read = await eng.read("Mira was born in Lisbon", namespace="a", mode="full")
        assert all(
            not r.fingerprint.envelope["cardinality_conflict"]
            for r in read.context.records
            if r.fingerprint.envelope is not None
        )
        # The read overlay never mutates the persisted current projection.
        persisted = await eng._require_started().get_record(first.record_id)
        assert persisted is not None and persisted.fingerprint.envelope["cardinality_conflict"]
    finally:
        await eng.stop()


async def test_peer_engine_erasure_hides_rival_in_current_and_known_time_reads(
    tmp_path: Path,
) -> None:
    def engine() -> Engine:
        return Engine(
            template="core",
            dotenv_path=None,
            deployment={"id": "rm45-peer-erasure"},
            storage={
                "records": {"provider": "sqlite", "settings": {"path": str(tmp_path / "peer.db")}}
            },
            model={"embedding": {"provider": "hash", "settings": {}}},
            memories={"semantic": {"enabled": True}},
        )

    writer, stale_reader = engine(), engine()
    await writer.start()
    await stale_reader.start()
    try:
        first, second = _born_in("Lisbon", "*"), _born_in("Porto", "*")
        for record in (first, second):
            await writer._append_and_project(
                MemoryEvent(
                    kind=EventKind.WRITE,
                    namespace="a",
                    payload={"record": record.model_dump(mode="json")},
                )
            )
        before = await stale_reader.get_envelope(first.record_id, "a")
        assert before is not None and before.cardinality_conflict
        await writer._append_and_project(
            MemoryEvent(
                kind=EventKind.MARKER,
                namespace="a",
                payload={
                    "marker": "erasure_manifest",
                    "manifest": {"ids": {"record_ids": []}},
                    "hidden": [second.record_id],
                },
            )
        )
        current = await stale_reader.get_envelope(first.record_id, "a")
        assert current is not None and not current.cardinality_conflict
        known = await stale_reader.search(
            "Mira was born in Lisbon",
            namespace="a",
            memory_types=["semantic"],
            top_k=1,
            recorded_before=datetime.now(UTC) + timedelta(days=1),
        )
        assert known and not known[0][0].fingerprint.envelope["cardinality_conflict"]
        await writer._append_and_project(
            MemoryEvent(
                kind=EventKind.MARKER,
                namespace="a",
                payload={"marker": "erasure_release", "record_ids": [second.record_id]},
            )
        )
        released = await stale_reader.get_envelope(first.record_id, "a")
        assert released is not None and released.cardinality_conflict
    finally:
        await stale_reader.stop()
        await writer.stop()


async def test_unknown_pinned_registry_is_rejected_before_canonical_append() -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        deployment={"id": "rm45-unknown-registry"},
        storage={"records": {"provider": "sqlite", "settings": {"path": ":memory:"}}},
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"semantic": {"enabled": True}},
    )
    await eng.start()
    try:
        record = _born_in("Lisbon", "*")
        raw = dict(record.fingerprint.envelope)
        raw["registry"] = {**raw["registry"], "registry_digest": "f" * 64}
        record = record.model_copy(
            update={"fingerprint": record.fingerprint.model_copy(update={"envelope": raw})}
        )
        before = await eng._require_started().read_events()
        with pytest.raises(UnknownRegistryDigest):
            await eng._append_and_project(
                MemoryEvent(
                    kind=EventKind.WRITE,
                    namespace="a",
                    actor="system",
                    payload={"record": record.model_dump(mode="json")},
                )
            )
        assert await eng._require_started().read_events() == before
        assert await eng._require_started().get_record(record.record_id) is None
    finally:
        await eng.stop()
