"""#53: session lifecycle (PASSIVE archival of idle sessions), opt-in."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memspine import Engine
from memspine.core.events import EventKind
from memspine.core.records import MemoryRecord, ScoringState, SourceInfo
from memspine.exceptions import ConfigError
from memspine.memories.episodic.lifecycle import parse_duration, plan_transitions

HORIZON = 1.0  # seconds; the tests wait past it in real time


def _engine(path: str, passive_after: object = HORIZON, **extra: Any) -> Engine:
    policies: dict[str, Any] = {}
    if passive_after is not None:
        policies["sessions"] = {"passive_after": passive_after}
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": path},
        embedding={"provider": "hash"},
        read={"hybrid": False, "record_access": False},
        memories={"episodic": {"enabled": True, "policies": policies}},
        **extra,
    )


def _turns(*texts: str) -> list[dict[str, str]]:
    return [{"role": "user", "content": t} for t in texts]


async def _two_sessions(eng: Engine) -> None:
    """``old`` idles past the horizon; ``new`` is written just before the sleep."""
    await eng.write_messages(
        _turns("Ana booked the flight to Lyon", "Ana packed the green bag"),
        namespace="a",
        session_id="old",
        group_id="old",
    )
    await asyncio.sleep(HORIZON + 0.2)
    await eng.write_messages(
        _turns("Ana asked about the Lyon hotel"), namespace="a", session_id="new", group_id="new"
    )


def _sessions(hits: list[tuple[MemoryRecord, float]] | list[MemoryRecord]) -> set[str | None]:
    records = [h[0] if isinstance(h, tuple) else h for h in hits]
    return {r.source.message_id for r in records}


@pytest.mark.parametrize(
    ("value", "seconds"),
    [(None, None), (90, 90.0), (0.5, 0.5), ("30d", 30 * 86400.0), ("12h", 43200.0), ("2w", 1209600.0)],
)
def test_parse_duration(value: object, seconds: float | None) -> None:
    parsed = parse_duration(value)
    assert (parsed.total_seconds() if parsed else None) == seconds


@pytest.mark.parametrize("value", ["soon", "-3d", 0, True, "3 years"])
def test_parse_duration_rejects(value: object) -> None:
    with pytest.raises(ConfigError):
        parse_duration(value)


async def test_bad_horizon_fails_at_start() -> None:
    eng = _engine(":memory:", passive_after="later")
    with pytest.raises(ConfigError, match="passive_after"):
        await eng.start()


def test_plan_transitions_both_directions() -> None:
    now = datetime(2026, 1, 10, tzinfo=UTC)

    def rec(sid: str, days_ago: int, passive: bool) -> MemoryRecord:
        return MemoryRecord(
            namespace="a",
            memory_type="episodic",
            content=sid,
            source=SourceInfo(message_id=sid),
            recorded_at=now - timedelta(days=days_ago),
            scoring=ScoringState(passive=passive),
        )

    plan = plan_transitions(
        [rec("idle", 9, False), rec("idle", 8, False), rec("back", 1, True), rec("kept", 9, True)],
        now,
        timedelta(days=7),
    )
    assert [(t.session_id, t.state, len(t.record_ids)) for t in plan] == [
        ("back", "active", 1),
        ("idle", "passive", 2),
    ]


async def test_idle_session_leaves_default_reads_and_stays_reachable(tmp_path: Path) -> None:
    eng = _engine(str(tmp_path / "s.db"))
    await eng.start()
    try:
        await _two_sessions(eng)
        stats = await eng.sleep()
        assert stats["session_lifecycle"] == {"status": "ok", "passivated": 1, "reopened": 0}
        query = "Ana Lyon"
        assert _sessions(await eng.search(query, namespace="a", top_k=10)) == {"new"}
        assert _sessions(await eng.retrieve(namespace="a", memory_type="episodic")) == {"new"}
        assert "old" in _sessions(
            await eng.search(query, namespace="a", top_k=10, include_passive=True)
        )
        assert "old" in _sessions(
            await eng.search(query, namespace="a", top_k=10, session_id="old")
        )
        assert _sessions(
            await eng.search(query, namespace="a", top_k=10, group_id="old")
        ) == {"old"}
        assert "old" in _sessions(
            await eng.retrieve(namespace="a", memory_type="episodic", include_passive=True)
        )
        read = await eng.read(query, namespace="a", mode="retrieve")
        assert _sessions(read.context.records) <= {"new", None}
        read_all = await eng.read(query, namespace="a", mode="retrieve", include_passive=True)
        assert "old" in _sessions(read_all.context.records)
        assembled = await eng.assemble(query, namespace="a", session_id="old")
        assert "old" in _sessions(assembled.records)
        # the decision is in the log: a rebuild replays the same passive set
        await eng.rebuild()
        assert _sessions(await eng.search(query, namespace="a", top_k=10)) == {"new"}
        # a second sleep never re-emits a session already passive (idempotent); by
        # now "new" has idled past the short test horizon too
        await eng.sleep()
        assert eng._storage is not None
        old = [
            e
            for e in await eng._storage.read_events()
            if e.kind is EventKind.SESSION and e.payload["session_id"] == "old"
        ]
        assert len(old) == 1
    finally:
        await eng.stop()


async def test_new_write_reopens_the_session(tmp_path: Path) -> None:
    eng = _engine(str(tmp_path / "s.db"))
    await eng.start()
    try:
        await _two_sessions(eng)
        await eng.sleep()
        await eng.write_messages(
            _turns("Ana changed the Lyon flight"), namespace="a", session_id="old", group_id="old"
        )
        hits = await eng.search("Ana Lyon", namespace="a", top_k=10)
        old = [r for r, _ in hits if r.source.message_id == "old"]
        assert len(old) == 3  # the two archived turns are back with the new one
        assert eng._storage is not None
        kinds = [
            (e.payload["state"], e.payload["reason"])
            for e in await eng._storage.read_events()
            if e.kind is EventKind.SESSION
        ]
        assert kinds == [("passive", "idle"), ("active", "new_write")]
    finally:
        await eng.stop()


async def test_lifecycle_off_by_default(tmp_path: Path) -> None:
    eng = _engine(str(tmp_path / "s.db"), passive_after=None)
    await eng.start()
    try:
        await _two_sessions(eng)
        stats = await eng.sleep()
        assert stats["session_lifecycle"]["status"] == "skipped"
        assert _sessions(await eng.search("Ana Lyon", namespace="a", top_k=10)) == {"old", "new"}
        assert eng._storage is not None
        events = await eng._storage.read_events()
        assert not [e for e in events if e.kind is EventKind.SESSION]
    finally:
        await eng.stop()
