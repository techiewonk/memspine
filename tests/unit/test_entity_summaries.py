"""GP-6 (#17): entity summaries, the ``summarize_entities`` sleep stage and the
``read.entity_summaries`` "About" block.

The fixture (``test_graph_leg_off_golden.seed``) writes six turns and four edge
facts; associative memory projects entity nodes from the facts.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from test_graph_leg_off_golden import T0, seed

from memspine import Engine
from memspine.config import constants
from memspine.core.records import MemoryRecord, RecordStatus
from memspine.workers.pipelines import (
    ENTITY_SUMMARIZED_MARKER,
    PIPELINES,
    SessionIndex,
    entity_summary_name,
    summarize_entities,
)
from memspine.workers.schedule import SLEEP_CYCLE_ORDER


def _engine(summaries: Any = True, **read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "semantic": {"enabled": True},
            "episodic": {"enabled": True},
            "associative": {
                "enabled": True,
                "policies": {"entity_nodes": True, "entity_summaries": summaries},
            },
        },
        read={"record_access": False, **read},
    )


class CountingSummarizer:
    """A fake ``summarize_entity`` LLM: counts calls and batch sizes."""

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    async def __call__(self, items: list[tuple[str, list[str]]]) -> dict[int, str]:
        self.batches.append([name for name, _ in items])
        return {i: f"{name} has {len(lines)} facts." for i, (name, lines) in enumerate(items, 1)}


async def _summaries(eng: Engine, ns: str = "a", *, live_only: bool = True) -> list[MemoryRecord]:
    records = await eng._require_started().list_records(ns, "semantic")
    return [
        r
        for r in records
        if r.source.channel == constants.ENTITY_SUMMARY_CHANNEL
        and (not live_only or r.status is RecordStatus.ACTIVATED)
    ]


def _by_name(records: list[MemoryRecord]) -> dict[str, MemoryRecord]:
    return {entity_summary_name(r): r for r in records}


async def _sweep(eng: Engine, llm: CountingSummarizer | None = None) -> dict[str, object]:
    ctx = eng._pipeline_ctx()
    ctx.summarize_entities = llm
    return await summarize_entities(ctx)


def test_stage_is_registered_in_the_sleep_cycle() -> None:
    assert "summarize_entities" in PIPELINES
    order = list(SLEEP_CYCLE_ORDER)
    assert order.index("extract_graph") < order.index("summarize_entities")
    assert order.index("summarize_entities") < order.index("reorganize")


async def test_off_by_default_the_stage_skips() -> None:
    eng = _engine(summaries=False)
    await eng.start()
    try:
        await seed(eng)
        stats = await _sweep(eng, CountingSummarizer())
        assert stats["status"] == "skipped"
        assert await _summaries(eng) == []
    finally:
        await eng.stop()


async def test_short_entities_are_summarised_for_free_and_once() -> None:
    eng = _engine()
    await eng.start()
    llm = CountingSummarizer()
    try:
        ids = await seed(eng)
        stats = await _sweep(eng, llm)
        assert stats["status"] == "ok" and stats["llm_calls"] == 0 and llm.batches == []
        about = _by_name(await _summaries(eng))
        melanie = about["Melanie"]
        # Facts for free: the dated lines themselves, oldest first.
        assert melanie.content.splitlines() == [
            '[2023-05-01] Melanie read "Charlotte\'s Web"',
            '[2023-05-03] Melanie read "Nothing Is Impossible"',
        ]
        assert sorted(melanie.source.parents) == sorted(
            [ids['Melanie read "Charlotte\'s Web"'], ids['Melanie read "Nothing Is Impossible"']]
        )
        assert melanie.source.role == constants.DERIVED_ROLE
        assert constants.ENTITY_SUMMARY_TAG in melanie.tags
        # Unchanged membership: no call, no record.
        again = await _sweep(eng, llm)
        assert again["summarized"] == 0 and again["unchanged"] == stats["summarized"]
        assert len(await _summaries(eng, live_only=False)) == stats["summarized"]
        assert llm.batches == []
    finally:
        await eng.stop()


async def test_long_entities_are_summarised_in_batches_of_at_most_30() -> None:
    eng = _engine()
    await eng.start()
    llm = CountingSummarizer()
    try:
        for i in range(35):
            for j in range(2):
                # Distinct tokens (no dedup merge), ~1,300 characters per record.
                filler = " ".join(f"p{i}n{j}w{k}" for k in range(150))
                await eng.write(
                    f"Person{i:02d} {filler}",
                    namespace="a",
                    entity=f"Person{i:02d}",
                    attribute=f"note{j}",
                    valid_from=T0 + timedelta(days=j),
                )
        stats = await _sweep(eng, llm)
        assert [len(batch) for batch in llm.batches] == [30, 5]
        assert stats["llm_calls"] == 2 and stats["summarized"] == 35 and stats["free"] == 0
        assert _by_name(await _summaries(eng))["Person07"].content == "Person07 has 2 facts."
        # A second sweep with nothing changed costs nothing.
        llm.batches.clear()
        again = await _sweep(eng, llm)
        assert llm.batches == [] and again["llm_calls"] == 0 and again["unchanged"] == 35
    finally:
        await eng.stop()


async def test_without_an_llm_long_entities_keep_their_newest_lines() -> None:
    eng = _engine({"free_chars": 60})
    await eng.start()
    try:
        await seed(eng)
        stats = await _sweep(eng, None)
        assert stats["llm_calls"] == 0
        melanie = _by_name(await _summaries(eng))["Melanie"]
        assert melanie.content == '[2023-05-03] Melanie read "Nothing Is Impossible"'
    finally:
        await eng.stop()


async def test_membership_drift_supersedes_the_summary() -> None:
    eng = _engine()
    await eng.start()
    try:
        await seed(eng)
        await _sweep(eng)
        old = _by_name(await _summaries(eng))["Melanie"]
        await eng.write(
            "Melanie read Dune",
            namespace="a",
            entity="Melanie",
            tags=["kind:event", "rel:read", "dst:Dune"],
            valid_from=T0 + timedelta(days=9),
        )
        stats = await _sweep(eng)
        assert stats["superseded"] >= 1
        new = _by_name(await _summaries(eng))["Melanie"]
        assert new.record_id != old.record_id and "Melanie read Dune" in new.content
        stale = await eng._require_started().get_record(old.record_id)
        assert stale is not None and stale.status is RecordStatus.ARCHIVED
    finally:
        await eng.stop()


async def test_trust_is_the_least_member_trust() -> None:
    from memspine.core.records import SourceInfo

    eng = _engine()
    await eng.start()
    try:
        await seed(eng)
        low = await eng.write(
            "Melanie owes a stranger money",
            namespace="a",
            entity="Melanie",
            tags=["kind:event", "rel:owes", "dst:stranger"],
            source=SourceInfo(role="tool", channel="web"),
            actor="tool",
        )
        assert not low.quarantined
        await _sweep(eng)
        melanie = _by_name(await _summaries(eng))["Melanie"]
        assert melanie.trust <= low.trust
        assert low.record_id in melanie.source.parents
    finally:
        await eng.stop()


async def test_soft_forget_re_derives_and_hard_forget_cascades() -> None:
    eng = _engine()
    await eng.start()
    try:
        ids = await seed(eng)
        await _sweep(eng)
        first = _by_name(await _summaries(eng))["Melanie"]
        await eng.forget(ids['Melanie read "Charlotte\'s Web"'], namespace="a")
        await _sweep(eng)
        second = _by_name(await _summaries(eng))["Melanie"]
        assert second.record_id != first.record_id
        assert "Charlotte" not in second.content and "Nothing Is Impossible" in second.content
        # Hard forget: the summary goes with its member (ADR-039 cascade), and its
        # watermark entry is erased, so the next sweep summarises what is left.
        await eng.write(
            "Melanie read Dune",
            namespace="a",
            entity="Melanie",
            tags=["kind:event", "rel:read", "dst:Dune"],
        )
        await _sweep(eng)
        third = _by_name(await _summaries(eng))["Melanie"]
        await eng.forget(ids['Melanie read "Nothing Is Impossible"'], namespace="a", hard=True)
        gone = await eng._require_started().get_record(third.record_id)
        assert gone is None or gone.status is RecordStatus.DELETED
        index = SessionIndex()
        await index.refresh(eng._require_started())
        # Every summary built on the erased fact went with it, and so did its watermark.
        assert ("a", "ent:a:melanie") not in index.entity_summaries
        await _sweep(eng)
        fourth = _by_name(await _summaries(eng))["Melanie"]
        assert "Nothing Is Impossible" not in fourth.content and "Dune" in fourth.content
    finally:
        await eng.stop()


async def test_watermark_marker_is_erasable() -> None:
    eng = _engine()
    await eng.start()
    try:
        await seed(eng)
        await _sweep(eng)
        storage = eng._require_started()
        markers = [
            e
            for e in await storage.read_events()
            if e.payload.get("marker") == ENTITY_SUMMARIZED_MARKER
        ]
        assert len(markers) == 1
        entry = markers[0].payload["entities"][0]
        assert set(entry) == {"record_id", "namespace", "entity", "content_fingerprint"}
        from memspine.core.erasure import redact_record

        payload = dict(markers[0].payload)
        assert redact_record(payload, entry["record_id"])
        scrubbed = next(e for e in payload["entities"] if e["record_id"] == entry["record_id"])
        assert not scrubbed["entity"] and not scrubbed["content_fingerprint"]
    finally:
        await eng.stop()


async def test_about_block_renders_for_seeded_entities_only() -> None:
    eng = _engine(entity_summaries=True)
    await eng.start()
    try:
        await seed(eng)
        await _sweep(eng)
        out = await eng.read(
            "what books has Melanie read", namespace="a", mode="retrieve", budget_tokens=600
        )
        blocks = [r for r in out.context.records if constants.ENTITY_SUMMARIES_TAG in r.tags]
        assert len(blocks) == 1
        text = blocks[0].content
        assert text.startswith(constants.ENTITY_SUMMARIES_MARKER)
        assert "About Melanie: [2023-05-01] Melanie read" in text
        assert "About Caroline" not in text
        # No seed, no block.
        out = await eng.read("the weather", namespace="a", mode="retrieve", budget_tokens=600)
        assert not [r for r in out.context.records if constants.ENTITY_SUMMARIES_TAG in r.tags]
    finally:
        await eng.stop()


async def test_about_block_off_by_default() -> None:
    eng = _engine()
    await eng.start()
    try:
        await seed(eng)
        await _sweep(eng)
        out = await eng.read(
            "what books has Melanie read", namespace="a", mode="retrieve", budget_tokens=600
        )
        assert not [r for r in out.context.records if constants.ENTITY_SUMMARIES_TAG in r.tags]
    finally:
        await eng.stop()


async def test_summaries_are_not_extract_graph_sources() -> None:
    from memspine.prompts.models import ExtractedEdge
    from memspine.workers.pipelines import extract_graph

    eng = _engine()
    await eng.start()
    try:
        await seed(eng)
        await _sweep(eng)
        seen: list[str] = []

        async def fake(content: str, _context: object = None) -> list[ExtractedEdge]:
            seen.append(content)
            return []

        ctx = eng._pipeline_ctx()
        ctx.extract_edges = fake
        await extract_graph(ctx)
        summaries = {r.content for r in await _summaries(eng)}
        assert seen and not summaries & set(seen)
    finally:
        await eng.stop()
