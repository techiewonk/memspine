"""I7 / I8: ``sessions=`` and ``roles=`` limit a read inside its namespace, in every
read mode, header and expansion."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from memspine import Engine

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)


async def _engine(**read: object) -> Engine:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"record_access": False, "recent_exchanges": 4, **read},
    )
    await eng.start()
    await eng.write_messages(
        [
            {"role": "user", "content": "trip one: we went camping by the lake"},
            {"role": "assistant", "content": "trip one: the lake sounds lovely"},
        ],
        namespace="u",
        session_id="s1",
        valid_from=T0,
    )
    await eng.write_messages(
        [
            {"role": "user", "content": "trip two: we went skiing in the alps"},
            {"role": "assistant", "content": "trip two: the alps sound cold"},
        ],
        namespace="u",
        session_id="s2",
        valid_from=T0,
    )
    return eng


def _text(records: list) -> str:
    return "\n".join(r.content for r in records)


@pytest.mark.parametrize("mode", ["replay", "retrieve", "compose", "full"])
async def test_sessions_filter_every_read_mode(mode: str) -> None:
    eng = await _engine()
    try:
        result = await eng.read(
            "where did we go on the trip?", namespace="u", mode=mode, sessions=["s1"]
        )
    finally:
        await eng.stop()
    text = _text(result.context.records)
    assert "trip one" in text
    assert "trip two" not in text


async def test_roles_filter_keeps_only_user_turns() -> None:
    eng = await _engine()
    try:
        hits = await eng.search("trip lake alps", namespace="u", roles=["user"], top_k=10)
        ctx = await eng.assemble("trip", namespace="u", roles=["user"], budget_tokens=600)
    finally:
        await eng.stop()
    assert hits and all(r.source.role == "user" for r, _ in hits)
    assert "sounds" not in _text(ctx.records) and "sound cold" not in _text(ctx.records)


async def test_sessions_and_roles_combine() -> None:
    eng = await _engine()
    try:
        hits = await eng.search(
            "trip", namespace="u", sessions=["s2"], roles=["assistant"], top_k=10
        )
    finally:
        await eng.stop()
    assert [r.content for r, _ in hits] == ["trip two: the alps sound cold"]


async def test_no_filter_reads_both_sessions() -> None:
    eng = await _engine()
    try:
        hits = await eng.search("trip", namespace="u", top_k=10)
    finally:
        await eng.stop()
    assert len(hits) == 4
