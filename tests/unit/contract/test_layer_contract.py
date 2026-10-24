"""MF-15: producer -> persistence -> event-log rebuild -> layer counts / registry -> retrieval legs.

These tests encode the INTENDED layer contract (docs/v2/MEMORY_LAYER_BUG_LIST_2026-10-15.md,
probe docs/v2/probe_memory_layers_2026_10_15.py). They fail on the pre-fix tree on purpose and are
not marked xfail: they go green when MF-01..MF-10 land.

Contract, in one table (layer is a label of what the record IS, whatever the lineage flag):

    raw turn                                   L0
    window fact / user fact                    L1
    session-close episode and summary          L2   (lineage on AND off)
    consolidation summary                      L2
    entity summary, community summary          L3
    observation                                L4
    standing answer                            L5

Uses the hash embedder, in-memory SQLite, no model calls, invented names.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import update

from memspine import Engine
from memspine.derive.pipeline.stages.envelope_adapter import is_fact
from memspine.derive.pipeline.stages.offline_common import is_input_fact
from memspine.derive.write import stamp_derived, write_derived
from memspine.read.legs.core import LegContext
from memspine.read.legs.episode import EpisodeLeg
from memspine.read.legs.graph import HopEdge, OneHopConvergent
from memspine.read.pipeline.lift import lift_episode_hits
from memspine.services.storage.sqlite.schema import memory_records
from memspine.store.layer_registry import LayerRegistry, LayerTemplate
from memspine.store.layers import derive_layer
from memspine.store.records import MemoryRecord, SourceInfo

NS = "contract"
TURN = "Noor said she is planning a pottery exhibition in Lisbon."
WHEN = datetime(2025, 3, 1, tzinfo=UTC)

# (channel, requested layer or None, expected layer). ``requested`` is what the producing stage
# passes to the door; None means the stage relies on the channel mapping.
PRODUCERS: list[tuple[str, str | None, str]] = [
    ("consolidation", None, "L2"),
    ("multires", "L2", "L2"),
    ("entity_summary", None, "L3"),
    ("reorganize", None, "L3"),
    ("community_summary", "L3", "L3"),
    ("observation", "L4", "L4"),
    ("standing", "L5", "L5"),
]


async def _engine(lineage: bool) -> Engine:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        write={"lineage_table": lineage},
    )
    await eng.start()
    return eng


@pytest.fixture(params=[False, True], ids=["lineage_off", "lineage_on"])
async def eng(request: pytest.FixtureRequest) -> AsyncIterator[Engine]:
    engine = await _engine(request.param)
    try:
        yield engine
    finally:
        await engine.stop()


async def _turn(eng: Engine, text: str = TURN) -> MemoryRecord:
    return await eng.write(text, namespace=NS, memory_type="episodic")


async def _derived(
    eng: Engine, parent: MemoryRecord, channel: str, requested: str | None, text: str
) -> MemoryRecord:
    record = MemoryRecord(
        namespace=NS,
        memory_type="semantic",
        content=text,
        source=SourceInfo(role="assistant", channel=channel, parents=[parent.record_id]),
        valid_from=WHEN,
    )
    async with eng._write_locks.setdefault(NS, asyncio.Lock()):
        return await write_derived(
            eng,
            eng._require_started(),
            NS,
            record,
            "summary",
            "system",
            parents=[parent.record_id],
            layer=requested,
            site=channel,
        )


async def _payload_layer(eng: Engine, record_id: str) -> str | None:
    events = await eng._require_started().read_events(after_seq=0, limit=10_000)
    for event in events:
        rec = (event.payload or {}).get("record") or {}
        if rec.get("record_id") == record_id:
            return rec.get("layer")
    raise AssertionError(f"no WRITE event carries {record_id}")


async def _close_record(eng: Engine, parent: MemoryRecord, kind: str) -> MemoryRecord:
    rec = await eng._deposit_session_record(
        NS, kind, TURN, [parent.record_id], WHEN, f"session-{kind}"
    )
    assert rec is not None
    return rec


# --- producers land on the intended layer, persist it, and survive rebuild ----------------------


async def test_raw_turn_is_l0_and_user_fact_is_l1(eng: Engine) -> None:
    turn = await _turn(eng)
    fact = await eng.write(
        "Bram prefers green tea.", namespace=NS, memory_type="semantic", entity="Bram",
        attribute="drink",
    )  # fmt: skip
    storage = eng._require_started()
    assert (await storage.get_record(turn.record_id)).layer == "L0"
    assert (await storage.get_record(fact.record_id)).layer == "L1"
    counts = await storage.layer_counts(NS)
    assert counts.get("L0", 0) >= 1 and counts.get("L1", 0) >= 1


async def test_window_fact_is_l1_through_the_derived_door(eng: Engine) -> None:
    turn = await _turn(eng)
    rec = await _derived(eng, turn, "mining", None, "Noor plans a pottery exhibition in Lisbon.")
    stored = await eng._require_started().get_record(rec.record_id)
    assert rec.layer == "L1" and stored.layer == "L1"


@pytest.mark.parametrize("kind", ["episode", "summary"])
async def test_session_close_record_lands_on_l2_and_survives_rebuild(
    eng: Engine, kind: str
) -> None:
    turn = await _turn(eng)
    rec = await _close_record(eng, turn, kind)
    storage = eng._require_started()
    assert rec.layer == "L2", "returned record"
    assert (await storage.get_record(rec.record_id)).layer == "L2", "stored record"
    assert (await storage.layer_counts(NS)).get("L2", 0) == 1
    payload_layer = await _payload_layer(eng, rec.record_id)
    assert payload_layer in (None, "L2")  # omitted only if the default derivation is L2 too
    if payload_layer is None:
        assert derive_layer("semantic", "session_close") == "L2"
    await eng.rebuild()
    replayed = await storage.get_record(rec.record_id)
    assert replayed.layer == "L2", "replay must not relabel the record"
    assert (await storage.layer_counts(NS)).get("L2", 0) == 1


@pytest.mark.parametrize(("channel", "requested", "expected"), PRODUCERS)
async def test_derived_producers_land_on_their_layer_and_survive_rebuild(
    eng: Engine, channel: str, requested: str | None, expected: str
) -> None:
    turn = await _turn(eng)
    rec = await _derived(
        eng, turn, channel, requested, f"Synthetic {channel} view of Noor's plans."
    )
    storage = eng._require_started()
    assert rec.layer == expected
    assert (await storage.get_record(rec.record_id)).layer == expected
    assert (await storage.layer_counts(NS)).get(expected, 0) >= 1
    await eng.rebuild()
    assert (await storage.get_record(rec.record_id)).layer == expected


async def test_derived_layer_is_independent_of_the_lineage_flag() -> None:
    seen: dict[bool, dict[str, str]] = {}
    for lineage in (False, True):
        engine = await _engine(lineage)
        try:
            turn = await _turn(engine)
            out: dict[str, str] = {}
            for channel, requested, _ in PRODUCERS:
                rec = await _derived(engine, turn, channel, requested, f"Synthetic {channel} note.")
                out[channel] = rec.layer
            for kind in ("episode", "summary"):
                out[f"close:{kind}"] = (await _close_record(engine, turn, kind)).layer
            seen[lineage] = out
        finally:
            await engine.stop()
    assert seen[False] == seen[True]


# --- explicit layer: valid values stick, invalid values raise -----------------------------------


@pytest.mark.parametrize("bad", ["episode", "summary", "L6", "l2", "", "layer2"])
def test_invalid_explicit_layer_raises_value_error(bad: str) -> None:
    rec = MemoryRecord(namespace=NS, memory_type="semantic", content="Synthetic note")
    with pytest.raises(ValueError, match=bad if bad else "layer"):
        stamp_derived(rec, "summary", "contract", bad)


async def test_invalid_explicit_layer_raises_through_the_door(eng: Engine) -> None:
    turn = await _turn(eng)
    with pytest.raises(ValueError, match="episode"):
        await _derived(eng, turn, "session_close", "episode", "Synthetic close note.")


@pytest.mark.parametrize(("channel", "requested", "expected"), PRODUCERS)
def test_stamp_derived_maps_route_name_summary_to_the_intended_layer(
    channel: str, requested: str | None, expected: str
) -> None:
    rec = MemoryRecord(
        namespace=NS,
        memory_type="semantic",
        content="Synthetic note",
        source=SourceInfo(role="assistant", channel=channel, parents=["p"]),
    )
    stamped = stamp_derived(rec, "summary", channel, requested)
    assert stamped.layer == expected
    assert MemoryRecord.model_validate(stamped.model_dump(mode="json")).layer == expected


def test_stamp_changes_nothing_but_layer_for_the_same_content() -> None:
    rec = MemoryRecord(
        namespace=NS,
        memory_type="semantic",
        content="Synthetic note",
        source=SourceInfo(role="assistant", channel="consolidation", parents=["p"]),
    )
    a = stamp_derived(rec, "summary", "consolidation")
    b = stamp_derived(rec, "summary", "consolidation", "L2")
    assert a.layer == b.layer == "L2"
    assert a.fingerprint.derivation == b.fingerprint.derivation


# --- retrieval legs read the contract ------------------------------------------------------------


async def test_episode_leg_finds_a_session_close_episode_and_lifts_to_source_turns(
    eng: Engine,
) -> None:
    turn = await _turn(eng)
    episode = await _close_record(eng, turn, "episode")
    storage = eng._require_started()
    registry = await LayerRegistry.load(storage, NS, LayerTemplate())
    assert registry.is_populated("L2")
    ctx = LegContext(
        query="pottery exhibition",
        namespace=NS,
        fetch_k=5,
        records=await storage.list_records(NS),
        layer_registry=registry,
    )
    out = await EpisodeLeg().run(ctx)
    assert out.empty_reason is None
    assert [h.record_id for h in out.hits][:1] == [episode.record_id]
    lifted = await lift_episode_hits(
        eng, NS, out.hits, {episode.record_id: episode}, max_turns=4, carry=0.8
    )
    assert [h.record_id for h in lifted] == [turn.record_id]
    assert lifted[0].score <= out.hits[0].score


async def test_graph_leg_returns_a_hit_from_l1_backed_edges_with_no_l3_records() -> None:
    fact = MemoryRecord(
        namespace=NS, memory_type="semantic", content="Ilse and Tomasz both visit Porto"
    )
    assert fact.layer == "L1"
    ctx = LegContext(
        query="where do they meet",
        namespace=NS,
        fetch_k=5,
        records=[fact],
        graph_seeds=["ilse", "tomasz"],
        graph_edges=[
            HopEdge("ilse", "porto", fact.record_id),
            HopEdge("tomasz", "porto", fact.record_id),
        ],
        layer_registry=LayerRegistry.of(NS, LayerTemplate(), {"L0": 2, "L1": 1}),
    )
    out = await OneHopConvergent().run(ctx)
    assert out.empty_reason is None
    assert [h.record_id for h in out.hits] == [fact.record_id]
    ctx.layer_registry = None
    assert [h.record_id for h in (await OneHopConvergent().run(ctx)).hits] == [fact.record_id]


async def test_graph_leg_stays_empty_without_edges_or_seeds() -> None:
    fact = MemoryRecord(namespace=NS, memory_type="semantic", content="Bram lives in Faro")
    base = {"query": "q", "namespace": NS, "fetch_k": 5, "records": [fact]}
    reg = LayerRegistry.of(NS, LayerTemplate(), {"L1": 1})
    # readiness is checked first (MF-07: usable edges), then seeds
    edges = [HopEdge("bram", "faro", fact.record_id)]
    no_seeds = await OneHopConvergent().run(
        LegContext(**base, graph_edges=edges, layer_registry=reg)
    )
    assert no_seeds.hits == [] and no_seeds.empty_reason == "no_seeds"
    no_edges = await OneHopConvergent().run(
        LegContext(**base, graph_seeds=["bram"], layer_registry=reg)
    )
    assert no_edges.hits == [] and no_edges.empty_reason == "no_edges"


# --- fact eligibility: derived views are never input facts ---------------------------------------

VIEW_CHANNELS = [
    "observation",
    "standing",
    "session_close",
    "entity_summary",
    "community_summary",
    "multires",
    "consolidation",
    "reflection",
    "anticipation",
    "list_card",
]


def _candidate(channel: str, *, keyed: bool = True) -> MemoryRecord:
    return MemoryRecord(
        namespace=NS,
        memory_type="semantic",
        entity="Noor" if keyed else None,
        attribute="plans" if keyed else None,
        content="Noor plans: a pottery exhibition",
        source=SourceInfo(role="assistant", channel=channel, parents=["p"]),
    )


@pytest.mark.parametrize("channel", VIEW_CHANNELS)
@pytest.mark.parametrize("keyed", [True, False], ids=["keyed", "unkeyed"])
def test_derived_views_are_not_input_facts(channel: str, keyed: bool) -> None:
    rec = _candidate(channel, keyed=keyed)
    assert is_fact(rec) is False, f"{channel}: envelope adapter treats a derived view as a fact"
    assert is_input_fact(rec) is False, f"{channel}: offline libraries treat a view as an input"


@pytest.mark.parametrize("channel", ["mining", "calibration", ""])
def test_a_supported_fact_is_still_an_input_fact(channel: str) -> None:
    rec = MemoryRecord(
        namespace=NS,
        memory_type="semantic",
        entity="Noor",
        attribute="plans",
        content="Noor plans: a pottery exhibition",
        source=SourceInfo(role="user", channel=channel),
    )
    assert is_fact(rec) is True
    assert is_input_fact(rec) is True


@pytest.mark.parametrize("channel", VIEW_CHANNELS)
def test_the_two_fact_predicates_agree(channel: str) -> None:
    rec = _candidate(channel)
    assert is_fact(rec) == is_input_fact(rec)


# --- layer_counts equals hydrated-record layers, including NULL-layer legacy rows -----------------


async def _null_layer(eng: Engine, record_ids: list[str]) -> None:
    storage: Any = eng._require_started()
    async with storage._client.engine.begin() as conn:
        await conn.execute(
            update(memory_records)
            .where(memory_records.c.record_id.in_(record_ids))
            .values(layer=None)
        )


async def test_layer_counts_equals_hydrated_layers_after_normal_writes(eng: Engine) -> None:
    turn = await _turn(eng)
    await eng.write("Ilse owns a kiln.", namespace=NS, memory_type="semantic", entity="Ilse",
                    attribute="owns")  # fmt: skip
    await _close_record(eng, turn, "summary")
    await _derived(eng, turn, "observation", "L4", "Synthetic observation.")
    storage = eng._require_started()
    live = [
        r
        for r in await storage.list_records(NS)
        if r.status.value in ("activated", "resolving") and not r.quarantined
    ]
    expected: dict[str, int] = {}
    for r in live:
        expected[r.layer] = expected.get(r.layer, 0) + 1
    assert await storage.layer_counts(NS) == expected
