"""Trust: erasure (T5-04; LIB-09 ``trust/erasure_plan.py``), behind ``privacy.erasure``.

A hard forget plans over the lineage graph instead of cascading by ``source.parents``:

1. the graph is the lineage table (``write.lineage_table``) plus the edges the records and the log
   name (``source.parents`` = ``support``; ``consolidation`` / ``reflection`` / CONSOLIDATE member
   lists = ``member``), so a flag-off history still plans;
2. ``plan_erasure`` gives each dependant one action (``hide_and_rederive``, ``unbind_and_hide``,
   ``rerun_summary``, ``keep``);
3. the erasure state (erased and hidden ids, generation) is set first, so the erased record and its
   hidden dependants are invisible at once (every read gate ends in ``Engine._consent_ok``, which
   asks :func:`visible_id`; the read cache revalidates every hit, POL-1);
4. a content-free manifest (ids and the erasure generation) is stored as a MARKER event, with the
   hidden ids, the re-derivation queue and the purge ids (all ids, never content);
5. the erased record goes through the ordinary hard-forget path, its lineage edges are removed, and
   a tracked purge job removes erased and hidden ids from the vector, lexical and graph indexes and
   bumps the cache generation, one task at a time, with progress;
6. ``hide_and_rederive`` / ``rerun_summary`` items wait in a queue the offline runner consumes
   (:func:`queue`, :func:`release`); this module never re-derives.

The in-memory state is rebuilt from the MARKER events at start (:func:`load`).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from memspine.config import constants
from memspine.exceptions import MemspineError
from memspine.observability import tap
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.erasure import ErasureManifest
from memspine.schemas.durable.lineage import LineageEdge, LineageRole
from memspine.services.graph.base import edge_weight
from memspine.services.graph.qualifiers import QUALIFIERS_PROP, ClaimState, edge_qualifiers
from memspine.services.storage.intake_port import intake_ledger_of
from memspine.store import lineage as lineage_store
from memspine.store.events import EventKind, EventLogMode, MemoryEvent
from memspine.store.records import MemoryRecord, RecordStatus
from memspine.store.scope import validate_namespace
from memspine.store.source_binding import source_binding
from memspine.trust.erasure import retained_fields
from memspine.trust.erasure_plan import (
    ErasurePlan,
    ErasureState,
    InMemoryLineageGraph,
    PlannedAction,
    PurgeProgress,
    apply_plan,
    manifest,
    plan_erasure,
    purge_steps,
    reapply_on_restore,
    visible,
)
from memspine.trust.restatement import valid_proof

__all__ = [
    "DROPPED",
    "MARKER_DROP",
    "MARKER_MANIFEST",
    "MARKER_PURGE",
    "MARKER_RELEASE",
    "PURGE_BATCH",
    "PURGE_STORES",
    "enabled",
    "forget",
    "load",
    "manifests",
    "mark_dropped",
    "queue",
    "reapply_manifests",
    "release",
    "resume_purges",
    "status",
    "visible_id",
]

#: queue-entry state of a hidden dependant hard-forgotten instead of re-derived: every one under
#: ``privacy.erasure_redact_hidden``, and by default one with no live source left (ADR-095)
DROPPED = "dropped_no_rederive"
MARKER_MANIFEST = "erasure_manifest"
MARKER_PURGE = "erasure_purge"
MARKER_RELEASE = "erasure_release"
MARKER_DROP = "erasure_drop"  # FU-ERASE: queue entries hard-forgotten as ``dropped_no_rederive``
PURGE_STORES = ("vector", "lexical", "graph", "cache")
PURGE_BATCH = 256

_EMPTY = ErasureState()


@dataclass(frozen=True)
class _CorrectionWitness:
    """Only the source identity needed to verify a predecessor restoration."""

    record_id: str
    namespace: str
    status: RecordStatus
    sender_id: str


def _correction_witness(record: MemoryRecord) -> _CorrectionWitness | None:
    sender = source_binding(record).sender_id
    if not sender or not sender.strip():
        return None
    return _CorrectionWitness(record.record_id, record.namespace, record.status, sender.strip())


def _correction_witness_from_write(
    snapshot: object, namespace: str, record_id: str
) -> _CorrectionWitness | None:
    """Read the canonical source identity without rebuilding its erased text or row."""
    if not isinstance(snapshot, dict):
        return None
    if snapshot.get("namespace") != namespace or snapshot.get("record_id") != record_id:
        return None
    source = snapshot.get("source")
    message = source.get("message") if isinstance(source, dict) else None
    sender = message.get("sender") if isinstance(message, dict) else None
    sender_id = sender.get("id") if isinstance(sender, dict) else None
    if not isinstance(sender_id, str) or not sender_id.strip():
        return None
    raw_status = snapshot.get("status")
    if not isinstance(raw_status, str):
        return None
    try:
        status = RecordStatus(raw_status)
    except ValueError:
        return None
    return _CorrectionWitness(record_id, namespace, status, sender_id.strip())


async def _erased_correction_witness(
    storage: Any, namespace: str, record_id: str
) -> _CorrectionWitness | None:
    """Use only a WRITE predating this identity's first irreversible manifest."""
    candidate: _CorrectionWitness | None = None
    after = 0
    while True:
        batch = await storage.read_events(after_seq=after, limit=1000)
        if not batch:
            return candidate
        for event in batch:
            if event.namespace != namespace:
                continue
            payload = event.payload
            if event.kind is EventKind.MARKER and payload.get("marker") == MARKER_MANIFEST:
                manifest = payload.get("manifest")
                ids = manifest.get("ids") if isinstance(manifest, dict) else None
                if isinstance(ids, dict) and record_id in (ids.get("record_ids") or []):
                    return candidate
            candidate = _advance_correction_witness(candidate, event, namespace, record_id)
        assert batch[-1].seq is not None
        after = batch[-1].seq


def _advance_correction_witness(
    candidate: _CorrectionWitness | None,
    event: MemoryEvent,
    namespace: str,
    record_id: str,
) -> _CorrectionWitness | None:
    payload = event.payload
    if event.kind is EventKind.WRITE:
        snapshot = payload.get("record")
        if isinstance(snapshot, dict) and snapshot.get("record_id") == record_id:
            # An invalid or scrubbed later snapshot must not borrow the
            # sender/status from an older valid WRITE.
            return (
                None
                if payload.get("redacted")
                else _correction_witness_from_write(snapshot, namespace, record_id)
            )
    elif event.kind is EventKind.DECAY_TRANSITION and payload.get("record_id") == record_id:
        delta = payload.get("set")
        if not isinstance(delta, dict) or set(delta) & {
            "status",
            "valid_to",
            "superseded_at",
            "evolve_to",
            "quarantined",
        }:
            # This recovery witness cannot prove a full current lifecycle from
            # a delta. A later full WRITE can establish it again.
            return None
    elif event.kind is EventKind.FORGET and payload.get("record_id") == record_id:
        return None
    return candidate


def enabled(eng: Any) -> bool:
    return True


def state(eng: Any) -> ErasureState:
    return eng.__dict__.get("_erasure_state") or _EMPTY


def visible_id(eng: Any, record_id: str) -> bool:
    """POL-1: False for an erased or hidden id. One dict read while nothing was ever erased."""
    st = eng.__dict__.get("_erasure_state")
    return st is None or visible(record_id, st)


def queue(eng: Any, *, include_dropped: bool = False) -> list[dict[str, object]]:
    """The re-derivation queue (ids, action, remaining live sources), oldest first. Entries marked
    ``dropped_no_rederive`` (``privacy.erasure_redact_hidden``) are left out unless asked for."""
    q: dict[str, dict[str, object]] = eng.__dict__.get("_erasure_queue", {})
    return [dict(v) for v in q.values() if include_dropped or v.get("state") != DROPPED]


def status(eng: Any) -> dict[str, object]:
    st = state(eng)
    purges: dict[str, dict[str, Any]] = eng.__dict__.get("_erasure_purges", {})
    return {
        "enabled": enabled(eng),
        "generation": max(st.generation, eng.__dict__.get("_erasure_generation", 0)),
        "erased": len(st.erased),
        "hidden": len(st.hidden),
        "queued": len(queue(eng)),
        "dropped_no_rederive": len(queue(eng, include_dropped=True)) - len(queue(eng)),
        "purges": {k: dict(v) for k, v in sorted(purges.items())},
    }


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def _marker(eng: Any, ns: str, payload: dict[str, object]) -> None:
    await eng._append_and_project(
        MemoryEvent(kind=EventKind.MARKER, namespace=ns, actor="system", payload=payload)
    )


def _unmapped_message_claim(record: MemoryRecord | None) -> tuple[str, str] | None:
    """Only the public message ID and immutable write ID needed for an unmapped tombstone."""
    if record is None or not record.source.message:
        return None
    message_id = record.source.message.get("message_id")
    write_ids = [
        tag.removeprefix(constants.IDEM_TAG_PREFIX)
        for tag in record.tags
        if tag.startswith(constants.IDEM_TAG_PREFIX)
    ]
    if not isinstance(message_id, str) or not message_id or len(write_ids) != 1:
        return None
    return message_id, write_ids[0]


def _lifecycle_tuple(snapshot: dict[str, Any]) -> tuple[object, ...]:
    fingerprint = snapshot.get("fingerprint")
    envelope = fingerprint.get("envelope") if isinstance(fingerprint, dict) else None
    return (
        snapshot.get("status"),
        snapshot.get("evolve_to"),
        snapshot.get("valid_to"),
        snapshot.get("superseded_at"),
        envelope.get("status") if isinstance(envelope, dict) else None,
    )


def _restore_writes_preserve_lifecycle(
    writes: Sequence[tuple[int, dict[str, Any]]], restore_seq: int, current: dict[str, Any]
) -> bool:
    """Allow evidence/quote scrubs after restore, but no later lifecycle transition."""
    restored = next((snapshot for seq, snapshot in writes if seq == restore_seq), None)
    if restored is None:
        return False
    lifecycle = _lifecycle_tuple(restored)
    return _lifecycle_tuple(current) == lifecycle and all(
        _lifecycle_tuple(snapshot) == lifecycle for seq, snapshot in writes if seq > restore_seq
    )


# ------------------------------------------------------------------ load


async def load(eng: Any) -> None:
    """Rebuild the erasure state, the queue and the purge table from the MARKER events."""
    erased: set[str] = set()
    hidden: set[str] = set()
    generation = 0
    q: dict[str, dict[str, object]] = {}
    purges: dict[str, dict[str, Any]] = {}
    pending_erased: list[tuple[str, str, _CorrectionWitness | None]] = []
    current_witnesses: dict[tuple[str, str], _CorrectionWitness | None] = {}
    pending_window_compensations: list[tuple[str, str, str, str]] = []
    restores: list[tuple[str, str, str, int]] = []
    record_writes: dict[tuple[str, str], list[tuple[int, dict[str, Any]]]] = {}
    storage = eng._storage
    if storage is not None and eng._config().event_log.mode is not EventLogMode.EPHEMERAL:
        after = 0
        while True:
            batch = await storage.read_events(after_seq=after, limit=1000)
            if not batch:
                break
            for ev in batch:
                raw_id: object = None
                if ev.kind is EventKind.WRITE:
                    snapshot = ev.payload.get("record")
                    if (
                        isinstance(snapshot, dict)
                        and isinstance(snapshot.get("record_id"), str)
                        and ev.seq is not None
                    ):
                        raw_id = snapshot["record_id"]
                        record_writes.setdefault(
                            (ev.namespace, str(snapshot["record_id"])), []
                        ).append((ev.seq, snapshot))
                    restore = ev.payload.get("correction_erasure_restore")
                    if (
                        isinstance(restore, dict)
                        and isinstance(restore.get("erased_source"), str)
                        and isinstance(restore.get("prior_id"), str)
                        and ev.seq is not None
                    ):
                        restores.append(
                            (
                                ev.namespace,
                                str(restore["prior_id"]),
                                str(restore["erased_source"]),
                                ev.seq,
                            )
                        )
                elif ev.kind in (EventKind.DECAY_TRANSITION, EventKind.FORGET):
                    raw_id = ev.payload.get("record_id")
                if isinstance(raw_id, str):
                    key = (ev.namespace, raw_id)
                    current_witnesses[key] = _advance_correction_witness(
                        current_witnesses.get(key), ev, ev.namespace, raw_id
                    )
                p = ev.payload if ev.kind is EventKind.MARKER else {}
                kind = p.get("marker")
                if kind == MARKER_MANIFEST:
                    man = p.get("manifest") or {}
                    ids = man.get("ids") or {}
                    erased.update(ids.get("record_ids") or [])
                    for rid in ids.get("record_ids") or []:
                        key = (ev.namespace, str(rid))
                        pending_erased.append((key[0], key[1], current_witnesses.pop(key, None)))
                    hidden.update(p.get("hidden") or [])
                    generation = max(generation, int(man.get("erasure_generation") or 0))
                    for item in p.get("queue") or []:
                        q[str(item["record_id"])] = dict(item)
                    purges[str(man.get("manifest_id"))] = {
                        "state": "pending",
                        "namespace": ev.namespace,
                        "generation": int(man.get("erasure_generation") or 0),
                        "erased": sorted(ids.get("record_ids") or []),
                        "hidden": sorted(p.get("hidden") or []),
                        "done": 0,
                        "total": 0,
                    }
                elif kind == "window_correction_compensation":
                    if all(
                        isinstance(p.get(key), str)
                        for key in ("successor_id", "prior_id", "source_id")
                    ):
                        pending_window_compensations.append(
                            (
                                ev.namespace,
                                str(p["successor_id"]),
                                str(p["prior_id"]),
                                str(p["source_id"]),
                            )
                        )
                elif kind == MARKER_PURGE and str(p.get("manifest_id")) in purges:
                    purges[str(p["manifest_id"])].update(
                        state=p.get("state"), done=p.get("done", 0), total=p.get("total", 0)
                    )
                elif kind == MARKER_DROP:
                    for rid in p.get("record_ids") or []:
                        if str(rid) in q:
                            q[str(rid)]["state"] = DROPPED
                elif kind == MARKER_RELEASE:
                    for rid in p.get("record_ids") or []:
                        hidden.discard(rid)
                        q.pop(str(rid), None)
            after = max(e.seq for e in batch if e.seq is not None)
    if erased or hidden or generation:
        eng.__dict__["_erasure_state"] = ErasureState(
            frozenset(erased), frozenset(hidden - erased), generation
        )
        eng.__dict__["_erasure_generation"] = max(
            eng.__dict__.get("_erasure_generation", 0), generation
        )
    eng.__dict__["_erasure_queue"] = q
    eng.__dict__["_erasure_purges"] = purges
    if storage is not None and eng._config().event_log.mode is not EventLogMode.EPHEMERAL:
        # Complete the narrow correction repair after the manifest has restored the
        # visibility fence. The projector may already have removed the erased source
        # row; its pre-manifest canonical WRITE can supply a transient, content-free
        # identity witness while the exact closure and live support remain authoritative.
        for ns, record_id, pre_manifest_witness in pending_erased:
            record = await storage.get_record(record_id)
            witness = (
                _correction_witness(record)
                if record is not None and record.namespace == ns
                else pre_manifest_witness
            )
            if witness is not None:
                await _restore_erased_correction(eng, storage, ns, witness)
        for ns, successor_id, prior_id, source_id in pending_window_compensations:
            await _restore_window_compensation(eng, storage, ns, successor_id, prior_id, source_id)
        for ns, prior_id, erased_source, seq in restores:
            current = await storage.get_record(prior_id)
            if (
                current is not None
                and current.namespace == ns
                and current.status is RecordStatus.ACTIVATED
                and erased_source in (state(eng).erased | state(eng).hidden)
                and visible_id(eng, prior_id)
                and _restore_writes_preserve_lifecycle(
                    record_writes.get((ns, prior_id), []),
                    seq,
                    current.model_dump(mode="json"),
                )
            ):
                await _reactivate_compiled_edges(eng, storage, ns, current)


# ------------------------------------------------------------------ plan


async def _derivation_roles(storage: Any, ns: str) -> dict[tuple[str, str], str]:
    """(child, parent) -> ``member`` for every pair a consolidation / reflection / CONSOLIDATE
    event names as a member of a summary (one pass over the log)."""
    out: dict[tuple[str, str], str] = {}

    def link(members: object, child: object) -> None:
        if isinstance(members, list) and isinstance(child, str) and child:
            for m in members:
                out[(child, str(m))] = "member"

    after = 0
    while True:
        batch = await storage.read_events(after_seq=after)
        if not batch:
            return out
        for ev in batch:
            if ev.namespace != ns:
                continue
            payload = ev.payload or {}
            if ev.kind is EventKind.WRITE:
                snap = payload.get("record")
                child = snap.get("record_id") if isinstance(snap, dict) else None
                for key in ("consolidation", "reflection"):
                    d = payload.get(key)
                    if isinstance(d, dict):
                        link(d.get("member_record_ids"), child)
            elif ev.kind is EventKind.CONSOLIDATE:
                link(payload.get("member_record_ids"), payload.get("summary_record_id"))
        after = max(e.seq for e in batch if e.seq is not None)


async def build_graph(
    eng: Any, storage: Any, ns: str, root: str, *, dependants: bool = True
) -> InMemoryLineageGraph:
    """The lineage graph around ``root``: table edges (flag on) plus the edges the records and the
    log name. Edges are unique per (child, parent, role); a pair named as ``member`` keeps that
    role next to ``support`` so a summary is re-run, not kept."""
    graph = InMemoryLineageGraph()
    if not dependants:
        return graph
    desc = await eng._descendants(storage, ns, [root])
    ids = [root, *desc]
    projected: set[tuple[str, str]] = set()
    source_live_cache: dict[str, bool] = {}

    async def source_is_live(source_id: str) -> bool:
        cached = source_live_cache.get(source_id)
        if cached is not None:
            return cached
        if source_id == root:
            # The requested root is lost by this plan even on an idempotent retry where its row
            # was already removed. Keep its edges so descendant traversal can finish cleanup.
            source_live_cache[source_id] = True
            return True
        source = await storage.get_record(source_id)
        live = bool(
            source is not None
            and source.namespace == ns
            and source.status.value not in ("deleted", "quarantined")
            and not source.quarantined
            and visible_id(eng, source_id)
        )
        source_live_cache[source_id] = live
        return live

    proof_cache: dict[str, set[str]] = {}

    async def live_proof_sources(child: Any) -> set[str]:
        cached = proof_cache.get(child.record_id)
        if cached is not None:
            return cached
        proof_sources = set(valid_proof(child))
        live = {parent for parent in proof_sources if await source_is_live(parent)}
        proof_cache[child.record_id] = live
        return live

    if eng._config().write.lineage_table:
        for e in await lineage_store.load_edges(storage, ids):
            edge = e
            if e.child != e.parent:
                child = await storage.get_record(e.child)
                if child is not None and len(set(child.source.parents)) > 1:
                    if child.fingerprint.envelope is not None and not await source_is_live(
                        e.parent
                    ):
                        projected.add((e.child, e.parent))
                        continue
                    proof_sources = (
                        await live_proof_sources(child)
                        if child.fingerprint.envelope is not None
                        else set()
                    )
                    if e.parent in proof_sources and e.role in ("support", "binding", "context"):
                        # A validated proof establishes that this source alone carries the
                        # complete claim, so its local binding/context labels are not cross-source
                        # dependencies for erasure purposes.
                        edge = e.model_copy(update={"role": "support"})
                    elif e.role == "support" and child.fingerprint.envelope is not None:
                        # The optional lineage projection is rebuildable evidence, not authority.
                        # Multi-source canonical claims need a current RM-22 proof for each support
                        # edge; stale or malformed rows cannot keep an unproven claim visible.
                        edge = e.model_copy(update={"role": "member"})
            graph.add_edge(edge)
            projected.add((e.child, e.parent))
    roles = await _derivation_roles(storage, ns)
    wanted = set(ids)
    for cid in desc:
        rec = await storage.get_record(cid)
        if rec is None:
            continue
        for pid in dict.fromkeys(rec.source.parents):
            if pid != cid and (cid, pid) not in projected:
                if (
                    rec.fingerprint.envelope is not None
                    and len(set(rec.source.parents)) > 1
                    and not await source_is_live(pid)
                ):
                    continue
                # Summary membership is conjunctive: another surviving member cannot
                # justify keeping text that also incorporated an erased member. This
                # holds even when the optional lineage projection is disabled.
                proven_support = await live_proof_sources(rec)
                fallback_role: LineageRole = (
                    "member"
                    if rec.source.channel
                    in {
                        "consolidation",
                        "reflection",
                        "entity_summary",
                        "community_summary",
                        "reorganize",
                        "multires",
                        "window_episode",
                        "session_close",
                    }
                    else "member"
                    if len(set(rec.source.parents)) > 1
                    else "support"
                )
                if pid in proven_support and rec.source.channel not in {
                    "consolidation",
                    "reflection",
                    "entity_summary",
                    "community_summary",
                    "reorganize",
                    "multires",
                    "window_episode",
                    "session_close",
                }:
                    fallback_role = "support"
                graph.add_edge(LineageEdge(child=cid, parent=pid, role=fallback_role))
    for (child, parent), role in sorted(roles.items()):
        if child in wanted and child != parent:
            graph.add_edge(LineageEdge(child=child, parent=parent, role=role))  # type: ignore[arg-type]
    return graph


def _reason_code(actor: str, reason: str | None) -> Any:
    r = (reason or "").lower()
    if r.startswith("retention"):
        return "retention"
    if "legal" in r:
        return "legal"
    return "admin" if actor == "admin" else "user_request"


async def forget(
    eng: Any,
    record_id: str,
    namespace: str,
    cascade: bool | None,
    *,
    actor: str,
    reason: str | None,
) -> None:
    """The hard forget under ``privacy.erasure`` (see the module docstring)."""
    storage = eng._require_started()
    ns = validate_namespace(namespace)
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        completion = await _forget_under_lock(
            eng, storage, ns, record_id, cascade, actor=actor, reason=reason
        )
    await _finish_forget(eng, ns, completion, actor=actor, reason=reason)


async def _forget_under_lock(
    eng: Any,
    storage: Any,
    ns: str,
    record_id: str,
    cascade: bool | None,
    *,
    actor: str,
    reason: str | None,
) -> tuple[str, list[str], list[str], list[str]]:
    """Plan and apply a hard forget while the caller holds the namespace write lock."""
    record = await eng._forget_target(storage, ns, record_id, True)  # scope + legal hold
    texts = eng._erasable_texts(record) if record is not None else []
    prior = state(eng)
    graph = await build_graph(eng, storage, ns, record_id, dependants=cascade is not False)
    generation = max(prior.generation, eng.__dict__.get("_erasure_generation", 0)) + 1
    plan = plan_erasure([record_id], graph, generation=generation, already_lost=prior.lost)
    redact = bool(eng._config().privacy.erasure_redact_hidden)
    dropped: list[str] = []  # F2: hidden dependants hard-forgotten instead of re-derived
    dropped_claims: list[tuple[str, str]] = []
    orphaned: set[str] = set()  # ADR-095 2026-11-02: no live source left to re-derive from
    for a in plan.actions:
        if a.hidden:
            dep = await storage.get_record(a.record_id)
            if dep is not None and dep.namespace == ns:
                texts.extend(eng._erasable_texts(dep))
                # ADR-095 amendment 2026-11-02 (owner): a dependant whose every source is
                # gone is redacted by default (text and quote removed; its id stays in the
                # manifest's hidden list and the queue as an id-only tombstone). One with a
                # live source stays hidden with its text, pending re-derivation, unless
                # ``erasure_redact_hidden`` redacts every hidden dependant.
                no_source = a.needs_rederivation and not a.remaining_sources
                if redact or no_source:
                    try:
                        await eng._forget_target(storage, ns, a.record_id, True)
                    except MemspineError:  # legal hold: it stays hidden, queued as planned
                        continue
                    dropped.append(a.record_id)
                    claim = _unmapped_message_claim(dep)
                    if claim is not None:
                        dropped_claims.append(claim)
                    if not redact:
                        orphaned.add(a.record_id)
    # Immediate invisibility: before any physical work, under the namespace lock.
    eng.__dict__["_erasure_state"] = apply_plan(prior, plan)
    eng.__dict__["_erasure_generation"] = generation
    q: dict[str, dict[str, object]] = eng.__dict__.setdefault("_erasure_queue", {})
    manifest_id = f"erm-{uuid.uuid4().hex[:16]}"
    queued = [
        {
            "record_id": a.record_id,
            "action": a.action,
            "remaining_sources": list(a.remaining_sources),
            "manifest_id": manifest_id,
            **({"state": DROPPED} if a.record_id in dropped else {}),
        }
        for a in plan.actions
        if a.needs_rederivation
    ]
    for item in queued:
        q[str(item["record_id"])] = item  # type: ignore[assignment]  # CI-GREEN: pre-existing typing debt
    for a in plan.actions:
        if a.action == "unbind_and_hide":
            q.pop(a.record_id, None)
    man = manifest(
        plan,
        manifest_id=manifest_id,
        scope=OwnerScope(tenant=ns, owner=ns),
        erased_at=_aware(eng._clock()),
        requested_by=actor,
        reason_code=_reason_code(actor, reason),
    ).model_copy(update={"purge_job_id": f"purge:{manifest_id}"})
    hidden_ids = sorted(plan.hidden - plan.erased)
    # The manifest's native records fold deletes erased rows immediately. Tombstone
    # any intake identities while their source's immutable write ID is still
    # available; the later hard-forget pass remains idempotent for other callers.
    ledger = intake_ledger_of(storage)
    if ledger is not None:
        seed_claim = _unmapped_message_claim(record)
        await ledger.tombstone_records([record_id, *dropped])
        await ledger.tombstone_unmapped_messages(
            ns, [*([seed_claim] if seed_claim is not None else []), *dropped_claims]
        )
    await _marker(
        eng,
        ns,
        {
            "marker": MARKER_MANIFEST,
            "manifest": man.model_dump(mode="json"),
            "hidden": hidden_ids,
            "queue": queued,
        },
    )
    # The manifest has made the correction invisible. Append any narrowly
    # proven predecessor restoration before hard-forget redacts its proof event.
    witness = (
        _correction_witness(record)
        if record is not None
        else await _erased_correction_witness(storage, ns, record_id)
    )
    if witness is not None:
        await _restore_erased_correction(eng, storage, ns, witness)
    await _resume_restored_correction_edges(eng, storage, ns, record_id)
    by_action: dict[str, int] = {}
    for a in plan.actions:
        by_action[a.action] = by_action.get(a.action, 0) + 1
    if tap.enabled():
        tap.event(
            "trust.erasure.plan",
            manifest=manifest_id,
            erased=len(plan.erased),
            dependants=by_action,
            hidden=len(hidden_ids),
            queued=len(queued),
            generation=generation,
            lineage_table=bool(eng._config().write.lineage_table),
        )
    await eng._forget_many(storage, ns, [record_id], True)
    if dropped:
        await eng._forget_many(storage, ns, dropped, True)
        # The opt-in drops the dropped rows' lineage too; a sourceless dependant redacted
        # by default keeps its id-only lineage edges (the tombstone the owner asked for).
        unlinked = [rid for rid in dropped if rid not in orphaned]
        if unlinked:
            gone = await storage.lineage_parents(unlinked)
            gone_kids = await storage.lineage_children(unlinked)
            edges = [e for es in (*gone.values(), *gone_kids.values()) for e in es]
            if edges:
                await storage.delete_lineage(edges)
        if tap.enabled():
            tap.event(
                "trust.erasure.redact_hidden",
                manifest=manifest_id,
                dropped=len(dropped),
                no_source=len(orphaned),
            )
    keep_lineage = str(reason or "").startswith("retention")  # expiry keeps lineage (ER-27)
    if eng._config().write.lineage_table and plan.edges_to_remove and not keep_lineage:
        await storage.delete_lineage(plan.edges_to_remove)
    purges: dict[str, dict[str, Any]] = eng.__dict__.setdefault("_erasure_purges", {})
    purges[manifest_id] = {
        "state": "pending",
        "namespace": ns,
        "generation": generation,
        "erased": sorted(plan.erased),
        "hidden": hidden_ids,
        "done": 0,
        "total": 0,
    }
    return manifest_id, texts, sorted(plan.erased), hidden_ids


async def _finish_forget(
    eng: Any,
    ns: str,
    completion: tuple[str, list[str], list[str], list[str]],
    *,
    actor: str,
    reason: str | None,
) -> None:
    """Run the existing post-lock audit and projector purge."""
    manifest_id, texts, erased_ids, hidden_ids = completion
    await eng._audit_action(
        "forget",
        ns,
        [*erased_ids, *hidden_ids],
        actor=actor,
        reason=reason,
        hard=True,
    )
    await _run_purge(eng, manifest_id, texts)


# ------------------------------------------------------------------ correction erasure


async def prepare_window_correction_compensation(
    eng: Any, storage: Any, ns: str, successor_id: str
) -> None:
    """Persist an ID-only recovery intent before a fenced window successor is forgotten."""
    successor = await storage.get_record(successor_id)
    if (
        successor is None
        or successor.namespace != ns
        or successor.status not in (RecordStatus.ACTIVATED, RecordStatus.DELETED)
        or not visible_id(eng, successor_id)
        or len(set(successor.source.parents)) != 1
    ):
        return
    source_id = successor.source.parents[0]
    source = await storage.get_record(source_id)
    if (
        source is None
        or source.namespace != ns
        or source_id not in _record_evidence_sources(successor)
    ):
        return
    source_owner = source_binding(source).sender_id
    if not source_owner or not source_owner.strip():
        return
    matches: list[str] = []
    existing_intents: set[tuple[str, str, str]] = set()
    after = 0
    while True:
        batch = await storage.read_events(after_seq=after, limit=1000)
        if not batch:
            break
        for event in batch:
            if event.namespace == ns and event.kind is EventKind.MARKER:
                payload = event.payload
                if payload.get("marker") == "window_correction_compensation":
                    existing_intents.add(
                        (
                            str(payload.get("successor_id")),
                            str(payload.get("prior_id")),
                            str(payload.get("source_id")),
                        )
                    )
            if event.namespace != ns or event.kind is not EventKind.WRITE:
                continue
            snapshot = event.payload.get("record")
            relation = event.payload.get("correction")
            if (
                isinstance(snapshot, dict)
                and isinstance(relation, dict)
                and relation.get("reason") == "rule_claim_change"
                and relation.get("supersedes") == snapshot.get("record_id")
                and snapshot.get("evolve_to") == successor_id
                and isinstance(snapshot.get("record_id"), str)
            ):
                matches.append(str(snapshot["record_id"]))
        assert batch[-1].seq is not None
        after = batch[-1].seq
    if len(matches) != 1:
        return
    if (successor_id, matches[0], source_id) in existing_intents:
        return
    prior = await storage.get_record(matches[0])
    if (
        prior is None
        or prior.namespace != ns
        or prior.status is not RecordStatus.ARCHIVED
        or prior.evolve_to != successor_id
        or not visible_id(eng, prior.record_id)
    ):
        return
    await _marker(
        eng,
        ns,
        {
            "marker": "window_correction_compensation",
            "successor_id": successor_id,
            "prior_id": prior.record_id,
            "source_id": source_id,
        },
    )


async def _restore_window_compensation(
    eng: Any, storage: Any, ns: str, successor_id: str, prior_id: str, source_id: str
) -> None:
    if successor_id not in (state(eng).erased | state(eng).hidden):
        return
    source = await storage.get_record(source_id)
    if source is not None and source.namespace == ns:
        await _restore_erased_correction(
            eng, storage, ns, source, compensation=(prior_id, successor_id)
        )
    await _resume_restored_correction_edges(eng, storage, ns, successor_id)


async def resume_window_correction_compensation(
    eng: Any, storage: Any, ns: str, successor_id: str
) -> None:
    """Finish a compensation after successor forget, including an interrupted LINK append."""
    after = 0
    intents: set[tuple[str, str]] = set()
    while True:
        batch = await storage.read_events(after_seq=after, limit=1000)
        if not batch:
            break
        for event in batch:
            payload = event.payload
            if (
                event.namespace == ns
                and event.kind is EventKind.MARKER
                and payload.get("marker") == "window_correction_compensation"
                and payload.get("successor_id") == successor_id
                and isinstance(payload.get("prior_id"), str)
                and isinstance(payload.get("source_id"), str)
            ):
                intents.add((str(payload["prior_id"]), str(payload["source_id"])))
        assert batch[-1].seq is not None
        after = batch[-1].seq
    for prior_id, source_id in sorted(intents):
        await _restore_window_compensation(eng, storage, ns, successor_id, prior_id, source_id)


def _record_evidence_sources(record: MemoryRecord) -> set[str]:
    """Source ids cited by a canonical semantic envelope plus persisted source parents."""
    cited = set(record.source.parents)
    raw = record.fingerprint.envelope
    if not isinstance(raw, dict):
        return cited
    collections: list[object] = [raw.get("evidence")]
    roles = raw.get("role_bindings")
    if isinstance(roles, list):
        collections.extend(role.get("evidence") for role in roles if isinstance(role, dict))
    for items in collections:
        if not isinstance(items, list):
            continue
        for evidence in items:
            if not isinstance(evidence, dict):
                continue
            turn = evidence.get("turn")
            source_id = turn.get("source_id") if isinstance(turn, dict) else None
            if isinstance(source_id, str) and source_id:
                cited.add(source_id)
    return cited


async def _restore_erased_correction(
    eng: Any,
    storage: Any,
    ns: str,
    correction: MemoryRecord | _CorrectionWitness,
    *,
    compensation: tuple[str, str] | None = None,
) -> bool:
    """Restore a claim only when an erased rules correction is its sole closure.

    The existing canonical WRITE event records both the exact target and successor. Requiring
    that proof and the pre-closure snapshot avoids guessing from graph lineage or a dangling
    ``evolve_to`` alone. Ambiguous, chained, changed or unsupported claims stay closed.
    """
    witness = (
        _correction_witness(correction) if isinstance(correction, MemoryRecord) else correction
    )
    if (
        witness is None
        or witness.namespace != ns
        or (
            witness.status is not RecordStatus.ACTIVATED
            and (compensation is None or witness.status is not RecordStatus.DELETED)
        )
    ):
        return False
    correction_owner = witness.sender_id
    events: list[MemoryEvent] = []
    after = 0
    while True:
        batch = await storage.read_events(after_seq=after, limit=1000)
        if not batch:
            break
        events.extend(event for event in batch if event.namespace == ns)
        assert batch[-1].seq is not None
        after = batch[-1].seq

    closures: list[tuple[MemoryEvent, str, str]] = []
    for event in events:
        if event.kind is not EventKind.WRITE:
            continue
        snapshot = event.payload.get("record")
        relation = event.payload.get("correction")
        if not isinstance(snapshot, dict) or not isinstance(relation, dict):
            continue
        prior_id = snapshot.get("record_id")
        successor_id = snapshot.get("evolve_to")
        if (
            relation.get("reason") == "rule_claim_change"
            and relation.get("supersedes") == prior_id
            and isinstance(prior_id, str)
            and isinstance(successor_id, str)
        ):
            closures.append((event, prior_id, successor_id))

    matches: list[tuple[MemoryEvent, str, str]] = []
    for entry in closures:
        successor_record = await storage.get_record(entry[2])
        if compensation is not None:
            if (entry[1], entry[2]) == compensation:
                matches.append(entry)
        elif (
            successor_record is not None
            and successor_record.namespace == ns
            and witness.record_id in _record_evidence_sources(successor_record)
        ):
            matches.append(entry)
    if len(matches) != 1:
        return False
    close_event, prior_id, successor_id = matches[0]
    close_seq = close_event.seq
    if close_seq is None:
        return False
    lost_id = successor_id if compensation is not None else witness.record_id
    correction_lost = lost_id in (state(eng).erased | state(eng).hidden)
    if not correction_lost:
        return False
    successor = await storage.get_record(successor_id)
    if (
        (successor is None and compensation is None)
        or (successor is not None and successor.namespace != ns)
        or (successor is not None and not successor.source.parents and compensation is None)
        or visible_id(eng, successor_id)
    ):
        # The corrected claim still has independent surviving support; keep it as the
        # current value rather than exposing its superseded predecessor as a second winner.
        return False
    # A live successor in a correction chain has precedence; never revive an older value.
    for later, target_id, next_id in closures:
        if (
            later.seq is not None
            and later.seq > close_seq
            and target_id == successor_id
            and next_id != lost_id
        ):
            later_record = await storage.get_record(next_id)
            if later_record is not None and visible_id(eng, next_id):
                return False
    # Multiple recorded successors from this target do not establish a unique predecessor.
    if sum(target_id == prior_id for _, target_id, _ in closures) != 1:
        return False

    current = await storage.get_record(prior_id)
    if (
        current is None
        or current.namespace != ns
        or current.status is not RecordStatus.ARCHIVED
        or current.evolve_to != successor_id
        or current.quarantined
        or not visible_id(eng, prior_id)
    ):
        return False
    prior_writes = [
        (event, snapshot)
        for event in events
        if event.kind is EventKind.WRITE
        and (snapshot := event.payload.get("record")) is not None
        and isinstance(snapshot, dict)
        and snapshot.get("record_id") == prior_id
    ]
    if not prior_writes:
        return False
    close_snapshot = close_event.payload.get("record")
    if not isinstance(close_snapshot, dict):
        return False
    for event, snapshot in prior_writes:
        if event.seq is None or event.seq <= close_seq:
            continue
        snapshot_envelope = snapshot.get("fingerprint")
        snapshot_envelope = (
            snapshot_envelope.get("envelope") if isinstance(snapshot_envelope, dict) else None
        )
        if (
            snapshot.get("status") != RecordStatus.ARCHIVED.value
            or snapshot.get("evolve_to") != successor_id
            or snapshot.get("valid_to") != close_snapshot.get("valid_to")
            or snapshot.get("superseded_at") != close_snapshot.get("superseded_at")
            or not isinstance(snapshot_envelope, dict)
            or snapshot_envelope.get("status") != "superseded"
        ):
            return False
    prior_snapshots: list[tuple[int, dict[str, Any]]] = []
    for event in events:
        if event.kind is not EventKind.WRITE or event.seq is None or event.seq >= close_seq:
            continue
        snapshot = event.payload.get("record")
        if isinstance(snapshot, dict) and snapshot.get("record_id") == prior_id:
            prior_snapshots.append((event.seq, snapshot))
    if not prior_snapshots:
        return False
    _, before = max(prior_snapshots, key=lambda item: item[0])
    if before.get("status") != RecordStatus.ACTIVATED.value:
        return False
    try:
        original = MemoryRecord.model_validate(before)
    except (TypeError, ValueError):
        return False
    # Independent support must still exist in the same owner scope.
    source_ids = tuple(dict.fromkeys(original.source.parents))
    if not source_ids:
        return False
    proof_sources = set(valid_proof(current))
    supported_ids = proof_sources or set(source_ids)
    if not supported_ids <= set(source_ids):
        return False
    if not proof_sources and len(supported_ids) != len(source_ids):
        return False
    if original.valid_to is not None or original.superseded_at is not None or original.evolve_to:
        # A finite interval or prior lifecycle closure is not reopened as current by erasure.
        return False
    live_support: set[str] = set()
    for source_id in supported_ids:
        source = await storage.get_record(source_id)
        if source is None or source.namespace != ns or source.quarantined:
            if not proof_sources:
                return False
            continue
        source_owner = source_binding(source).sender_id
        if (
            source.status.value not in ("deleted", "quarantined")
            and visible_id(eng, source_id)
            and source_owner
            and source_owner.strip() == correction_owner
        ):
            live_support.add(source_id)
        elif not proof_sources:
            return False
    if not live_support:
        return False
    closed = close_event.payload.get("record")
    if (
        not isinstance(closed, dict)
        or closed.get("record_id") != prior_id
        or closed.get("evolve_to") != current.evolve_to
        or closed.get("status") != RecordStatus.ARCHIVED.value
        or current.fingerprint.envelope is None
        or current.fingerprint.envelope.get("status") != "superseded"
    ):
        return False
    # The close event must still describe the current interval boundary. If another lifecycle
    # operation changed it, the old snapshot cannot safely reopen the record.
    raw_valid_to = closed.get("valid_to")
    if raw_valid_to is None:
        if current.valid_to is not None:
            return False
    else:
        try:
            expected_valid_to = datetime.fromisoformat(str(raw_valid_to).replace("Z", "+00:00"))
            if expected_valid_to != current.valid_to:
                return False
        except ValueError:
            return False
    raw_superseded_at = closed.get("superseded_at")
    if raw_superseded_at is None:
        if current.superseded_at is not None:
            return False
    else:
        try:
            expected_superseded_at = datetime.fromisoformat(
                str(raw_superseded_at).replace("Z", "+00:00")
            )
            if expected_superseded_at != current.superseded_at:
                return False
        except ValueError:
            return False
    # Keep the current row's content, source and sanitized evidence. The historical
    # snapshot supplies lifecycle fields only, so another prior erasure cannot be undone.
    restored_envelope = {
        **dict(current.fingerprint.envelope or {}),
        "status": "active",
    }
    fingerprint_updates: dict[str, Any] = {"envelope": restored_envelope}
    proof = current.fingerprint.restatement_support
    if proof is not None:
        if valid_proof(current):
            from memspine.trust.restatement import claim_digest

            digest = claim_digest(restored_envelope)
            if digest is not None:
                fingerprint_updates["restatement_support"] = proof.model_copy(
                    update={"claim_digest": digest}
                )
            else:
                fingerprint_updates["restatement_support"] = None
        else:
            fingerprint_updates["restatement_support"] = None
    restored = current.model_copy(
        update={
            "status": RecordStatus.ACTIVATED,
            "valid_to": original.valid_to,
            "superseded_at": original.superseded_at,
            "evolve_to": original.evolve_to,
            "fingerprint": current.fingerprint.model_copy(update=fingerprint_updates),
        }
    )
    await eng._append_and_project(
        MemoryEvent(
            kind=EventKind.WRITE,
            namespace=ns,
            actor="system",
            payload={
                "record": restored.model_dump(mode="json"),
                "correction_erasure_restore": {
                    "erased_source": lost_id,
                    "prior_id": prior_id,
                },
            },
        )
    )
    await _reactivate_compiled_edges(eng, storage, ns, restored, allowed_evidence=live_support)
    if eng._semantic is not None:
        eng._semantic.invalidate_index(ns)
    return True


async def _reactivate_compiled_edges(
    eng: Any,
    storage: Any,
    ns: str,
    record: MemoryRecord,
    *,
    allowed_evidence: set[str] | None = None,
) -> None:
    """Append canonical LINK updates for this restored assertion, preserving its qualifiers."""
    graph = getattr(eng, "_graph", None)
    if graph is None or not eng._config().graph.edge_compiler:
        return
    candidates = allowed_evidence or set(valid_proof(record)) or set(record.source.parents)
    live_evidence: set[str] = set()
    for source_id in candidates:
        source = await storage.get_record(source_id)
        if (
            source is not None
            and source.namespace == ns
            and source.status.value not in ("deleted", "quarantined")
            and not source.quarantined
            and visible_id(eng, source_id)
        ):
            live_evidence.add(source_id)
    if not live_evidence:
        return
    for edge in await graph.edge_list(ns):
        props = edge.properties
        if props.get("compiled") != "edge_compiler" or props.get("record_id") != record.record_id:
            continue
        evidence = props.get("evidence_ids")
        if not isinstance(evidence, list | tuple) or not evidence:
            continue
        retained = [str(source_id) for source_id in evidence if str(source_id) in live_evidence]
        if not retained:
            continue
        qualifiers = edge_qualifiers(props)
        if qualifiers is None:
            continue
        updated = dict(props)
        roots = props.get("evidence_roots")
        if isinstance(roots, dict):
            updated["evidence_roots"] = {
                key: value for key, value in roots.items() if key in retained
            }
        updated["evidence_ids"] = retained
        valid_to = record.valid_to.isoformat() if record.valid_to is not None else None
        contested = bool(props.get("evidence_erased"))
        updated.update(
            closed=False,
            status="active",
            valid_to=valid_to,
            contested=contested,
        )
        updated[QUALIFIERS_PROP] = qualifiers.model_copy(
            update={
                "claim_state": ClaimState.CONTESTED if contested else ClaimState.CURRENT,
                "valid_to": record.valid_to,
                "valid_open_ended": record.valid_to is None and record.valid_from is not None,
                "evidence_ids": tuple(retained),
            }
        ).to_properties()
        weight = edge_weight(props)
        if props == updated and edge_weight(updated) == weight:
            continue
        await eng._append_and_project(
            MemoryEvent(
                kind=EventKind.LINK,
                namespace=ns,
                actor="system",
                payload={
                    "src": edge.src,
                    "dst": edge.dst,
                    "rel": edge.rel_type,
                    "weight": weight,
                    "reason": "edge_compiler",
                    "props": updated,
                },
            )
        )


async def _resume_restored_correction_edges(
    eng: Any, storage: Any, ns: str, erased_source: str
) -> None:
    """Finish missing target LINK events when the same erase operation is retried."""
    after = 0
    record_writes: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    restores: list[tuple[str, int]] = []
    while True:
        batch = await storage.read_events(after_seq=after, limit=1000)
        if not batch:
            break
        for event in batch:
            if event.namespace != ns or event.kind is not EventKind.WRITE:
                continue
            snapshot = event.payload.get("record")
            if (
                isinstance(snapshot, dict)
                and isinstance(snapshot.get("record_id"), str)
                and event.seq is not None
            ):
                record_writes.setdefault(str(snapshot["record_id"]), []).append(
                    (event.seq, snapshot)
                )
            restore = event.payload.get("correction_erasure_restore")
            if (
                isinstance(restore, dict)
                and restore.get("erased_source") == erased_source
                and isinstance(restore.get("prior_id"), str)
                and event.seq is not None
            ):
                restores.append((str(restore["prior_id"]), event.seq))
        assert batch[-1].seq is not None
        after = batch[-1].seq
    if erased_source not in (state(eng).erased | state(eng).hidden):
        return
    for prior_id, seq in restores:
        current = await storage.get_record(prior_id)
        if (
            current is not None
            and current.namespace == ns
            and current.status is RecordStatus.ACTIVATED
            and visible_id(eng, prior_id)
            and _restore_writes_preserve_lifecycle(
                record_writes.get(prior_id, []), seq, current.model_dump(mode="json")
            )
        ):
            await _reactivate_compiled_edges(eng, storage, ns, current)


# ------------------------------------------------------------------ purge job


def _purge_plan(entry: dict[str, Any]) -> ErasurePlan:
    return ErasurePlan(
        erased=frozenset(entry["erased"]),
        generation=int(entry["generation"]),
        actions=tuple(PlannedAction(rid, "hide_and_rederive", (), ()) for rid in entry["hidden"]),
        edges_to_remove=(),
    )


async def _purge_step(eng: Any, ns: str, step: Any, texts: Sequence[str]) -> None:
    if step.store == "vector":
        for rid in step.ids:
            await eng._vector.delete(rid)
    elif step.store == "lexical":
        if eng._lexical is not None:
            for rid in step.ids:
                await eng._lexical.delete(rid)
    elif step.store == "graph":
        if eng._graph is not None:
            for rid in step.ids:
                await eng._graph.delete_node(rid)
            # RM-130: the purged records' entity links go from the name index too.
            index = getattr(eng, "_entity_index", None)
            if index is not None:
                await index.purge_records(ns, step.ids)
        if eng._graph is not None and eng._config().graph.edge_compiler:
            await eng._retract_compiled_edges(ns, list(step.ids))  # F-02c: edges sourced from them
    elif step.kind == "bump_generation":
        d = eng.__dict__
        d["_erasure_generation"] = max(d.get("_erasure_generation", 0), step.generation)
        d["_store_generation"] = d.get("_store_generation", 0) + 1
        d.pop("_read_result_cache", None)
        await eng._purge_caches(list(texts))
        hidden = [i for i in state(eng).hidden]
        for memory in (
            eng._working,
            eng._semantic,
            eng._episodic,
            eng._resource,
            eng._procedural,
            eng._reflective,
            eng._associative,
            eng._prospective,
            eng._shared,
        ):
            if memory is not None:
                for rid in hidden:
                    await memory.on_forget(ns, rid)
        if eng._windows is not None:
            for rid in hidden:
                await eng._windows.on_forget(ns, rid)


async def _run_purge(eng: Any, manifest_id: str, texts: Sequence[str] = ()) -> PurgeProgress:
    """Run (or resume) the purge job of one manifest, one task at a time, recording progress."""
    purges: dict[str, dict[str, Any]] = eng.__dict__.setdefault("_erasure_purges", {})
    entry = purges[manifest_id]
    ns = str(entry["namespace"])
    progress = purge_steps(_purge_plan(entry), PURGE_STORES, batch_size=PURGE_BATCH)
    entry.update(state="running", total=len(progress.steps), done=0)
    failed: str | None = None
    for step in progress.steps:
        try:
            await _purge_step(eng, ns, step, texts)
        except Exception as exc:  # a failed task stops the job; resume_purges retries it
            failed = type(exc).__name__
            if tap.enabled():
                tap.event(
                    "trust.erasure.purge",
                    manifest=manifest_id,
                    step=step.step_id,
                    store=step.store,
                    outcome="failed",
                    error=failed,
                )
            break
        progress = progress.mark_done(step.step_id)
        entry["done"] = progress.done_count
        if tap.enabled():
            tap.event(
                "trust.erasure.purge",
                manifest=manifest_id,
                step=step.step_id,
                store=step.store,
                outcome="ok",
                done=progress.done_count,
                pending=progress.pending_count,
            )
    if failed is None:
        purge = getattr(eng._vector, "purge_deleted", None)
        if callable(purge):
            await purge()  # drop the deleted rows' bytes and the older table versions
        from memspine.trust.pipeline.erasure import compact_cache, compact_graph

        await compact_graph(eng)  # the hidden ids' graph nodes the graph step deleted
        await compact_cache(eng)  # the dependants' cache keys left after the bump step
        if eng._sql is not None:
            await eng._sql.checkpoint()
    entry["state"] = "failed" if failed else "done"
    await _marker(
        eng,
        ns,
        {
            "marker": MARKER_PURGE,
            "manifest_id": manifest_id,
            "state": entry["state"],
            "done": progress.done_count,
            "total": len(progress.steps),
        },
    )
    if failed is None and eng._sql is not None:
        await eng._sql.checkpoint()
    if tap.enabled():
        tap.event(
            "trust.erasure.done",
            manifest=manifest_id,
            state=entry["state"],
            erased=len(entry["erased"]),
            hidden=len(entry["hidden"]),
            generation=entry["generation"],
            done=progress.done_count,
            total=len(progress.steps),
        )
    return progress


async def resume_purges(eng: Any) -> list[str]:
    """Re-run every purge job that is not done (after a restart or a failed task). Returns the
    manifest ids that reached ``done``."""
    purges: dict[str, dict[str, Any]] = eng.__dict__.get("_erasure_purges", {})
    finished: list[str] = []
    for manifest_id in sorted(purges):
        if purges[manifest_id]["state"] != "done":
            await _run_purge(eng, manifest_id)
            if purges[manifest_id]["state"] == "done":
                finished.append(manifest_id)
    return finished


async def classify_descendants(
    eng: Any, storage: Any, erased_id: str, descendants: Sequence[str]
) -> dict[str, list[str]]:
    """F4: sort the dependants still in the read model after a hard forget of ``erased_id``.

    * ``hidden_pending_rederive``: hidden (invisible to every read) and queued for re-derivation;
    * ``kept``: not hidden and another live parent supports it (the plan said ``keep``);
    * ``leaked``: anything else, i.e. visible with no other live parent, or hidden with no queue
      entry (``unbind_and_hide``) while its row still holds the erased lineage.

    Hidden rows hard-forgotten by ``privacy.erasure_redact_hidden`` are no longer descendants.
    """
    st = state(eng)
    q: dict[str, dict[str, object]] = eng.__dict__.get("_erasure_queue", {})
    out: dict[str, list[str]] = {"hidden_pending_rederive": [], "kept": [], "leaked": []}
    members: dict[str, dict[str, list[str]]] = {}  # ns -> child -> member parents (event log)
    for rid in descendants:
        if rid in st.hidden:
            queued = rid in q and q[rid].get("state") != DROPPED
            out["hidden_pending_rederive" if queued else "leaked"].append(rid)
            continue
        row = await storage.get_record(rid)
        if row is None:
            continue
        parents = list(row.source.parents)
        if eng._config().write.lineage_table:
            edges = await storage.lineage_parents([rid])
            parents.extend(e.parent for e in edges.get(rid, []))
        # FU-ERASE: a summary linked only by the member lists of the event log (consolidation,
        # reflection, CONSOLIDATE) is supported by those members, as build_graph plans it.
        if row.namespace not in members:
            by_child: dict[str, list[str]] = {}
            for child, parent in await _derivation_roles(storage, row.namespace):
                by_child.setdefault(child, []).append(parent)
            members[row.namespace] = by_child
        parents.extend(members[row.namespace].get(rid, []))
        live = []
        for p in dict.fromkeys(parents):
            if p == erased_id or p in st.erased or p in st.hidden:
                continue
            parent = await storage.get_record(p)
            if parent is not None:
                live.append(p)
        out["kept" if live else "leaked"].append(rid)
    return out


async def mark_dropped(eng: Any, record_ids: Sequence[str], namespace: str) -> int:
    """FU-ERASE: these queued hidden ids were hard-forgotten instead of re-derived. Their entries
    stay in the queue as ``dropped_no_rederive`` (hidden, row gone); survives a restart."""
    ns = validate_namespace(namespace)
    q: dict[str, dict[str, object]] = eng.__dict__.setdefault("_erasure_queue", {})
    ids = [r for r in dict.fromkeys(record_ids) if r in q]
    for rid in ids:
        q[rid]["state"] = DROPPED
    if ids:
        await _marker(eng, ns, {"marker": MARKER_DROP, "record_ids": ids})
    return len(ids)


async def release(eng: Any, record_ids: Sequence[str], namespace: str = "default") -> int:
    """The offline runner re-derived (or dropped) these hidden ids: they leave the hidden set and
    the queue. Erased ids never come back. Returns how many ids were released."""
    ns = validate_namespace(namespace)
    st = state(eng)
    ids = [r for r in dict.fromkeys(record_ids) if r in st.hidden]
    if not ids:
        return 0
    eng.__dict__["_erasure_state"] = st.release(ids)
    q: dict[str, dict[str, object]] = eng.__dict__.setdefault("_erasure_queue", {})
    for rid in ids:
        q.pop(rid, None)
    await _marker(eng, ns, {"marker": MARKER_RELEASE, "record_ids": ids})
    if tap.enabled():
        tap.event("trust.erasure.release", released=len(ids), remaining=len(state(eng).hidden))
    return len(ids)


# ------------------------------------------------------------------ backup restore


async def manifests(eng: Any) -> list[ErasureManifest]:
    """ADR-095: every erasure manifest in the log, oldest first (ids and generations only, never
    content). An operator keeps these outside the backups, so a restore can re-apply them."""
    storage = eng._require_started()
    found: list[ErasureManifest] = []
    after = 0
    while True:
        batch = await storage.read_events(after_seq=after, limit=1000)
        if not batch:
            return found
        for ev in batch:
            payload = ev.payload if ev.kind is EventKind.MARKER else {}
            if payload.get("marker") == MARKER_MANIFEST and isinstance(
                payload.get("manifest"), dict
            ):
                found.append(ErasureManifest.model_validate(payload["manifest"]))
        after = max(e.seq for e in batch if e.seq is not None)


async def reapply_manifests(eng: Any, applied: Iterable[ErasureManifest]) -> list[str]:
    """ADR-095 (restored backups): re-erase every id the manifests name that a restored backup
    brought back, in the read model or as identifying data in the log. A backup taken before an
    erasure holds the erased record and none of its manifests; this re-runs the hard forget
    (hide, redact, purge) for each such id under the namespace of its manifest's scope, so the
    restored deployment ends where the live one was. Idempotent: an id with no row and nothing
    left in the log is skipped. Returns the re-erased ids."""
    storage = eng._require_started()
    listed = list(applied)
    owner: dict[str, str] = {}
    for m in listed:
        for rid in m.ids.record_ids:
            owner.setdefault(rid, m.scope.tenant)
    if not owner:
        return []
    restored: set[str] = set()
    for rid in owner:
        row = await storage.get_record(rid)
        if row is not None:
            restored.add(rid)
            owner[rid] = row.namespace
    pending = set(owner) - restored
    after = 0
    while pending:
        batch = await storage.read_events(after_seq=after, limit=1000)
        if not batch:
            break
        for ev in batch:
            for rid in [r for r in pending if retained_fields(ev.payload, r)]:
                pending.discard(rid)
                restored.add(rid)
        after = max(e.seq for e in batch if e.seq is not None)
    again = sorted(reapply_on_restore(listed, restored))
    for rid in again:
        await eng.forget(rid, namespace=owner[rid], hard=True, actor="system", reason="restore")
    if again and tap.enabled():
        tap.event("trust.erasure.reapply", reapplied=len(again), manifests=len(listed))
    return again
