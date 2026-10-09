"""``read.list_mode`` (gap B1): speaker vote leg, pool 30 and rank-tiered windows. Opt-in."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.core.query_shape import is_set_question
from memspine.core.records import MemoryRecord
from memspine.core.temporal_query import LegHit, speaker_vector_leg
from memspine.engine import search_forensics

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"record_access": False, **read},
    )


def _rec(text: str, tags: list[str] | None = None) -> MemoryRecord:
    return MemoryRecord(namespace="a", memory_type="episodic", content=text, tags=tags or [])


# -- trigger ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "What activities does Melanie partake in?",
        "What do Melanie's kids like?",
        "What events has Caroline participated in to help children?",
        "What kind of fiction stories does Tim write?",
        "Which books has Tim read?",
    ],
)
def test_is_set_question_true(question: str) -> None:
    assert is_set_question(question)


@pytest.mark.parametrize(
    "question",
    [
        "When did Melanie go camping?",
        "How many kids does Melanie have?",
        "Did Melanie go to the park?",
        "What book did Melanie read from Caroline's suggestion?",
        "What is Melanie's job?",
        "Where did Tim travel last summer?",
    ],
)
def test_is_set_question_false(question: str) -> None:
    assert not is_set_question(question)


# -- pure leg --------------------------------------------------------------------------


def test_speaker_vector_leg_keeps_only_the_named_speaker_in_vector_order() -> None:
    recs = [
        _rec("Melanie: I went camping"),
        _rec("Caroline: I went to a march"),
        _rec("Melanie: pottery today"),
        _rec("Melanie: more camping"),
    ]
    hits = [LegHit(r.record_id, 1.0 - i / 10) for i, r in enumerate(recs)]
    out = speaker_vector_leg("What does Melanie's family do?", recs, hits, top_k=2)
    assert [h.record_id for h in out] == [recs[0].record_id, recs[2].record_id]
    assert [h.score for h in out] == [1.0, 0.8]  # the vector scores are kept


def test_speaker_vector_leg_uses_tag_when_present() -> None:
    tagged = _rec("no prefix here", ["speaker:melanie"])
    other = _rec("Caroline: hi")
    hits = [LegHit(other.record_id, 0.9), LegHit(tagged.record_id, 0.8)]
    out = speaker_vector_leg("what does melanie like", [tagged, other], hits)
    assert [h.record_id for h in out] == [tagged.record_id]


def test_speaker_vector_leg_needs_exactly_one_speaker() -> None:
    recs = [_rec("Melanie: a"), _rec("Caroline: b")]
    hits = [LegHit(r.record_id, 1.0) for r in recs]
    assert speaker_vector_leg("What do Melanie and Caroline like?", recs, hits) == []
    assert speaker_vector_leg("What do people like?", recs, hits) == []
    untagged = [_rec("untagged text"), _rec("more untagged text")]
    assert speaker_vector_leg("What does Melanie like?", untagged, hits) == []


# -- engine ----------------------------------------------------------------------------


async def _two_speakers(eng: Engine) -> None:
    lines = [
        "Melanie: I love camping with the kids",
        "Caroline: I joined a march for equality",
        "Melanie: We went swimming at the beach",
        "Caroline: Our support group meets weekly",
        "Melanie: Pottery class was great today",
        "Caroline: I painted a sunrise",
    ]
    for i, text in enumerate(lines):
        await eng.write(
            text, namespace="a", memory_type="episodic", valid_from=T0 + timedelta(minutes=i)
        )


async def _vote_contents(eng: Engine, question: str) -> list[str] | None:
    with search_forensics() as stages:
        await eng.read(question, namespace="a", mode="replay", top_k=3)
    leg = dict(stages.get("extra_legs", [])).get("speaker_vote")
    if leg is None:
        return None
    return [(await eng._require_started().get_record(rid)).content for rid, _ in leg]


async def test_leg_fires_for_set_question_naming_one_speaker() -> None:
    eng = _engine(list_mode=True)
    await eng.start()
    try:
        await _two_speakers(eng)
        got = await _vote_contents(eng, "What activities does Melanie do?")
        assert got is not None
        assert len(got) == 3  # every Melanie turn
        assert all(c.startswith("Melanie:") for c in got)  # and no Caroline turn
    finally:
        await eng.stop()


async def test_leg_silent_without_trigger_or_with_two_speakers_or_when_off() -> None:
    on = _engine(list_mode=True)
    off = _engine()
    await on.start()
    await off.start()
    try:
        await _two_speakers(on)
        await _two_speakers(off)
        assert await _vote_contents(on, "When did Melanie go camping?") is None  # no trigger
        assert await _vote_contents(on, "What activities do Melanie and Caroline do?") is None
        assert await _vote_contents(off, "What activities does Melanie do?") is None  # off
    finally:
        await on.stop()
        await off.stop()


async def test_leg_failure_is_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(list_mode=True)
    await eng.start()
    try:
        await _two_speakers(eng)

        def boom(*_: Any, **__: Any) -> Any:
            raise RuntimeError("boom")

        monkeypatch.setattr("memspine.engine.speaker_vector_leg", boom)
        out = await eng.read("What activities does Melanie do?", namespace="a", mode="replay")
        assert out.context.records  # the read still answers
    finally:
        await eng.stop()


async def test_deep_vector_fetch_for_the_vote() -> None:
    eng = _engine(list_mode=True, list_vote_depth=50)
    await eng.start()
    try:
        await _two_speakers(eng)
        depths: list[int] = []
        orig = eng._vector_leg

        async def spy(ns: str, vec: list[float], k: int) -> Any:
            depths.append(k)
            return await orig(ns, vec, k)

        eng._vector_leg = spy  # type: ignore[method-assign]
        await eng.read("What activities does Melanie do?", namespace="a", mode="replay")
        assert max(depths) >= 50
    finally:
        await eng.stop()


# -- pool and tiered windows -------------------------------------------------------------


async def _sessions(eng: Engine, monkeypatch: pytest.MonkeyPatch, n: int = 30) -> list[list[Any]]:
    """``n`` sessions of 8 turns a day apart; the fake search hits turn 3 of every session."""
    sessions: list[list[Any]] = []
    for j in range(n):
        turns = []
        for i in range(8):
            words = " ".join(f"tok{j}x{i}x{k}" for k in range(6))
            turns.append(
                await eng.write(
                    f"Tim: {words}",
                    namespace="a",
                    memory_type="episodic",
                    valid_from=T0 + timedelta(days=j, minutes=i),
                )
            )
        sessions.append(turns)
    hits = [s[3] for s in sessions]

    async def fake_search(
        probe: str, namespace: str = "a", top_k: int = 8, **_: Any
    ) -> list[tuple[MemoryRecord, float]]:
        return [(h, 0.9) for h in hits[:top_k]]

    monkeypatch.setattr(eng, "_search", fake_search)
    return sessions


async def _read(eng: Engine, question: str) -> list[Any]:
    out = await eng.read(
        question, namespace="a", mode="replay", top_k=10, replay_window=2, budget_tokens=100000
    )
    return out.context.records


async def test_tiered_windows_only_on_the_first_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(
        list_mode=True,
        window_full_hits=10,
        replay_window_before=2,
        replay_window_after=4,
        reply_links=False,
    )
    await eng.start()
    try:
        sessions = await _sessions(eng, monkeypatch)
        got = {r.record_id for r in await _read(eng, "What activities does Tim do?")}
        for j, turns in enumerate(sessions):
            ids = [t.record_id for t in turns]
            if j < 10:
                assert set(ids[1:8]) <= got  # hit 3, two before, four after
            else:
                assert got & set(ids) == {ids[3]}  # a single turn, no neighbours
        assert len(got) == 10 * 7 + 20  # a pool of 30 candidates was drawn
    finally:
        await eng.stop()


async def test_window_full_hits_none_keeps_every_window(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(list_mode=True, window_full_hits=None, reply_links=False)
    await eng.start()
    try:
        sessions = await _sessions(eng, monkeypatch)
        got = {r.record_id for r in await _read(eng, "What activities does Tim do?")}
        assert len(got) == 30 * 5  # the +-2 window around each of 30 hits
        assert {t.record_id for t in sessions[29][1:6]} <= got
    finally:
        await eng.stop()


async def test_off_or_untriggered_reads_are_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    off = _engine(reply_links=False)
    on = _engine(list_mode=True, reply_links=False)
    await off.start()
    await on.start()
    try:
        await _sessions(off, monkeypatch)
        base = await _read(off, "What activities does Tim do?")
        await _sessions(on, monkeypatch)
        not_fired = await _read(on, "When did Tim go camping?")  # not a set question
        assert len(base) == len(not_fired) == 10 * 5
        assert [r.content for r in base] == [r.content for r in not_fired]
    finally:
        await off.stop()
        await on.stop()
