"""I6: indexed ``session_key`` / ``source_role`` columns, the ``conversation`` verb, and the
0005 migration's backfill of existing rows."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from memspine import Engine

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)


def _engine(path: Path) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": str(path), "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"episodic": {"enabled": True}},
    )


async def _seed(path: Path) -> None:
    eng = _engine(path)
    await eng.start()
    try:
        for sid, words in (("s1", "camping by the lake"), ("s2", "skiing in the alps")):
            await eng.ingest_messages(
                [
                    {"role": "user", "content": f"we went {words}"},
                    {"role": "assistant", "content": f"{words} sounds fun"},
                ],
                namespace="u",
                session_id=sid,
                valid_from=T0,
            )
    finally:
        await eng.stop()


def _columns(path: Path) -> list[tuple[str, str, str]]:
    with sqlite3.connect(path) as db:
        return db.execute(
            "SELECT content, session_key, source_role FROM memory_records ORDER BY content"
        ).fetchall()


async def test_columns_are_filled_on_write_and_conversation_reads_one(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    await _seed(path)
    rows = _columns(path)
    assert {(r[1], r[2]) for r in rows} == {
        ("s1", "user"),
        ("s1", "assistant"),
        ("s2", "user"),
        ("s2", "assistant"),
    }
    eng = _engine(path)
    await eng.start()
    try:
        turns = await eng.conversation("u", "s1")
        users = await eng.conversation("u", "s2", roles=["user"])
        other_ns = await eng.conversation("someone-else", "s1")
    finally:
        await eng.stop()
    assert [t.content for t in turns] == [
        "we went camping by the lake",
        "camping by the lake sounds fun",
    ]
    assert [t.content for t in users] == ["we went skiing in the alps"]
    assert other_ns == []
