"""GTIME: keep derived lead blocks and graph machinery away from "when" questions.

``read.graph_time_guard`` (``docs/v2/GTIME.md``): see the config key. The decision is a pure rule
on the question text, so a read and its cache key stay deterministic.
"""

from __future__ import annotations

from typing import Any

from memspine.models.encoders.query.shape import is_temporal, is_time_shaped
from memspine.observability import tap

__all__ = ["blocks_guarded", "guard_mode", "legs_guarded"]


def guard_mode(read: Any, query: str) -> str:
    """``off``, ``blocks`` or ``blocks+legs``: what the guard does to THIS question."""
    mode = read.graph_time_guard
    if mode == "off":
        return "off"
    wide = read.graph_time_guard_shape == "time_shaped"
    shaped = is_time_shaped(query) if wide else is_temporal(query)
    return mode if shaped else "off"


def blocks_guarded(read: Any, query: str) -> bool:
    """True when the entity-summary and graph-facts header blocks are left out of this read."""
    on = guard_mode(read, query) != "off"
    if on and tap.enabled():
        tap.event("read.graph_time_guard", blocks=True, legs=legs_guarded(read, query))
    return on


def legs_guarded(read: Any, query: str) -> bool:
    """True when the graph leg, the graph node search and the derived lift are skipped."""
    return guard_mode(read, query) == "blocks+legs"
