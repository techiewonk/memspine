"""Opt-in trace sinks for evals and debugging (zero cost when no sink is installed).

Two ``ContextVar`` sinks, both ``None`` by default so every hook is a single ``is None`` test:

- :data:`FORENSICS` is the read-side dict that ``memspine.engine.search_forensics()`` installs.
  Besides the ranked lists of each retrieval stage, hooks add ``cuts`` (one entry per candidate
  dropped, with the reason) and ``window`` (the replay-window neighbours added around a hit).
- :data:`WRITE` is a list that :func:`write_forensics` installs around a write: the firewall's
  signals and verdict for each record, and the conflict ladder's verdict (rule, incumbent) for
  each keyed write. Events carry the engine's ``record_id`` so a harness can join them to the
  record it stored.

Nothing here changes behaviour: hooks only append to a sink that a caller asked for.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

#: read side: the dict ``search_forensics()`` fills (see ``memspine.engine``)
FORENSICS: ContextVar[dict[str, Any] | None] = ContextVar("memspine_forensics", default=None)

#: write side: events appended by the firewall and the conflict ladder
WRITE: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "memspine_write_forensics", default=None
)


@contextmanager
def write_forensics() -> Iterator[list[dict[str, Any]]]:
    """Capture the write-side events (firewall signals + verdict, conflict-ladder verdict) of
    every write made inside the block, in order."""
    sink: list[dict[str, Any]] = []
    token = WRITE.set(sink)
    try:
        yield sink
    finally:
        WRITE.reset(token)


def write_active() -> bool:
    return WRITE.get() is not None


def write_note(kind: str, **data: Any) -> None:
    """Append one write-side event; a no-op unless :func:`write_forensics` is active."""
    sink = WRITE.get()
    if sink is not None:
        sink.append({"kind": kind, **data})


def cut(reason: str, record_id: str, **detail: Any) -> None:
    """Record that a read dropped ``record_id`` and why; a no-op without a forensics sink."""
    fx = FORENSICS.get()
    if fx is not None:
        fx.setdefault("cuts", []).append({"id": record_id, "reason": reason, **detail})
