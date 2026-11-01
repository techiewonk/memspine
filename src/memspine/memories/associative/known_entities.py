"""DS-11 (ADR-090 item 6): the window prompt's KNOWN entities, read from the RM-130 entity index.

Every entry carries its provenance (:class:`~memspine.derive.prompt_context.KnownEntity`): the
index entry (``entity_id``), its ``version`` (the index sequence of its latest name or alias row),
``known_seq`` (the sequence at which its canonical name became known) and its visibility (the
namespace that owns the index rows). Aliases are the index's evidence-cited aliases only, so an
alias whose evidence was forgotten or erased is gone from the index and from KNOWN with it.

KNOWN names are prompt hints: they never key a record, link a mention or assert anything.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from memspine.config import constants
from memspine.derive.prompt_context import KnownEntity, KnownSet, KnownVisibility
from memspine.memories.associative.entity_index import (
    GONE_KIND,
    EntityIndexProjector,
    index_watermark,
)
from memspine.services.storage.entity_index_tables import (
    EntityIndexRow,
    EntityIndexStore,
    RowCursor,
    entity_index_store_of,
    row_cursor,
)

__all__ = ["KNOWN_SOURCE", "KnownEntityCursor", "index_known_entities"]

#: The ``KnownEntity.source`` of entries read from the entity index.
KNOWN_SOURCE = "entity_index"


@dataclass
class _Entry:
    """One entity's name rows as the index holds them."""

    entity_id: str
    #: ``ref -> (text, seq)``: ``name`` is the canonical name, ``alias:<id>`` an alias
    names: dict[str, tuple[str, int]] = field(default_factory=dict)

    def add(self, row: EntityIndexRow) -> None:
        held = self.names.get(row.ref)
        # One canonical-name row per (name, entity); the oldest seq is when it became known.
        if held is None or (row.ref == "name" and row.seq < held[1]):
            self.names[row.ref] = (row.key, row.seq)

    @property
    def latest(self) -> int:
        return max((seq for _, seq in self.names.values()), default=0)

    def rank(self) -> tuple[int, str]:
        """RM-154: the stable order of index entries, from index state only: the most recently
        changed (latest name or alias row) first, then by entity id."""
        return (-self.latest, self.entity_id)

    def known(self, namespace: str) -> KnownEntity | None:
        canonical = self.names.get("name")
        if canonical is None:
            return None  # only aliases left (a partial page): not presentable
        aliases = sorted(
            (text for ref, (text, _) in self.names.items() if ref.startswith("alias:")),
        )
        return KnownEntity(
            _display(canonical[0]),
            tuple(_display(a) for a in aliases if a != canonical[0]),
            source=KNOWN_SOURCE,
            entity_id=self.entity_id,
            version=self.latest,
            known_seq=canonical[1],
            visibility=KnownVisibility(namespace=namespace),
        )


def _display(name: str) -> str:
    """Index names are canonical (casefolded); the prompt shows them title-cased."""
    return name.title()


def _entries(rows: Iterable[EntityIndexRow], namespace: str) -> dict[str, _Entry]:
    out: dict[str, _Entry] = {}
    for row in rows:
        # The index is namespace-local: a row of another namespace is never visible here.
        if row.namespace != namespace or row.kind != "name" or not row.entity_id:
            continue
        out.setdefault(row.entity_id, _Entry(row.entity_id)).add(row)
    return out


def _key(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


@dataclass
class _Cursor:
    """One namespace's refresh state."""

    #: every name row with seq at or below it has been read (None: nothing proven yet)
    mark: int | None = None
    #: the last row of the scan that set ``mark`` (the next scan resumes there when it can)
    last: RowCursor | None = None
    #: a scan in progress: its keyset position and the watermark taken when it started
    scanning: bool = False
    scan_after: RowCursor | None = None
    scan_mark: int | None = None
    entries: dict[str, _Entry] = field(default_factory=dict)
    #: entities past ``max_entities`` exist in the index (recomputed by a rescan, never sticky)
    capped: bool = False
    #: entities a removal touched: re-read by id at the next refresh
    recheck: set[str] = field(default_factory=set)
    #: other processes' removal signals of the namespace as last seen (None: never read)
    gone: dict[str, int] | None = None


class KnownEntityCursor:
    """DS-12 (ADR-090 item 6): the bounded, incremental KNOWN refresh over the entity index.

    Each call reads at most ``max_pages`` pages of ``page`` name rows past the namespace's
    keyset cursor (``select_after``), never the whole alias table or index.

    **Completeness (RM-154).** Each refresh first takes the index's per-namespace projection
    watermark (:func:`~memspine.memories.associative.entity_index.index_watermark`: the
    namespace's backlog is caught up, and every event at or below it is in the index). A scan
    started at that watermark reads every row past the previous one, so once it ends every name
    row at or below the watermark has been read, including a row another process projected late
    with a low seq. The set is complete only then; without a provable watermark, or with rows
    left to read, it is deferred (``behind``).

    **Removals (RM-154, ADR-095).** The index projector tells the cursor of every name removal
    it applies (a forget, an erasure, a re-write, a reset): the entities go from memory at once
    and are re-read by id at the next refresh. A removal another process applied is seen as a
    change of the namespace's removal signal row, and the namespace is read again. A removal
    while the set is capped also reads the namespace again, so ``capped`` is recomputed.

    **Determinism (RM-154).** The kept set (at most ``max_entities``) and the returned order are
    derived from index state only: the stable key of :meth:`_Entry.rank`. The entries a call
    returns (at most ``limit``: those named in the NEW text first, then the rest, each in rank
    order) are re-read by entity id; one that is gone is dropped. A capped or partly read set
    is returned as a deferred :class:`KnownSet`: incomplete, and per ADR-089 never a basis for
    an auto-link."""

    def __init__(
        self,
        storage: Callable[[], Any],
        *,
        projector: Callable[[], EntityIndexProjector | None] | None = None,
        page: int = constants.KNOWN_REFRESH_PAGE,
        max_pages: int = constants.KNOWN_REFRESH_PAGES,
        max_entities: int = constants.KNOWN_MAX_ENTITIES,
        limit: int = 40,
    ) -> None:
        if min(page, max_pages, max_entities, limit) < 1:
            raise ValueError("KnownEntityCursor bounds must be positive")
        self._storage = storage
        self._projector = projector
        self._bound: EntityIndexProjector | None = None
        self.page = page
        self.max_pages = max_pages
        self.max_entities = max_entities
        self.limit = limit
        self._state: dict[str, _Cursor] = {}
        #: rows read by the last call (the bounded work, for tests and stats)
        self.rows_read = 0

    def reset(self, namespace: str | None = None) -> None:
        """Forget the refresh state (every namespace when None): the next call starts over."""
        if namespace is None:
            self._state.clear()
        else:
            self._state.pop(namespace, None)

    def evict(self, namespace: str | None, entity_ids: frozenset[str] | None) -> None:
        """The projector's removal hook (:class:`~memspine.memories.associative.entity_index.
        NameEvictionListener`): drop the named entities from memory now."""
        if namespace is None:
            self._state.clear()
            return
        state = self._state.get(namespace)
        if state is None:
            return
        if entity_ids is None or state.capped:
            # Unknown scope, or a capped set (the removal may free room): recomputed by a rescan.
            self._state[namespace] = _Cursor(gone=state.gone)
            return
        for entity_id in entity_ids:
            state.entries.pop(entity_id, None)
        state.recheck.update(entity_ids)

    def _bind(self) -> EntityIndexProjector | None:
        projector = self._projector() if self._projector is not None else None
        if projector is not None and projector is not self._bound:
            projector.add_listener(self)
            self._bound = projector
        return projector

    async def __call__(self, namespace: str, text: str) -> KnownSet:
        projector = self._bind()
        storage = self._storage()
        store = entity_index_store_of(storage)
        self._state.setdefault(namespace, _Cursor())
        # The watermark first: its catch-up may apply removals (the hook evicts them).
        watermark = await index_watermark(storage, namespace, projector)
        await self._signal(store, namespace)
        state = self._state[namespace]
        await self._recheck(store, namespace, state)
        complete = await self._advance(store, namespace, state, watermark)
        chosen = self._choose(state, text)
        known: list[KnownEntity] = []
        for entry in chosen:
            fresh = _entries(
                await store.select(namespace, "name", entity_id=entry.entity_id), namespace
            )
            current = fresh.get(entry.entity_id)
            if current is None:
                state.entries.pop(entry.entity_id, None)  # forgotten or erased since it was read
                continue
            state.entries[entry.entity_id] = current
            entity = current.known(namespace)
            if entity is not None:
                known.append(entity)
        reason = "capped" if state.capped else (None if complete else "behind")
        return KnownSet(known, deferred=reason is not None, reason=reason)

    async def _signal(self, store: EntityIndexStore, namespace: str) -> None:
        """A removal another process applied (its signal row changed): read the namespace
        again. This process's own removals came through :meth:`evict` already."""
        own = self._bound.instance if self._bound is not None else None
        gone = {
            row.key: int(row.data.get("count", 0))
            for row in await store.select(namespace, GONE_KIND)
            if row.key != own
        }
        state = self._state[namespace]
        if gone == state.gone:
            return
        fresh = state.mark is None and not state.entries and not state.scanning
        if state.gone is not None and not fresh:
            state = self._state[namespace] = _Cursor()
        state.gone = gone

    async def _recheck(self, store: EntityIndexStore, namespace: str, state: _Cursor) -> None:
        """Re-read the entities a removal touched; one that still has names is kept again."""
        ids, state.recheck = sorted(state.recheck), set()
        for entity_id in ids:
            current = _entries(
                await store.select(namespace, "name", entity_id=entity_id), namespace
            ).get(entity_id)
            if current is not None:
                state.entries[entity_id] = current
        self._trim(state)

    async def _advance(
        self, store: EntityIndexStore, namespace: str, state: _Cursor, watermark: int | None
    ) -> bool:
        """Scan past the proven mark; True when every row up to ``watermark`` has been read."""
        self.rows_read = 0
        for _ in range(self.max_pages):
            if not state.scanning:
                state.scanning, state.scan_mark = True, watermark
                state.scan_after = self._start(state)
            rows = await store.select_after(
                namespace, "name", after=state.scan_after, limit=self.page
            )
            self.rows_read += len(rows)
            partial: set[str] = set()
            for row in rows:
                if row.namespace != namespace or not row.entity_id:
                    continue
                entry = state.entries.get(row.entity_id)
                if entry is None:
                    entry = state.entries[row.entity_id] = _Entry(row.entity_id)
                    if row.ref != "name":
                        partial.add(row.entity_id)  # its canonical row was read (and cut) before
                entry.add(row)
            if rows:
                state.scan_after = row_cursor(rows[-1])
            self._trim(state)
            for entity_id in sorted(partial & state.entries.keys()):
                found = _entries(
                    await store.select(namespace, "name", entity_id=entity_id), namespace
                )
                if entity_id in found:
                    state.entries[entity_id] = found[entity_id]
            if len(rows) < self.page:
                state.scanning = False
                if state.scan_mark is not None:
                    state.mark = max(state.mark or 0, state.scan_mark)
                    state.last = state.scan_after
                if watermark is None or (state.mark is not None and state.mark >= watermark):
                    break
        return (
            not state.scanning
            and watermark is not None
            and state.mark is not None
            and state.mark >= watermark
        )

    @staticmethod
    def _start(state: _Cursor) -> RowCursor | None:
        """Where a scan starts: every row at or below ``mark`` was read. The last row of the
        scan that proved it is a tighter start when it is at or below the mark (no row between
        it and the mark exists or can appear); otherwise rows past the mark are read again."""
        if state.mark is None:
            return None
        if state.last is not None and state.last[0] <= state.mark:
            return state.last
        return (state.mark, "", "", "")

    def _trim(self, state: _Cursor) -> None:
        """Keep the ``max_entities`` best-ranked entities; the rest make the set capped."""
        if len(state.entries) <= self.max_entities:
            return
        kept = sorted(state.entries.values(), key=_Entry.rank)[: self.max_entities]
        state.entries = {entry.entity_id: entry for entry in kept}
        state.capped = True

    def _choose(self, state: _Cursor, text: str) -> list[_Entry]:
        haystack = _key(text)
        mentioned: list[_Entry] = []
        rest: list[_Entry] = []
        for entry in sorted(state.entries.values(), key=_Entry.rank):
            if any(_key(name) and _key(name) in haystack for name, _ in entry.names.values()):
                mentioned.append(entry)
            else:
                rest.append(entry)
        return (mentioned + rest)[: self.limit]


async def index_known_entities(storage: Any, namespace: str) -> list[KnownEntity]:
    """Every entity of ``namespace`` in the index, with provenance, newest known first."""
    store = entity_index_store_of(storage)
    live = {row.entity_id for row in await store.select(namespace, "entity")}
    entries = _entries(await store.select(namespace, "name"), namespace)
    known = [
        entity
        for entry in entries.values()
        if entry.entity_id in live and (entity := entry.known(namespace)) is not None
    ]
    known.sort(key=lambda k: (-(k.known_seq or 0), k.name, k.entity_id or ""))
    return known
