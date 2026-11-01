"""RM-130 (ADR-089): opaque entity identity, write-time linking and the entity name index.

**Ids are event-sourced.** An entity id (``ent:<ns>:<uuid7>``) is minted once, on the write path
(:func:`resolve_record_links`), and recorded in the WRITE event's ``entity_links`` payload. The
graph projector and this module's :class:`EntityIndexProjector` only read ids from the log; replay
never mints one, so a rebuild reproduces the same ids and the same index.

**Linking rule.** A mention links to an existing entity automatically only when, in the same
namespace and against the *complete* name index, exactly one entity (a merge group counts once)
matches by exact canonical name or alias with a compatible type (``basis=unique_name``). Such a
link stays ``provisional`` until cited identity evidence (an alias or an explicit merge) confirms
the entity. Otherwise:

* no entity has the name: a new entity (``basis=new``);
* only entities of a clashing type have it: a new provisional entity (``basis=type_clash``),
  the clashing entities recorded as candidates;
* two or more compatible candidates: the mention joins the name's ambiguity placeholder
  (``basis=ambiguous``, candidates recorded), creating it when there is none;
* the index is behind the log for the namespace (deferred), or the check is capped: never an
  auto-link, a new provisional entity (``basis=deferred``);
* an ``entity_ref:<id>`` tag pins the subject (``basis=explicit``, confirmed); ``entity_ref:new``
  makes a distinct new entity.

**Merges and aliases** are MARKER events (``entity_index.merge`` / ``entity_index.alias``) citing
evidence record ids. Forgetting (or erasing) the last live cited record undoes them; the
entities involved are then ``contested``. Merges are namespace-local: cross-namespace grantee
merges stay compiled graph rows under the RM-55/F13 visibility rules.

**The index is a projection** kept in storage.records
(:mod:`~memspine.services.storage.entity_index_tables`), written only by
:class:`EntityIndexProjector` and cleared + replayed on rebuild.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from memspine.memories.associative import entity_cache
from memspine.memories.associative.entities import (
    ENTITY_REF_TAG,
    EntityPolicy,
    canonical_entity,
    match_key,
    name_fingerprint,
    new_entity_id,
    parse_entity_node,
    record_entity_names,
    record_name_types,
    types_compatible,
)
from memspine.services.storage.entity_index_tables import (
    EntityIndexRow,
    EntityIndexStore,
    entity_index_store_of,
)
from memspine.store.events import EventKind, MemoryEvent, fingerprint_payload
from memspine.store.projections.projector import Projector
from memspine.store.records import MemoryRecord

__all__ = [
    "ALIAS_MARKER",
    "ENTITY_STATUSES",
    "INDEX_PROJECTOR",
    "LINKS_KEY",
    "MERGE_MARKER",
    "EntityIndexProjector",
    "alias_event",
    "entity_names",
    "entity_status",
    "link_names",
    "merge_event",
    "record_entity_ids",
    "resolve_record_links",
    "with_entity_links",
]

#: The projector name (its durable high-water mark key).
INDEX_PROJECTOR = "entity_index"
#: WRITE payload key holding the record's resolved entity links.
LINKS_KEY = "entity_links"
MERGE_MARKER = "entity_index.merge"
ALIAS_MARKER = "entity_index.alias"
#: ADR-089 status vocabulary (``pending_merge`` is reserved for a proposed, unapplied merge).
ENTITY_STATUSES = ("confirmed", "provisional", "contested", "pending_merge")
#: Events read past the index mark before a freshness check gives up (capped: deferred).
_FRESHNESS_CAP = 64
#: Merge chains followed to a group representative (a cycle or deeper chain stops there).
_MAX_CHAIN = 16


def _store(storage: Any) -> EntityIndexStore:
    return entity_index_store_of(storage)


def _superset_names(record: MemoryRecord, *, turns: bool) -> list[str]:
    """Every canonical name ``record`` could mention under any policy (link names are matched
    by fingerprint, so the projection is independent of today's policy)."""
    return record_entity_names(record, EntityPolicy(blocklist=frozenset(), turn_mentions=turns))


def link_names(
    record: MemoryRecord, links: Sequence[Mapping[str, Any]]
) -> list[tuple[str, dict[str, Any]]]:
    """``(canonical name, link)`` for each recorded link whose name ``record`` still carries
    (a redacted record carries none: its links project nothing)."""
    if not links:
        return []
    by_fp: dict[str, str] = {}
    for name in _superset_names(record, turns=False):
        by_fp.setdefault(name_fingerprint(name), name)
    if any(str(link.get("name_fp")) not in by_fp for link in links):
        for name in _superset_names(record, turns=True):
            by_fp.setdefault(name_fingerprint(name), name)
    out: list[tuple[str, dict[str, Any]]] = []
    for link in links:
        found = by_fp.get(str(link.get("name_fp")))
        if found is not None and isinstance(link.get("entity_id"), str):
            out.append((found, dict(link)))
    return out


# ------------------------------------------------------------------ the write path


async def _index_complete(storage: Any, namespace: str, applied: int) -> bool:
    """Is the name index complete for ``namespace`` at the log head? The index projector's mark
    (durable or this process's) must cover every entity-relevant event of the namespace; a
    capped read past the mark counts as incomplete (never an auto-link)."""
    try:
        durable = int(await storage.get_offset(INDEX_PROJECTOR))
    except Exception:
        durable = 0
    after = max(durable, applied)
    reader = getattr(storage, "read_events", None)
    if not callable(reader):
        return True
    try:
        pending = await reader(after_seq=after, limit=_FRESHNESS_CAP)
    except Exception:
        return False
    if len(pending) >= _FRESHNESS_CAP:
        return False
    return not any(
        e.namespace == namespace and e.kind in (EventKind.WRITE, EventKind.MARKER, EventKind.FORGET)
        for e in pending
    )


async def _representative(store: EntityIndexStore, namespace: str, entity_id: str) -> str:
    current = entity_id
    for _ in range(_MAX_CHAIN):
        kept = await store.select(namespace, "merge", ref=current)
        if not kept:
            return current
        current = kept[0].entity_id
    return current


async def _entity_row(
    store: EntityIndexStore, namespace: str, entity_id: str
) -> EntityIndexRow | None:
    rows = await store.select(namespace, "entity", key=entity_id)
    return rows[0] if rows else None


def _new_link(
    namespace: str,
    name: str,
    etype: str | None,
    basis: str,
    candidates: Iterable[str] = (),
    status: str = "provisional",
) -> dict[str, Any]:
    return {
        "name_fp": name_fingerprint(name),
        "entity_id": new_entity_id(namespace),
        "new": True,
        "basis": basis,
        "status": status,
        "type": etype,
        "candidates": sorted(set(candidates)),
    }


async def _resolve_one(
    store: EntityIndexStore,
    namespace: str,
    name: str,
    etype: str | None,
    *,
    complete: bool,
    pin: str | None,
) -> dict[str, Any]:
    if pin == "new":
        return _new_link(namespace, name, etype, "explicit", status="confirmed")
    if pin is not None:
        parsed = parse_entity_node(pin)
        if (
            parsed is not None
            and parsed[0] == namespace
            and await _entity_row(store, namespace, pin)
        ):
            return {
                "name_fp": name_fingerprint(name),
                "entity_id": pin,
                "new": False,
                "basis": "explicit",
                "status": "confirmed",
                "type": etype,
                "candidates": [],
            }
    if not complete:
        return _new_link(namespace, name, etype, "deferred")
    rows = await store.select(namespace, "name", key=name)
    if not rows:
        return _new_link(namespace, name, etype, "new")
    groups: dict[str, EntityIndexRow] = {}
    for entity in dict.fromkeys(r.entity_id for r in rows):
        rep = await _representative(store, namespace, entity)
        row = await _entity_row(store, namespace, rep)
        if row is not None:
            groups.setdefault(rep, row)
    if not groups:
        return _new_link(namespace, name, etype, "new")
    compatible = {
        eid: row for eid, row in groups.items() if types_compatible(row.data.get("type"), etype)
    }
    if not compatible:
        return _new_link(namespace, name, etype, "type_clash", groups)
    real = sorted(e for e, r in compatible.items() if r.data.get("basis") != "ambiguous")
    placeholders = sorted(e for e, r in compatible.items() if r.data.get("basis") == "ambiguous")
    if len(real) <= 1:
        # exactly one real candidate (or only the name's placeholder left): a unique-name link
        return {
            "name_fp": name_fingerprint(name),
            "entity_id": real[0] if real else placeholders[0],
            "new": False,
            "basis": "unique_name",
            "status": "provisional",
            "type": etype,
            "candidates": [],
        }
    if placeholders:
        return {
            "name_fp": name_fingerprint(name),
            "entity_id": placeholders[0],
            "new": False,
            "basis": "ambiguous",
            "status": "provisional",
            "type": etype,
            "candidates": real,
        }
    return _new_link(namespace, name, etype, "ambiguous", real)


async def resolve_record_links(
    storage: Any,
    record: MemoryRecord,
    policy: EntityPolicy,
    *,
    applied: int = 0,
) -> list[dict[str, Any]]:
    """The entity links of one record about to be written (ids minted here, once)."""
    if record.trust <= 0:
        return []
    names = record_entity_names(record, policy)
    if not names:
        return []
    store = _store(storage)
    complete = await _index_complete(storage, record.namespace, applied)
    types = record_name_types(record, names)
    subject = canonical_entity(record.entity or "")
    pin = next(
        (t[len(ENTITY_REF_TAG) :].strip() for t in record.tags if t.startswith(ENTITY_REF_TAG)),
        None,
    )
    # A re-written record (an update of the same record id) keeps the links it already has.
    kept = {r.ref: r for r in await store.select(record.namespace, "link", key=record.record_id)}
    return [
        {
            "name_fp": name_fingerprint(name),
            "entity_id": kept[name_fingerprint(name)].entity_id,
            "new": False,
            "basis": kept[name_fingerprint(name)].data.get("basis"),
            "status": kept[name_fingerprint(name)].data.get("status"),
            "type": types.get(name),
            "candidates": list(kept[name_fingerprint(name)].data.get("candidates") or []),
        }
        if name_fingerprint(name) in kept
        else await _resolve_one(
            store,
            record.namespace,
            name,
            types.get(name),
            complete=complete,
            pin=pin if name == subject and pin else None,
        )
        for name in names
    ]


async def with_entity_links(
    storage: Any, event: MemoryEvent, policy: EntityPolicy | None, *, applied: int = 0
) -> MemoryEvent:
    """The WRITE ``event`` with its ``entity_links`` resolved (unchanged when there are none,
    when the policy is off, or when the event already carries links: a re-delivered effect)."""
    if policy is None or event.kind is not EventKind.WRITE or LINKS_KEY in event.payload:
        return event
    raw = event.payload.get("record")
    if not isinstance(raw, dict):
        return event
    record = MemoryRecord.model_validate(raw)
    links = await resolve_record_links(storage, record, policy, applied=applied)
    if not links:
        return event
    payload = {**event.payload, LINKS_KEY: links}
    return event.model_copy(
        update={"payload": payload, "fingerprint": fingerprint_payload(payload)}
    )


def merge_event(
    namespace: str, keep: str, absorb: str, evidence: Sequence[str], *, actor: str = "user"
) -> MemoryEvent:
    """An evidence-cited merge of entity ``absorb`` into ``keep`` (both in ``namespace``)."""
    cited = sorted(set(evidence))
    merge_id = fingerprint_payload({"merge": [keep, absorb, cited]})
    return MemoryEvent(
        kind=EventKind.MARKER,
        namespace=namespace,
        actor=actor,
        payload={
            "marker": MERGE_MARKER,
            "merge_id": merge_id,
            "keep": keep,
            "absorb": absorb,
            "evidence": cited,
        },
    )


def alias_event(
    namespace: str, entity_id: str, alias: str, evidence: Sequence[str], *, actor: str = "user"
) -> MemoryEvent:
    """An evidence-cited alias name for ``entity_id``."""
    cited = sorted(set(evidence))
    canonical = canonical_entity(alias)
    alias_id = fingerprint_payload({"alias": [entity_id, canonical, cited]})
    return MemoryEvent(
        kind=EventKind.MARKER,
        namespace=namespace,
        actor=actor,
        payload={
            "marker": ALIAS_MARKER,
            "alias_id": alias_id,
            "entity_id": entity_id,
            "alias": canonical,
            "evidence": cited,
        },
    )


# ------------------------------------------------------------------ the read side


async def entity_names(storage: Any, namespace: str, entity_ids: Iterable[str]) -> dict[str, str]:
    """``entity id -> canonical name`` from the index (ids it does not hold are absent)."""
    store = _store(storage)
    out: dict[str, str] = {}
    for entity_id in dict.fromkeys(entity_ids):
        row = await _entity_row(store, namespace, entity_id)
        if row is not None and isinstance(row.data.get("name"), str):
            out[entity_id] = str(row.data["name"])
    return out


async def namespace_entity_names(storage: Any, namespace: str) -> list[str]:
    """Every canonical entity name of ``namespace`` in the index (sorted, once each)."""
    rows = await _store(storage).select(namespace, "entity")
    return sorted({str(r.data["name"]) for r in rows if isinstance(r.data.get("name"), str)})


async def record_entity_ids(
    storage: Any, namespace: str, record_ids: Iterable[str]
) -> dict[str, dict[str, str]]:
    """``record id -> {canonical name: entity id}`` of the records' current links."""
    store = _store(storage)
    out: dict[str, dict[str, str]] = {}
    for record_id in dict.fromkeys(record_ids):
        for row in await store.select(namespace, "link", key=record_id):
            name = row.data.get("name")
            if isinstance(name, str):
                out.setdefault(record_id, {})[name] = row.entity_id
    return out


async def entity_status(storage: Any, namespace: str, entity_id: str) -> str | None:
    """The entity's ADR-089 status: ``confirmed`` (explicit, or a live merge / alias cites
    evidence for it), ``contested`` (identity evidence for it was revoked), else
    ``provisional``; None when the index does not hold it."""
    store = _store(storage)
    row = await _entity_row(store, namespace, entity_id)
    if row is None:
        return None
    if row.data.get("basis") == "explicit":
        return "confirmed"
    if await store.select(namespace, "merge", entity_id=entity_id) or await store.select(
        namespace, "merge", ref=entity_id
    ):
        return "confirmed"
    if any(
        r.ref.startswith("alias:")
        for r in await store.select(namespace, "name", entity_id=entity_id)
    ):
        return "confirmed"
    return "contested" if row.data.get("contested") else "provisional"


# ------------------------------------------------------------------ the projector


class EntityIndexProjector(Projector):
    """Projects entity creation, links, aliases and merges into the name index."""

    name = INDEX_PROJECTOR

    def __init__(self, storage: Any) -> None:
        self._storage = storage
        #: the last seq this process applied (the write path's freshness check reads it)
        self.applied = 0

    @property
    def store(self) -> EntityIndexStore:
        return _store(self._storage)

    async def apply(self, event: MemoryEvent) -> None:
        if event.kind is EventKind.WRITE:
            await self._write(event)
        elif event.kind is EventKind.FORGET:
            await self._forget(event.namespace, str(event.payload.get("record_id", "")))
        elif event.kind is EventKind.MARKER:
            marker = event.payload.get("marker")
            if marker == MERGE_MARKER:
                await self._merge(event)
            elif marker == ALIAS_MARKER:
                await self._alias(event)
        if event.seq is not None:
            self.applied = max(self.applied, int(event.seq))
        entity_cache.note_applied(self._storage, event.seq)  # RM-110: own-process events

    async def _write(self, event: MemoryEvent) -> None:
        raw = event.payload.get("record")
        if not isinstance(raw, dict):
            return
        record = MemoryRecord.model_validate(raw)
        ns, seq = record.namespace, int(event.seq or 0)
        store = self.store
        previous = {r.entity_id for r in await store.select(ns, "link", key=record.record_id)}
        await store.delete(ns, "link", key=record.record_id)
        raw_links = event.payload.get(LINKS_KEY)
        links = raw_links if isinstance(raw_links, list) and record.trust > 0 else []
        rows: list[EntityIndexRow] = []
        linked: set[str] = set()
        for name, link in link_names(record, links):
            entity_id = str(link["entity_id"])
            etype = link.get("type")
            existing = await _entity_row(store, ns, entity_id)
            if existing is None:
                rows.append(
                    EntityIndexRow(
                        ns,
                        "entity",
                        entity_id,
                        entity_id,
                        data={
                            "name": name,
                            "type": etype,
                            "basis": str(link.get("basis", "new")),
                            "candidates": list(link.get("candidates") or []),
                        },
                        seq=seq,
                    )
                )
            elif existing.data.get("type") is None and etype is not None:
                rows.append(
                    EntityIndexRow(
                        ns,
                        "entity",
                        entity_id,
                        entity_id,
                        data={**existing.data, "type": etype},
                        seq=existing.seq,
                    )
                )
            if existing is None or not await store.select(
                ns, "name", key=name, entity_id=entity_id
            ):
                rows.append(
                    EntityIndexRow(ns, "name", name, entity_id, "name", match_key(name), seq=seq)
                )
            rows.append(
                EntityIndexRow(
                    ns,
                    "link",
                    record.record_id,
                    entity_id,
                    str(link.get("name_fp", "")),
                    data={
                        "name": name,
                        "basis": link.get("basis"),
                        "status": link.get("status"),
                        "candidates": list(link.get("candidates") or []),
                    },
                    seq=seq,
                )
            )
            linked.add(entity_id)
        await store.put(rows)
        await self._drop_orphans(ns, previous - linked)
        entity_cache.invalidate(self._storage, ns)

    async def _drop_orphans(self, ns: str, entity_ids: Iterable[str]) -> None:
        """Remove each entity no record links to any more (with its names and merges)."""
        store = self.store
        for entity_id in sorted(entity_ids):
            if await store.select(ns, "link", entity_id=entity_id):
                continue
            await store.delete(ns, "entity", key=entity_id)
            await store.delete(ns, "name", entity_id=entity_id)
            for row in [
                *await store.select(ns, "merge", entity_id=entity_id),
                *await store.select(ns, "merge", ref=entity_id),
            ]:
                await store.delete(ns, "merge", key=row.key)

    async def _forget(self, ns: str, record_id: str) -> None:
        if not record_id:
            return
        store = self.store
        previous = {r.entity_id for r in await store.select(ns, "link", key=record_id)}
        await store.delete(ns, "link", key=record_id)
        cites = await store.select(ns, "cite", key=record_id)
        await store.delete(ns, "cite", key=record_id)
        for cite in cites:
            if await store.select(ns, "cite", entity_id=cite.entity_id):
                continue  # other live evidence still supports it
            if cite.ref == "merge":
                for merge in await store.select(ns, "merge", key=cite.entity_id):
                    await store.delete(ns, "merge", key=merge.key)
                    await self._contest(ns, [merge.entity_id, merge.ref])
            elif cite.ref == "alias":
                for alias in await self._alias_rows(ns, cite.entity_id):
                    await store.delete(
                        ns, "name", key=alias.key, entity_id=alias.entity_id, ref=alias.ref
                    )
                    await self._contest(ns, [alias.entity_id])
        await self._drop_orphans(ns, previous)
        entity_cache.invalidate(self._storage, ns)

    async def _alias_rows(self, ns: str, alias_id: str) -> list[EntityIndexRow]:
        return await self.store.select(ns, "name", ref=f"alias:{alias_id}")

    async def _contest(self, ns: str, entity_ids: Iterable[str]) -> None:
        store = self.store
        for entity_id in entity_ids:
            row = await _entity_row(store, ns, entity_id)
            if row is not None and not row.data.get("contested"):
                await store.put(
                    [
                        EntityIndexRow(
                            ns,
                            "entity",
                            entity_id,
                            entity_id,
                            data={**row.data, "contested": True},
                            seq=row.seq,
                        )
                    ]
                )

    @staticmethod
    def _evidence(payload: Mapping[str, Any]) -> list[str]:
        raw = payload.get("evidence")
        return [str(e) for e in raw if str(e)] if isinstance(raw, list) else []

    async def _merge(self, event: MemoryEvent) -> None:
        p, ns = event.payload, event.namespace
        keep, absorb, evidence = str(p.get("keep", "")), str(p.get("absorb", "")), self._evidence(p)
        merge_id = str(p.get("merge_id", ""))
        store = self.store
        if (
            not evidence
            or not merge_id
            or keep == absorb
            or await _entity_row(store, ns, keep) is None
            or await _entity_row(store, ns, absorb) is None
        ):
            return
        rows = [
            EntityIndexRow(
                ns,
                "merge",
                merge_id,
                keep,
                absorb,
                data={"evidence": evidence},
                seq=int(event.seq or 0),
            )
        ]
        rows += [
            EntityIndexRow(ns, "cite", rid, merge_id, "merge", seq=int(event.seq or 0))
            for rid in evidence
        ]
        await store.put(rows)
        entity_cache.invalidate(self._storage, ns)

    async def _alias(self, event: MemoryEvent) -> None:
        p, ns = event.payload, event.namespace
        entity_id, evidence = str(p.get("entity_id", "")), self._evidence(p)
        alias, alias_id = canonical_entity(str(p.get("alias", ""))), str(p.get("alias_id", ""))
        store = self.store
        if (
            not evidence
            or not alias
            or not alias_id
            or await _entity_row(store, ns, entity_id) is None
        ):
            return
        seq = int(event.seq or 0)
        rows = [
            EntityIndexRow(
                ns, "name", alias, entity_id, f"alias:{alias_id}", match_key(alias), seq=seq
            )
        ]
        rows += [EntityIndexRow(ns, "cite", rid, alias_id, "alias", seq=seq) for rid in evidence]
        await store.put(rows)
        entity_cache.invalidate(self._storage, ns)

    async def purge_records(self, ns: str, record_ids: Iterable[str]) -> None:
        """Erasure's direct purge (records removed outside the projector)."""
        for record_id in record_ids:
            await self._forget(ns, record_id)

    async def reset(self) -> None:
        await self.store.clear()
        self.applied = 0
        entity_cache.invalidate(self._storage)
