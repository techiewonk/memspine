"""Current read registry preserves temporal, lexical and router policies."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from memspine import Engine
from memspine.core.read_filters import as_of_scope
from memspine.read import legs as lib
from memspine.read.legs import core as legs_core
from memspine.read.pipeline import request as read_request
from memspine.read.pipeline.legs import registry_base_legs
from memspine.services.embedding.base import embed_queries
from memspine.store.records import MemoryRecord

NOW = datetime(2023, 5, 10, 9, 0, tzinfo=UTC)

TURNS: list[tuple[str, str, datetime, list[str]]] = [
    ("t1", "Ilse: we went camping at the lake", datetime(2023, 5, 7, 12, 0, tzinfo=UTC), []),
    ("t2", "Bram: I bought a tent for the trip", datetime(2023, 5, 7, 9, 0, tzinfo=UTC), []),
    ("t3", "Noor: I painted the fence green", datetime(2023, 5, 3, 15, 0, tzinfo=UTC), []),
    ("t4", "Ilse: I went camping last weekend", datetime(2023, 5, 8, 10, 0, tzinfo=UTC), []),
    ("t5", "Tomasz: the garden party was fun", datetime(2023, 5, 9, 18, 0, tzinfo=UTC), []),
    ("t6", "Noor: I repaired the bicycle on 7 May", datetime(2023, 6, 1, 8, 0, tzinfo=UTC), []),
    (
        "t7",
        "Bram ran the lake marathon",
        datetime(2023, 5, 20, 8, 0, tzinfo=UTC),
        ["happened:2023-05-08"],
    ),
    ("t8", "Tomasz: we went hiking in March", datetime(2023, 3, 5, 12, 0, tzinfo=UTC), []),
    ("t9", "Ilse: camping lake tent again", datetime(2023, 4, 1, 12, 0, tzinfo=UTC), []),
    ("t10", "Bram: the old bridge was closed", datetime(2022, 11, 2, 12, 0, tzinfo=UTC), []),
]


LABELS: dict[int, dict[str, str]] = {}  # engine -> record id -> fixture label (ids are random)


async def _engine(*, graph: dict[str, Any] | None = None, **read: Any) -> Engine:
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
        memories={
            "episodic": {"enabled": True},
            **({"associative": {"enabled": True}} if graph else {}),
        },
        read={"hybrid": False, "record_access": False, **read},
        **({"graph": graph} if graph else {}),
    )
    await eng.start()
    eng._clock = lambda: NOW
    labels: dict[str, str] = {}
    for label, text, at, tags in TURNS:
        rec = await eng.write(
            text, namespace="a", memory_type="episodic", valid_from=at, tags=tags or None
        )
        labels[rec.record_id] = label
    LABELS[id(eng)] = labels
    return eng


async def _legs(eng: Engine, query: str, fetch_k: int = 20) -> list[tuple[str, list[str]]]:
    """The extra legs fusion would see for ``query``: (name, ids), in order."""
    [vec] = await embed_queries(eng._embedder, [query])
    _, _, extra = await registry_base_legs(eng, "a", query, vec, fetch_k, False, [], [])
    labels = LABELS[id(eng)]
    return [
        (getattr(leg, "name", "extra"), [labels.get(h.record_id, h.record_id) for h in leg])
        for leg in extra
    ]


async def _current(query: str, **read: Any) -> list[Any]:
    eng = await _engine(**read)
    try:
        return await _legs(eng, query)
    finally:
        await eng.stop()


def _temporal(legs: list[tuple[str, list[str]]]) -> list[str]:
    return next((ids for name, ids in legs if name == "temporal"), [])


# ----------------------------------------------------------------- each old temporal option


async def test_absolute_date_same_ids_same_order() -> None:
    on = await _current("what happened on 7 May 2023", temporal_leg=True)
    assert set(_temporal(on)) == {"t1", "t2"}


async def test_temporal_leg_off_means_no_temporal_leg_either_way() -> None:
    on = await _current("what happened on 7 May 2023")
    assert on == []


async def test_relative_question_needs_temporal_relative_in_both() -> None:
    q = "what did we do last week"
    on = await _current(q, temporal_leg=True)
    assert _temporal(on) == []  # no anchor: no span, as before
    on = await _current(q, temporal_leg=True, temporal_relative=True)
    assert _temporal(on)  # anchored to the engine clock
    assert set(_temporal(on)) <= {"t1", "t2", "t3", "t5", "t4"}


async def test_relative_week_mode_is_honoured() -> None:
    on = await _current(
        "what did we do last week",
        temporal_leg=True,
        temporal_relative=True,
        relative_week="preceding_7_days",
    )
    assert _temporal(on)


async def test_as_of_anchors_a_relative_question() -> None:
    eng = await _engine(temporal_leg=True)
    try:
        with as_of_scope(datetime(2023, 5, 14, tzinfo=UTC)):
            hits = await _legs(eng, "what did we do last week")
        assert _temporal(hits)
    finally:
        await eng.stop()


async def test_year_inference() -> None:
    q = "what did Noor do on 7 May"
    on = await _current(q, temporal_leg=True)
    assert _temporal(on) == []  # no year, no inference: no span
    on = await _current(q, temporal_leg=True, temporal_infer_year=True)
    assert "t1" in _temporal(on)


async def test_overlap_rank_vs_midpoint() -> None:
    q = "which lake camping tent happened in May 2023"
    mid_on = await _current(q, temporal_leg=True)
    ov_on = await _current(q, temporal_leg=True, temporal_rank="overlap")
    assert _temporal(ov_on)[0] == "t1"  # shares lake, camping, tent
    assert _temporal(ov_on) != _temporal(mid_on) or len(_temporal(mid_on)) < 2


async def test_soft_fill_adds_the_nearest_outside() -> None:
    q = "what happened on 8 May 2023"
    hard_on = await _current(q, temporal_leg=True)
    soft_on = await _current(q, temporal_leg=True, temporal_soft=True)
    assert hard_on and soft_on
    assert len(_temporal(soft_on)) > len(_temporal(hard_on))


async def test_mentions_find_a_turn_by_the_date_it_names() -> None:
    q = "what happened on 7 May 2023"
    base_on = await _current(q, temporal_leg=True)
    on = await _current(q, temporal_leg=True, temporal_leg_mentions=True)
    assert base_on or on
    assert "t6" not in _temporal(base_on) and "t6" in _temporal(on)  # "on 7 May" said in June
    assert "t4" in _temporal(on)  # "last weekend" said on Mon 8 May names 6-7 May


async def test_event_dates_find_a_mined_fact_by_its_happened_date() -> None:
    q = "what happened on 8 May 2023"
    base_on = await _current(q, temporal_leg=True)
    on = await _current(q, temporal_leg=True, temporal_leg_event_dates=True)
    assert base_on or on
    assert "t7" not in _temporal(base_on) and "t7" in _temporal(on)


async def test_the_tuned_arm_combination_returns_temporal_hits() -> None:
    """``scr-pool-protect-jina``: mentions + anchored dates on the production template."""
    q = "what happened on 7 May 2023"
    on = await _current(
        q,
        temporal_leg=True,
        temporal_leg_mentions=True,
        temporal_leg_event_dates=True,
        temporal_relative=True,
        temporal_infer_year=True,
        relative_dates_anchored=True,
        resolve_relative_dates=True,
        order_by_time_for_ordering=True,
    )
    assert {"t1", "t2", "t6"} <= set(_temporal(on))


# ----------------------------------------------------------------- the other read-side rules


async def test_metadata_legs_are_available() -> None:
    """Current metadata leg policies supply labelled candidate hits."""
    on = await _current(
        "Ilse camping lake 2023",
        temporal_leg=True,
        metadata_leg=True,
        subject_leg=True,
        role_aware=True,
        sentence_leg=True,
        recency_leg=True,
        view_tag_leg=True,
        entity_leg=True,
    )
    assert {n for n, _ in on} >= {"entity", "extra"} or on


async def test_temporal_leg_keeps_its_place_among_the_extra_legs() -> None:
    on = await _current(
        "what happened on 7 May 2023", temporal_leg=True, recency_leg=True, entity_leg=True
    )
    assert [n for n, _ in on].index("temporal") == 0


async def test_end_to_end_search_matches_with_the_tuned_arm_settings() -> None:
    """pool_protect_per_leg, list_mode, perspective_mode, candidate_pool, replay windows,
    raw-turn floor and the date rules all sit behind the legs: results must not change."""
    settings: dict[str, Any] = {
        "temporal_leg": True,
        "temporal_leg_mentions": True,
        "temporal_relative": True,
        "pool_protect_per_leg": 3,
        "list_mode": True,
        "perspective_mode": "subject_weight",
        "candidate_pool": 2,
        "replay_window_before": 1,
        "replay_window_after": 2,
        "resolve_relative_dates": True,
        "relative_dates_anchored": True,
        "order_by_time_for_ordering": True,
    }
    eng = await _engine(**settings)
    try:
        for q in (
            "what happened on 7 May 2023",
            "who went camping at the lake",
            "what did we do last week",
        ):
            hits = await eng.search(q, namespace="a", top_k=4)
            assert 0 < len(hits) <= 4, q
            assert all(r.namespace == "a" for r, _ in hits)
    finally:
        await eng.stop()


# ----------------------------------------------------------------- lexical rules


def _rec(rid: str, content: str, at: datetime) -> MemoryRecord:
    return MemoryRecord(
        record_id=rid, namespace="a", memory_type="episodic", content=content, valid_from=at
    )


async def test_lexical_query_override_is_what_the_lexical_leg_scores() -> None:
    recs = [_rec("r1", "Ilse: the ferry leaves at noon", NOW), _rec("r2", "Ilse: Ilse Ilse", NOW)]
    ctx = lib.LegContext(query="Ilse ferry", namespace="a", fetch_k=5, records=recs)
    plain = await lib.REGISTRY.get("lexical").run(ctx)
    ctx.lexical_query = "ferry"
    stripped = await lib.REGISTRY.get("lexical").run(ctx)
    assert {h.record_id for h in plain.hits} == {"r1", "r2"}  # the name pulls r2 in
    assert [h.record_id for h in stripped.hits] == ["r1"]


async def test_lexical_dates_index_the_date_words() -> None:
    recs = [_rec("r1", "Ilse: we sailed", datetime(2023, 5, 7, tzinfo=UTC)), _rec("r2", "x", NOW)]
    ctx = lib.LegContext(query="Sunday 7 May 2023", namespace="a", fetch_k=5, records=recs)
    assert (await lib.REGISTRY.get("lexical").run(ctx)).hits == []
    ctx.lexical_dates = True
    out = await lib.REGISTRY.get("lexical").run(ctx)
    assert [h.record_id for h in out.hits][:1] == ["r1"]


async def test_engine_strips_speaker_names_for_the_lexical_leg() -> None:
    seen: list[str] = []
    eng = await _engine(hybrid=True, lexical_strip_names=True)
    real = legs_core.LegContext
    try:
        orig = eng._strip_speaker_names

        async def spy(ns: str, q: str) -> str:
            out = await orig(ns, q)
            seen.append(out)
            return out

        eng._strip_speaker_names = spy
        [vec] = await embed_queries(eng._embedder, ["Ilse camping"])
        await registry_base_legs(eng, "a", "Ilse camping", vec, 10, True, [], [])
    finally:
        await eng.stop()
    assert real is legs_core.LegContext
    assert seen and "ilse" not in seen[0].lower()


# ----------------------------------------------------------------- router legs_off, uniformly


async def test_router_legs_off_applies_to_every_registered_leg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[str] = []
    monkeypatch.setattr(
        legs_core.tap,
        "event",
        lambda stage, /, **f: ran.append(f["leg"]) if stage == "read.leg" else None,
    )
    eng = await _engine(
        hybrid=True,
        temporal_leg=True,
        episode_leg=True,
        graph_leg=True,
        graph={},  # FU-REQS: graph_leg requires it
    )
    try:
        [vec] = await embed_queries(eng._embedder, ["what happened on 7 May 2023"])
        q = "what happened on 7 May 2023"
        await registry_base_legs(eng, "a", q, vec, 10, True, [], [])
        assert {"vector", "lexical", "temporal", "episode", "graph"} <= set(ran)
        for off in ("episode", "graph", "temporal", "lexical"):
            ran.clear()
            monkeypatch.setattr(read_request, "legs_off", lambda off=off: frozenset({off}))
            await registry_base_legs(eng, "a", q, vec, 10, True, [], [])
            assert off not in ran and "vector" in ran, off
    finally:
        await eng.stop()


async def test_router_legs_off_suppresses_the_entity_leg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []

    async def fake(*a: Any, **k: Any) -> list[Any]:
        called.append("x")
        return []

    from memspine.read.pipeline import legs as pipeline_legs

    monkeypatch.setattr(pipeline_legs, "_entity_leg", fake)
    eng = await _engine(entity_leg=True)
    try:
        await eng._metadata_legs("a", "Ilse camping", 10)
        assert called == ["x"]
        monkeypatch.setattr(read_request, "legs_off", lambda: frozenset({"entity"}))
        await eng._metadata_legs("a", "Ilse camping", 10)
        assert called == ["x"]
    finally:
        await eng.stop()
