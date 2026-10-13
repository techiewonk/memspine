"""OW-D / O4-06: entity communities and summaries (``offline.communities_v2``), model-free."""

from __future__ import annotations

from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema.observability import TapConfig
from memspine.derive.pipeline.stages.communities import reorganize
from memspine.derive.pipeline.stages.entity_summaries import summarize_entities
from memspine.observability import tap
from memspine.store import lineage as lineage_store
from memspine.store.records import MemoryRecord, RecordStatus

CLUSTER_A = [("Alice", "Bob"), ("Bob", "Carol"), ("Alice", "Carol")]
CLUSTER_B = [("Dave", "Erin"), ("Erin", "Frank"), ("Dave", "Frank")]


@pytest.fixture(autouse=True)
def _tap_reset():
    tap.shutdown()
    yield
    tap.shutdown()


def _engine(
    *, v2: bool, lineage: bool = False, free_chars: int | None = None, **offline: Any
) -> Engine:
    summaries: Any = {"free_chars": free_chars} if free_chars is not None else True
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
        read={"record_access": False},
        write={"lineage_table": lineage},
        offline={"communities_v2": v2, **offline},
    )


async def _seed(
    eng: Engine, pairs: list[tuple[str, str]], ns: str = "a", what: str = "the garden project"
) -> list[str]:
    ids = []
    for a, b in pairs:
        rec = await eng.write(
            f"{a} works with {b} on {what}",
            namespace=ns,
            memory_type="semantic",
            entity=a,
            tags=["kind:event", "rel:works_with", f"dst:{b}"],
        )
        ids.append(rec.record_id)
    return ids


async def _com_summaries(eng: Engine, ns: str = "a", *, live: bool = True) -> list[MemoryRecord]:
    return [
        r
        for r in await eng._require_started().list_records(ns, "semantic")
        if r.source.channel == "community_summary"
        and (not live or r.status is RecordStatus.ACTIVATED)
    ]


class _Summarize:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, text: str) -> str:
        self.calls += 1
        return f"model summary {self.calls}"


async def test_flag_off_runs_the_old_reorganizer() -> None:
    eng = _engine(v2=False)
    await eng.start()
    try:
        await _seed(eng, CLUSTER_A + CLUSTER_B)
        stats = await reorganize(eng._pipeline_ctx())
        assert "summary_model_calls" not in stats and "created" not in stats
        assert await _com_summaries(eng, live=False) == []
    finally:
        await eng.stop()


async def test_flag_on_summarises_each_community_once() -> None:
    eng = _engine(v2=True)
    await eng.start()
    llm = _Summarize()
    try:
        ids = await _seed(eng, CLUSTER_A + CLUSTER_B)
        ctx = eng._pipeline_ctx()
        ctx.summarize = llm
        stats = await reorganize(ctx)
        assert stats["status"] == "ok"
        assert stats["communities"] == 2 and stats["created"] == 2 and stats["summaries"] == 2
        assert llm.calls == 2
        found = await _com_summaries(eng)
        assert len(found) == 2
        for rec in found:
            assert rec.content.startswith("model summary")
            assert len(rec.source.parents) == 3 and set(rec.source.parents) <= set(ids)
            members = sorted(t[3:] for t in rec.tags if t.startswith("cm:"))
            assert len(members) == 3
            parents = [await eng._require_started().get_record(p) for p in rec.source.parents]
            assert rec.trust <= min(p.trust for p in parents if p is not None)
        # Nothing changed: no new summary, no model call.
        again = await reorganize(ctx)
        assert again["unchanged"] == 2 and again["summaries"] == 0 and llm.calls == 2
        assert len(await _com_summaries(eng, live=False)) == 2
    finally:
        await eng.stop()


async def test_a_new_fact_reruns_only_its_community() -> None:
    eng = _engine(v2=True)
    await eng.start()
    llm = _Summarize()
    try:
        await _seed(eng, CLUSTER_A + CLUSTER_B)
        ctx = eng._pipeline_ctx()
        ctx.summarize = llm
        await reorganize(ctx)
        before = {r.record_id for r in await _com_summaries(eng)}
        await _seed(eng, [("Alice", "Bob")], what="the pond")  # more evidence in cluster A
        stats = await reorganize(ctx)
        assert stats["changed"] == 1 and stats["unchanged"] == 1 and stats["summaries"] == 1
        after = await _com_summaries(eng)
        assert len(after) == 2 and len({r.record_id for r in after} & before) == 1
        assert (
            len(await _com_summaries(eng, live=False)) == 3
        )  # the old one is archived, not deleted
    finally:
        await eng.stop()


async def test_budget_defers_the_rest_and_keeps_them_dirty() -> None:
    eng = _engine(v2=True, community_summary_budget_tokens=400)
    await eng.start()
    try:
        await _seed(eng, CLUSTER_A + CLUSTER_B)
        ctx = eng._pipeline_ctx()
        first = await reorganize(ctx)
        assert first["summaries"] == 1 and first["deferred"] == 1
        second = await reorganize(ctx)  # the deferred one is still created, the done one unchanged
        assert second["summaries"] == 1 and second["unchanged"] == 1
        assert len(await _com_summaries(eng)) == 2
    finally:
        await eng.stop()


async def test_lineage_edges_through_the_door() -> None:
    eng = _engine(v2=True, lineage=True)
    await eng.start()
    try:
        await _seed(eng, CLUSTER_A)
        await reorganize(eng._pipeline_ctx())
        (rec,) = await _com_summaries(eng)
        edges = await lineage_store.parents_of(
            eng._require_started()._client.engine, [rec.record_id]
        )
        assert {e.parent for e in edges[rec.record_id]} == set(rec.source.parents)
        assert rec.layer == "L3"
    finally:
        await eng.stop()


async def test_forgotten_parent_dissolves_the_summary() -> None:
    eng = _engine(v2=True)
    await eng.start()
    try:
        ids = await _seed(eng, CLUSTER_A)
        ctx = eng._pipeline_ctx()
        await reorganize(ctx)
        assert len(await _com_summaries(eng)) == 1
        await eng.forget(ids[0], namespace="a", hard=True)
        await reorganize(ctx)
        live = await _com_summaries(eng)
        assert all(ids[0] not in r.source.parents for r in live)
    finally:
        await eng.stop()


async def test_entity_summary_model_call_runs_outside_the_lock() -> None:
    eng = _engine(v2=True, lineage=True, free_chars=5)
    await eng.start()
    seen: list[bool] = []

    async def llm(items: list[tuple[str, list[str]]]) -> dict[int, str]:
        seen.append(eng._namespace_lock("a").locked())
        return {i: f"{name} summary" for i, (name, _) in enumerate(items, 1)}

    try:
        await _seed(eng, CLUSTER_A)
        ctx = eng._pipeline_ctx()
        ctx.summarize_entities = llm
        stats = await summarize_entities(ctx)
        assert stats["status"] == "ok" and stats["llm_calls"] == 1 and stats["summarized"] == 3
        assert seen == [False]
        rows = [
            r
            for r in await eng._require_started().list_records("a", "semantic")
            if r.source.channel == "entity_summary"
        ]
        assert len(rows) == 3 and all(r.content.endswith("summary") for r in rows)
        edges = await lineage_store.parents_of(
            eng._require_started()._client.engine, [r.record_id for r in rows]
        )
        assert all(len(edges[r.record_id]) == len(r.source.parents) > 0 for r in rows)
        again = await summarize_entities(ctx)
        assert again["summarized"] == 0 and again["unchanged"] == 3
    finally:
        await eng.stop()


async def test_tap_event_reports_the_decisions() -> None:
    tap.configure(TapConfig(enabled=True, sink="memory"))
    eng = _engine(v2=True)
    await eng.start()
    try:
        await _seed(eng, CLUSTER_A + CLUSTER_B)
        with tap.request("job", stage="reorganize"):
            await reorganize(eng._pipeline_ctx())
    finally:
        await eng.stop()
    events = [
        e for t in tap.memory_traces() for e in t["events"] if e["stage"] == "job.communities"
    ]
    assert len(events) == 1
    f = events[0]["fields"]
    assert f["communities"] == 2 and f["created"] == 2 and f["jobs_planned"] == 2
    assert f["jobs_deferred"] == 0 and f["summaries_written"] == 2
    assert not any(
        "field_rejected" in e["fields"] for t in tap.memory_traces() for e in t["events"]
    )
