"""#54: ``Engine.feedback`` (like / dislike / note) as FEEDBACK events feeding utility."""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from memspine import Engine
from memspine.config import constants
from memspine.core.events import EventKind
from memspine.core.policies.scoring import ScoringPolicy, feedback_utility
from memspine.core.records import MemoryRecord, ScoringState
from memspine.exceptions import ConflictError, MemspineError

_BASE: dict[str, Any] = {
    "embedding": {"provider": "hash"},
    "read": {"hybrid": False},
}


@pytest.fixture
async def engine() -> AsyncIterator[Engine]:
    eng = Engine(template="core", dotenv_path=None, storage={"path": ":memory:"}, **_BASE)
    await eng.start()
    yield eng
    await eng.stop()


async def test_counts_per_signal(engine: Engine) -> None:
    record = await engine.write("Ana likes green tea", namespace="a")
    await engine.feedback(record.record_id, "like", namespace="a")
    await engine.feedback(record.record_id, "like", namespace="a", note="spot on")
    updated = await engine.feedback(record.record_id, "dislike", namespace="a")
    assert (updated.scoring.likes, updated.scoring.dislikes, updated.scoring.notes) == (2, 1, 1)
    noted = await engine.feedback(record.record_id, "note", note="outdated?", namespace="a")
    assert noted.scoring.notes == 2 and noted.scoring.likes == 2


async def test_invalid_calls_raise(engine: Engine) -> None:
    record = await engine.write("Ana likes green tea", namespace="a")
    with pytest.raises(MemspineError, match="like, dislike or note"):
        await engine.feedback(record.record_id, "love", namespace="a")
    with pytest.raises(MemspineError, match="non-empty note"):
        await engine.feedback(record.record_id, "note", note="  ", namespace="a")
    with pytest.raises(ConflictError):  # foreign namespace: same error as a missing id
        await engine.feedback(record.record_id, "like", namespace="b")
    with pytest.raises(ConflictError):
        await engine.feedback("no-such-id", "like", namespace="a")
    await engine.forget(record.record_id, namespace="a")
    with pytest.raises(ConflictError):
        await engine.feedback(record.record_id, "like", namespace="a")


async def test_unrated_record_serializes_as_before(engine: Engine) -> None:
    record = await engine.write("Ana likes green tea", namespace="a")
    scoring = record.model_dump(mode="json")["scoring"]
    assert not {"likes", "dislikes", "notes", "passive"} & set(scoring)


def test_feedback_term_is_bounded_and_zero_without_feedback() -> None:
    assert feedback_utility(ScoringState()) == 0.0
    assert feedback_utility(ScoringState(likes=3)) == pytest.approx(
        math.tanh(3 / constants.FEEDBACK_UTILITY_SCALE)
    )
    assert 0.0 < feedback_utility(ScoringState(likes=10_000)) <= 1.0
    assert feedback_utility(ScoringState(dislikes=2)) < 0.0


def _record(**scoring: int) -> MemoryRecord:
    return MemoryRecord(
        namespace="a", memory_type="semantic", content="x", scoring=ScoringState(**scoring)
    )


@pytest.mark.parametrize("mode", ["blend", "relevance_first"])
def test_utility_weight_gates_the_feedback_term(mode: str) -> None:
    on = ScoringPolicy.bind({"utility_weight": 0.5, "mode": mode})
    off = ScoringPolicy.bind({"utility_weight": 0.0, "mode": mode})
    liked, plain, disliked = _record(likes=4), _record(), _record(dislikes=4)
    now = plain.recorded_at
    score_on = [on.composite_score(r, 0.5, now) for r in (liked, plain, disliked)]
    assert score_on[0] > score_on[1] > score_on[2]
    score_off = [off.composite_score(r, 0.5, now) for r in (liked, plain, disliked)]
    assert score_off[0] == pytest.approx(score_off[1]) == pytest.approx(score_off[2])


async def test_dislike_demotes_in_search_when_utility_weighted() -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        read={"hybrid": False, "record_access": False, "scoring": {"utility_weight": 1.0}},
    )
    await eng.start()
    try:
        first = await eng.write("Ana drinks green tea every morning", namespace="a")
        second = await eng.write("Ana drinks green tea every evening", namespace="a")
        before = [r.record_id for r, _ in await eng.search("Ana green tea", namespace="a")]
        top = first if before[0] == first.record_id else second
        for _ in range(3):
            await eng.feedback(top.record_id, "dislike", namespace="a")
        after = [r.record_id for r, _ in await eng.search("Ana green tea", namespace="a")]
        assert after[0] != top.record_id
    finally:
        await eng.stop()


async def test_counts_survive_rebuild(tmp_path: Path) -> None:
    eng = Engine(
        template="core", dotenv_path=None, storage={"path": str(tmp_path / "f.db")}, **_BASE
    )
    await eng.start()
    try:
        record = await eng.write("Ana likes green tea", namespace="a")
        await eng.feedback(record.record_id, "like", namespace="a")
        await eng.feedback(record.record_id, "dislike", namespace="a", note="not anymore")
        await eng.rebuild()
        (rebuilt,) = await eng.retrieve(namespace="a")
        assert (rebuilt.scoring.likes, rebuilt.scoring.dislikes, rebuilt.scoring.notes) == (
            1,
            1,
            1,
        )
    finally:
        await eng.stop()


async def test_hard_forget_erases_the_note(tmp_path: Path) -> None:
    eng = Engine(
        template="core", dotenv_path=None, storage={"path": str(tmp_path / "f.db")}, **_BASE
    )
    await eng.start()
    try:
        record = await eng.write("Ana likes green tea", namespace="a")
        await eng.feedback(record.record_id, "note", note="the private number", namespace="a")
        await eng.forget(record.record_id, namespace="a", hard=True)
        assert eng._storage is not None
        events = await eng._storage.read_events()
        notes = [e for e in events if e.kind is EventKind.FEEDBACK]
        assert notes and all(not e.payload.get("content") for e in notes)
        proof = await eng.verify_forget(record.record_id, namespace="a")
        assert proof["clean"] is True
    finally:
        await eng.stop()


async def test_note_is_screened_by_the_firewall_redaction() -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        firewall={"redact_secrets": True},
        **_BASE,
    )
    await eng.start()
    try:
        record = await eng.write("Ana likes green tea", namespace="a")
        await eng.feedback(record.record_id, "note", note="mail ana@example.com", namespace="a")
        assert eng._storage is not None
        events = await eng._storage.read_events()
        (event,) = [e for e in events if e.kind is EventKind.FEEDBACK]
        assert "ana@example.com" not in str(event.payload["content"])
    finally:
        await eng.stop()
