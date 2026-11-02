"""Caller-scoped commit holds for projection stores (RM-72, RM-160).

A projection store whose per-write commit is costly (a Tantivy commit, a LanceDB
table version) may hold its writes while one caller runs a projection batch
(:meth:`~memspine.store.projections.projector.Projector.begin_batch`) and write
them together when the caller flushes. The hold belongs to the calling context,
never to the store handle, because several Engines may share one handle (ADR-079).

Each :meth:`CommitDeferral.hold` pushes a hold object onto the calling context, and
:meth:`CommitDeferral.release` pops it and closes it. A task started inside the batch
copies the context and so sees the same hold object. Once the batch releases it, the
hold is closed for that task too, so a long-lived task spawned during the batch (a
background drain) commits its own writes again instead of holding them past the
batch, when nothing would flush them before its checkpoint advanced.
"""

from __future__ import annotations

import contextvars
import itertools
from collections.abc import Mapping
from types import MappingProxyType

__all__ = ["CommitDeferral"]


class _Hold:
    __slots__ = ("open",)

    def __init__(self) -> None:
        self.open = True


#: The holds of the calling context, per deferral token, innermost last.
_HOLDS: contextvars.ContextVar[Mapping[int, tuple[_Hold, ...]]] = contextvars.ContextVar(
    "memspine_commit_holds", default=MappingProxyType({})
)
_TOKENS = itertools.count(1)


class CommitDeferral:
    """The commit holds one store (or a family of stores sharing it) honours."""

    def __init__(self) -> None:
        self._token = next(_TOKENS)

    def hold(self) -> None:
        """Start holding commits in the calling context (nestable)."""
        held = dict(_HOLDS.get())
        held[self._token] = (*held.get(self._token, ()), _Hold())
        _HOLDS.set(MappingProxyType(held))

    def release(self) -> None:
        """End the calling context's innermost hold (no-op without one)."""
        held = dict(_HOLDS.get())
        holds = held.pop(self._token, ())
        if not holds:
            return
        holds[-1].open = False
        if len(holds) > 1:
            held[self._token] = holds[:-1]
        _HOLDS.set(MappingProxyType(held))

    def active(self) -> bool:
        """True while the calling context holds an open hold."""
        return any(hold.open for hold in _HOLDS.get().get(self._token, ()))
