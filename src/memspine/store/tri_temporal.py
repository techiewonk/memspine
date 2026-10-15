# ruff: noqa: E501
"""Tri-temporal stamping (F-02b; M-10, schema session 6): when it was said, and how well the event is dated.

Pure functions over primitives (no store access), shared by the write door, the Alembic backfill and
the tests. Rule S6-2: never invent a date. A field stays ``None`` unless something known sets it:

* a turn written with a timestamp: ``source_at`` = that timestamp (precision ``instant``), and its event
  interval starts there, basis ``explicit``;
* a derived record: ``source_at`` is its earliest source parent's, precision likewise;
* a window episode, or a window fact that carries the first window turn's date, took the *window start*
  as its event time: basis ``inferred`` at the window's precision (``day``), so the "undated fact takes
  the window start" behaviour is visible as data instead of passing as an exact date.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

__all__ = [
    "WINDOW_FACT_TAG",
    "WINDOW_PRECISION",
    "derived_times",
    "turn_times",
]

#: Precision of a window's start: the date of its first turn, rendered as a day.
WINDOW_PRECISION = "day"
#: Tag of a fact the window extractor deposited (``workers.windows``).
WINDOW_FACT_TAG = "window_fact"
_WINDOW_EPISODE_CHANNEL = "window_episode"  # constants.WINDOW_EPISODE_CHANNEL


def turn_times(timestamp: datetime | None) -> dict[str, Any]:
    """Fields for a turn: its timestamp as the source time and the start of its event interval.

    ``None`` (the source gave no timestamp; ``valid_from`` is the write clock) sets nothing."""
    if timestamp is None:
        return {}
    return {
        "source_at": timestamp,
        "source_precision": "instant",
        "event_precision": "instant",
        "event_basis": "explicit",
    }


def _earliest(parents: Iterable[Any]) -> Any | None:
    dated = [p for p in parents if getattr(p, "source_at", None) is not None]
    if not dated:
        return None
    return min(dated, key=lambda p: (_naive_key(p.source_at), p.record_id))


def _naive_key(moment: datetime) -> tuple[float, int]:
    return (moment.replace(tzinfo=None).timestamp(), 0)


def derived_times(
    *,
    channel: str,
    tags: Sequence[str],
    valid_from: datetime,
    parents: Sequence[Any],
) -> dict[str, Any]:
    """Fields for a record derived from ``parents`` (records with ``source_at``/``valid_from``/``record_id``).

    Returns only what is known; empty when no parent is dated and the record is not window-derived."""
    out: dict[str, Any] = {}
    first = _earliest(parents)
    if first is not None:
        out["source_at"] = first.source_at
        if first.source_precision is not None:
            out["source_precision"] = first.source_precision
    windowed = channel == _WINDOW_EPISODE_CHANNEL or WINDOW_FACT_TAG in tags
    if windowed and parents:
        start = min(parents, key=lambda p: _naive_key(p.valid_from)).valid_from
        if channel == _WINDOW_EPISODE_CHANNEL or _same_instant(valid_from, start):
            out["event_precision"] = WINDOW_PRECISION
            out["event_basis"] = "inferred"
        else:  # a date the extractor read out of the text
            out["event_precision"] = WINDOW_PRECISION
            out["event_basis"] = "explicit"
    return out


def _same_instant(a: datetime, b: datetime) -> bool:
    return a.replace(tzinfo=None) == b.replace(tzinfo=None)
