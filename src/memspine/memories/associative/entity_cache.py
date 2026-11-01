"""RM-110 / RM-130: the read path's entity-name lookup, cached over the durable name index.

The name index (RM-130, :mod:`~memspine.memories.associative.entity_index`) is a projection
kept in storage.records: ``match_key(name or alias) -> entity ids``, written only by the
``entity_index`` projector. Graph reads (``_graph_seeds``) look names up many times per query
(every n-gram), so this module keeps one namespace's name map in process, loaded with one
index query and dropped whenever the projector changes that namespace.

Other processes. Each cached entry remembers the projector's durable high-water mark
(``get_offset``) it covers, and every event seq this process applied is noted here. A lookup
reads the durable mark (one primary-key read); more events behind the mark than this process
applied since the entry was built means another process (or Engine) wrote to the store, and
the entry is reloaded.

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

#: The name index projector's name, the key of its durable high-water mark.
INDEX_PROJECTOR = "entity_index"


@dataclass
class _Entry:
    index: dict[str, list[str]]
    #: entity id -> its merge partners (both directions, transitive)
    groups: dict[str, list[str]] = field(default_factory=dict)
    #: the durable projector mark this entry covers (None: no durable mark available)
    baseline: int | None = None
    #: seqs this process applied to the store after ``baseline``
    own: set[int] = field(default_factory=set)

    def fresh(self, durable: int | None) -> bool:
        """No other process / Engine projected into the store since ``baseline``.

        The mark is shared and advance-only, so it is compared by count: every event
        in ``(baseline, durable]`` must be one this process applied (event seqs are
        dense on SQLite; a gap only causes a needless reload)."""
        if durable is None or self.baseline is None:
            return durable is None and self.baseline is None
        if durable < self.baseline:
            return False
        if durable - self.baseline > sum(1 for seq in self.own if seq <= durable):
            return False
        self.baseline = durable
        self.own = {seq for seq in self.own if seq > durable}
        return True


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
    """The index projector of this process applied the event ``seq``."""
    if seq is None:
        return
    for entry in _state(storage).entries.values():
        if entry.baseline is not None and seq > entry.baseline:
            entry.own.add(int(seq))


def invalidate(storage: Any, namespace: str | None = None) -> None:
    """Drop the cached ``namespace`` (every namespace when None) of ``storage``."""
    state = _state(storage)
    state.version += 1
    if namespace is None:
        state.entries.clear()
    else:
        state.entries.pop(namespace, None)


async def _durable_mark(storage: Any) -> int | None:
    get_offset = getattr(storage, "get_offset", None)
    if not callable(get_offset):
        return None
    try:
        return int(await get_offset(INDEX_PROJECTOR))
    except Exception:  # no offsets table / fake storage: in-process tracking only
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
    durable = await _durable_mark(storage)
    entry = state.entries.get(namespace)
    if entry is not None and not entry.fresh(durable):
        entry = None  # another process / Engine moved the projection: reload
    if entry is None:
        version = state.version
        index, groups = await _load(storage, namespace)
        entry = _Entry(index, groups, baseline=durable)
        if state.version == version:  # no change during the load
            state.entries[namespace] = entry
    return EntityLookup(entry)
