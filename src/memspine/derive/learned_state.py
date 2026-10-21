"""FU-GAPS (``write.learned_persist``): the learned registries as MARKER events.

Two registries grow while windows are written and used to vanish on restart: the W2-06a open kind
set (:class:`~memspine.derive.envelope_compiler.KindRegistry`, kinds the model coined up to the cap)
and the W2-13 learned pre-pass aliases (:class:`~memspine.derive.prepass.Prepass`). This module logs
what each learned as ``memory.marker`` events (``learned.kind`` / ``learned.alias``, the same door
the W2-09 entity registry uses) and folds them back in once per process, before the first window.

Scope: kinds are extraction vocabulary and stay engine-wide. Aliases are namespace data (RM-18): a
``learned.alias`` event carries the namespace that learned the names, is restored into that
namespace only, and a ``learned.alias.reset`` event (written by namespace erasure) drops what the
namespace learned before it, so a replay of the log equals the live per-namespace state.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from memspine.derive.envelope_compiler import KindRegistry
from memspine.derive.prepass import Prepass
from memspine.observability import tap
from memspine.store.events import EventKind, MemoryEvent

__all__ = [
    "LEARNED_ALIAS_MARKER",
    "LEARNED_ALIAS_RESET_MARKER",
    "LEARNED_KIND_MARKER",
    "LearnedState",
    "alias_reset_event",
]

LEARNED_KIND_MARKER = "learned.kind"
LEARNED_ALIAS_MARKER = "learned.alias"
LEARNED_ALIAS_RESET_MARKER = "learned.alias.reset"
_BATCH = 1000


def alias_reset_event(namespace: str) -> MemoryEvent:
    """The marker that forgets every alias ``namespace`` learned before it (namespace erasure)."""
    return MemoryEvent(
        kind=EventKind.MARKER,
        namespace=namespace,
        actor="system",
        payload={"marker": LEARNED_ALIAS_RESET_MARKER},
    )


class LearnedState:
    """Load the logged registries once, then flush what each learned since the last flush."""

    def __init__(
        self,
        storage: Callable[[], Any] | None,
        *,
        kinds: KindRegistry | None = None,
        prepass: Prepass | None = None,
    ) -> None:
        self._storage = storage
        self._kinds = kinds
        self._prepass = prepass
        self._loaded = storage is None
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        """Fold the logged markers into the registries, once per process."""
        async with self._lock:
            if self._loaded:
                return
            storage = self._storage() if self._storage else None
            if storage is None:
                return  # not started yet: try again on the next call
            kinds: list[str] = []
            aliases: dict[str, list[str]] = {}
            after = 0
            while True:
                events = await storage.read_events(after_seq=after, limit=_BATCH)
                if not events:
                    break
                for ev in events:
                    if ev.kind is not EventKind.MARKER:
                        continue
                    marker = ev.payload.get("marker")
                    if marker == LEARNED_KIND_MARKER:
                        kinds.extend(str(k) for k in ev.payload.get("kinds", []))
                    elif marker == LEARNED_ALIAS_MARKER:
                        names = ev.payload.get("names", [])
                        aliases.setdefault(ev.namespace, []).extend(str(a) for a in names)
                    elif marker == LEARNED_ALIAS_RESET_MARKER:
                        aliases.pop(ev.namespace, None)
                after = max(e.seq for e in events if e.seq is not None)
            added_k = self._kinds.restore(kinds) if self._kinds is not None else 0
            added_a = 0
            if self._prepass is not None:
                for ns, names in aliases.items():
                    added_a += self._prepass.restore(names, namespace=ns)
            self._loaded = True
            tap.event(
                "write.learned.loaded", kinds=added_k, aliases=added_a, namespaces=len(aliases)
            )

    async def flush(self, namespace: str) -> int:
        """Append what was learned since the last flush (kinds, and ``namespace``'s own aliases);
        returns the number of events."""
        storage = self._storage() if self._storage else None
        if storage is None:
            return 0
        sent = 0
        batches = (
            (LEARNED_KIND_MARKER, "kinds", self._kinds.drain_new() if self._kinds else []),
            (
                LEARNED_ALIAS_MARKER,
                "names",
                self._prepass.drain_new(namespace) if self._prepass else [],
            ),
        )
        for marker, key, items in batches:
            if not items:
                continue
            await storage.append_event(
                MemoryEvent(
                    kind=EventKind.MARKER,
                    namespace=namespace,
                    actor="system",
                    payload={"marker": marker, key: items},
                )
            )
            sent += 1
        if sent:
            tap.event("write.learned.flushed", events=sent)
        return sent
