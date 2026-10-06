"""``storage.path: "tempdir"`` gives each item a fresh file-backed store, removed on close."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("memspine")

from memspine_evals.contracts import Turn
from memspine_evals.systems.memspine_system import TEMPDIR_STORAGE, MemspineSystem


def test_tempdir_storage_is_per_item_and_removed() -> None:
    async def run() -> tuple[str, str, bool]:
        system = MemspineSystem(
            config={"storage": {"path": TEMPDIR_STORAGE}, "embedding": {"provider": "hash"}},
            template="core",
        )
        await system.reset("a")
        first = system._tempdir
        assert first is not None and Path(first).is_dir()
        await system.insert(Turn(turn_id="t1", session_id="s1", speaker="u", text="hello"))
        await system.reset("b")
        second = system._tempdir
        await system.close()
        return first, second, Path(first).exists() or Path(second).exists()

    first, second, left = asyncio.run(run())
    assert first != second
    assert not left
