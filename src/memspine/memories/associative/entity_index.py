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

import weakref
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Any, Protocol, cast

import fastuuid

from memspine.exceptions import StorageError
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
from memspine.services.storage.records_port import (
    EntityIndexAdvance,
    EntityIndexEffects,
    EntityIndexProjectionView,
)
from memspine.store.events import EventKind, MemoryEvent, fingerprint_payload
from memspine.store.projections.projector import Projector
from memspine.store.records import MemoryRecord, RecordStatus
from memspine.trust.erasure import LINKS_KEY

__all__ = [
    "ALIAS_MARKER",
    "ENTITY_STATUSES",
    "GONE_KIND",
    "INDEX_PROJECTOR",
    "LINKS_KEY",
    "LINK_MARKER",
    "MERGE_MARKER",
    "EntityIndexProjector",
    "NameEvictionListener",
    "alias_event",
    "entity_link_event",
    "entity_names",
    "entity_status",
    "index_watermark",
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
_SOURCE_KIND = "source_state"
_BARRIER_KIND = "merge_barrier"
_BARRIER_CITE_KIND = "barrier_cite"
_PENDING_KIND = "pending_positive"
_PENDING_CITE_KIND = "pending_cite"
_PURGE_KIND = "purge_guard"


class EntityIndexProjectionIncomplete(StorageError):
    """A bounded ordered page committed, but has not reached the requested event."""


#: The event kinds the name index projects.
_INDEX_KINDS = (
    EventKind.WRITE,
    EventKind.MARKER,
    EventKind.FORGET,
    EventKind.DECAY_TRANSITION,
    EventKind.REGISTRY_ERASURE_FENCE,
)
#: Merge chains followed to a group representative (a cycle or deeper chain stops there).
_MAX_CHAIN = 16
#: RM-154: the row kind of the name-removal signal. One row per (namespace, projector
#: instance): ``key`` is the instance, ``data.count`` its removals in the namespace so far. A
#: KNOWN cursor of another process sees the count change and reads the namespace again. Each
#: instance writes only its own row (no lost update between processes); the row carries no
#: name and no entity id (ADR-095).
GONE_KIND = "gone"


class NameEvictionListener(Protocol):
    """RM-154: told by :class:`EntityIndexProjector` when name rows go (a forget, an erasure,
    a re-write that moves a record's links, a reset). ``namespace`` None is every namespace;
    ``entity_ids`` None is every entity of the namespace."""

    def evict(self, namespace: str | None, entity_ids: frozenset[str] | None) -> None: ...


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


#: :func:`_namespace_mark` of a storage without an event log (the index is all there is).
_NO_LOG = -1


async def _namespace_mark(
    storage: Any, namespace: str, projector: EntityIndexProjector | None
) -> int | None:
    """The log sequence up to which the name index provably holds every event of ``namespace``
    (None: it is behind; ``_NO_LOG`` without an event log).

    Freshness is per namespace: events of other namespaces past the mark never make it stale.
    The namespace's pending index events are fetched by the records port under a captured
    global head. At most ``_CATCH_UP_PAGE * _CATCH_UP_PAGES`` relevant events are applied
    per check; a longer backlog counts as behind. Without a projector any pending event
    of the namespace is behind."""
    if projector is not None and callable(getattr(storage, "advance_entity_index", None)):
        try:
            receipt = await projector.advance_to(namespace, 2**63 - 1)
        except Exception:
            return None
        return receipt.namespace_cursor if receipt.complete else None
    # The scan starts where this process last scanned the namespace to the head (its own
    # in-order mark is no start: another process's event may sit below it), else at the
    # durable mark (startup catch-up brought the index there).
    after = projector.caught_up.get(namespace) if projector is not None else None
    if after is None:
        try:
            after = int(await storage.get_offset(INDEX_PROJECTOR))
        except Exception:
            after = 0
    reader = getattr(storage, "read_scoped_events", None)
    if not callable(reader):
        # A reference store without an event log has only its in-memory name index.
        # A real log without the required scoped capability fails closed.
        return _NO_LOG if not callable(getattr(storage, "read_events", None)) else None
    budget = _CATCH_UP_PAGE * _CATCH_UP_PAGES
    if budget <= 0:
        return None
    try:
        pending, head = await reader(namespace, _INDEX_KINDS, after_seq=after, limit=budget + 1)
    except Exception:
        return None
    if not isinstance(head, int) or head < after or len(pending) > budget + 1:
        return None
    last = after
    for event in pending:
        if (
            event.seq is None
            or event.seq <= last
            or event.seq > head
            or event.namespace != namespace
            or event.kind not in _INDEX_KINDS
        ):
            return None
        last = event.seq
    if pending and projector is None:
        return None
    for event in pending[:budget]:
        assert projector is not None
        await projector.catch_up(event)
    if len(pending) > budget:
        if projector is not None:
            projector.mark_scanned(namespace, int(pending[budget - 1].seq or after))
        return None
    if projector is not None:
        projector.mark_scanned(namespace, head)
    return head


async def _index_complete(
    storage: Any, namespace: str, projector: EntityIndexProjector | None = None
) -> bool:
    """Is the name index complete for ``namespace`` at the log head? (A capped catch-up is
    incomplete: never an auto-link.)"""
    return await _namespace_mark(storage, namespace, projector) is not None


async def index_watermark(
    storage: Any, namespace: str, projector: EntityIndexProjector | None = None
) -> int | None:
    """RM-154 (DS-12): the name index's per-namespace projection watermark.

    Every event of ``namespace`` at or below the returned log sequence is applied to the index
    (the namespace's backlog is caught up first, as for the write path's uniqueness check), so
    every name row whose seq is at or below it is present now and no later projection adds one.
    None when that cannot be proven: the backlog is longer than the bounded catch-up, or there
    is no event log to compare with. A reader is complete only once it has read every row up
    to the watermark."""
    mark = await _namespace_mark(storage, namespace, projector)
    return None if mark is None or mark == _NO_LOG else mark


async def _representative(
    store: EntityIndexStore, namespace: str, entity_id: str, *, through_seq: int | None = None
) -> str:
    current = entity_id
    for _ in range(_MAX_CHAIN):
        kept = await store.select(namespace, "merge", ref=current)
        if through_seq is not None:
            kept = [row for row in kept if row.seq <= through_seq]
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
        rep = await _representative(
            store, namespace, entity, through_seq=before_seq - 1 if before_seq is not None else None
        )
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
        #: RM-154: in-process caches of index names (the KNOWN cursors), told of removals
        self._listeners: weakref.WeakSet[Any] = weakref.WeakSet()
        #: RM-154: this instance's key in the removal signal rows, and its removal counts
        self.instance = str(fastuuid.uuid4())
        self._removals: dict[str, int] = {}

    def add_listener(self, listener: NameEvictionListener) -> None:
        """Tell ``listener`` of every name removal this projector applies (held weakly)."""
        self._listeners.add(listener)

    async def _names_removed(self, ns: str, entity_ids: Iterable[str], seq: int | None) -> None:
        """Name rows of ``entity_ids`` went: signal it in the index (other processes) and to
        this process's listeners (evicted now, not at their next refresh)."""
        gone = frozenset(entity_ids)
        if not gone:
            return
        count = self._removals[ns] = self._removals.get(ns, 0) + 1
        await self.store.put(
            [
                EntityIndexRow(
                    ns, GONE_KIND, self.instance, "", "", data={"count": count}, seq=seq or 0
                )
            ]
        )
        for listener in list(self._listeners):
            listener.evict(ns, gone)

    @property
    def store(self) -> EntityIndexStore:
        return _store(self._storage)

    def _changed(self, namespace: str) -> None:
        entity_cache.invalidate(self._storage, namespace)

    async def apply(self, event: MemoryEvent) -> None:
        if event.seq is None:
            raise StorageError("ordered entity index requires a committed event sequence")
        receipt = await self.advance_to(event.namespace, int(event.seq))
        if not receipt.complete or receipt.namespace_cursor < event.seq:
            raise EntityIndexProjectionIncomplete(
                "entity index projection is behind its committed event"
            )

    async def advance_to(self, namespace: str, target_seq: int) -> EntityIndexAdvance:
        advance = getattr(self._storage, "advance_entity_index", None)
        if not callable(advance):
            raise StorageError("records store lacks ordered entity-index projection")

        async def apply_batch(
            view: EntityIndexProjectionView, events: Sequence[MemoryEvent]
        ) -> EntityIndexEffects:
            reducer = _EntityIndexReducer(view, self.instance)
            for pending in await view.pending_events(_CATCH_UP_PAGE * _CATCH_UP_PAGES):
                await reducer.retry_positive(pending)
            for event in events:
                await reducer._project(event)
            return reducer.effects()

        receipt: EntityIndexAdvance = await advance(
            namespace, target_seq, _CATCH_UP_PAGE * _CATCH_UP_PAGES, apply_batch
        )
        self.applied = max(self.applied, receipt.namespace_cursor)
        self.mark_scanned(namespace, receipt.namespace_cursor)
        if receipt.effects.changed:
            entity_cache.invalidate(self._storage, namespace)
        if receipt.effects.removed_entity_ids:
            gone = frozenset(receipt.effects.removed_entity_ids)
            for listener in list(self._listeners):
                listener.evict(namespace, gone)
        return receipt

    def mark_scanned(self, namespace: str, seq: int) -> None:
        """The freshness check scanned ``namespace`` up to ``seq``: own seqs at or below it
        are behind the scan mark now."""
        self.caught_up[namespace] = max(self.caught_up.get(namespace, 0), seq)
        own = self._own.get(namespace)
        if own:
            own.difference_update({s for s in own if s <= seq})

    async def catch_up(self, event: MemoryEvent) -> None:
        """Route catch-up through the same canonical ordered storage door as direct apply."""
        await self.apply(event)

    async def _project(self, event: MemoryEvent) -> None:
        if event.kind is EventKind.WRITE:
            await self._write(event)
        elif event.kind is EventKind.FORGET:
            await self._forget(
                event.namespace,
                str(event.payload.get("record_id", "")),
                hard=bool(event.payload.get("hard")),
                seq=event.seq,
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
        await self._names_removed(ns, await self._drop_orphans(ns, previous - linked), seq)
        self._changed(ns)

    async def _drop_orphans(self, ns: str, entity_ids: Iterable[str]) -> list[str]:
        """Remove each entity no record links to any more (with its names and merges); returns
        the removed ids."""
        store = self.store
        dropped: list[str] = []
        for entity_id in sorted(entity_ids):
            if await store.select(ns, "link", entity_id=entity_id):
                continue
            dropped.append(entity_id)
            await store.delete(ns, "entity", key=entity_id)
            await store.delete(ns, "name", entity_id=entity_id)
            for row in [
                *await store.select(ns, "merge", entity_id=entity_id),
                *await store.select(ns, "merge", ref=entity_id),
            ]:
                await store.delete(ns, "merge", key=row.key)
        return dropped

    async def _forget(
        self, ns: str, record_id: str, *, hard: bool = False, seq: int | None = None
    ) -> None:
        if not record_id:
            return
        store = self.store
        removed: set[str] = set()
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
                    removed.add(alias.entity_id)
                    await self._contest(ns, [alias.entity_id])
                await store.delete(ns, "cite", entity_id=cite.entity_id, ref="alias")
        removed.update(await self._drop_orphans(ns, previous))
        await self._names_removed(ns, removed, seq)
        self._changed(ns)

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
        self._changed(ns)

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
        self._changed(ns)

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
        keeper = await _entity_row(store, ns, target)
        seq = int(event.seq or 0)
        if (
            entity is None
            or entity.data.get("basis") not in _RELINKABLE
            or keeper is None
            or entity.seq > seq
            or keeper.seq > seq
        ):
            return
        row = EntityIndexRow(
            ns,
            "merge",
            link_id,
            target,
            entity_id,
            data={"link": True, "basis": "unique_name"},
            seq=seq,
        )
        existing = await store.select(ns, "merge", ref=entity_id)
        if any(r.seq <= seq for r in existing):
            return  # an earlier link or cited merge already settled this entity
        if any(r.pk == row.pk and r.data.get("link") for r in existing):
            # An identical later link raced ahead of this one. The store keeps
            # the first eligible event's seq atomically across peer Engines.
            await store.put([row])
            self._changed(ns)
            return
        if await _representative(store, ns, target, through_seq=seq) == entity_id:
            return
        await store.put([row])
        self._changed(ns)

    async def purge_records(self, ns: str, record_ids: Iterable[str]) -> None:
        """Erasure's direct purge (records removed outside the projector)."""
        ids = tuple(dict.fromkeys(rid for rid in record_ids if rid))
        if not ids:
            return
        purge = getattr(self._storage, "purge_entity_index", None)
        if not callable(purge):
            raise StorageError("records store lacks ordered entity-index purge")

        async def apply_purge(
            view: EntityIndexProjectionView, batch: Sequence[str]
        ) -> EntityIndexEffects:
            reducer = _EntityIndexReducer(view, self.instance)
            await view.put([EntityIndexRow(ns, _PURGE_KIND, rid, "", seq=0) for rid in batch])
            reducer._did_change = True
            for record_id in batch:
                await reducer._forget(ns, record_id, hard=True)
            return reducer.effects()

        receipt: EntityIndexAdvance = await purge(ns, ids, apply_purge)
        if receipt.effects.changed:
            entity_cache.invalidate(self._storage, ns)
        if receipt.effects.removed_entity_ids:
            gone = frozenset(receipt.effects.removed_entity_ids)
            for listener in list(self._listeners):
                listener.evict(ns, gone)

    async def reset(self) -> None:
        reset = getattr(self._storage, "reset_entity_index", None)
        if not callable(reset):
            raise StorageError("records store lacks ordered entity-index reset")
        await reset()
        self.applied = 0
        self.caught_up.clear()
        self._own.clear()
        entity_cache.invalidate(self._storage)
        for listener in list(self._listeners):
            listener.evict(None, None)


class _EntityIndexReducer(EntityIndexProjector):
    """One retry-pure fold, explicitly bound to the storage transaction's view."""

    def __init__(self, view: EntityIndexProjectionView, instance: str) -> None:
        super().__init__(view)
        self._view = view
        self.instance = instance
        self._did_change = False
        self._removed: set[str] = set()

    @property
    def store(self) -> EntityIndexStore:
        return cast(EntityIndexStore, self._view)

    def _changed(self, namespace: str) -> None:
        self._did_change = True

    async def _names_removed(self, ns: str, entity_ids: Iterable[str], seq: int | None) -> None:
        gone = frozenset(entity_ids)
        if not gone:
            return
        self._removed.update(gone)
        previous = await self._view.select(ns, GONE_KIND, key=self.instance)
        count = int(previous[0].data.get("count", 0)) + 1 if previous else 1
        await self._view.put(
            [
                EntityIndexRow(
                    ns, GONE_KIND, self.instance, "", "", data={"count": count}, seq=seq or 0
                )
            ]
        )
        self._did_change = True

    def effects(self) -> EntityIndexEffects:
        return EntityIndexEffects(self._did_change, tuple(sorted(self._removed)))

    async def _contest(self, ns: str, entity_ids: Iterable[str]) -> None:
        await super()._contest(ns, entity_ids)
        self._did_change = True

    async def _source_live(self, namespace: str, record_id: str) -> bool:
        rows = await self._view.select(namespace, _SOURCE_KIND, key=record_id)
        return bool(rows and rows[0].data.get("live"))

    async def _set_source(
        self,
        namespace: str,
        record_id: str,
        live: bool,
        seq: int,
        *,
        status: str = "",
        quarantined: bool = False,
        gone: bool = False,
    ) -> None:
        if not record_id:
            return
        await self._view.put(
            [
                EntityIndexRow(
                    namespace,
                    _SOURCE_KIND,
                    record_id,
                    "",
                    data={
                        "live": live,
                        "status": status,
                        "quarantined": quarantined,
                        "gone": gone,
                    },
                    seq=seq,
                )
            ]
        )
        self._did_change = True

    async def _project(self, event: MemoryEvent) -> None:
        if event.kind is EventKind.DECAY_TRANSITION:
            record_id = event.payload.get("record_id")
            delta = event.payload.get("set")
            if (
                isinstance(record_id, str)
                and isinstance(delta, dict)
                and ("status" in delta or "quarantined" in delta)
            ):
                rows = await self._view.select(event.namespace, _SOURCE_KIND, key=record_id)
                if rows and not rows[0].data.get("gone"):
                    status = str(delta.get("status", rows[0].data.get("status", "")))
                    quarantined = bool(
                        delta.get("quarantined", rows[0].data.get("quarantined", False))
                    )
                    live = (
                        status
                        in {
                            RecordStatus.ACTIVATED.value,
                            RecordStatus.ARCHIVED.value,
                        }
                        and not quarantined
                    )
                    await self._set_source(
                        event.namespace,
                        record_id,
                        live,
                        int(event.seq or 0),
                        status=status,
                        quarantined=quarantined,
                    )
            return
        if event.kind is EventKind.REGISTRY_ERASURE_FENCE:
            raw = event.payload.get("source_ids")
            if isinstance(raw, list):
                for record_id in raw:
                    if isinstance(record_id, str) and record_id:
                        await self._forget(event.namespace, record_id, hard=True, seq=event.seq)
            return
        await super()._project(event)

    async def _write(self, event: MemoryEvent) -> None:
        raw = event.payload.get("record")
        if not isinstance(raw, dict):
            return
        record_id = raw.get("record_id")
        if not isinstance(record_id, str) or not record_id:
            return
        if raw.get("namespace") != event.namespace:
            return
        if await self._view.select(event.namespace, _PURGE_KIND, key=record_id):
            await self._set_source(
                event.namespace, record_id, False, int(event.seq or 0), gone=True
            )
            return
        live = raw.get("status") in {
            RecordStatus.ACTIVATED.value,
            RecordStatus.ARCHIVED.value,
        } and not bool(raw.get("quarantined", False))
        await self._set_source(
            event.namespace,
            record_id,
            live,
            int(event.seq or 0),
            status=str(raw.get("status", "")),
            quarantined=bool(raw.get("quarantined", False)),
        )
        await super()._write(event)

    async def _forget(
        self, ns: str, record_id: str, *, hard: bool = False, seq: int | None = None
    ) -> None:
        await self._set_source(ns, record_id, False, int(seq or 0), gone=True)
        for cite in await self._view.select(ns, _PENDING_CITE_KIND, key=record_id):
            pending = await self._view.select(ns, _PENDING_KIND, key=cite.entity_id)
            if pending and hard and pending[0].ref == "alias":
                # Hard erasure scrubs the alias marker's text even if another
                # citation survives. A pending retry must not materialize the
                # old text in the gap before the historical marker is scrubbed.
                await self._contest_pending(ns, pending[0])
                await self._clear_pending_seq(ns, cite.entity_id)
                continue
            remaining = False
            for rid in pending[0].data.get("evidence", []) if pending else []:
                if await self._source_live(ns, rid):
                    remaining = True
                    break
            if pending and not remaining:
                await self._contest_pending(ns, pending[0])
                await self._clear_pending_seq(ns, cite.entity_id)
        citations = await self._view.select(ns, _BARRIER_CITE_KIND, key=record_id)
        await self._view.delete(ns, _BARRIER_CITE_KIND, key=record_id)
        for cite in citations:
            if not await self._view.select(ns, _BARRIER_CITE_KIND, entity_id=cite.entity_id):
                await self._view.delete(ns, _BARRIER_KIND, key=cite.entity_id)
        await super()._forget(ns, record_id, hard=hard, seq=seq)

    async def _clear_pending_seq(self, namespace: str, key: str) -> None:
        if await self._view.select(namespace, _PENDING_KIND, key=key):
            await self._view.delete(namespace, _PENDING_KIND, key=key)
            cites = await self._view.select(namespace, _PENDING_CITE_KIND, entity_id=key)
            for cite in cites:
                await self._view.delete(namespace, _PENDING_CITE_KIND, key=cite.key, entity_id=key)
            self._did_change = True

    async def _clear_pending(self, event: MemoryEvent) -> None:
        await self._clear_pending_seq(event.namespace, str(event.seq or 0))

    async def _contest_pending(self, namespace: str, row: EntityIndexRow) -> None:
        if row.ref == "merge":
            await self._contest(
                namespace, [str(row.data.get("keep", "")), str(row.data.get("absorb", ""))]
            )
        elif row.ref == "alias":
            await self._contest(namespace, [str(row.data.get("entity_id", ""))])

    async def _contest_positive(self, event: MemoryEvent) -> None:
        payload = event.payload
        if payload.get("marker") == MERGE_MARKER:
            await self._contest(
                event.namespace, [str(payload.get("keep", "")), str(payload.get("absorb", ""))]
            )
        elif payload.get("marker") == ALIAS_MARKER:
            await self._contest(event.namespace, [str(payload.get("entity_id", ""))])

    async def _positive_ready(self, event: MemoryEvent) -> bool | None:
        """True if every prefix-live citation has a lawful current read-model row.

        None means the marker needs a later bounded retry. False means canonical
        prefix state already rules out a positive identity effect.
        """
        evidence = self._evidence(event.payload)
        if not evidence:
            return False
        live = [rid for rid in evidence if await self._source_live(event.namespace, rid)]
        if not live:
            return False
        for record_id in live:
            record = await self._view.get_record(record_id)
            if record is None:
                return None
            if (
                record.namespace != event.namespace
                or record.status not in {RecordStatus.ACTIVATED, RecordStatus.ARCHIVED}
                or bool(getattr(record, "quarantined", False))
            ):
                return None
        return True

    async def _materialize_positive(self, event: MemoryEvent, *, retry: bool) -> None:
        marker = event.payload.get("marker")
        if marker not in {MERGE_MARKER, ALIAS_MARKER}:
            await self._clear_pending(event)
            return
        if marker == ALIAS_MARKER and not canonical_entity(str(event.payload.get("alias", ""))):
            await self._clear_pending(event)
            if event.payload.get("redacted"):
                await self._contest_positive(event)
            return
        ready = await self._positive_ready(event)
        if ready is None:
            if not retry:
                seq = int(event.seq or 0)
                marker_id = str(
                    event.payload.get("merge_id" if marker == MERGE_MARKER else "alias_id", "")
                )
                await self._view.put(
                    [
                        EntityIndexRow(
                            event.namespace,
                            _PENDING_KIND,
                            str(seq),
                            marker_id,
                            "merge" if marker == MERGE_MARKER else "alias",
                            data={
                                "evidence": self._evidence(event.payload),
                                "keep": str(event.payload.get("keep", "")),
                                "absorb": str(event.payload.get("absorb", "")),
                                "entity_id": str(event.payload.get("entity_id", "")),
                            },
                            seq=seq,
                        ),
                        *[
                            EntityIndexRow(
                                event.namespace, _PENDING_CITE_KIND, rid, str(seq), seq=seq
                            )
                            for rid in self._evidence(event.payload)
                        ],
                    ]
                )
                self._did_change = True
            return
        await self._clear_pending(event)
        if not ready:
            await self._contest_positive(event)
            return
        if marker == MERGE_MARKER:
            await super()._merge(event)
        else:
            await super()._alias(event)

    async def retry_positive(self, event: MemoryEvent) -> None:
        await self._materialize_positive(event, retry=True)

    async def _merge(self, event: MemoryEvent) -> None:
        payload, ns = event.payload, event.namespace
        keep, absorb = payload.get("keep"), payload.get("absorb")
        merge_id = payload.get("merge_id")
        evidence = self._evidence(payload)
        if (
            isinstance(keep, str)
            and keep
            and isinstance(absorb, str)
            and absorb
            and keep != absorb
            and isinstance(merge_id, str)
            and merge_id
            and evidence
        ):
            live = [rid for rid in evidence if await self._source_live(ns, rid)]
            if live:
                seq = int(event.seq or 0)
                await self._view.put(
                    [
                        EntityIndexRow(ns, _BARRIER_KIND, merge_id, absorb, keep, seq=seq),
                        *[
                            EntityIndexRow(ns, _BARRIER_CITE_KIND, rid, merge_id, absorb, seq=seq)
                            for rid in live
                        ],
                    ]
                )
                self._did_change = True
        await self._materialize_positive(event, retry=False)

    async def _alias(self, event: MemoryEvent) -> None:
        await self._materialize_positive(event, retry=False)

    async def _link(self, event: MemoryEvent) -> None:
        absorb = event.payload.get("entity_id")
        if isinstance(absorb, str) and await self._view.select(
            event.namespace, _BARRIER_KIND, entity_id=absorb
        ):
            return
        await super()._link(event)
