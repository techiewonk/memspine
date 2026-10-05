"""Projector contract: idempotent event appliers with durable high-water marks.

Every derived store (vector/graph/FTS/cache/relational read models) implements
:class:`Projector`. The engine replays events past each projector's high-water
mark at startup (catch-up, §4 step 6) and can rebuild any projector from seq 0 —
unless the event log runs in a reduced-retention mode (D-45), in which case
rebuild raises :class:`~memspine.exceptions.RebuildUnavailableError`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from memspine.core.events import MemoryEvent

__all__ = ["Projector"]


class Projector(ABC):
    """Idempotent apply(event) + reset() for rebuilds.

    Idempotency contract: applying the same event twice must leave the projection
    in the same state — catch-up may re-deliver the event at the high-water mark.
    """

    #: Stable name used as the key for the durable high-water mark.
    name: str

    @abstractmethod
    async def apply(self, event: MemoryEvent) -> None:
        """Apply one event. Must be idempotent."""

    @abstractmethod
    async def reset(self) -> None:
        """Drop all projected state so a rebuild can replay from seq 0."""

    def begin_batch(self) -> None:
        """A run of applies starts that the engine closes with :meth:`flush`.

        A projector may then buffer work that is costly per event (an index
        commit), provided every read it serves still sees each applied event.
        The engine holds this projector's high-water mark back until
        :meth:`flush` returns, so a crash before it only causes a replay.
        Default: nothing is buffered."""
        return None

    async def flush(self) -> None:
        """Make every apply since :meth:`begin_batch` durable and end the batch.
        Default: applies are durable when they return."""
        return None
