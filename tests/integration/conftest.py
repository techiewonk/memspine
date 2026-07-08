"""Integration-suite helpers: FILE-BACKED engines on ``tmp_path``.

Unlike ``tests/combinations`` (every boot on a throwaway ``:memory:`` log),
integration tests prove the event-sourced core across *process-like lifecycle
boundaries*: build → write → ``stop()`` → construct a NEW engine on the SAME db
file → ``start()`` → the projectors catch up from the persisted log + offsets.
That is exactly what a ``:memory:`` fixture cannot exercise, so these tests are
deliberately file-backed and slower.

Discovery works the same way as ``tests/combinations``: no ``__init__.py`` — the
``tests`` tree is not an importable package, so pytest hands this conftest to
every module in the directory by location and each gets the factory for free.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from memspine import Engine

FileEngineFactory = Callable[..., Engine]


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """One stable on-disk database path per test — reopening the SAME path is
    the whole point (persistence + replay across a construct/teardown cycle)."""
    return str(tmp_path / "memspine.db")


@pytest.fixture
def make_file_engine(db_path: str) -> FileEngineFactory:
    """Factory for a FILE-BACKED engine (deterministic ``hash`` embedder).

    Every call defaults ``storage.path`` to the per-test ``db_path``, so calling
    the factory twice constructs two engines over the *same* file — the reopen
    scenario. Pass an explicit ``storage=`` to point at a different file (the
    runner-parity test isolates each runner on its own db)."""

    def _factory(**overrides: Any) -> Engine:
        overrides.setdefault("storage", {"path": db_path})
        overrides.setdefault("embedding", {"provider": "hash"})
        return Engine(dotenv_path=None, **overrides)

    return _factory
