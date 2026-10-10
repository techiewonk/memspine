"""I6 / I20 / I31 / I33: token-aware replay window, budget scaling, hits first,
near-duplicate removal before assembly, relevance-gated profile lines."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memspine import Engine
from memspine.config.schema import ReadConfig
from memspine.core.dedupe import dedupe_scored
from memspine.core.records import MemoryRecord, SourceInfo
from memspine.engine import search_forensics


def _rec(text: str, minute: int = 0, channel: str = "doc") -> MemoryRecord:
    return MemoryRecord(
        namespace="a",
        memory_type="episodic",
        content=text,
        valid_from=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(minutes=minute),
        source=SourceInfo(role="user", channel=channel),
    )


# ---------------------------------------------------------------- defaults


def test_new_keys_default_to_todays_behaviour() -> None:
    cfg = ReadConfig()
    assert cfg.replay_window_unit == "turns"
    assert cfg.replay_budget_scaling is False
    assert cfg.replay_hits_first is False
    assert cfg.dedupe == "off"
    assert cfg.dedupe_threshold == 0.9
    assert cfg.profile_relevance_gate == "off"


# ---------------------------------------------------------------- I31 dedupe


def test_dedupe_off_is_identity() -> None:
    scored = [(_rec("same text here"), 0.9), (_rec("same text here"), 0.8)]
    out, dropped = dedupe_scored(scored, "off")
    assert out == scored and dropped == []


def test_exact_dedupe_keeps_best_and_records_the_drop() -> None:
    a, b = _rec("Alice  adopted a DOG"), _rec("alice adopted a dog", 5)
    out, dropped = dedupe_scored([(a, 0.9), (b, 0.8)], "exact")
    assert [r.record_id for r, _ in out] == [a.record_id]
    assert (dropped[0].dropped, dropped[0].kept) == (b.record_id, a.record_id)


def test_jaccard_dedupe_threshold_and_keep_earliest() -> None:
    late = _rec("Melanie painted a lake sunrise last summer", 30)
    early = _rec("Melanie painted a lake sunrise last summer too", 1)
    other = _rec("Caleb bought a red truck yesterday", 2)
    scored = [(late, 0.9), (other, 0.5), (early, 0.4)]
    out, dropped = dedupe_scored(scored, "jaccard", threshold=0.8, keep="earliest")
    ids = [r.record_id for r, _ in out]
    assert ids == [early.record_id, other.record_id]
    assert out[0][1] == 0.9  # the survivor takes the better score
    assert dropped[0].dropped == late.record_id
    best, _ = dedupe_scored(scored, "jaccard", threshold=0.8, keep="best")
    assert [r.record_id for r, _ in best] == [late.record_id, other.record_id]
    loose, _ = dedupe_scored(scored, "jaccard", threshold=1.0)
    assert len(loose) == 3  # not identical word sets


def test_embedding_dedupe_uses_vectors_else_jaccard() -> None:
    a, b, c = _rec("alpha beta"), _rec("gamma delta", 1), _rec("epsilon zeta", 2)
    vecs = {a.record_id: [1.0, 0.0], b.record_id: [0.99, 0.01], c.record_id: [0.0, 1.0]}
    out, dropped = dedupe_scored(
        [(a, 0.9), (b, 0.8), (c, 0.7)], "embedding", threshold=0.95, vectors=vecs
    )
    assert [r.record_id for r, _ in out] == [a.record_id, c.record_id]
    assert dropped[0].dropped == b.record_id


def test_persona_is_never_deduped() -> None:
    p1, p2 = _rec("be kind always", channel="persona"), _rec("be kind always", channel="persona")
    out, _ = dedupe_scored([(p1, 1.0), (p2, 1.0)], "exact")
    assert len(out) == 2


async def test_read_dedupes_before_assembly_and_reports_it() -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"semantic": {"enabled": True}},
        read={"hybrid": False, "dedupe": "jaccard"},
    )
    await eng.start()
    try:
        for text in ["Nina keeps two cats named Pip and Moss.", "Nina keeps two cats named Pip and Moss."]:
            await eng.write(text, namespace="a", memory_type="semantic")
        await eng.write("Omar plays the cello on weekends.", namespace="a", memory_type="semantic")
        with search_forensics() as sink:
            out = await eng.read("What pets does Nina have?", namespace="a", mode="retrieve")
        cats = [r for r in out.context.records if "cats" in r.content]
        assert len(cats) <= 1
        assert "dedupe_dropped" in sink
    finally:
        await eng.stop()


# ------------------------------------------------- I6 / I20 window helper


class _Stub:
    """The bare pieces ``Engine._window_turns`` and ``_budget_factor`` use."""

    _window_turns = Engine._window_turns
    _budget_factor = Engine._budget_factor
    _window_sides = Engine._window_sides

    def __init__(self, turns: dict[str, MemoryRecord], **read: object) -> None:
        self.turns = turns
        self.cfg = type("C", (), {"read": ReadConfig(**read)})()  # type: ignore[arg-type]

    def _config(self):  # noqa: ANN202
        return self.cfg

    async def _replay_neighbour(self, rid: str, ns: str) -> MemoryRecord | None:
        return self.turns.get(rid)


def _session(sizes: list[int]) -> tuple[list[str], dict[str, MemoryRecord]]:
    recs = [_rec("w" * (4 * n), i) for i, n in enumerate(sizes)]
    return [r.record_id for r in recs], {r.record_id: r for r in recs}


async def test_turns_unit_is_the_old_window() -> None:
    ids, turns = _session([8] * 9)
    stub = _Stub(turns)
    got = await stub._window_turns(
        "a", ids, 4, 2, budget_tokens=10_000, used=0, seen=set(), hit=turns[ids[4]]
    )
    order = [rid for rid, _, _ in got]
    assert order == [ids[4], ids[3], ids[5], ids[2], ids[6]]  # hit, then nearest first


async def test_token_window_stops_at_a_long_assistant_turn() -> None:
    # neighbours: short, short, then a 2,000-token reply that must close the side
    ids, turns = _session([8, 2000, 8, 8, 8, 8, 2000, 8])
    stub = _Stub(
        turns,
        replay_window_unit="tokens",
        replay_window_tokens_before=100,
        replay_window_tokens_after=100,
    )
    got = await stub._window_turns(
        "a", ids, 3, 2, budget_tokens=10_000, used=0, seen=set(), hit=turns[ids[3]]
    )
    taken = {rid for rid, _, _ in got}
    assert ids[3] in taken and ids[4] in taken and ids[5] in taken
    assert ids[2] in taken  # short neighbour before
    assert ids[1] not in taken  # the 2,000-token turn passes the allowance
    assert ids[6] not in taken
    assert ids[0] not in taken  # the side stays closed past the long turn


async def test_token_window_uses_more_short_turns_than_two() -> None:
    ids, turns = _session([8] * 20)
    stub = _Stub(
        turns,
        replay_window_unit="tokens",
        replay_window_tokens_before=40,
        replay_window_tokens_after=80,
    )
    got = await stub._window_turns(
        "a", ids, 10, 2, budget_tokens=10_000, used=0, seen=set(), hit=turns[ids[10]]
    )
    assert len(got) > 5  # 3 + 5-ish turns of ~9 tokens, not the fixed 2 / 4


async def test_budget_is_never_overrun_and_hit_comes_first() -> None:
    ids, turns = _session([100, 100, 100])
    stub = _Stub(turns, replay_window_unit="tokens")
    got = await stub._window_turns(
        "a", ids, 1, 2, budget_tokens=110, used=0, seen=set(), hit=turns[ids[1]]
    )
    assert [rid for rid, _, _ in got] == [ids[1]]


def test_budget_scaling_factor() -> None:
    off = _Stub({})
    assert off._budget_factor(500) == 1.0
    on = _Stub({}, replay_budget_scaling=True, replay_budget_reference=4000)
    assert on._budget_factor(8000) == 1.0
    assert on._budget_factor(2000) == 0.5
    assert on._budget_factor(10) == 0.25


async def test_scaled_turn_window_shrinks_but_keeps_one_side_turn() -> None:
    ids, turns = _session([8] * 12)
    stub = _Stub(turns, replay_budget_scaling=True, replay_budget_reference=4000)
    got = await stub._window_turns(
        "a", ids, 6, 2, budget_tokens=10_000, used=0, seen=set(),
        hit=turns[ids[6]], factor=0.25,
    )
    assert [rid for rid, _, _ in got] == [ids[6], ids[5], ids[7]]


# ---------------------------------------------------- I20 hits first (engine)


async def test_replay_hits_first_keeps_second_hit_under_tight_budget() -> None:
    async def run(**read: object) -> list[str]:
        eng = Engine(
            template="core",
            dotenv_path=None,
            storage={"path": ":memory:"},
            embedding={"provider": "hash"},
            memories={"episodic": {"enabled": True}},
            read={"hybrid": False, "record_access": False, **read},
        )
        await eng.start()
        try:
            base = datetime(2024, 1, 1, tzinfo=UTC)
            lines = [
                "filler talk about the weather today " * 2,
                "Ruth adopted a greyhound named Biscuit.",
                "more filler talk about the weather today " * 2,
                "other chatter about trains and schedules " * 2,
                "Ruth's greyhound Biscuit loves the beach.",
                "closing filler talk about the weather " * 2,
            ]
            for i, text in enumerate(lines):
                await eng.write(
                    text, namespace="a", memory_type="episodic",
                    valid_from=base + timedelta(minutes=i),
                )
            out = await eng.read(
                "Tell me about Ruth's greyhound Biscuit", namespace="a",
                mode="replay", budget_tokens=120, top_k=2,
            )
            return [r.content for r in out.context.records]
        finally:
            await eng.stop()

    first = await run(replay_hits_first=True)
    assert any("adopted a greyhound" in c for c in first)
    assert any("loves the beach" in c for c in first)


# --------------------------------------------------------- I33 profile gate


def test_profile_line_gate_off_always_true() -> None:
    cfg = type("C", (), {"read": ReadConfig()})()
    stub = type("S", (), {"_config": lambda self: cfg})()
    assert Engine._profile_line_relevant(stub, "What is the capital of France?", "likes jazz")


@pytest.mark.parametrize(
    ("query", "line", "expected"),
    [
        ("What music does Caroline like?", "Caroline likes jazz music", True),
        ("What is the capital of France?", "Caroline likes jazz music", False),
        ("Where does Caroline live?", "Caroline likes jazz music", False),  # a name alone
        ("", "Caroline likes jazz music", True),  # session start: no question, no gate
    ],
)
def test_profile_line_gate_overlap(query: str, line: str, expected: bool) -> None:
    cfg = type("C", (), {"read": ReadConfig(profile_relevance_gate="overlap")})()
    stub = type("S", (), {"_config": lambda self: cfg})()
    assert Engine._profile_line_relevant(stub, query, line) is expected
