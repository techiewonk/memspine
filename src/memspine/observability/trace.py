"""Read-trace capture for harnesses and tests (moved out of the engine facade, F-13a)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from memspine.read.pipeline.state import _FORENSICS

__all__ = ["search_forensics"]


@contextmanager
def search_forensics() -> Iterator[dict[str, Any]]:
    """Capture the stage-by-stage ranking of every ``search`` inside the block.

    The yielded dict is filled by the last ``_search`` that runs: ``vector``, ``lexical``,
    ``extra_legs`` (``[(name, hits)]``), ``fused``, ``pool``, ``reranker`` (id or None),
    ``rerank_scores`` and ``final``, each a list of ``(record_id, score)``.
    """
    sink: dict[str, Any] = {}
    token = _FORENSICS.set(sink)
    try:
        yield sink
    finally:
        _FORENSICS.reset(token)
