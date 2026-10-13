"""Trust: erasure v2 (T5-04; LIB-09 ``trust/erasure_plan.py``), behind ``privacy.erasure_v2``.

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
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from memspine.observability import tap
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.lineage import LineageEdge
from memspine.store import lineage as lineage_store
from memspine.store.events import EventKind, EventLogMode, MemoryEvent
from memspine.store.scope import validate_namespace
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
    visible,
)

__all__ = [
    "MARKER_MANIFEST",
    "MARKER_PURGE",
    "MARKER_RELEASE",
    "PURGE_BATCH",
    "PURGE_STORES",
    "enabled",
    "forget",
    "load",
    "queue",
    "release",
    "resume_purges",
    "status",
    "visible_id",
]

MARKER_MANIFEST = "erasure_manifest"
MARKER_PURGE = "erasure_purge"
MARKER_RELEASE = "erasure_release"
PURGE_STORES = ("vector", "lexical", "graph", "cache")
PURGE_BATCH = 256

_EMPTY = ErasureState()


def enabled(eng: Any) -> bool:
    return bool(eng._config().privacy.erasure_v2)


def state(eng: Any) -> ErasureState:
    return eng.__dict__.get("_erasure_state") or _EMPTY


def visible_id(eng: Any, record_id: str) -> bool:
    """POL-1: False for an erased or hidden id. One dict read while nothing was ever erased."""
    st = eng.__dict__.get("_erasure_state")
    return st is None or visible(record_id, st)


def queue(eng: Any) -> list[dict[str, object]]:
    """The re-derivation queue (ids, action, remaining live sources), oldest first."""
    q: dict[str, dict[str, object]] = eng.__dict__.get("_erasure_queue", {})
    return [dict(v) for v in q.values()]


def status(eng: Any) -> dict[str, object]:
    st = state(eng)
    purges: dict[str, dict[str, Any]] = eng.__dict__.get("_erasure_purges", {})
    return {
        "enabled": enabled(eng),
        "generation": max(st.generation, eng.__dict__.get("_erasure_generation", 0)),
        "erased": len(st.erased),
        "hidden": len(st.hidden),
        "queued": len(queue(eng)),
        "purges": {k: dict(v) for k, v in sorted(purges.items())},
    }


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def _marker(eng: Any, ns: str, payload: dict[str, object]) -> None:
    await eng._append_and_project(
        MemoryEvent(kind=EventKind.MARKER, namespace=ns, actor="system", payload=payload)
    )


# ------------------------------------------------------------------ load


async def load(eng: Any) -> None:
    """Rebuild the erasure state, the queue and the purge table from the MARKER events."""
    erased: set[str] = set()
    hidden: set[str] = set()
    generation = 0
    q: dict[str, dict[str, object]] = {}
    purges: dict[str, dict[str, Any]] = {}
    storage = eng._storage
    if storage is not None and eng._config().event_log.mode is not EventLogMode.EPHEMERAL:
        after = 0
        while True:
            batch = await storage.read_events(after_seq=after, limit=1000)
            if not batch:
                break
            for ev in batch:
                p = ev.payload if ev.kind is EventKind.MARKER else {}
                kind = p.get("marker")
                if kind == MARKER_MANIFEST:
                    man = p.get("manifest") or {}
                    ids = man.get("ids") or {}
                    erased.update(ids.get("record_ids") or [])
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
                elif kind == MARKER_PURGE and str(p.get("manifest_id")) in purges:
                    purges[str(p["manifest_id"])].update(
                        state=p.get("state"), done=p.get("done", 0), total=p.get("total", 0)
                    )
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
    if eng._config().write.lineage_table:
        for e in await lineage_store.load_edges(storage._client.engine, ids):
            graph.add_edge(e)
    roles = await _derivation_roles(storage, ns)
    wanted = set(ids)
    for cid in desc:
        rec = await storage.get_record(cid)
        if rec is None:
            continue
        for pid in dict.fromkeys(rec.source.parents):
            if pid != cid:
                graph.add_edge(LineageEdge(child=cid, parent=pid, role="support"))
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
    """The hard forget under ``privacy.erasure_v2`` (see the module docstring)."""
    storage = eng._require_started()
    ns = validate_namespace(namespace)
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        record = await eng._forget_target(storage, ns, record_id, True)  # scope + legal hold
        texts = eng._erasable_texts(record) if record is not None else []
        prior = state(eng)
        graph = await build_graph(eng, storage, ns, record_id, dependants=cascade is not False)
        generation = max(prior.generation, eng.__dict__.get("_erasure_generation", 0)) + 1
        plan = plan_erasure([record_id], graph, generation=generation, already_lost=prior.lost)
        for a in plan.actions:
            if a.hidden:
                dep = await storage.get_record(a.record_id)
                if dep is not None and dep.namespace == ns:
                    texts.extend(eng._erasable_texts(dep))
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
            }
            for a in plan.actions
            if a.needs_rederivation
        ]
        for item in queued:
            q[str(item["record_id"])] = item
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
        keep_lineage = str(reason or "").startswith("retention")  # expiry keeps lineage (ER-27)
        if eng._config().write.lineage_table and plan.edges_to_remove and not keep_lineage:
            await lineage_store.delete_edges(storage._client.engine, plan.edges_to_remove)
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
    await eng._audit_action(
        "forget",
        ns,
        [*sorted(plan.erased), *hidden_ids],
        actor=actor,
        reason=reason,
        hard=True,
    )
    await _run_purge(eng, manifest_id, texts)


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
        if eng._client is not None:
            await eng._client.checkpoint()
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
