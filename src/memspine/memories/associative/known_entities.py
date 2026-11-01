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
            version=max(seq for _, seq in self.names.values()),
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

    after: RowCursor | None = None
    entries: dict[str, _Entry] = field(default_factory=dict)
    #: the entity cap was hit: entities past it were not kept (sticky until a reset)
    capped: bool = False
    #: the last refresh stopped at its page bound with rows left to read
    behind: bool = False


class KnownEntityCursor:
    """DS-12 (ADR-090 item 6): the bounded, incremental KNOWN refresh over the entity index.

    Each call reads at most ``max_pages`` pages of ``page`` name rows past the namespace's
    keyset cursor (``select_after``), never the whole alias table or index. A short lookback
    of ``lookback`` sequence numbers is re-read after a caught-up refresh, so a row another
    process projected late (a lower seq landing after a higher one) is still picked up.
    The kept set is capped at ``max_entities``. Rows deleted by a forget or an erasure are
    not visible to a cursor, so the entries a call returns (at most ``limit``: those named in
    the NEW text first, then the most recently changed) are re-read by entity id first; one
    that is gone is dropped. A capped or partly read set is returned as a deferred
    :class:`KnownSet`: incomplete, and per ADR-089 never a basis for an auto-link."""

    def __init__(
        self,
        storage: Callable[[], Any],
        *,
        page: int = constants.KNOWN_REFRESH_PAGE,
        max_pages: int = constants.KNOWN_REFRESH_PAGES,
        max_entities: int = constants.KNOWN_MAX_ENTITIES,
        limit: int = 40,
        lookback: int = constants.KNOWN_LOOKBACK_SEQ,
    ) -> None:
        if min(page, max_pages, max_entities, limit) < 1 or lookback < 0:
            raise ValueError("KnownEntityCursor bounds must be positive")
        self._storage = storage
        self.page = page
        self.max_pages = max_pages
        self.max_entities = max_entities
        self.limit = limit
        self.lookback = lookback
        self._state: dict[str, _Cursor] = {}
        #: rows read by the last call (the bounded work, for tests and stats)
        self.rows_read = 0

    def reset(self, namespace: str | None = None) -> None:
        """Forget the refresh state (every namespace when None): the next call starts over."""
        if namespace is None:
            self._state.clear()
        else:
            self._state.pop(namespace, None)

    async def __call__(self, namespace: str, text: str) -> KnownSet:
        store = entity_index_store_of(self._storage())
        state = self._state.setdefault(namespace, _Cursor())
        await self._advance(store, namespace, state)
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
        reason = "capped" if state.capped else ("behind" if state.behind else None)
        return KnownSet(known, deferred=reason is not None, reason=reason)

    async def _advance(self, store: EntityIndexStore, namespace: str, state: _Cursor) -> None:
        after = state.after
        if after is not None and not state.behind and self.lookback:
            after = (max(after[0] - self.lookback, 0), "", "", "")
        self.rows_read = 0
        state.behind = True
        for _ in range(self.max_pages):
            rows = await store.select_after(namespace, "name", after=after, limit=self.page)
            self.rows_read += len(rows)
            for row in rows:
                if row.namespace != namespace or not row.entity_id:
                    continue
                entry = state.entries.get(row.entity_id)
                if entry is None:
                    if len(state.entries) >= self.max_entities:
                        state.capped = True
                        continue
                    entry = state.entries[row.entity_id] = _Entry(row.entity_id)
                entry.add(row)
            if rows:
                after = row_cursor(rows[-1])
                if state.after is None or after > state.after:
                    state.after = after
            if len(rows) < self.page:
                state.behind = False
                break

    def _choose(self, state: _Cursor, text: str) -> list[_Entry]:
        haystack = _key(text)
        mentioned: list[_Entry] = []
        rest: list[_Entry] = []
        for entry in state.entries.values():
            if any(_key(name) and _key(name) in haystack for name, _ in entry.names.values()):
                mentioned.append(entry)
            else:
                rest.append(entry)
        rest.sort(key=lambda e: -max((seq for _, seq in e.names.values()), default=0))
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
    known.sort(key=lambda k: (-(k.known_seq or 0), k.name))
    return known
