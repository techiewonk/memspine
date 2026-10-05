"""#87: replay determinism of the read path.

Two fresh engines fed the same conversation must return the same context for every
question: same records, same order. The G8a sweep found otherwise (LoCoMo conv-26,
hash embedder, firewall on): equal fused RRF scores broke ties on the random
``record_id``, so a different record won the ``top_k`` cut from run to run, and the
turns of one session (all at the session's time) were laid out in uuid order.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from typing import Any

from memspine import Engine
from memspine.core.records import MemoryRecord, chrono_key, record_time
from memspine.core.ties import settle_ties
from memspine.services.lexical.base import LexicalHit, rrf_fuse
from memspine.services.vector.base import VectorHit

#: The G8a sweep's firewall-on arm (``evals/sweep_wrapper_threshold.py``), wrapper off.
FIREWALL_ON: dict[str, Any] = {
    "dotenv_path": None,
    "storage": {"path": ":memory:"},
    "embedding": {"provider": "hash"},
    "read": {
        "record_access": False,
        "scoring": {"recency_weight": 0.0, "importance_weight": 0.0, "utility_weight": 0.0},
    },
    "firewall": {"enabled": True},
    "integrity": {
        "enabled": True,
        "implicit_parents": "turn",
        "live_reevaluation": True,
        "untrusted_wrap_below": 0.0,
    },
}

_SPEAKERS = ("Ana", "Ben")
_TOPICS = (
    "the pottery class",
    "the camping trip",
    "the new puppy",
    "the charity run",
    "the art show",
    "the guitar lessons",
)
_LINES = (
    "{a}: I keep thinking about {t}, it was great.",
    "{a}: Thanks! {t} really made my week.",
    "{a}: Wow, that's cool! Tell me more about {t}.",
    "{a}: Yes, {t} was fun and the kids loved it.",
    "{a}: That's great, {b}! I want to try {t} too.",
)


def _conversation(sessions: int = 4, turns: int = 15) -> list[tuple[datetime, list[str]]]:
    """Short, repetitive chat turns (ties in every leg), one stamp per session."""
    start = datetime(2023, 5, 8, 13, 56, tzinfo=UTC)
    out: list[tuple[datetime, list[str]]] = []
    for s in range(sessions):
        lines = []
        for i in range(turns):
            a, b = _SPEAKERS[i % 2], _SPEAKERS[(i + 1) % 2]
            topic = _TOPICS[(s + i) % len(_TOPICS)]
            lines.append(_LINES[(s * 3 + i) % len(_LINES)].format(a=a, b=b, t=topic))
        out.append((start + timedelta(days=9 * s), lines))
    return out


_QUESTIONS = (
    "What did Ana think about the pottery class?",
    "Who loved the camping trip?",
    "What made Ben's week?",
    "When did they talk about the charity run?",
    "What does Ben want to try?",
    "What did the kids love?",
    "Tell me about the guitar lessons",
    "What was cool about the art show?",
)


async def _contexts(mode: str) -> list[list[str]]:
    engine = Engine(template="base", **FIREWALL_ON)
    await engine.start()
    try:
        for s, (when, lines) in enumerate(_conversation()):
            for line in lines:
                # The harness's write path: one user turn per call, the session stamp
                # as event time, so every turn of a session shares ``valid_from``.
                await engine.write_messages(
                    [{"role": "user", "content": line}],
                    namespace="eval",
                    session_id=f"s{s}",
                    group_id=f"s{s}",
                    valid_from=when,
                )
        out = []
        for question in _QUESTIONS:
            result = await engine.read(
                question, namespace="eval", mode=mode, budget_tokens=600, top_k=6
            )
            out.append([record.content for record in result.context.records])
        return out
    finally:
        await engine.stop()


async def test_two_fresh_engines_give_identical_replay_contexts() -> None:
    first = await _contexts("replay")
    second = await _contexts("replay")
    assert any(first), "the fixture must retrieve something"
    for question, a, b in zip(_QUESTIONS, first, second, strict=True):
        assert a == b, question


async def test_two_fresh_engines_give_identical_retrieve_contexts() -> None:
    first = await _contexts("retrieve")
    second = await _contexts("retrieve")
    for question, a, b in zip(_QUESTIONS, first, second, strict=True):
        assert a == b, question


# ── the pieces ────────────────────────────────────────────────────────────────


def test_rrf_ties_do_not_depend_on_record_ids() -> None:
    """Equal fused scores order by the legs, so relabelling ids keeps the order."""

    def fused_positions(ids: dict[str, str]) -> list[str]:
        vector = [VectorHit(ids[x], 1.0) for x in ("a", "b", "c", "d", "e")]
        lexical = [LexicalHit(ids[x], 1.0) for x in ("x", "y", "e", "z", "a")]
        back = {v: k for k, v in ids.items()}
        return [back[rid] for rid, _ in rrf_fuse(vector, lexical)]

    names = ("a", "b", "c", "d", "e", "x", "y", "z")
    ascending = {n: f"id-{i}" for i, n in enumerate(names)}
    descending = {n: f"id-{len(names) - i}" for i, n in enumerate(names)}
    assert fused_positions(ascending) == fused_positions(descending)


async def test_settle_ties_orders_runs_by_content_key_whatever_the_store_order() -> None:
    keys = {"r1": (1,), "r2": (2,), "r3": (3,), "r4": (4,), "r5": (5,)}

    async def key_of(ids: list[str]) -> dict[str, Any]:
        return {rid: keys[rid] for rid in ids}

    def store(order: list[str]) -> Any:
        scores = {"r1": 0.5, "r2": 0.5, "r3": 0.5, "r4": 0.5, "r5": 0.9}

        async def fetch(n: int) -> list[VectorHit]:
            # the store returns ties in ``order``; asked for fewer, it cuts there
            ranked = sorted(order, key=lambda rid: -scores[rid])
            return [VectorHit(rid, scores[rid]) for rid in ranked[:n]]

        return fetch

    one = await settle_ties(store(["r4", "r2", "r5", "r3", "r1"]), 3, key_of)
    two = await settle_ties(store(["r1", "r3", "r5", "r2", "r4"]), 3, key_of)
    assert [h.record_id for h in one] == [h.record_id for h in two] == ["r5", "r1", "r2"]


async def test_settle_ties_without_ties_needs_no_keys() -> None:
    async def fetch(n: int) -> list[VectorHit]:
        return [VectorHit(f"r{i}", 1.0 - i / 10) for i in range(min(n, 6))]

    async def key_of(ids: list[str]) -> dict[str, Any]:
        raise AssertionError("no tie, no lookup")

    hits = await settle_ties(fetch, 4, key_of)
    assert [h.record_id for h in hits] == ["r0", "r1", "r2", "r3"]


def test_record_time_is_strictly_increasing() -> None:
    stamps = [record_time() for _ in range(2000)]
    assert all(a < b for a, b in itertools.pairwise(stamps))


def test_chrono_key_orders_equal_event_times_by_write_order_not_id() -> None:
    when = datetime(2023, 5, 8, tzinfo=UTC)
    records = [
        MemoryRecord(namespace="n", memory_type="episodic", content=f"turn {i}", valid_from=when)
        for i in range(30)
    ]
    shuffled = sorted(records, key=lambda r: r.record_id)  # uuid order: random
    assert [r.content for r in sorted(shuffled, key=chrono_key)] == [r.content for r in records]
