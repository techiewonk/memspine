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
* the index is behind the log for the namespace even after a bounded catch-up (the namespace's
  pending events are applied first), or another process's write lands between resolution and
  append: never an auto-link, a new provisional entity (``basis=deferred``, or ``new`` for the
  race).
* an ``entity_ref:<id>`` tag pins the subject (``basis=explicit``, confirmed); ``entity_ref:new``
  makes a distinct new entity.

**Re-linking.** A ``deferred`` or ``new`` entity is never a permanent separate identity by
accident: once the namespace's index is complete, the resolution pass (:func:`relink_events`)
resolves it as the write would have against the complete index at its own sequence (only name
rows older than it count). A unique match gets an ``entity_index.link`` MARKER folding the
entity into that match (still ``provisional``: a link is no identity evidence). The pass runs
before each write's resolution and after each write that minted an entity, so a cross-process
race (two writes both minting "Alice") is repaired by whichever writer sees both.

**Merges and aliases** are MARKER events (``entity_index.merge`` / ``entity_index.alias``) citing
evidence record ids. Forgetting (or erasing) the last live cited record undoes them; the
entities involved are then ``contested``. A merge or alias projected when none of its cited
evidence is live any more (a forget that won the race) is ignored and contests its entities.
A hard erase of any cited record undoes an alias and scrubs the alias text from its marker
(:func:`scrub_alias_marker`). Merges are namespace-local: cross-namespace grantee merges stay
compiled graph rows under the RM-55/F13 visibility rules.

**The index is a projection** kept in storage.records
(:mod:`~memspine.services.storage.entity_index_tables`), written only by
:class:`EntityIndexProjector` and cleared + replayed on rebuild.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Any

import fastuuid

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
from memspine.store.records import MemoryRecord, RecordStatus
from memspine.trust.erasure import LINKS_KEY

__all__ = [
    "ALIAS_MARKER",
    "ENTITY_STATUSES",
    "INDEX_PROJECTOR",
    "LINKS_KEY",
    "LINK_MARKER",
    "MERGE_MARKER",
    "EntityIndexProjector",
    "alias_event",
    "entity_link_event",
    "entity_names",
    "entity_status",
    "link_names",
    "merge_event",
    "record_entity_ids",
    "relink_events",
    "resolve_record_links",
    "scrub_alias_marker",
    "with_entity_links",
]

#: The projector name (its durable high-water mark key).
INDEX_PROJECTOR = "entity_index"
MERGE_MARKER = "entity_index.merge"
ALIAS_MARKER = "entity_index.alias"
#: The resolution pass folding a deferred / raced entity into its unique match (no evidence).
LINK_MARKER = "entity_index.link"
#: ADR-089 status vocabulary (``pending_merge`` is reserved for a proposed, unapplied merge).
ENTITY_STATUSES = ("confirmed", "provisional", "contested", "pending_merge")
#: Bounded catch-up of the freshness check: events read per page past the index mark, and the
#: pages read before the check gives up (the namespace is then incomplete: deferred).
_CATCH_UP_PAGE = 256
_CATCH_UP_PAGES = 8
#: Entity bases a resolution pass may re-link (minted without a complete-index uniqueness check).
_RELINKABLE = frozenset({"new", "deferred"})
#: The event kinds the name index projects.
_INDEX_KINDS = (EventKind.WRITE, EventKind.MARKER, EventKind.FORGET)
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


async def _index_complete(
    storage: Any, namespace: str, projector: EntityIndexProjector | None = None
) -> bool:
    """Is the name index complete for ``namespace`` at the log head?

    Freshness is per namespace: events of other namespaces past the mark never make it stale.
    The namespace's own pending events (another process's writes, not yet in the shared index)
    are caught up by applying them to ``projector`` (applies are idempotent), at most
    ``_CATCH_UP_PAGES`` pages of the log; a longer backlog counts as incomplete (never an
    auto-link). Without a projector any pending event of the namespace is incomplete."""
    # The scan starts where this process last scanned the namespace to the head (its own
    # in-order mark is no start: another process's event may sit below it), else at the
    # durable mark (startup catch-up brought the index there).
    after = projector.caught_up.get(namespace) if projector is not None else None
    if after is None:
        try:
            after = int(await storage.get_offset(INDEX_PROJECTOR))
        except Exception:
            after = 0
    reader = getattr(storage, "read_events", None)
    if not callable(reader):
        return True
    for _ in range(_CATCH_UP_PAGES):
        try:
            pending = await reader(after_seq=after, limit=_CATCH_UP_PAGE)
        except Exception:
            return False
        relevant = [e for e in pending if e.namespace == namespace and e.kind in _INDEX_KINDS]
        if relevant and projector is None:
            return False
        for event in relevant:
            assert projector is not None
            await projector.catch_up(event)
        if pending and pending[-1].seq is not None:
            after = int(pending[-1].seq)
        if projector is not None:
            projector.mark_scanned(namespace, after)
        if len(pending) < _CATCH_UP_PAGE:
            return True
    return False


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
    outcome, entity_id, candidates = await _match(store, namespace, name, etype)
    if outcome == "none":
        return _new_link(namespace, name, etype, "new")
    if outcome == "type_clash":
        return _new_link(namespace, name, etype, "type_clash", candidates)
    if outcome == "unique":
        assert entity_id is not None
        return {
            "name_fp": name_fingerprint(name),
            "entity_id": entity_id,
            "new": False,
            "basis": "unique_name",
            "status": "provisional",
            "type": etype,
            "candidates": [],
        }
    if entity_id is not None:  # join the name's ambiguity placeholder
        return {
            "name_fp": name_fingerprint(name),
            "entity_id": entity_id,
            "new": False,
            "basis": "ambiguous",
            "status": "provisional",
            "type": etype,
            "candidates": candidates,
        }
    return _new_link(namespace, name, etype, "ambiguous", candidates)


async def _match(
    store: EntityIndexStore,
    namespace: str,
    name: str,
    etype: str | None,
    *,
    before_seq: int | None = None,
    exclude: str | None = None,
) -> tuple[str, str | None, list[str]]:
    """The auto-link rule against the index: ``(outcome, entity id, candidates)``.

    ``outcome`` is ``none`` (no entity has the name), ``type_clash`` (only clashing types; the
    candidates are they), ``unique`` (one real compatible group, or only the name's placeholder:
    the entity id), or ``ambiguous`` (two or more real groups: the candidates, and the name's
    placeholder to join when there is one). ``before_seq`` counts only name rows projected
    before it (the index as a write at that sequence saw it); ``exclude`` drops that entity's
    own group (the resolution pass asks what an entity should have linked to)."""
    rows = await store.select(namespace, "name", key=name)
    if before_seq is not None:
        rows = [r for r in rows if r.seq < before_seq]
    groups: dict[str, EntityIndexRow] = {}
    for entity in dict.fromkeys(r.entity_id for r in rows):
        rep = await _representative(store, namespace, entity)
        if exclude is not None and rep == exclude:
            continue
        row = await _entity_row(store, namespace, rep)
        if row is not None and (before_seq is None or row.seq < before_seq):
            groups.setdefault(rep, row)
    if not groups:
        return "none", None, []
    compatible = {
        eid: row for eid, row in groups.items() if types_compatible(row.data.get("type"), etype)
    }
    if not compatible:
        return "type_clash", None, sorted(groups)
    real = sorted(e for e, r in compatible.items() if r.data.get("basis") != "ambiguous")
    placeholders = sorted(e for e, r in compatible.items() if r.data.get("basis") == "ambiguous")
    if len(real) <= 1:
        # exactly one real candidate (or only the name's placeholder left): a unique-name link
        return "unique", real[0] if real else placeholders[0], []
    return "ambiguous", placeholders[0] if placeholders else None, real


async def resolve_record_links(
    storage: Any,
    record: MemoryRecord,
    policy: EntityPolicy,
    *,
    projector: EntityIndexProjector | None = None,
) -> list[dict[str, Any]]:
    """The entity links of one record about to be written (ids minted here, once).

    ``projector``: this process's index projector, which catches the namespace up before the
    uniqueness check (without one, any pending event of the namespace defers the links)."""
    if record.trust <= 0:
        return []
    names = record_entity_names(record, policy)
    if not names:
        return []
    store = _store(storage)
    complete = await _index_complete(storage, record.namespace, projector)
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
    storage: Any,
    event: MemoryEvent,
    policy: EntityPolicy | None,
    *,
    projector: EntityIndexProjector | None = None,
) -> MemoryEvent:
    """The WRITE ``event`` with its ``entity_links`` resolved (unchanged when there are none,
    when the policy is off, or when the event already carries links: a re-delivered effect)."""
    if policy is None or event.kind is not EventKind.WRITE or LINKS_KEY in event.payload:
        return event
    raw = event.payload.get("record")
    if not isinstance(raw, dict):
        return event
    record = MemoryRecord.model_validate(raw)
    links = await resolve_record_links(storage, record, policy, projector=projector)
    if not links:
        return event
    payload = {**event.payload, LINKS_KEY: links}
    return event.model_copy(
        update={"payload": payload, "fingerprint": fingerprint_payload(payload)}
    )


async def relink_events(
    storage: Any,
    event: MemoryEvent,
    policy: EntityPolicy | None,
    *,
    projector: EntityIndexProjector | None = None,
) -> list[MemoryEvent]:
    """The resolution pass for the names a WRITE ``event`` mentions: an
    ``entity_index.link`` MARKER for each ``deferred`` / ``new`` entity of those names that,
    resolved against the complete index at its own sequence (name rows older than it), has
    exactly one match. Empty while the namespace's index is incomplete (the pass then runs at a
    later write). Call it before resolving a write (so a deferred entity no longer splits the
    name's candidates) and after appending one that minted an entity (a cross-process race:
    whichever writer sees both entities folds the newer one into the older)."""
    if policy is None or event.kind is not EventKind.WRITE:
        return []
    raw = event.payload.get("record")
    if not isinstance(raw, dict):
        return []
    record = MemoryRecord.model_validate(raw)
    if record.trust <= 0:
        return []
    names = record_entity_names(record, policy)
    if not names or not await _index_complete(storage, record.namespace, projector):
        return []
    store, ns = _store(storage), record.namespace
    out: list[MemoryEvent] = []
    seen: set[str] = set()
    for name in names:
        rows = await store.select(ns, "name", key=name)
        if len({r.entity_id for r in rows}) < 2:
            continue
        for entity_id in dict.fromkeys(r.entity_id for r in rows):
            if entity_id in seen:
                continue
            seen.add(entity_id)
            entity = await _entity_row(store, ns, entity_id)
            if entity is None or entity.data.get("basis") not in _RELINKABLE:
                continue
            if await store.select(ns, "merge", ref=entity_id) or await store.select(
                ns, "merge", entity_id=entity_id
            ):
                continue  # already folded, or the keeper of a merge: its identity is settled
            outcome, target, _ = await _match(
                store,
                ns,
                str(entity.data.get("name", name)),
                entity.data.get("type"),
                before_seq=entity.seq,
                exclude=entity_id,
            )
            if outcome == "unique" and target is not None:
                out.append(entity_link_event(ns, entity_id, target))
    return out


def entity_link_event(namespace: str, entity_id: str, target: str) -> MemoryEvent:
    """The resolution pass's link of a deferred / raced entity into its unique match."""
    return MemoryEvent(
        kind=EventKind.MARKER,
        namespace=namespace,
        actor="system",
        payload={
            "marker": LINK_MARKER,
            "link_id": fingerprint_payload({"link": [entity_id, target]}),
            "entity_id": entity_id,
            "target": target,
        },
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
    """An evidence-cited alias name for ``entity_id``.

    The ``alias_id`` is opaque (a uuid7 minted once into the event, like an entity id): a hash
    of the alias text would survive the text's erasure and be reversible by guessing."""
    cited = sorted(set(evidence))
    canonical = canonical_entity(alias)
    alias_id = str(fastuuid.uuid7())
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


def scrub_alias_marker(payload: dict[str, Any], erased: Collection[str]) -> bool:
    """ADR-095 (entity name index copy class): empty the alias text of an ``entity_index.alias``
    marker payload that cites any of the ``erased`` record ids (the alias was read from that
    evidence). The marker, its ids and the evidence ids stay; replay of a scrubbed marker
    contests the entity, as the live undo did. Mutates ``payload``; True when it changed."""
    if payload.get("marker") != ALIAS_MARKER or not payload.get("alias"):
        return False
    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or not {str(e) for e in evidence} & set(erased):
        return False
    payload["alias"] = ""
    return True


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
    merges = [
        *await store.select(namespace, "merge", entity_id=entity_id),
        *await store.select(namespace, "merge", ref=entity_id),
    ]
    if any(not m.data.get("link") for m in merges):  # a resolution link is no evidence
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
        #: the last seq this process applied, in log order
        self.applied = 0
        #: namespace -> the seq up to which the freshness check scanned that namespace's events
        #: to the head and applied them (:func:`_index_complete`)
        self.caught_up: dict[str, int] = {}
        #: seqs this process applied past the namespace's scan mark (a catch-up skips them)
        self._own: dict[str, set[int]] = {}

    @property
    def store(self) -> EntityIndexStore:
        return _store(self._storage)

    async def apply(self, event: MemoryEvent) -> None:
        await self._project(event)
        if event.seq is not None:
            self.applied = max(self.applied, int(event.seq))
            if event.namespace in self.caught_up and event.kind in _INDEX_KINDS:
                self._own.setdefault(event.namespace, set()).add(int(event.seq))
        entity_cache.note_applied(self._storage, event.seq)  # RM-110: own-process events

    def mark_scanned(self, namespace: str, seq: int) -> None:
        """The freshness check scanned ``namespace`` up to ``seq``: own seqs at or below it
        are behind the scan mark now."""
        self.caught_up[namespace] = max(self.caught_up.get(namespace, 0), seq)
        own = self._own.get(namespace)
        if own:
            own.difference_update({s for s in own if s <= seq})

    async def catch_up(self, event: MemoryEvent) -> None:
        """Apply an event of a namespace's backlog (the freshness check's bounded catch-up):
        another process's write the shared index may not hold yet. An event this process
        already applied is skipped; applies are idempotent either way."""
        own = self._own.get(event.namespace)
        if own is not None and event.seq is not None and int(event.seq) in own:
            own.discard(int(event.seq))
            return
        await self._project(event)

    async def _project(self, event: MemoryEvent) -> None:
        if event.kind is EventKind.WRITE:
            await self._write(event)
        elif event.kind is EventKind.FORGET:
            await self._forget(
                event.namespace,
                str(event.payload.get("record_id", "")),
                hard=bool(event.payload.get("hard")),
            )
        elif event.kind is EventKind.MARKER:
            marker = event.payload.get("marker")
            if marker == MERGE_MARKER:
                await self._merge(event)
            elif marker == ALIAS_MARKER:
                await self._alias(event)
            elif marker == LINK_MARKER:
                await self._link(event)

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

    async def _forget(self, ns: str, record_id: str, *, hard: bool = False) -> None:
        if not record_id:
            return
        store = self.store
        previous = {r.entity_id for r in await store.select(ns, "link", key=record_id)}
        await store.delete(ns, "link", key=record_id)
        cites = await store.select(ns, "cite", key=record_id)
        await store.delete(ns, "cite", key=record_id)
        for cite in cites:
            # A hard erase undoes an alias read from the erased record even while other
            # evidence remains: the marker's alias text is scrubbed (scrub_alias_marker), so a
            # replay does not project it either.
            if (not hard or cite.ref != "alias") and await store.select(
                ns, "cite", entity_id=cite.entity_id
            ):
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
                await store.delete(ns, "cite", entity_id=cite.entity_id, ref="alias")
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

    async def _live_evidence(self, ns: str, evidence: Sequence[str]) -> list[str]:
        """The cited records still live in ``ns`` (not forgotten, not erased). A store without
        a read model (projector fakes) counts every citation as live.

        Live, this closes the race with a forget that committed between an identity change's
        check and its append. On a rebuild the read model holds the final state, so a merge
        whose evidence was forgotten later is ignored (and contested) at replay: the same end
        state as the live undo."""
        get_record = getattr(self._storage, "get_record", None)
        if not callable(get_record):
            return list(evidence)
        live: list[str] = []
        for record_id in evidence:
            record = await get_record(record_id)
            if (
                record is not None
                and record.namespace == ns
                and record.status is not RecordStatus.DELETED
            ):
                live.append(record_id)
        return live

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
        evidence = await self._live_evidence(ns, evidence)
        if not evidence:  # every cited record was forgotten first: never applied
            await self._contest(ns, [keep, absorb])
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
        if not evidence or not alias_id or await _entity_row(store, ns, entity_id) is None:
            return
        if not alias:
            if p.get("redacted"):  # scrubbed by a hard erase: the live undo contested it
                await self._contest(ns, [entity_id])
            return
        evidence = await self._live_evidence(ns, evidence)
        if not evidence:  # every cited record was forgotten first: never applied
            await self._contest(ns, [entity_id])
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

    async def _link(self, event: MemoryEvent) -> None:
        """The resolution pass's link: fold a deferred / raced entity into its unique match.
        Kept as a ``merge`` row marked ``link`` (groups, seeds and lookups follow it like a
        merge) without evidence, so the entities stay provisional."""
        p, ns = event.payload, event.namespace
        entity_id, target = str(p.get("entity_id", "")), str(p.get("target", ""))
        link_id = str(p.get("link_id", ""))
        store = self.store
        if not link_id or not entity_id or not target or entity_id == target:
            return
        entity = await _entity_row(store, ns, entity_id)
        if (
            entity is None
            or entity.data.get("basis") not in _RELINKABLE
            or await _entity_row(store, ns, target) is None
            or await store.select(ns, "merge", ref=entity_id)
            or await _representative(store, ns, target) == entity_id
        ):
            return
        await store.put(
            [
                EntityIndexRow(
                    ns,
                    "merge",
                    link_id,
                    target,
                    entity_id,
                    data={"link": True, "basis": "unique_name"},
                    seq=int(event.seq or 0),
                )
            ]
        )
        entity_cache.invalidate(self._storage, ns)

    async def purge_records(self, ns: str, record_ids: Iterable[str]) -> None:
        """Erasure's direct purge (records removed outside the projector)."""
        for record_id in record_ids:
            await self._forget(ns, record_id)

    async def reset(self) -> None:
        await self.store.clear()
        self.applied = 0
        self.caught_up.clear()
        self._own.clear()
        entity_cache.invalidate(self._storage)
