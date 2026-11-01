"""DS-11 (ADR-090 item 6): the window prompt's KNOWN entities, read from the RM-130 entity index.

Every entry carries its provenance (:class:`~memspine.derive.prompt_context.KnownEntity`): the
index entry (``entity_id``), its ``version`` (the index sequence of its latest name or alias row),
``known_seq`` (the sequence at which its canonical name became known) and its visibility (the
namespace that owns the index rows). Aliases are the index's evidence-cited aliases only, so an
alias whose evidence was forgotten or erased is gone from the index and from KNOWN with it.

KNOWN names are prompt hints: they never key a record, link a mention or assert anything.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from memspine.derive.prompt_context import KnownEntity, KnownVisibility
from memspine.services.storage.entity_index_tables import EntityIndexRow, entity_index_store_of

__all__ = ["KNOWN_SOURCE", "index_known_entities"]

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
