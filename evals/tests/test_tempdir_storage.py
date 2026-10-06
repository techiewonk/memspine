"""``storage.path: "tempdir"`` gives each item a fresh file-backed store, removed on close."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("memspine")

from memspine_evals.contracts import Turn
from memspine_evals.systems.memspine_system import TEMPDIR_STORAGE, MemspineSystem


def test_tempdir_storage_is_per_item_and_removed() -> None:
    system = MemspineSystem(
        config={"storage": {"path": TEMPDIR_STORAGE}, "embedding": {"provider": "hash"}},
        template="core",
    )

    async def first_item() -> str | None:
        await system.reset("a")
        await system.insert(Turn(turn_id="t1", session_id="s1", speaker="u", text="hello"))
        return system._tempdir

    async def second_item() -> str | None:
        await system.reset("b")
        return system._tempdir

    first = asyncio.run(first_item())
    assert first is not None and Path(first).is_dir()
    second = asyncio.run(second_item())
    assert second is not None and second != first
    assert not Path(first).exists()  # reset removed the first item's store
    asyncio.run(system.close())
    assert not Path(second).exists()
