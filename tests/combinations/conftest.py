"""C6 combination-matrix helpers (structure plan §6).

Every boot in this suite runs on an in-memory event log (``:memory:``) with the
deterministic ``hash`` embedder, so the whole matrix passes with zero optional
extras installed. Tests that genuinely need an extra skip-with-reason.

``make_engine`` is exposed as a fixture (not a cross-module import) because the
``tests`` tree is not an importable package — pytest discovers this conftest by
directory, so every module under ``tests/combinations`` gets the factory for
free.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from memspine import Engine

EngineFactory = Callable[..., Engine]


@pytest.fixture
def make_engine() -> EngineFactory:
    """Factory for an Engine wired for a hermetic boot: in-memory log + hash
    embedder. Extra kwargs (template, memories, workers, graph, …) layer on."""

    def _factory(template: str | None = None, **overrides: Any) -> Engine:
        overrides.setdefault("storage", {"path": ":memory:"})
        overrides.setdefault("embedding", {"provider": "hash"})
        return Engine(template=template, dotenv_path=None, **overrides)

    return _factory
