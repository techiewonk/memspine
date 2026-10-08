"""I6: indexed ``session_key`` / ``source_role`` columns, the ``conversation`` verb, and the
0005 migration's backfill of existing rows."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from alembic import command

from memspine import Engine
from memspine.services.storage.sqlite.migrations import alembic_config, upgrade_to_head

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)


def _engine(path: Path) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": str(path)},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
    )


async def _seed(path: Path) -> None:
    eng = _engine(path)
    await eng.start()
    try:
        for sid, words in (("s1", "camping by the lake"), ("s2", "skiing in the alps")):
            await eng.write_messages(
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


async def test_migration_backfills_existing_rows(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    await _seed(path)
    command.downgrade(alembic_config(path), "0004")  # drops the two columns
    with sqlite3.connect(path) as db:
        cols = {row[1] for row in db.execute("PRAGMA table_info(memory_records)")}
    assert "session_key" not in cols
    upgrade_to_head(path)
    assert all(r[1] in ("s1", "s2") and r[2] in ("user", "assistant") for r in _columns(path))
