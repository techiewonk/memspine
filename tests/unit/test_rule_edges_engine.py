"""W12 (plan v3.2, ADR-061): the ``rule_edges`` sleep stage and ``read.causal_walk``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from memspine import Engine
from memspine.config import constants
from memspine.config.schema import ReadConfig
from memspine.core.rule_edges import BECAUSE_REL
from memspine.workers.pipelines import RULE_EDGES_CHANNEL, rule_edges

T0 = datetime(2023, 5, 8, 13, 0, tzinfo=UTC)

TURNS = [
    ("user", "Ana: The rehearsals kept clashing with my night shifts at the hospital."),
    ("assistant", "Bo: That sounds exhausting."),
    ("user", "Ana: My sister Lena says hi, by the way."),
    ("assistant", "Bo: Say hi back!"),
    ("user", "Ana: I quit the band because the rehearsals kept clashing with my shifts."),
]


def _engine(rule_policy: object = True, **read: Any) -> Engine:
    associative: dict[str, Any] = {"enabled": True}
    if rule_policy is not None:
        associative["policies"] = {"rule_edges": rule_policy}
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "semantic": {"enabled": True},
            "episodic": {"enabled": True},
            "associative": associative,
        },
        read={"hybrid": False, "record_access": False, **read},
    )


async def _seed(eng: Engine) -> list[str]:
    msgs = [
        {"role": r, "content": c, "timestamp": (T0 + timedelta(minutes=i)).isoformat()}
        for i, (r, c) in enumerate(TURNS)
    ]
    records = await eng.write_messages(msgs, namespace="a", session_id="s1")
    return [r.record_id for r in records]


def test_read_keys_default_off() -> None:
    cfg = ReadConfig()
    assert cfg.causal_walk == "off"
    assert cfg.reply_links is False


async def test_stage_writes_because_links_and_kinship_facts_idempotently() -> None:
    eng = _engine()
    await eng.start()
    try:
        ids = await _seed(eng)
        stats = await rule_edges(eng._pipeline_ctx())
        assert stats["status"] == "ok"
        assert stats["links"] == 1
        assert stats["facts"] == 1
        edges = [e for e in await eng._graph.edges_of(ids[4]) if e.rel_type == BECAUSE_REL]
        assert [(e.src, e.dst) for e in edges] == [(ids[4], ids[0])]
        assert 0 < edges[0].weight <= constants.RULE_EDGE_WEIGHT
        facts = [
            r
            for r in await eng._require_started().list_records("a", "semantic")
            if r.source.channel == RULE_EDGES_CHANNEL
        ]
        assert [f.content for f in facts] == ["Lena is Ana's sister."]
        assert facts[0].entity == "Lena"
        assert facts[0].source.parents == [ids[2]]
        assert "dst:ana" in facts[0].tags
        again = await rule_edges(eng._pipeline_ctx())
        assert again["links"] == 0 and again["facts"] == 0
        assert again["existing_links"] == 1
    finally:
        await eng.stop()


async def test_stage_skips_when_off_and_sleep_cycle_runs_it_only_when_on() -> None:
    eng = _engine(rule_policy=None)
    await eng.start()
    try:
        await _seed(eng)
        stats = await rule_edges(eng._pipeline_ctx())
        assert stats["status"] == "skipped"
        assert "rule_edges" not in await eng.sleep()
    finally:
        await eng.stop()
    eng = _engine()
    await eng.start()
    try:
        await _seed(eng)
        cycle = await eng.sleep()
        assert list(cycle).index("rule_edges") == list(cycle).index("extract_graph") + 1
        assert cycle["rule_edges"]["links"] == 1
    finally:
        await eng.stop()


async def test_policy_options_turn_one_rule_off() -> None:
    eng = _engine(rule_policy={"kinship": False})
    await eng.start()
    try:
        await _seed(eng)
        stats = await rule_edges(eng._pipeline_ctx())
        assert stats["links"] == 1 and stats["facts"] == 0
    finally:
        await eng.stop()


async def test_causal_walk_brings_the_cause_turn_to_a_why_question() -> None:
    query = "Why did Ana quit the band?"
    eng = _engine(causal_walk="why")
    await eng.start()
    try:
        ids = await _seed(eng)
        await rule_edges(eng._pipeline_ctx())
        base = await eng._search(query, "a", 5, keep_k=5)
        scores = {r.record_id: s for r, s in base}
        assert ids[4] in scores
        walked = await eng._causal_walk("a", base[:1], None, None, None)
        got = {r.record_id: s for r, s in walked}
        assert base[0][0].record_id == ids[4]
        assert got[ids[0]] == scores[ids[4]] * constants.CAUSAL_WALK_DECAY
    finally:
        await eng.stop()


async def test_causal_walk_off_and_non_why_questions_are_unchanged() -> None:
    query_why = "Why did Ana quit the band?"
    query_plain = "Did Ana quit the band?"
    off = _engine()
    on = _engine(causal_walk="why")
    await off.start()
    await on.start()
    try:
        fillers = [
            "Ana: The band is playing at the pub on Friday.",
            "Bo: The band has a new drummer.",
        ]
        for eng in (off, on):
            await eng.write_messages(
                [{"role": "user", "content": text} for text in fillers],
                namespace="a",
                session_id="s0",
                valid_from=T0 - timedelta(days=1),
            )
            await _seed(eng)
            await rule_edges(eng._pipeline_ctx())

        def contents(hits: list[Any]) -> list[str]:
            return [r.content for r, _ in hits]

        assert contents(await off.search(query_plain, "a", top_k=1)) == contents(
            await on.search(query_plain, "a", top_k=1)
        )
        assert TURNS[0][1] not in contents(await off.search(query_why, "a", top_k=2))
        assert TURNS[0][1] in contents(await on.search(query_why, "a", top_k=2))
    finally:
        await off.stop()
        await on.stop()
