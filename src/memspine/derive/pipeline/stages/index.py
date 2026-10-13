"""Derive: stage markers and the incremental log index shared by the derived stages (F-15)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from memspine.memories.associative.entities import canonical_entity
from memspine.memories.associative.resolution import MERGE_METHODS
from memspine.store.events import EventKind, MemoryEvent, fingerprint_payload
from memspine.workers.list_cards import LIST_CLASSES_MARKER

if TYPE_CHECKING:
    from memspine.derive.pipeline.stages.context import PipelineStorage


#: KB-12: MARKER payload carrying one namespace's community partition as a
#: delta (``set``: node -> anchor, ``drop``: nodes gone) plus the incremental
#: refresh counters. Written only with ``community.incremental``.
COMMUNITY_PARTITION_MARKER = "community_partition"


#: GP-8a: MARKER payload naming the sources one extract_graph sweep already sent
#: to the LLM, with each source's content fingerprint at the time. ``sources`` is
#: a list of ``{"record_id", "content_fingerprint"}`` dicts, the snapshot shape
#: the M7 erasure walker scrubs, so a hard forget also erases the fingerprint
#: (logs written before carry a ``{record_id: fingerprint}`` map, still read).
GRAPH_EXTRACTED_MARKER = "graph_extracted"


def _graph_marker_sources(raw: object) -> list[tuple[str, str]]:
    """``(record_id, fingerprint)`` pairs of a ``graph_extracted`` marker, either
    shape; a redacted entry (empty fingerprint) is no watermark."""
    pairs: list[tuple[str, str]] = []
    if isinstance(raw, dict):  # pre-erasure-fix shape: {record_id: fingerprint}
        pairs = [(str(rid), str(fp)) for rid, fp in raw.items()]
    elif isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict) and entry.get("record_id"):
                fp = entry.get("content_fingerprint")
                pairs.append((str(entry["record_id"]), str(fp) if fp else ""))
    return [(rid, fp) for rid, fp in pairs if fp]


#: GP-7 (#18): MARKER payload carrying one extract_graph sweep's entity-resolution
#: decisions. ``decisions`` is a list of ``{"record_id", "namespace", "entity",
#: "attribute", "method"}`` dicts: the name (``entity``), the entity it resolved to
#: or, for ``contested``, the one it was kept apart from (``attribute``), keyed by
#: the source record that named it. These are the record-snapshot keys the M7
#: erasure walker scrubs, so erasing the source erases the decision too. A
#: session-level fact cites several turns (#20): its decision is one entry per
#: cited turn, sharing a ``group`` index, and the alias holds only while every
#: entry of the group is intact — erasing ANY cited turn erases the decision.
ENTITY_RESOLVED_MARKER = "entity_resolved"


#: GP-6 (#17): MARKER payload naming the entities one summarize_entities sweep
#: summarised. ``entities`` is a list of ``{"record_id", "namespace", "entity",
#: "content_fingerprint"}`` dicts: the summary record, the entity node and the
#: membership fingerprint it was written from. Erasing the summary (a member's
#: hard forget cascades to it) scrubs the entry, so the entity is summarised again.
ENTITY_SUMMARIZED_MARKER = "entity_summarized"


def _erasable_entries(raw: object) -> list[dict[str, Any]]:
    return [entry for entry in raw if isinstance(entry, dict)] if isinstance(raw, list) else []


#: The background stages that derive records from consolidated sessions and
#: keep a per-session done marker (R2-6/R5-2) in the event log.
DERIVED_STAGES = ("mine_facts", "anticipate", "reflect_profile")


def _members_fp(record_ids: list[str]) -> str:
    return fingerprint_payload({"members": sorted(record_ids)})


def stage_marker(
    namespace: str,
    stage: str,
    session_key: str,
    *,
    cleared: bool = False,
    members_fp: str | None = None,
) -> MemoryEvent:
    """A ``stage_done`` (or ``stage_cleared``) MARKER event for one session.

    The marker lives in the log, so it survives replay and rebuild like the
    CONSOLIDATE event it refers to. ``Engine.repair_taint`` appends the cleared
    form so a repaired session is processed again from its clean members.
    ``members_fp`` fingerprints the live members the stage actually read.
    """
    payload: dict[str, object] = {
        "marker": "stage_cleared" if cleared else "stage_done",
        "stage": stage,
        "session_key": session_key,
    }
    if members_fp is not None:
        payload["members_fp"] = members_fp
    return MemoryEvent(kind=EventKind.MARKER, namespace=namespace, actor="system", payload=payload)


@dataclass
class CommunityState:
    """KB-12 incremental community state of one namespace (from the log)."""

    #: node -> anchor (the smallest member of its community).
    anchors: dict[str, str] = field(default_factory=dict)
    #: Sleeps since the last full refresh.
    sleeps: int = 0
    #: Nodes placed incrementally since the last full refresh.
    placed: int = 0


@dataclass
class SessionIndex:
    """Consolidated sessions and stage markers, read from the log incrementally.

    One index lives on a :class:`PipelineContext`, so the three derived stages
    of a sleep cycle share one pass over the log (R2-12): each ``refresh`` only
    reads events past the last seq it saw. A session key seen twice (repair
    re-consolidates under the same key) keeps its latest membership.

    Done-ness is also tracked per live-membership fingerprint: after a repair,
    the cleared session and the fresh consolidation of the same clean turns are
    one unit of work, so the LLM sees those turns once, not twice.
    """

    after_seq: int = 0
    sessions: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    #: (stage, namespace, session_key) -> True (done) / False (cleared).
    markers: dict[tuple[str, str, str], bool] = field(default_factory=dict)
    #: (stage, namespace, session_key) -> membership fingerprint of its done marker.
    key_fp: dict[tuple[str, str, str], str] = field(default_factory=dict)
    #: (stage, namespace, fingerprint) -> session keys done with that membership.
    done_fp: dict[tuple[str, str, str], set[str]] = field(default_factory=dict)
    #: GP-8a: (namespace, source record id) -> content fingerprint the
    #: extract_graph stage already sent to the LLM (``graph_extracted`` markers).
    graph_sources: dict[tuple[str, str], str] = field(default_factory=dict)
    #: KB-12: namespace -> community partition + refresh counters, folded from
    #: ``community_partition`` markers (``community.incremental``).
    communities: dict[str, CommunityState] = field(default_factory=dict)
    #: #30: (namespace, fact record id) -> the LLM list class ("" = none), folded
    #: from ``list_classes`` markers so a fact is classed once.
    list_classes: dict[tuple[str, str], str] = field(default_factory=dict)
    #: GP-7 (#18): namespace -> {alias canonical: target canonical}, folded from
    #: the merge decisions of ``entity_resolved`` markers.
    entity_aliases: dict[str, dict[str, str]] = field(default_factory=dict)
    #: GP-6 (#17): (namespace, entity node id) -> membership fingerprint the
    #: ``summarize_entities`` stage last summarised (``entity_summarized`` markers).
    entity_summaries: dict[tuple[str, str], str] = field(default_factory=dict)

    async def refresh(self, storage: PipelineStorage) -> None:
        while True:
            events = await storage.read_events(after_seq=self.after_seq, limit=1000)
            if not events:
                return
            for event in events:
                self.observe(event)
            self.after_seq = max([self.after_seq, *(e.seq for e in events if e.seq is not None)])

    def observe(self, event: MemoryEvent) -> None:
        payload = event.payload or {}
        if event.kind is EventKind.FORGET:
            # M7: a forgotten source keeps no watermark (its fingerprint is erased).
            self.graph_sources.pop((event.namespace, str(payload.get("record_id", ""))), None)
            self.list_classes.pop((event.namespace, str(payload.get("record_id", ""))), None)
        elif event.kind is EventKind.CONSOLIDATE:
            key = str(payload.get("session_key", ""))
            # #56: a summary of a still-open session is not a consolidated
            # session yet; the derived stages wait for its closing CONSOLIDATE.
            if key and not payload.get("open_session"):
                members = [str(m) for m in payload.get("member_record_ids", [])]
                self.sessions[(event.namespace, key)] = members
        elif event.kind is EventKind.MARKER:
            marker = payload.get("marker")
            if marker == GRAPH_EXTRACTED_MARKER:
                for record_id, source_fp in _graph_marker_sources(payload.get("sources")):
                    self.graph_sources[(event.namespace, record_id)] = source_fp
            elif marker == COMMUNITY_PARTITION_MARKER:
                self._fold_partition(event.namespace, payload)
            elif marker == LIST_CLASSES_MARKER and isinstance(payload.get("labels"), dict):
                for record_id, label in payload["labels"].items():
                    self.list_classes[(event.namespace, str(record_id))] = str(label)
            elif marker == ENTITY_RESOLVED_MARKER:
                self._fold_resolutions(event.namespace, payload.get("decisions"))
            elif marker == ENTITY_SUMMARIZED_MARKER:
                for entry in _erasable_entries(payload.get("entities")):
                    entity, fp = entry.get("entity"), entry.get("content_fingerprint")
                    if entity and fp:  # a redacted entry (erased summary) is no watermark
                        self.entity_summaries[(event.namespace, str(entity))] = str(fp)
            elif marker in ("stage_done", "stage_cleared"):
                fp = payload.get("members_fp")
                self.mark(
                    str(payload.get("stage", "")),
                    event.namespace,
                    str(payload.get("session_key", "")),
                    done=marker == "stage_done",
                    members_fp=str(fp) if fp else None,
                )

    def _fold_resolutions(self, namespace: str, raw: object) -> None:
        aliases = self.entity_aliases.setdefault(namespace, {})
        # Entries sharing a ``group`` are one decision keyed by several cited
        # records; an entry without one (pre-fix logs) is its own decision.
        groups: dict[object, list[dict[str, Any]]] = {}
        for i, entry in enumerate(_erasable_entries(raw)):
            groups.setdefault(entry.get("group", ("solo", i)), []).append(entry)
        for entries in groups.values():
            # Any erased entry (a forgotten cited record) erases the decision.
            if not all(e.get("entity") and e.get("attribute") for e in entries):
                continue
            first = entries[0]
            name, target = first.get("entity"), first.get("attribute")
            if first.get("method") in MERGE_METHODS and name and target:
                aliases[canonical_entity(str(name))] = canonical_entity(str(target))

    def _fold_partition(self, namespace: str, payload: dict[str, Any]) -> None:
        state = self.communities.setdefault(namespace, CommunityState())
        changed = payload.get("set")
        if isinstance(changed, dict):
            state.anchors.update({str(n): str(a) for n, a in changed.items()})
        dropped = payload.get("drop")
        if isinstance(dropped, list):
            for node in dropped:
                state.anchors.pop(str(node), None)
        state.sleeps = int(payload.get("sleeps_since_refresh", 0))
        state.placed = int(payload.get("placed_since_refresh", 0))

    def mark(
        self, stage: str, namespace: str, key: str, *, done: bool, members_fp: str | None
    ) -> None:
        slot = (stage, namespace, key)
        self.markers[slot] = done
        old = self.key_fp.pop(slot, None)
        if old is not None:
            self.done_fp.get((stage, namespace, old), set()).discard(key)
        if done and members_fp is not None:
            self.key_fp[slot] = members_fp
            self.done_fp.setdefault((stage, namespace, members_fp), set()).add(key)

    def membership_done(self, stage: str, namespace: str, members_fp: str) -> bool:
        return bool(self.done_fp.get((stage, namespace, members_fp)))

    def state(self, stage: str, namespace: str, key: str) -> bool | None:
        """True = done, False = cleared, None = no marker (a pre-marker log)."""
        return self.markers.get((stage, namespace, key))
