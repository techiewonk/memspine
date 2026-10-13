"""Derive stage: ``erasure_rederive`` (FU-ERASE), behind ``privacy.erasure_rederive``.

``privacy.erasure_v2`` hides a dependant of an erased record and queues it for re-derivation
(:func:`memspine.trust.pipeline.erasure_v2.queue`). This stage consumes that queue:

* **re-derivable kind** (a session or community summary, a mined fact) with live parents left: the
  producer's model role (``summarize`` / ``extract``, outside the namespace lock) is re-run over the
  remaining live parents only, the new version goes through ``derive/write.py:write_derived`` with
  those parents (a mined fact through the engine's ``deposit_fact``, which ends in the same door),
  and under the lock the hidden old row is hard-forgotten (content purged) and released;
* **no live parent left, or a kind that cannot be re-derived**: the hidden row is hard-forgotten and
  its queue entry marked ``dropped_no_rederive``.

A new version is tagged ``rederived_from:<old id>``, so a crash between the write and the forget
re-runs without a second model call or a duplicate (the old row is simply purged next time). The
queue is rebuilt from the MARKER events at start, so a restart resumes where it stopped.

Profile card entries are not records and are not queued: with ``offline.profile_card_v2`` the stage
also runs the card upkeep (``profile_sleep``), which expires entries whose support turn is gone.

Free text never reaches a tap event (ids and counts only).
"""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

from memspine.config import constants
from memspine.core.policies.consolidation import extractive_summary
from memspine.derive.pipeline.stages.consolidate import _render_members
from memspine.derive.pipeline.stages.context import PipelineContext
from memspine.derive.pipeline.stages.mining import _mining_transcript
from memspine.derive.pipeline.stages.offline_door import SUMMARY_CHANNELS
from memspine.derive.write import write_derived
from memspine.exceptions import MemspineError
from memspine.memories.episodic.sessions import _session_key
from memspine.observability import tap
from memspine.observability.logging import get_logger
from memspine.store import lineage as lineage_store
from memspine.store.records import MemoryRecord, RecordStatus, SourceInfo
from memspine.trust.firewall import instruction_shaped
from memspine.trust.pipeline import erasure_v2

_log = get_logger(__name__)

__all__ = ["REDERIVED_TAG", "erasure_rederive"]

REDERIVED_TAG = "rederived_from:"
_SUMMARY_CHANNELS = frozenset({"consolidation", constants.SESSION_CLOSE_CHANNEL}) | SUMMARY_CHANNELS


def _kind(record: MemoryRecord) -> str | None:
    """``summary`` / ``fact`` for a dependant the stage can re-derive, else None."""
    if record.source.channel in _SUMMARY_CHANNELS:
        return "summary"
    if record.source.channel == "mining" or "atomic_fact" in record.tags:
        return "fact"
    return None


def _live(record: MemoryRecord | None, ns: str) -> bool:
    return (
        record is not None
        and record.namespace == ns
        and record.status is RecordStatus.ACTIVATED
        and not record.quarantined
    )


async def erasure_rederive(ctx: PipelineContext) -> dict[str, object]:
    """Consume the erasure re-derivation queue (see the module docstring)."""
    if not ctx.config.privacy.erasure_rederive:
        return {"status": "skipped", "reason": "privacy.erasure_rederive is off"}
    eng = ctx.erasure_engine
    if eng is None or not ctx.config.privacy.erasure_v2:
        return {"status": "skipped", "reason": "privacy.erasure_v2 is off or no engine bound"}
    totals: Counter[str] = Counter()
    errors: list[str] = []
    for entry in erasure_v2.queue(eng):
        try:
            outcome = await _one(eng, ctx, entry)
        except Exception as exc:  # one bad item must not stop the queue; it stays queued
            errors.append(f"{entry.get('record_id')}: {type(exc).__name__}")
            _log.warning("erasure_rederive.failed", record=entry.get("record_id"), error=str(exc))
            continue
        totals[outcome] += 1
    if ctx.config.offline.profile_card_v2:
        from memspine.derive.pipeline.stages.profile_card import profile_sleep

        expired = (await profile_sleep(ctx)).get("expired")
        totals["profile_cards"] = expired if isinstance(expired, int) else 0
    if tap.enabled():
        tap.event("trust.erasure.rederive", errors=len(errors), **totals)
    stats: dict[str, object] = {"status": "partial" if errors else "ok", **totals}
    if errors:
        stats["errors"] = errors
    return stats


def _namespace(eng: Any, entry: dict[str, object], dep: MemoryRecord | None) -> str | None:
    if dep is not None:
        return dep.namespace
    purges: dict[str, dict[str, Any]] = eng.__dict__.get("_erasure_purges", {})
    ns = entry.get("namespace") or purges.get(str(entry.get("manifest_id")), {}).get("namespace")
    return str(ns) if ns else None


async def _one(eng: Any, ctx: PipelineContext, entry: dict[str, object]) -> str:
    storage = eng._require_started()
    rid = str(entry["record_id"])
    st = erasure_v2.state(eng)
    if rid not in st.hidden:
        eng.__dict__.get("_erasure_queue", {}).pop(rid, None)
        return "stale"
    dep = await storage.get_record(rid)
    ns = _namespace(eng, entry, dep)
    if ns is None:
        return "unresolved"
    if dep is None:  # the row is gone already (a retried forget): nothing left to re-derive
        await erasure_v2.release(eng, [rid], ns)
        return "released"

    # Parents still alive: those the plan left plus the row's own, never an erased or hidden one.
    candidates = dict.fromkeys(
        [*dep.source.parents, *map(str, entry.get("remaining_sources") or [])]
    )
    live: list[MemoryRecord] = []
    for pid in candidates:
        if pid == rid or pid in st.erased or pid in st.hidden:
            continue
        parent = await storage.get_record(pid)
        if _live(parent, ns):
            assert parent is not None
            live.append(parent)
    kind = _kind(dep)

    if not live or kind is None or (kind == "fact" and ctx.mine_facts is None):
        return await _drop(eng, storage, ns, dep)

    done = await _already_rederived(storage, ns, rid)
    if done:  # crash between the new write and the old row's purge: just purge it
        return await _retire(eng, storage, ns, dep)

    if kind == "summary":
        return await _summary(eng, ctx, storage, ns, dep, live)
    return await _fact(eng, ctx, storage, ns, dep, live)


async def _already_rederived(storage: Any, ns: str, rid: str) -> bool:
    tag = f"{REDERIVED_TAG}{rid}"
    return any(tag in r.tags for r in await storage.list_records(ns, "semantic"))


async def _purge_row(eng: Any, storage: Any, ns: str, rid: str) -> bool:
    """Hard-forget one hidden row and its lineage edges (lock held). False: legal hold."""
    try:
        await eng._forget_target(storage, ns, rid, True)
    except MemspineError:
        return False
    await eng._forget_many(storage, ns, [rid], True)
    engine = storage._client.engine
    gone = await lineage_store.parents_of(engine, [rid])
    kids = await lineage_store.children_of(engine, [rid])
    edges = [e for es in (*gone.values(), *kids.values()) for e in es]
    if edges:
        await lineage_store.delete_edges(engine, edges)
    return True


async def _drop(eng: Any, storage: Any, ns: str, dep: MemoryRecord) -> str:
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        if not await _purge_row(eng, storage, ns, dep.record_id):
            return "held"  # legal hold: it stays hidden and queued
        await erasure_v2.mark_dropped(eng, [dep.record_id], ns)
    return "dropped_no_rederive"


async def _retire(eng: Any, storage: Any, ns: str, dep: MemoryRecord) -> str:
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        if not await _purge_row(eng, storage, ns, dep.record_id):
            return "held"
        await erasure_v2.release(eng, [dep.record_id], ns)
    return "rederived"


def _retag(dep: MemoryRecord) -> list[str]:
    tags = [t for t in dep.tags if not t.startswith(REDERIVED_TAG)]
    out: list[str] = []
    for t in tags:
        if t.startswith(constants.SUMMARY_SINCE_REBUILD_PREFIX):
            t = f"{constants.SUMMARY_SINCE_REBUILD_PREFIX}0"  # a full rebuild over the live turns
        out.append(t)
    out.append(f"{REDERIVED_TAG}{dep.record_id}")
    return out


async def _summary(
    eng: Any,
    ctx: PipelineContext,
    storage: Any,
    ns: str,
    dep: MemoryRecord,
    live: list[MemoryRecord],
) -> str:
    members = sorted(live, key=lambda r: (r.valid_from, r.record_id))
    text = extractive_summary(
        [m.content for m in members], constants.CONSOLIDATION_SUMMARY_MAX_CHARS
    )
    if ctx.summarize is not None:  # outside the namespace lock
        try:
            text = await ctx.summarize(_render_members(members))
        except Exception as exc:  # the LLM is an enhancer, never a gate (N6)
            _log.warning("erasure_rederive.summarize_fallback", namespace=ns, error=str(exc))
    parents = [m.record_id for m in members]
    # A consolidation summary is keyed by its membership: the new key makes consolidate skip it.
    message_id = (
        _session_key(members) if dep.source.channel == "consolidation" else dep.source.message_id
    )
    record = MemoryRecord(
        namespace=ns,
        memory_type="semantic",
        content=text,
        valid_from=dep.valid_from,
        valid_to=dep.valid_to,
        source=SourceInfo(
            role=dep.source.role,
            channel=dep.source.channel,
            message_id=message_id,
            parents=parents,
        ),
        trust=min([dep.trust, *(m.trust for m in members)]),
        instruction_flag=instruction_shaped(text) or any(m.instruction_flag for m in members),
        tags=_retag(dep),
    )
    cap = [record.trust]
    integrity = await eng._parent_trust_cap(ns, parents)
    if integrity:
        cap.extend(integrity)
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        if await eng._derived_parents_gone(ns, parents, "erasure_rederive"):  # I72
            return "deferred"
        await write_derived(
            eng,
            storage,
            ns,
            record,
            "summary",
            "system",
            parents=parents,
            roles=dict.fromkeys(parents, "member"),  # a later erasure re-runs it, never keeps it
            trust_cap=cap,
            site="erasure_rederive",
        )
        if not await _purge_row(eng, storage, ns, dep.record_id):
            return "held"
        await erasure_v2.release(eng, [dep.record_id], ns)
    return "rederived"


async def _fact(
    eng: Any,
    ctx: PipelineContext,
    storage: Any,
    ns: str,
    dep: MemoryRecord,
    live: list[MemoryRecord],
) -> str:
    assert ctx.mine_facts is not None
    if ctx.deposit_fact is None:
        return await _drop(eng, storage, ns, dep)
    segment = sorted(live, key=lambda r: (r.valid_from, r.record_id))
    mined = await ctx.mine_facts(_mining_transcript(segment, False))  # outside the lock
    ent = (dep.entity or "").casefold()
    attr = (dep.attribute or "").casefold()
    picked = [
        f
        for f in mined
        if ent
        and (f.entity or "").casefold() == ent
        and (not attr or (f.attribute or "").casefold() == attr)
    ]
    if not picked:  # the live turns no longer state this fact
        return await _drop(eng, storage, ns, dep)
    parents = [m.record_id for m in segment]
    stored = 0
    for fact in picked:
        text = f"{fact.entity} {fact.attribute}: {fact.value}"
        written = await ctx.deposit_fact(
            ns,
            text,
            fact.entity or None,
            fact.attribute or None,
            parents,
            dep.valid_from,
            f"rederived:{dep.record_id}",
            kind=getattr(fact, "kind", "event") or "event",
            extra_tags=[f"{REDERIVED_TAG}{dep.record_id}"],
        )
        stored += written is not None
    if not stored:
        return "deferred"  # a parent left meanwhile (I72): next run
    return await _retire(eng, storage, ns, dep)
