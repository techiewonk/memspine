"""Wave C (gaps plan 2026-10-08): N52 min-max fusion, N43 cohesion leg, N32 entity
expansion with N53 damping. All off by default."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from memspine import Engine
from memspine.config.schema import ReadConfig
from memspine.core.records import MemoryRecord
from memspine.core.temporal_query import cohesion_leg, entity_expand_leg
from memspine.services.lexical.base import minmax_fuse, rrf_fuse

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)


@dataclass
class Hit:
    record_id: str
    score: float = 1.0


def test_keys_are_off_by_default() -> None:
    read = ReadConfig()
    assert (read.fusion, read.cohesion_leg, read.entity_expand_leg) == ("rrf", False, False)


def test_minmax_uses_score_margins_rrf_ignores() -> None:
    # Vector: a is far ahead of b. Lexical: b barely ahead of a.
    vector = [Hit("a", 0.95), Hit("b", 0.20), Hit("c", 0.10)]
    lexical = [Hit("b", 5.01), Hit("a", 5.00), Hit("c", 1.00)]
    assert minmax_fuse(vector, lexical)[0][0] == "a"
    # RRF sees only ranks: a (1, 2) and b (2, 1) tie, broken by the vector rank.
    assert {rid for rid, _ in rrf_fuse(vector, lexical)[:2]} == {"a", "b"}


def test_minmax_normalises_tied_rule_legs_by_rank() -> None:
    fused = dict(minmax_fuse([], [], extra=[[Hit("x"), Hit("y")]]))
    assert fused["x"] > fused["y"]
    assert max(fused.values()) == 1.0


def _rec(content: str, minutes: int) -> MemoryRecord:
    return MemoryRecord(
        namespace="a",
        memory_type="episodic",
        content=content,
        valid_from=T0 + timedelta(minutes=minutes),
    )


def test_cohesion_leg_finds_turns_said_close_to_an_anchor() -> None:
    anchor = _rec("Ana: where shall we camp?", 0)
    reply = _rec("Ben: the lake", 2)
    later = _rec("Ben: unrelated", 60)
    hits = cohesion_leg([anchor], [anchor, reply, later], 5, timedelta(minutes=5))
    assert [h.record_id for h in hits] == [reply.record_id]


def test_entity_expand_follows_names_and_damps_common_ones() -> None:
    anchor = _rec("Ana: Luna the cat loves the Hobbit", 0)
    other = _rec("Ben: Luna knocked a cup over", 30)
    filler = [_rec(f"Ana: Hobbit chat {i}", 40 + i) for i in range(3)]
    records = [anchor, other, *filler]
    undamped = entity_expand_leg([anchor], records, 10, exclude=["ana", "ben"])
    damped = entity_expand_leg([anchor], records, 10, exclude=["ana", "ben"], max_share=0.5)
    assert other.record_id in {h.record_id for h in undamped}
    # "Hobbit" appears in 4 of 5 records: too common, so only Luna is followed.
    assert [h.record_id for h in damped] == [other.record_id]


async def test_engine_wires_the_anchor_legs() -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"record_access": False, "cohesion_leg": True, "fusion": "minmax"},
    )
    await eng.start()
    try:
        for i, text in enumerate(["Ana: where shall we camp?", "Ben: the lake", "Ana: ok"]):
            await eng.write(
                text, namespace="a", memory_type="episodic", valid_from=T0 + timedelta(minutes=i)
            )
        hits = await eng.search("where shall we camp", namespace="a", top_k=3)
    finally:
        await eng.stop()
    assert len(hits) == 3
    assert all(0.0 <= score <= 1.0 for _, score in hits)
