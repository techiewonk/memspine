"""R3-05c: the episode leg (L2) lifted to member turns (read.episode_leg_v2). Model-free."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import ReadConfig
from memspine.read import legs as lib
from memspine.read.legs import core as legs_core
from memspine.read.pipeline import lift as pipeline_lift
from memspine.store.layer_registry import LayerRegistry, LayerTemplate
from memspine.store.records import MemoryRecord, SourceInfo

T0 = datetime(2023, 5, 7, 10, 0, tzinfo=UTC)
CAT = "Alice adopted a grey cat called Pepper"
VET = "Pepper the cat went to the vet for shots"
FENCE = "Bob repaired the garden fence"
BIKE = "Carol bought a red bicycle"
EPISODE = "Episode: Alice adopted the cat Pepper and took the cat to the vet"


def _rec(rid: str, content: str, **kw: Any) -> MemoryRecord:
    kw.setdefault("memory_type", "episodic")
    return MemoryRecord(record_id=rid, namespace="a", content=content, valid_from=T0, **kw)


def _episode(rid: str, content: str, parents: list[str]) -> MemoryRecord:
    return _rec(
        rid,
        content,
        memory_type="semantic",
        source=SourceInfo(role="derived", channel="window_episode", parents=parents),
    )


def _ctx(records: list[MemoryRecord], query: str, **kw: Any) -> lib.LegContext:
    return lib.LegContext(query=query, namespace="a", fetch_k=10, records=records, **kw)


# ------------------------------------------------------------------ config / registry


def test_flag_defaults_off() -> None:
    cfg = ReadConfig()
    assert cfg.episode_leg_v2 is False
    assert cfg.episode_lift_max_turns == 8 and cfg.episode_lift_carry == 0.9


def test_episode_leg_is_not_in_the_default_registry_but_in_the_extended_one() -> None:
    assert "episode" not in lib.REGISTRY.names()
    ext = lib.with_episode_leg(lib.REGISTRY)
    assert ext.names() == (*lib.REGISTRY.names(), "episode")
    assert ext.family_of("episode") == "derived"
    assert ext.get("episode").layers == ("L2",)
    assert lib.with_episode_leg(lib.REGISTRY) is ext
    assert "episode" not in lib.REGISTRY.names()


def test_carry_score() -> None:
    assert lib.carry_score(1.0, 0.9) == pytest.approx(0.9)
    assert lib.carry_score(0.5, 0.9, hops=2) == pytest.approx(0.405)


# ------------------------------------------------------------------ the leg


async def test_l2_absent_gives_an_empty_leg_with_a_reason() -> None:
    recs = [_rec("t1", CAT), _rec("t2", FENCE)]
    out = await lib.EpisodeLeg().run(_ctx(recs, "which cat did Alice adopt"))
    assert out.hits == [] and out.empty_reason == "no_records:L2"


async def test_layer_registry_says_l2_unpopulated_even_with_records_in_view() -> None:
    reg = LayerRegistry.of("a", LayerTemplate(), {"L0": 3})
    recs = [_episode("e1", EPISODE, ["t1"])]
    out = await lib.EpisodeLeg().run(_ctx(recs, "cat", layer_registry=reg))
    assert out.hits == [] and out.empty_reason == "no_records:L2"


async def test_leg_ranks_episodes_by_vector_and_lexical_match() -> None:
    recs = [
        _episode("e1", EPISODE, ["t1"]),
        _episode("e2", "Episode: Bob repaired the garden fence", ["t2"]),
        _rec("t1", CAT),
    ]

    async def search(k: int) -> list[lib.Hit]:
        # the vector store ranks every layer together; the leg keeps only the L2 ids
        return [lib.Hit("t1", 0.95), lib.Hit("e2", 0.6), lib.Hit("e1", 0.5)]

    out = await lib.EpisodeLeg().run(_ctx(recs, "which cat did Alice adopt", vector_search=search))
    ids = [h.record_id for h in out.hits]
    assert "t1" not in ids and set(ids) == {"e1", "e2"}
    assert ids[0] == "e1"  # lexical puts e1 first, vector only e2: e1 has both votes
    assert all(0.0 < h.score <= 1.0 for h in out.hits)


async def test_leg_works_without_an_embedder() -> None:
    recs = [_episode("e1", EPISODE, ["t1"])]
    out = await lib.EpisodeLeg().run(_ctx(recs, "Alice cat"))
    assert [h.record_id for h in out.hits] == ["e1"]


async def test_read_leg_tap_reports_the_episode_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(legs_core.tap, "event", lambda stage, /, **f: seen.append((stage, f)))
    ext = lib.with_episode_leg(lib.REGISTRY)
    await lib.run_legs(ext, ["episode"], _ctx([_rec("t1", CAT)], "cat"))
    await lib.run_legs(ext, ["episode"], _ctx([_episode("e1", EPISODE, ["t1"])], "Alice cat"))
    ev = [f for stage, f in seen if stage == "read.leg"]
    assert ev[0]["leg"] == "episode" and ev[0]["family"] == "derived"
    assert ev[0]["layers"] == ["L2"] and ev[0]["count"] == 0
    assert ev[0]["empty_reason"] == "no_records:L2"
    assert ev[1]["count"] == 1 and ev[1]["empty_reason"] is None


# ------------------------------------------------------------------ the wiring (engine)


async def _engine(**read: Any) -> tuple[Engine, dict[str, str]]:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"hybrid": True, "record_access": False, "legs_v2": True, **read},
    )
    await eng.start()
    ids: dict[str, str] = {}
    for key, text in (("cat", CAT), ("vet", VET), ("fence", FENCE), ("bike", BIKE)):
        rec = await eng.write(text, namespace="a", memory_type="episodic", valid_from=T0)
        ids[key] = rec.record_id
    return eng, ids


async def _add_episode(eng: Engine, ids: dict[str, str]) -> str:
    ep = await eng.write(
        EPISODE,
        namespace="a",
        memory_type="semantic",
        source=SourceInfo(
            role="derived", channel="window_episode", parents=[ids["cat"], ids["vet"]]
        ),
        valid_from=T0,
    )
    assert ep.layer == "L2"
    return ep.record_id


async def _leg_names(eng: Engine, q: str, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(
        legs_core.tap,
        "event",
        lambda stage, /, **f: seen.append(f["leg"]) if stage == "read.leg" else None,
    )
    await eng.search(q, namespace="a", top_k=3)
    return seen


async def test_flag_off_never_runs_the_episode_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    eng, ids = await _engine()
    try:
        await _add_episode(eng, ids)
        names = await _leg_names(eng, "which cat did Alice adopt", monkeypatch)
    finally:
        await eng.stop()
    assert "episode" not in names and "vector" in names


async def test_flag_on_without_l2_gives_an_empty_leg_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng, _ = await _engine(episode_leg_v2=True)
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        legs_core.tap,
        "event",
        lambda stage, /, **f: events.append(f) if stage == "read.leg" else None,
    )
    try:
        hits = await eng.search("which cat did Alice adopt", namespace="a", top_k=3)
    finally:
        await eng.stop()
    assert CAT in [r.content for r, _ in hits]
    ep = next(e for e in events if e["leg"] == "episode")
    assert ep["count"] == 0 and ep["empty_reason"] == "no_records:L2"


async def test_an_episode_hit_lifts_to_its_member_turns_and_fusion_receives_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng, ids = await _engine(episode_leg_v2=True, family_fusion=True)
    taps: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(pipeline_lift.tap, "event", lambda stage, /, **f: taps.append((stage, f)))
    monkeypatch.setattr(pipeline_lift.tap, "enabled", lambda: True)
    got: list[Any] = []
    from memspine.read.pipeline import router

    real = router.fuse_families

    def spy(
        vector_hits: Any, lexical_hits: Any, use_hybrid: Any, extra_legs: Any, **kw: Any
    ) -> Any:
        got.extend(extra_legs)
        return real(vector_hits, lexical_hits, use_hybrid, extra_legs, **kw)

    monkeypatch.setattr(router, "fuse_families", spy)
    try:
        ep_id = await _add_episode(eng, ids)
        recs = await eng.search("did the cat Pepper see the vet", namespace="a", top_k=4)
    finally:
        await eng.stop()
    assert ep_id
    lifted = [leg for leg in got if getattr(leg, "name", "") == "episode"]
    assert lifted  # fusion received the leg; it carries the turn ids, never the episode id
    assert {h.record_id for h in lifted[0]} == {ids["cat"], ids["vet"]}
    stage = [f for s, f in taps if s == "read.lift" and f.get("source") == "episode"]
    assert stage and stage[0]["episodes"] == [ep_id]
    assert set(stage[0]["turns"]) == {ids["cat"], ids["vet"]} and stage[0]["dropped"] == []
    contents = [r.content for r, _ in recs]
    assert CAT in contents and VET in contents


async def test_lift_function_scores_turns_with_the_carry_rule() -> None:
    eng, ids = await _engine(episode_leg_v2=True)
    try:
        ep_id = await _add_episode(eng, ids)
        ep = await eng._require_started().get_record(ep_id)
        out = await pipeline_lift.lift_episode_hits(
            eng, "a", [lib.Hit(ep_id, 0.8)], {ep_id: ep}, max_turns=8, carry=0.5
        )
        capped = await pipeline_lift.lift_episode_hits(
            eng, "a", [lib.Hit(ep_id, 0.8)], {ep_id: ep}, max_turns=1, carry=0.5
        )
    finally:
        await eng.stop()
    assert {h.record_id for h in out} == {ids["cat"], ids["vet"]}
    assert all(h.score == pytest.approx(0.4) for h in out)
    assert len(capped) == 1


async def test_lift_skips_forgotten_turns() -> None:
    eng, ids = await _engine(episode_leg_v2=True)
    try:
        ep_id = await _add_episode(eng, ids)
        await eng.forget(ids["vet"], namespace="a")
        ep = await eng._require_started().get_record(ep_id)
        out = await pipeline_lift.lift_episode_hits(
            eng, "a", [lib.Hit(ep_id, 0.8)], {ep_id: ep}, max_turns=8, carry=0.9
        )
    finally:
        await eng.stop()
    got = {h.record_id for h in out}
    assert ids["vet"] not in got  # forgotten: never served
    # strict lineage (POL-2): an episode that cites a hidden turn loses its slot, so its
    # remaining turn is not lifted through it either
    assert got == set()


async def test_lift_skips_hard_erased_turns() -> None:
    eng, ids = await _engine(episode_leg_v2=True)
    try:
        ep_id = await _add_episode(eng, ids)
        await eng.forget(ids["cat"], namespace="a", hard=True, cascade=False)
        ep = await eng._require_started().get_record(ep_id)
        out = await pipeline_lift.lift_episode_hits(
            eng, "a", [lib.Hit(ep_id, 0.8)], {ep_id: ep}, max_turns=8, carry=0.9
        )
    finally:
        await eng.stop()
    assert ids["cat"] not in {h.record_id for h in out}


async def test_lift_does_not_serve_an_episode_the_consent_gate_hides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng, ids = await _engine(episode_leg_v2=True)
    try:
        ep_id = await _add_episode(eng, ids)
        ep = await eng._require_started().get_record(ep_id)
        monkeypatch.setattr(eng, "_consent_ok", lambda r: r.record_id != ids["cat"])
        out = await pipeline_lift.lift_episode_hits(
            eng, "a", [lib.Hit(ep_id, 0.8)], {ep_id: ep}, max_turns=8, carry=0.9
        )
    finally:
        await eng.stop()
    assert ids["cat"] not in {h.record_id for h in out}


async def test_lift_tap_reports_dropped_not_live(monkeypatch: pytest.MonkeyPatch) -> None:
    eng, ids = await _engine(episode_leg_v2=True)
    taps: list[dict[str, Any]] = []
    monkeypatch.setattr(
        pipeline_lift.tap,
        "event",
        lambda stage, /, **f: taps.append(f) if stage == "read.lift" else None,
    )
    monkeypatch.setattr(pipeline_lift.tap, "enabled", lambda: True)
    try:
        ep_id = await _add_episode(eng, ids)
        await eng.forget(ids["vet"], namespace="a")
        ep = await eng._require_started().get_record(ep_id)
        await pipeline_lift.lift_episode_hits(
            eng, "a", [lib.Hit(ep_id, 0.8)], {ep_id: ep}, max_turns=8, carry=0.9
        )
    finally:
        await eng.stop()
    assert taps and taps[0]["turns"] == []
    reasons = {d["reason"] for d in taps[0]["dropped"]}
    assert "lifted_turn_not_visible" in reasons and "partial_lineage_hidden" in reasons
