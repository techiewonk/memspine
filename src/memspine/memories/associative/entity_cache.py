"""RM-110 / RM-130: the read path's entity-name lookup, cached over the durable name index.

The name index (RM-130, :mod:`~memspine.memories.associative.entity_index`) is a projection
kept in storage.records: ``match_key(name or alias) -> entity ids``, written only by the
``entity_index`` projector. Graph reads (``_graph_seeds``) look names up many times per query
(every n-gram), so this module keeps one namespace's name map in process, loaded with one
index query and dropped whenever the projector changes that namespace.

Other processes. Each cached entry remembers the durable reset epoch and namespace mutation
revision. A peer's projection or direct purge changes that revision even while the conservative
global replay offset is held back by an unrelated namespace.

Order: each key's ids are sorted (uuid7 ids sort by creation time). Merge groups are not
folded into a key; :meth:`EntityLookup.expand` adds the merged partners of found ids.

Concurrency: a build snapshots the namespace's version before its query and installs the
result only when no change was noted meanwhile.
"""

from __future__ import annotations

import weakref
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "EntityLookup",
    "entity_lookup",
    "invalidate",
    "note_applied",
]


@dataclass
class _Entry:
    index: dict[str, list[str]]
    #: entity id -> its merge partners (both directions, transitive)
    groups: dict[str, list[str]] = field(default_factory=dict)
    #: Durable ``(reset_epoch, namespace_mutation_revision)`` this entry covers.
    baseline: tuple[int, int] | None = None

    def fresh(self, durable: tuple[int, int] | None) -> bool:
        return self.baseline == durable


@dataclass
class _StoreState:
    entries: dict[str, _Entry] = field(default_factory=dict)
    #: bumped by every change (a build installs only when it did not move)
    version: int = 0


_STATES: weakref.WeakKeyDictionary[Any, _StoreState] = weakref.WeakKeyDictionary()
_FALLBACK: dict[int, tuple[Any, _StoreState]] = {}


def _state(storage: Any) -> _StoreState:
    try:
        state = _STATES.get(storage)
        if state is None:
            state = _STATES[storage] = _StoreState()
        return state
    except TypeError:  # not weak-referenceable: keep it alive with its state
        held = _FALLBACK.get(id(storage))
        if held is None or held[0] is not storage:
            held = _FALLBACK[id(storage)] = (storage, _StoreState())
        return held[1]


def note_applied(storage: Any, seq: int | None) -> None:
    """Compatibility hook; ordered projection invalidates after its atomic commit."""


def invalidate(storage: Any, namespace: str | None = None) -> None:
    """Drop the cached ``namespace`` (every namespace when None) of ``storage``."""
    state = _state(storage)
    state.version += 1
    if namespace is None:
        state.entries.clear()
    else:
        state.entries.pop(namespace, None)


async def _durable_mark(storage: Any, namespace: str) -> tuple[int, int] | None:
    revision = getattr(storage, "entity_index_revision", None)
    if not callable(revision):
        return None
    try:
        epoch, changed = await revision(namespace)
        return int(epoch), int(changed)
    except Exception:  # no projection metadata / fake storage: local invalidation only
        return None


class EntityLookup:
    """One read's view of a namespace's name index (see :func:`entity_lookup`)."""

    def __init__(self, entry: _Entry) -> None:
        self._entry = entry

    async def get(self, key: str) -> list[str]:
        """The entity ids named ``key`` (a match key), sorted."""
        return list(self._entry.index.get(key, ()))

    def expand(self, nodes: list[str]) -> list[str]:
        """``nodes`` plus their merge partners (after them, in order)."""
        out = list(dict.fromkeys(nodes))
        for node in list(out):
            for partner in self._entry.groups.get(node, ()):
                if partner not in out:
                    out.append(partner)
        return out

    async def nonempty(self) -> bool:
        return any(self._entry.index.values())

    async def as_dict(self) -> dict[str, list[str]]:
        return {k: list(v) for k, v in self._entry.index.items() if v}


async def _load(storage: Any, namespace: str) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    from memspine.services.storage.entity_index_tables import entity_index_store_of

    store = entity_index_store_of(storage)
    index: dict[str, list[str]] = {}
    for row in await store.select(namespace, "name"):
        nodes = index.setdefault(row.match, [])
        if row.entity_id not in nodes:
            nodes.append(row.entity_id)
    adjacency: dict[str, set[str]] = {}
    for row in await store.select(namespace, "merge"):
        adjacency.setdefault(row.entity_id, set()).add(row.ref)
        adjacency.setdefault(row.ref, set()).add(row.entity_id)
    groups: dict[str, list[str]] = {}
    for start in adjacency:
        seen, frontier = {start}, [start]
        while frontier:
            nxt = [n for f in frontier for n in adjacency.get(f, ()) if n not in seen]
            seen.update(nxt)
            frontier = nxt
        groups[start] = sorted(seen - {start})
    return {k: sorted(v) for k, v in index.items()}, groups


async def entity_lookup(storage: Any, namespace: str) -> EntityLookup:
    """The name index of ``namespace`` from the cache when it is fresh, else loaded once and
    cached. Costs one durable-mark read when fresh."""
    state = _state(storage)
    durable = await _durable_mark(storage, namespace)
    entry = state.entries.get(namespace)
    if entry is not None and not entry.fresh(durable):
        entry = None  # another process / Engine moved the projection: reload
    if entry is None:
        version = state.version
        index, groups = await _load(storage, namespace)
        observed = await _durable_mark(storage, namespace)
        entry = _Entry(index, groups, baseline=observed)
        if state.version == version and observed == durable:  # stable across the load
            state.entries[namespace] = entry
    return EntityLookup(entry)
