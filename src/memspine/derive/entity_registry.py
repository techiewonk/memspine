"""Entity registry with reversible MERGE and SPLIT events (W2-09; decisions M-15, W-19).

An entity is an opaque id plus a name, a type, an alias list and a scope. The registry is keyed by a
typed scope key (tenant + owner), so
an entity of one owner is never a candidate for another's mention and ``merge`` refuses two entities
that are not in the same scope.

Every change is a small JSON payload (``entity.create``, ``entity.alias``, ``entity.pending``,
``entity.merge``, ``entity.split``) that the caller appends to the event log as a ``memory.marker``
event; :meth:`EntityRegistry.apply` is the only mutator, so a registry rebuilt from the log (a replay
through F-16b migrate copies every event, markers included) equals the live one. A merge keeps the
dropped entity (``merged_into`` points at the survivor) and records exactly the aliases it added, so one
SPLIT event undoes it without touching anything else.

Pure: no I/O, no model call. The registry only queues payloads; :meth:`drain` hands them over.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from memspine.store.registry_evidence import (
    ENTITY_MARKERS,
    scrub_registry_marker,
    valid_source_refs,
)

__all__ = [
    "ENTITY_MARKERS",
    "Entity",
    "EntityRegistry",
    "MergeRecord",
    "ScopeKey",
]

ScopeKey = tuple[str, ...]

#: ``payload["marker"]`` values this registry writes.


@dataclass
class Entity:
    entity_id: str
    name: str
    type: str = ""
    context: str = ""
    aliases: list[str] = field(default_factory=list)
    lang: str | None = None
    #: the survivor of a merge, None while this entity is live
    merged_into: str | None = None
    #: entities this one might be the same as (a grey-zone resolution), never merged online
    pending_with: list[str] = field(default_factory=list)
    # Each source-bound mention is independent support for its own label and context.
    contributions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.merged_into is None

    def names(self) -> list[str]:
        return [n for n in [self.name, *self.aliases] if n]


@dataclass
class MergeRecord:
    merge_id: str
    key: ScopeKey
    keep: str
    drop: str
    added_aliases: list[str]
    reason: str = ""
    reverted: bool = False


class EntityRegistry:
    """Scope-keyed entities, MERGE/SPLIT events, and replay from payloads."""

    def __init__(self) -> None:
        self._ents: dict[ScopeKey, dict[str, Entity]] = {}
        self._merges: dict[str, MergeRecord] = {}
        self._queue: list[dict[str, Any]] = []
        self._history: list[dict[str, Any]] = []

    # --- read ------------------------------------------------------------------------------------
    def get(self, key: ScopeKey, entity_id: str) -> Entity | None:
        return self._ents.get(key, {}).get(entity_id)

    def scopes(self) -> list[ScopeKey]:
        """Every scope that holds at least one entity."""
        return list(self._ents)

    def active(self, key: ScopeKey) -> list[Entity]:
        """The live entities of one scope, in creation order."""
        return [e for e in self._ents.get(key, {}).values() if e.active and e.names()]

    def count(self, key: ScopeKey) -> int:
        return len(self._ents.get(key, {}))

    def canonical(self, key: ScopeKey, entity_id: str) -> str:
        """Follow ``merged_into`` to the live entity (the id itself when live or unknown)."""
        seen: set[str] = set()
        cur = entity_id
        ents = self._ents.get(key, {})
        while cur in ents and ents[cur].merged_into and cur not in seen:
            seen.add(cur)
            cur = ents[cur].merged_into  # type: ignore[assignment]
        return cur

    def merge_record(self, merge_id: str) -> MergeRecord | None:
        return self._merges.get(merge_id)

    def pending(self, key: ScopeKey) -> list[Entity]:
        """Live entities waiting for a decision (the sleep reconciliation reads these)."""
        return [e for e in self.active(key) if e.pending_with]

    # --- change (each queues one payload and applies it) ----------------------------------------
    def create(
        self,
        key: ScopeKey,
        entity_id: str,
        name: str,
        *,
        type: str = "",
        context: str = "",
        lang: str | None = None,
        pending_with: Iterable[str] = (),
        source_refs: Iterable[tuple[str, int]] = (),
        namespace: str | None = None,
    ) -> Entity:
        if self.get(key, entity_id) is not None:
            raise ValueError(f"entity {entity_id!r} already exists in this scope")
        refs = list(source_refs)
        self._do(
            {
                "marker": "entity.create",
                "scope": list(key),
                "entity": entity_id,
                "name": name,
                "type": type,
                "context": context,
                "lang": lang,
                "pending_with": sorted(pending_with),
                "source_ids": [rid for rid, _ in refs],
                "source_versions": [version for _, version in refs],
            },
            namespace=namespace,
        )
        return self._ents[key][entity_id]

    def add_alias(
        self,
        key: ScopeKey,
        entity_id: str,
        alias: str,
        *,
        context: str = "",
        source_refs: Iterable[tuple[str, int]] = (),
        namespace: str | None = None,
    ) -> bool:
        """Add an alias (and fold in a little context); False when nothing changed."""
        ent = self.get(key, entity_id)
        if ent is None or not ent.active:
            return False
        new_alias = alias != ent.name and alias not in ent.aliases
        merged_ctx = _join_context(ent.context, context)
        refs = list(source_refs)
        if not new_alias and merged_ctx == ent.context and not refs:
            return False
        self._do(
            {
                "marker": "entity.alias",
                "scope": list(key),
                "entity": entity_id,
                "alias": alias if new_alias else "",
                "context": context if refs else merged_ctx,
                "surface": alias if refs else "",
                "source_ids": [rid for rid, _ in refs],
                "source_versions": [version for _, version in refs],
            },
            namespace=namespace,
        )
        return True

    def mark_pending(
        self,
        key: ScopeKey,
        entity_id: str,
        candidates: Iterable[str],
        *,
        namespace: str | None = None,
    ) -> None:
        self._do(
            {
                "marker": "entity.pending",
                "scope": list(key),
                "entity": entity_id,
                "pending_with": sorted(candidates),
            },
            namespace=namespace,
        )

    def merge(
        self,
        key: ScopeKey,
        keep: str,
        drop: str,
        *,
        reason: str = "",
        namespace: str | None = None,
    ) -> str:
        """Merge ``drop`` into ``keep`` (same scope, both live); returns the merge id (the SPLIT handle)."""
        a, b = self.get(key, keep), self.get(key, drop)
        if a is None or b is None:
            raise ValueError("both entities must exist in the same scope to merge")
        if keep == drop or not a.active or not b.active:
            raise ValueError("a merge needs two different live entities")
        merge_id = f"mrg_{len(self._merges) + 1:06d}_{keep[-6:]}_{drop[-6:]}"
        self._do(
            {
                "marker": "entity.merge",
                "scope": list(key),
                "merge": merge_id,
                "keep": keep,
                "drop": drop,
                "reason": reason if namespace is None else "",
            },
            namespace=namespace,
        )
        return merge_id

    def split(self, merge_id: str, *, namespace: str | None = None) -> bool:
        """Undo one merge: the dropped entity is live again, the aliases the merge added are removed."""
        rec = self._merges.get(merge_id)
        if rec is None or rec.reverted:
            return False
        self._do(
            {"marker": "entity.split", "scope": list(rec.key), "merge": merge_id},
            namespace=namespace,
        )
        return True

    # --- events ----------------------------------------------------------------------------------
    def queued(self, namespace: str | None = None) -> list[dict[str, Any]]:
        """Pending payloads for one authoritative event namespace (all if omitted)."""
        return [
            p for p in self._queue if namespace is None or p.get("_queue_namespace") == namespace
        ]

    def drain(self, namespace: str | None = None) -> list[dict[str, Any]]:
        """The payloads queued since the last call (append each as a ``memory.marker`` event)."""
        selected = self.queued(namespace)
        selected_ids = {id(p) for p in selected}
        self._queue = [p for p in self._queue if id(p) not in selected_ids]
        return [
            {k: v for k, v in p.items() if k not in {"_queue_namespace", "_event_namespace"}}
            for p in selected
        ]

    def apply(self, payload: Mapping[str, Any], *, namespace: str | None = None) -> None:
        """The only mutator: apply one payload (live changes and replay take this same path)."""
        stored = deepcopy(dict(payload))
        if namespace is not None:
            stored["_event_namespace"] = namespace
        self._history.append(stored)
        marker = payload.get("marker")
        key: ScopeKey = tuple(str(p) for p in payload.get("scope", ()))
        ents = self._ents.setdefault(key, {})
        if marker == "entity.create":
            eid = str(payload["entity"])
            ents[eid] = Entity(
                entity_id=eid,
                name=str(payload["name"]),
                type=str(payload.get("type", "")),
                context=str(payload.get("context", "")),
                lang=payload.get("lang"),
                pending_with=[str(p) for p in payload.get("pending_with", ())],
            )
            if payload.get("source_ids"):
                ents[eid].contributions.append(dict(payload))
        elif marker == "entity.alias":
            ent = ents.get(str(payload["entity"]))
            if ent is not None:
                if payload.get("source_ids"):
                    ent.contributions.append(dict(payload))
                alias = str(payload.get("alias", ""))
                surface = str(payload.get("surface", ""))
                if not ent.name and surface:
                    ent.name = surface
                    alias = ""
                elif surface and surface != ent.name and surface not in ent.aliases:
                    alias = surface
                if alias and alias not in ent.aliases and alias != ent.name:
                    ent.aliases.append(alias)
                context = str(payload.get("context", ""))
                ent.context = (
                    _join_context(ent.context, context)
                    if payload.get("source_ids")
                    else context or ent.context
                )
        elif marker == "entity.pending":
            ent = ents.get(str(payload["entity"]))
            if ent is not None:
                ent.pending_with = [str(p) for p in payload.get("pending_with", ())]
        elif marker == "entity.merge":
            keep, drop = ents.get(str(payload["keep"])), ents.get(str(payload["drop"]))
            if keep is None or drop is None:
                return
            have = {name.casefold() for name in keep.names()}
            added = [name for name in drop.names() if name.casefold() not in have]
            for alias in added:
                if alias not in keep.aliases and alias != keep.name:
                    keep.aliases.append(alias)
            drop.merged_into = keep.entity_id
            keep.pending_with = [p for p in keep.pending_with if p != drop.entity_id]
            self._merges[str(payload["merge"])] = MergeRecord(
                str(payload["merge"]),
                key,
                keep.entity_id,
                drop.entity_id,
                added,
                str(payload.get("reason", "")),
            )
        elif marker == "entity.split":
            rec = self._merges.get(str(payload["merge"]))
            if rec is None or rec.reverted:
                return
            keep, drop = ents.get(rec.keep), ents.get(rec.drop)
            if keep is not None:
                independently_supported = {
                    str(contribution.get("surface") or contribution.get("name") or "").casefold()
                    for contribution in keep.contributions
                    if contribution.get("source_ids")
                    and (contribution.get("surface") or contribution.get("name"))
                }
                keep.aliases = [
                    alias
                    for alias in keep.aliases
                    if alias not in rec.added_aliases or alias.casefold() in independently_supported
                ]
            if drop is not None:
                drop.merged_into = None
            rec.reverted = True

    def _do(self, payload: dict[str, Any], *, namespace: str | None = None) -> None:
        if namespace is not None:
            payload["_queue_namespace"] = namespace
        self.apply(payload, namespace=namespace)
        self._queue.append(self._history[-1])

    def forget_sources(self, source_ids: Iterable[str], *, namespace: str | None = None) -> None:
        """Discard erased contributions and rebuild the warm state from sanitized markers."""
        erased = set(source_ids)
        for payload in self._history:
            if namespace is None or payload.get("_event_namespace") == namespace:
                scrub_registry_marker(payload, erased)
        self._rebuild_from_history()

    def forget_pending_unattributed(self, namespace: str) -> None:
        """Prevent a queued source-free mention from becoming durable evidence."""
        for payload in self.queued(namespace):
            if payload.get("marker") in {"entity.create", "entity.alias"} and not valid_source_refs(
                payload
            ):
                for field_name in ("name", "alias", "surface", "context"):
                    if field_name in payload:
                        payload[field_name] = ""
        self._rebuild_from_history()

    def forget_pending_sources(self, source_ids: Iterable[str], namespace: str) -> None:
        """Reject stale queued contributions without rewriting committed history."""
        erased = set(source_ids)
        for payload in self.queued(namespace):
            scrub_registry_marker(payload, erased)
        self._rebuild_from_history()

    def _rebuild_from_history(self) -> None:
        pending_ids = {id(payload) for payload in self._queue}
        pending_indexes = {
            index for index, payload in enumerate(self._history) if id(payload) in pending_ids
        }
        self._ents = {}
        self._merges = {}
        self._queue = []
        history, self._history = self._history, []
        for index, payload in enumerate(history):
            self.apply(payload)
            if index in pending_indexes:
                self._queue.append(self._history[-1])

    @classmethod
    def replay(cls, payloads: Iterable[Mapping[str, Any]]) -> EntityRegistry:
        """Rebuild a registry from logged payloads (non-entity payloads are skipped)."""
        reg = cls()
        for payload in payloads:
            if payload.get("marker") in ENTITY_MARKERS:
                reg.apply(payload)
        return reg

    def snapshot(self) -> dict[ScopeKey, dict[str, tuple[Any, ...]]]:
        """A comparable view of the state (tests: live equals replayed)."""
        return {
            k: {
                i: (
                    e.name,
                    e.type,
                    e.context,
                    tuple(e.aliases),
                    e.merged_into,
                    tuple(e.pending_with),
                )
                for i, e in v.items()
            }
            for k, v in self._ents.items()
        }


def _join_context(old: str, new: str, cap: int = 400) -> str:
    """Append a context line unless it is already there; keep the first ``cap`` characters."""
    new = new.strip()
    if not new or new in old:
        return old
    return (f"{old} {new}" if old else new)[:cap]
