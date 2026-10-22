"""RM-110: the read path's entity-name index, maintained instead of rescanned.

``AssociativeMemory.entity_index`` builds ``match_key(name) -> entity node ids`` from
every edge of a namespace (``edge_list``). The read path needed that on every graph
read (``_graph_seeds``), which cost 0.9-3.8 s p95 at 100k edges. This module keeps the
index per (graph store, namespace) and maintains it from the one writer of ``mentions``
edges, :class:`~memspine.memories.associative.projector.GraphProjector`:

* a live mention projected (WRITE, or a LINK of rel ``mentions``) adds the entity to
  the cached index in place (appended, as a new edge row is appended to the scan);
* a mention tombstoned or deleted (a re-projected WRITE's stale names, FORGET, an
  orphan entity dropped, a ``mentions`` LINK of weight 0) marks the entity *suspect*:
  the next lookup that would return it re-checks it with one capped one-hop query on
  that entity alone and drops it when no live mention is left;
* ``reset``/``clear`` and an erasure purge (``trust.pipeline.erasure_v2``, which deletes
  record nodes directly) drop the store's entries, so the next read rebuilds from one
  scan, exactly as before.

Other processes. Each cached entry remembers the projector's durable high-water mark
(``get_offset``) it covers, and every event seq this process applies is noted here. A
lookup reads the durable mark (one primary-key read); more events behind the mark than
this process applied since the entry was built means another process (or Engine) wrote
to the store, and the entry is rebuilt from a scan. Like the RM-77 read cache, a purge
another process runs after its last event is not seen until that process's next event
(documented residual).

The index content matches the scan whenever the cache is fresh: same keys, same nodes.
Order: each key's nodes are kept sorted by node id. A key holds more than one node only
when canonical names differ just in punctuation ("o'brien", "o-brien"); the scan listed
those in the store's row order (on SQLite the index order of the mentioning record's
random id, on other stores their internal order), so that order was arbitrary and is
now deterministic. Seed lists are otherwise identical to the scan's.

Concurrency: a build snapshots the store's version before its scan and installs the
result only when no hook ran meanwhile (the scan may have missed that change).
"""

from __future__ import annotations

import bisect
import weakref
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from memspine.memories.associative.entities import MENTIONS_REL, match_key, parse_entity_node

__all__ = [
    "EntityLookup",
    "entity_lookup",
    "invalidate",
    "note_applied",
    "note_mentioned",
    "note_unmentioned",
]

#: The graph projector's name, the key of its durable high-water mark.
GRAPH_PROJECTOR = "graph"


@dataclass
class _Entry:
    index: dict[str, list[str]]
    #: entity nodes a removal touched since they were indexed (re-checked on lookup)
    suspects: set[str] = field(default_factory=set)
    #: the durable projector mark this entry covers (None: no durable mark available)
    baseline: int | None = None
    #: seqs this process applied to the store after ``baseline``
    own: set[int] = field(default_factory=set)

    def fresh(self, durable: int | None) -> bool:
        """No other process / Engine projected into the store since ``baseline``.

        The mark is shared and advance-only, so it is compared by count: every event
        in ``(baseline, durable]`` must be one this process applied (event seqs are
        dense on SQLite; a gap only causes a needless rebuild)."""
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
    #: bumped by every hook (a build installs only when it did not move)
    version: int = 0


_STATES: weakref.WeakKeyDictionary[Any, _StoreState] = weakref.WeakKeyDictionary()
_FALLBACK: dict[int, tuple[Any, _StoreState]] = {}


def _state(store: Any) -> _StoreState:
    try:
        state = _STATES.get(store)
        if state is None:
            state = _STATES[store] = _StoreState()
        return state
    except TypeError:  # not weak-referenceable: keep it alive with its state
        held = _FALLBACK.get(id(store))
        if held is None or held[0] is not store:
            held = _FALLBACK[id(store)] = (store, _StoreState())
        return held[1]


def note_applied(store: Any, seq: int | None) -> None:
    """A projector of this process applied the event ``seq`` to ``store``."""
    if seq is None:
        return
    for entry in _state(store).entries.values():
        if entry.baseline is not None and seq > entry.baseline:
            entry.own.add(int(seq))


def note_mentioned(store: Any, node_id: str) -> None:
    """A live ``mentions`` edge to entity ``node_id`` was projected into ``store``."""
    parsed = parse_entity_node(node_id)
    state = _state(store)
    state.version += 1
    if parsed is None:
        return
    entry = state.entries.get(parsed[0])
    if entry is None:
        return
    nodes = entry.index.setdefault(match_key(parsed[1]), [])
    if node_id not in nodes:
        bisect.insort(nodes, node_id)
    entry.suspects.discard(node_id)


def note_unmentioned(store: Any, node_ids: Iterable[str]) -> None:
    """``mentions`` edges to these entity nodes were tombstoned or deleted."""
    state = _state(store)
    state.version += 1
    for node_id in node_ids:
        parsed = parse_entity_node(node_id)
        entry = state.entries.get(parsed[0]) if parsed is not None else None
        if entry is not None:
            entry.suspects.add(node_id)


def invalidate(store: Any) -> None:
    """Drop every cached namespace of ``store`` (rebuild, clear, direct purge)."""
    state = _state(store)
    state.version += 1
    state.entries.clear()


async def _durable_mark(storage: Any) -> int | None:
    get_offset = getattr(storage, "get_offset", None)
    if not callable(get_offset):
        return None
    try:
        return int(await get_offset(GRAPH_PROJECTOR))
    except Exception:  # no offsets table / fake storage: in-process tracking only
        return None


async def _still_mentioned(graph: Any, namespace: str, node_id: str) -> bool:
    """Does a live ``mentions`` edge of ``namespace`` still point at ``node_id``? One
    capped one-hop query on the entity alone (``max_degree=1``)."""
    found = await graph.neighbors(node_id, MENTIONS_REL, 1, namespace=namespace, max_degree=1)
    return bool(found)


class EntityLookup:
    """One read's view of a namespace's entity index (see :func:`entity_lookup`)."""

    def __init__(self, entry: _Entry, graph: Any, namespace: str) -> None:
        self._entry = entry
        self._graph = graph
        self._namespace = namespace

    async def get(self, key: str) -> list[str]:
        """The entity nodes of ``key`` with a live mention, in index order; a suspect is
        re-checked first and dropped from the cache when its last mention is gone."""
        entry = self._entry
        nodes = entry.index.get(key)
        if not nodes:
            return []
        for node in [n for n in nodes if n in entry.suspects]:
            entry.suspects.discard(node)
            if not await _still_mentioned(self._graph, self._namespace, node):
                nodes.remove(node)
        if not nodes:
            entry.index.pop(key, None)
        return list(nodes)

    async def nonempty(self) -> bool:
        """Does any entity of the namespace have a live mention (``entity_index`` is
        non-empty)? Re-checks suspects only when nothing else is indexed."""
        entry = self._entry
        if any(n not in entry.suspects for nodes in entry.index.values() for n in nodes):
            return True
        for key in list(entry.index):
            if await self.get(key):
                return True
        return False

    async def as_dict(self) -> dict[str, list[str]]:
        """The whole index (every suspect re-checked): ``entity_index`` with each key's nodes
        sorted by node id."""
        out: dict[str, list[str]] = {}
        for key in list(self._entry.index):
            nodes = await self.get(key)
            if nodes:
                out[key] = nodes
        return out


async def entity_lookup(
    graph: Any,
    storage: Any,
    namespace: str,
    build: Callable[[], Awaitable[dict[str, list[str]]]],
) -> EntityLookup:
    """The entity index of ``namespace`` (``build`` is the full scan) from the cache when it
    is fresh, else rebuilt once and cached. Costs one durable-mark read when fresh."""
    state = _state(graph)
    durable = await _durable_mark(storage)
    entry = state.entries.get(namespace)
    if entry is not None and not entry.fresh(durable):
        entry = None  # another process / Engine moved the projection: rebuild
    if entry is None:
        version = state.version
        index = await build()
        entry = _Entry({k: sorted(v) for k, v in index.items()}, baseline=durable)
        if state.version == version:  # no hook ran during the scan
            state.entries[namespace] = entry
    return EntityLookup(entry, graph, namespace)
