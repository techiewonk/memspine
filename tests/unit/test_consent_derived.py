"""#50 privacy review (findings 2 and 3): the purpose gate covers the read's lead
blocks and graph blocks, and derived records inherit their parents' purposes and
PII tier.

Fake miners and summarisers only; no model is called.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.config import constants
from memspine.core.privacy import (
    PURPOSE_ANY,
    PURPOSE_NONE,
    inherited_consent,
    inherited_pii,
    purpose_allows,
    read_scope,
)
from memspine.core.records import MemoryRecord, PiiTier, RecordStatus, SourceInfo
from memspine.prompts.models import ExtractedFact
from memspine.workers.pipelines import entity_summary_name, summarize_entities

T0 = datetime(2023, 5, 8, 13, 0, tzinfo=UTC)

# ── the inheritance rule ─────────────────────────────────────────────────────


def test_inherited_consent_is_the_intersection() -> None:
    assert inherited_consent([[], []]) == []
    assert inherited_consent([["medical"], []]) == ["medical"]
    assert inherited_consent([["medical", "care"], ["care", "x"]]) == ["care"]
    assert inherited_consent([["medical"], ["marketing"]]) == [PURPOSE_NONE]
    assert inherited_consent([[PURPOSE_ANY], ["medical"]]) == ["medical"]
    assert inherited_consent([[PURPOSE_ANY], []]) == [PURPOSE_ANY]
    assert inherited_consent([["medical"], []], "deny") == [PURPOSE_NONE]
    assert inherited_consent([[], []], "deny") == []


def test_inherited_pii_is_the_maximum() -> None:
    assert inherited_pii([]) is PiiTier.NONE
    assert inherited_pii(["low", PiiTier.REGULATED, "none"]) is PiiTier.REGULATED


def test_no_purpose_marker_matches_no_read() -> None:
    from memspine.config.schema import ConsentConfig

    record = MemoryRecord(
        namespace="a", memory_type="semantic", content="x", consent_tags=[PURPOSE_NONE]
    )
    consent = ConsentConfig(enforce=True)
    assert not purpose_allows(record, PURPOSE_NONE, consent)
    assert not purpose_allows(record, "medical", consent)


# ── finding 2: lead blocks and graph admission honour the purpose ──────────


def _lead_engine() -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        read={
            "hybrid": False,
            "standing_instructions": True,
            "topic_timelines": True,
            "standing_min_trust": 0.0,
        },
        memories={
            "semantic": {"enabled": True},
            "episodic": {"enabled": True},
            "reflective": {"enabled": True},
        },
        consent={"enforce": True},
    )


async def _seed_lead(eng: Engine) -> None:
    await eng.write(
        "From now on, please always mention my medication schedule SECRETSTANDING",
        memory_type="episodic",
        source=SourceInfo(role="user"),
        purposes=["medical"],
    )
    for city in ("Paris SECRETTL1", "Berlin SECRETTL2"):
        await eng.write(
            f"Alice lives in {city}",
            memory_type="semantic",
            entity="Alice",
            attribute="city",
            purposes=["medical"],
        )
    await eng.write(
        "Alice works at Acme", memory_type="semantic", entity="Alice", attribute="employer"
    )


@pytest.mark.parametrize("mode", ["retrieve", "replay"])
async def test_lead_blocks_respect_the_read_purpose(mode: str) -> None:
    eng = _lead_engine()
    await eng.start()
    try:
        await _seed_lead(eng)
        query = "Where does Alice live and what about medication?"
        other = await eng.read(query, purpose="marketing", mode=mode)
        text = "\n".join(r.content for r in other.context.records)
        assert "SECRETSTANDING" not in text and "SECRETTL" not in text
        assert "Acme" in text
        own = await eng.read(query, purpose="medical", mode=mode)
        text = "\n".join(r.content for r in own.context.records)
        assert "SECRETSTANDING" in text and "SECRETTL1" in text
    finally:
        await eng.stop()


async def test_lead_record_carries_its_parts_consent_and_tier() -> None:
    eng = _lead_engine()
    await eng.start()
    try:
        parts = [
            MemoryRecord(
                namespace="a", memory_type="semantic", content="x", consent_tags=["medical", "care"]
            ),
            MemoryRecord(
                namespace="a",
                memory_type="semantic",
                content="y",
                consent_tags=["care"],
                pii_tier=PiiTier.HIGH,
            ),
            MemoryRecord(namespace="a", memory_type="semantic", content="z"),
        ]
        block = eng._lead_record("a", "block", parts)
        assert block.consent_tags == ["care"] and block.pii_tier is PiiTier.HIGH
        # The final purpose filter drops a block its parts would not all pass.
        with read_scope("marketing"):
            assert not eng._consent_ok(block)
    finally:
        await eng.stop()


async def test_graph_admit_applies_purpose_and_passive_gates() -> None:
    eng = _lead_engine()
    await eng.start()
    try:
        admit = eng._graph_admit("a")
        tagged = MemoryRecord(
            namespace="a", memory_type="semantic", content="x", consent_tags=["medical"]
        )
        with read_scope("marketing"):
            assert not admit(tagged)
        with read_scope("medical"):
            assert admit(tagged)
        passive = MemoryRecord(namespace="a", memory_type="semantic", content="p")
        passive.scoring.passive = True
        assert not admit(passive)
    finally:
        await eng.stop()


# ── finding 3: derived records inherit purposes and PII tier ────────────────


def _mining_engine(**consolidation: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        read={"hybrid": False, "record_access": False},
        memories={
            "episodic": {
                "enabled": True,
                "policies": {"consolidation": {"mine_facts": True, **consolidation}},
            },
            "semantic": {"enabled": True},
        },
        consent={"enforce": True},
    )


async def _tagged_session(eng: Engine) -> None:
    turns = [
        "Melanie: I did pottery and went camping this year",
        "Caroline: wow, that is a lot",
        "Melanie: my HIV clinic visit is on Friday",
    ]
    for i, text in enumerate(turns):
        await eng.write(
            text,
            namespace="a",
            memory_type="episodic",
            source=SourceInfo(role="user", channel="message"),
            session_id="s1",
            group_id="s1",
            valid_from=T0 + timedelta(minutes=i),
            purposes=["medical", "care"] if i != 1 else ["medical"],
            pii_tier=PiiTier.REGULATED if i == 2 else PiiTier.LOW,
        )


def _fake_miner() -> Any:
    async def mine(text: str) -> list[ExtractedFact]:
        return [
            ExtractedFact(entity="Melanie", attribute="activity", value=v, date=d)
            for v, d in (
                ("Melanie took a pottery class", "2023-05-02"),
                ("Melanie went camping with her kids", "2023-07-14"),
                ("Melanie went swimming at the lake", "2023-08-03"),
            )
        ]

    return lambda: mine


async def test_mined_facts_summaries_and_list_cards_inherit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = _mining_engine(list_cards=True)
    monkeypatch.setattr(eng, "_build_fact_miner", _fake_miner())
    await eng.start()
    try:
        await _tagged_session(eng)
        await eng.sleep()
        semantic = await eng._require_started().list_records("a", "semantic")
        facts = [r for r in semantic if "atomic_fact" in r.tags]
        summaries = [r for r in semantic if r.source.channel == "consolidation"]
        cards = [
            r
            for r in semantic
            if constants.LIST_CARD_TAG in r.tags and r.status is RecordStatus.ACTIVATED
        ]
        assert facts and summaries and cards
        for record in [*facts, *summaries, *cards]:
            assert record.consent_tags == ["medical"], record.source.channel
            assert record.pii_tier is PiiTier.REGULATED, record.source.channel
        hidden = await eng.search("pottery camping", namespace="a", purpose="marketing")
        assert hidden == []
    finally:
        await eng.stop()


async def test_entity_summaries_inherit() -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "semantic": {"enabled": True},
            "episodic": {"enabled": True},
            "associative": {
                "enabled": True,
                "policies": {"entity_nodes": True, "entity_summaries": True},
            },
        },
        read={"record_access": False, "entity_summaries": True},
        consent={"enforce": True},
    )
    await eng.start()
    try:
        for i, (book, tags, tier) in enumerate(
            [
                ("Charlotte's Web", ["medical"], PiiTier.HIGH),
                ("Nothing Is Impossible", ["medical", "care"], PiiTier.LOW),
            ]
        ):
            await eng.write(
                f'Melanie read "{book}" SECRETBOOK',
                namespace="a",
                entity="Melanie",
                tags=["kind:event", "rel:read", f"dst:{book}"],
                valid_from=T0 + timedelta(days=i),
                purposes=tags,
                pii_tier=tier,
            )
        stats = await summarize_entities(eng._pipeline_ctx())
        assert stats["status"] == "ok"
        records = await eng._require_started().list_records("a", "semantic")
        about = {
            entity_summary_name(r): r
            for r in records
            if r.source.channel == constants.ENTITY_SUMMARY_CHANNEL
        }
        melanie = about["Melanie"]  # both facts: the common purpose, the higher tier
        assert melanie.consent_tags == ["medical"] and melanie.pii_tier is PiiTier.HIGH
        book = about["Nothing Is Impossible"]  # one fact: its own purposes and tier
        assert book.consent_tags == ["care", "medical"] and book.pii_tier is PiiTier.LOW
        other = await eng.read("what did Melanie read", namespace="a", purpose="marketing")
        assert all("SECRETBOOK" not in r.content for r in other.context.records)
    finally:
        await eng.stop()


async def test_untagged_parents_leave_derived_records_and_events_unchanged() -> None:
    eng = _lead_engine()
    await eng.start()
    try:
        parent = await eng.write("Ana likes green tea", namespace="a")
        child = await eng.reflect("Ana enjoys tea", [parent.record_id], namespace="a")
        stored = await eng._require_started().get_record(child.record_id)
        assert stored is not None and stored.consent_tags == [] and stored.pii_tier is PiiTier.NONE
    finally:
        await eng.stop()


async def test_reflection_inherits_and_foreign_parents_do_not_count() -> None:
    eng = _lead_engine()
    await eng.start()
    try:
        parent = await eng.write(
            "Ana's diagnosis is private", namespace="a", purposes=["medical"], pii_tier="high"
        )
        child = await eng.reflect("Ana has a diagnosis", [parent.record_id], namespace="a")
        stored = await eng._require_started().get_record(child.record_id)
        assert stored is not None
        assert stored.consent_tags == ["medical"] and stored.pii_tier is PiiTier.HIGH
        # A write in another namespace naming that record as parent learns nothing.
        foreign = await eng.write(
            "unrelated", namespace="b", source=SourceInfo(role="user", parents=[parent.record_id])
        )
        stored = await eng._require_started().get_record(foreign.record_id)
        assert stored is not None and stored.consent_tags == [] and stored.pii_tier is PiiTier.NONE
    finally:
        await eng.stop()
