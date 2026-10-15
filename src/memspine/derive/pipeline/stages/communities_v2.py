# ruff: noqa: E501
"""Derive stage: entity communities and their summaries (O4-06, ``offline.communities_v2``).

The wiring of ``memspine.derive.communities`` (LIB-36). Today's ``reorganize`` partitions the record
association graph and rewrites a summary parent whenever membership drifts; this path clusters the
*entity* graph instead and rebuilds only what changed:

1. **Adapter in.** A live record that names two or more entities (live ``mentions`` edges, the entity
   policy already applied) adds one undirected :class:`CommunityEdge` per entity pair; the weight is the
   number of such records and the edge's ``assertion_ids`` are those record ids (the summary lineage). A
   record naming more than ``community_max_entities_per_record`` entities adds none (counted ``wide``).
   Hubs (configured names, plus entities linked to ``community_hub_fraction`` of the others) are kept out.
2. **Library.** ``update`` re-clusters (deterministic label propagation, stable ids) against the previous
   communities, which are read back from the live ``community_summary`` records (``com:`` and ``cm:``
   tags, ``source.parents``), and reports created / changed / unchanged / removed.
   ``plan_summary_jobs`` fits the dirty ones into the token budget; the rest stay dirty and are counted.
3. **Model call outside the lock.** A summary of three or more entities uses the existing ``summarize``
   role (extractive without it or on failure); the namespace lock is taken only to check that the parents
   are still live, to write through the derived-write door (lineage, trust cap, firewall) and to archive the
   summary it replaces. A summary is never more trusted than its least-trusted parent (D-47 section 5).

Nothing is deleted: a dissolved community's summary is archived. Off by default.
"""

from __future__ import annotations

from collections import Counter
from itertools import combinations

from memspine.compose.compression import CompressionPolicy
from memspine.config import constants
from memspine.core.policies.consolidation import extractive_summary
from memspine.derive.communities import (
    Community,
    CommunityConfig,
    CommunityEdge,
    GraphDelta,
    plan_summary_jobs,
    update,
)
from memspine.derive.pipeline.stages.context import (
    PipelineContext,
    _parents_gone,
    live_record,
    live_records,
    sweep_namespaces,
)
from memspine.derive.pipeline.stages.offline_door import (
    COMMUNITY_CHANNEL,
    COMMUNITY_TAG,
    SUMMARY_CHANNELS,
    archive_records,
    is_live,
    write_summary_record,
)
from memspine.memories.associative.entities import (
    MENTIONS_REL,
    canonical_entity,
    entity_node_id,
    parse_entity_node,
)
from memspine.observability import tap
from memspine.observability.logging import get_logger
from memspine.store.records import MemoryRecord, SourceInfo, chrono_key
from memspine.trust.firewall import instruction_shaped

_log = get_logger(__name__)

_COM_PREFIX = "com:"
_MEMBER_PREFIX = "cm:"


def _community_id_of(record: MemoryRecord) -> str | None:
    return next((t[len(_COM_PREFIX) :] for t in record.tags if t.startswith(_COM_PREFIX)), None)


async def _previous(
    ctx: PipelineContext, namespace: str
) -> tuple[list[Community], dict[str, list[MemoryRecord]]]:
    """The live community summaries of ``namespace`` as library communities, and their records by id."""
    by_id: dict[str, list[MemoryRecord]] = {}
    for record in await live_records(ctx, namespace, "semantic"):
        if record.source.channel != COMMUNITY_CHANNEL or not is_live(record):
            continue
        cid = _community_id_of(record)
        if cid is not None:
            by_id.setdefault(cid, []).append(record)
    prev: list[Community] = []
    for cid, records in sorted(by_id.items()):
        newest = max(records, key=chrono_key)
        members = sorted(
            t[len(_MEMBER_PREFIX) :] for t in newest.tags if t.startswith(_MEMBER_PREFIX)
        )
        prev.append(
            Community(
                id=cid,
                members=tuple(members),
                top_entities=(),
                assertion_ids=tuple(newest.source.parents),
            )
        )
    return prev, by_id


async def _entity_edges(
    ctx: PipelineContext, namespace: str, max_entities: int
) -> tuple[list[CommunityEdge], dict[str, MemoryRecord], int]:
    """Entity-pair edges from the live records' ``mentions`` links, the live records, and the wide count."""
    assert ctx.graph is not None
    mentions: dict[str, set[str]] = {}
    for edge in await ctx.graph.edge_list(namespace):
        if edge.rel_type != MENTIONS_REL or edge.weight <= 0:
            continue
        parsed = parse_entity_node(edge.dst)
        if parsed is not None and parsed[0] == namespace:
            mentions.setdefault(edge.src, set()).add(edge.dst)
    live: dict[str, MemoryRecord] = {}
    for rid in sorted(mentions):
        record = await live_record(ctx, rid)
        if (
            record is not None
            and record.namespace == namespace
            and is_live(record)
            and record.source.channel not in SUMMARY_CHANNELS
        ):
            live[rid] = record
    weight: Counter[tuple[str, str]] = Counter()
    ids: dict[tuple[str, str], set[str]] = {}
    wide = 0
    for rid in sorted(live):
        entities = sorted(mentions[rid])
        if len(entities) < 2:
            continue
        if len(entities) > max_entities:
            wide += 1
            continue
        for a, b in combinations(entities, 2):
            weight[(a, b)] += 1
            ids.setdefault((a, b), set()).add(rid)
    edges = [
        CommunityEdge(src=a, dst=b, weight=float(w), assertion_ids=tuple(sorted(ids[(a, b)])))
        for (a, b), w in sorted(weight.items())
    ]
    return edges, live, wide


def _hubs(
    namespace: str, edges: list[CommunityEdge], names: list[str], fraction: float
) -> frozenset[str]:
    hubs = {entity_node_id(namespace, canonical_entity(n)) for n in names if n.strip()}
    neighbours: dict[str, set[str]] = {}
    for e in edges:
        neighbours.setdefault(e.src, set()).add(e.dst)
        neighbours.setdefault(e.dst, set()).add(e.src)
    if fraction > 0 and len(neighbours) >= 10:
        need = fraction * (len(neighbours) - 1)
        hubs |= {n for n, nb in neighbours.items() if len(nb) >= need}
    return frozenset(hubs)


async def reorganize_v2(ctx: PipelineContext) -> dict[str, object]:
    """O4-06: entity communities over each namespace's graph, summaries for the dirty ones only."""
    if ctx.append_event is None:
        return {"status": "skipped", "reason": "read-only context (no write door)"}
    if ctx.graph is None:
        return {"status": "skipped", "reason": "no graph store (associative memory disabled)"}
    totals: Counter[str] = Counter()
    errors: list[str] = []
    for namespace in await sweep_namespaces(ctx):
        try:
            await _namespace(ctx, namespace, totals)
        except Exception as exc:  # one bad namespace must not kill the sweep
            errors.append(f"{namespace}: {exc}")
            _log.warning("communities_v2.namespace_failed", namespace=namespace, error=str(exc))
    stats: dict[str, object] = {"status": "partial" if errors else "ok", **totals}
    if errors:
        stats["errors"] = errors
    return stats


async def _namespace(ctx: PipelineContext, namespace: str, totals: Counter[str]) -> None:
    assert ctx.append_event is not None
    cfg = ctx.config.offline
    inflate = CompressionPolicy.bind()
    edges, live, wide = await _entity_edges(ctx, namespace, cfg.community_max_entities_per_record)
    hubs = _hubs(namespace, edges, cfg.community_hubs, cfg.community_hub_fraction)
    prev, prev_records = await _previous(ctx, namespace)
    removed_assertions = {a for p in prev for a in p.assertion_ids} - set(live)
    ccfg = CommunityConfig(
        hubs=hubs,
        hub_weight=0.0,
        min_size=cfg.community_min_size,
        max_community_size=cfg.community_max_size,
    )
    changes = update(
        prev,
        GraphDelta(removed_assertion_ids=frozenset(removed_assertions)),
        edges=edges,
        config=ccfg,
    )
    plan = plan_summary_jobs(
        changes,
        cfg.community_summary_budget_tokens,
        trust_of={rid: rec.trust for rid, rec in live.items()},
    )
    oversized = [c.id for c in changes.communities if c.oversized]
    by_id = {c.id: c for c in changes.communities}

    written = skipped_gone = model_calls = quarantined = 0
    for job in plan.jobs:
        community = by_id[job.community_id]
        inputs = sorted(
            (inflate.inflate(live[a]) for a in job.input_assertion_ids if a in live), key=chrono_key
        )
        if not inputs:
            continue
        text = extractive_summary(
            [m.content for m in inputs], constants.CONSOLIDATION_SUMMARY_MAX_CHARS
        )
        used_model = False
        if ctx.summarize is not None and len(job.members) >= 3:
            try:
                text = await ctx.summarize("\n".join(m.content for m in inputs))
                used_model = True
                model_calls += 1
            except Exception as exc:  # the LLM is an enhancer, never a gate (N6)
                _log.warning(
                    "communities_v2.summarize_fallback", namespace=namespace, error=str(exc)
                )
        async with ctx.lock(namespace):
            if await _parents_gone(ctx, namespace, list(job.lineage), "communities"):
                skipped_gone += (
                    1  # a parent was forgotten since it was read; the next sweep redoes it
                )
                continue
            summary = MemoryRecord(
                namespace=namespace,
                memory_type="semantic",
                content=text,
                tags=[
                    COMMUNITY_TAG,
                    f"{_COM_PREFIX}{job.community_id}",
                    *[f"{_MEMBER_PREFIX}{m}" for m in community.members],
                ],
                valid_from=max(m.valid_from for m in inputs),
                source=SourceInfo(
                    role=constants.DERIVED_ROLE,
                    channel=COMMUNITY_CHANNEL,
                    message_id=job.community_id,
                    parents=list(job.lineage),
                ),
                trust=job.trust_cap if job.trust_cap is not None else min(m.trust for m in inputs),
                instruction_flag=instruction_shaped(text)
                or any(m.instruction_flag for m in inputs),
            )
            stored, quar = await write_summary_record(
                ctx, namespace, summary, job.lineage, "L3", "communities"
            )
            quarantined += int(quar)
            await archive_records(
                ctx,
                namespace,
                [
                    r
                    for r in prev_records.get(job.community_id, [])
                    if r.record_id != stored.record_id
                ],
                "community_summary->superseded",
                "membership_drift",
            )
            written += 1
        if tap.enabled():
            tap.event(
                "job.communities.summary",
                ns=namespace,
                community=job.community_id,
                reason=job.reason,
                members=len(job.members),
                lineage=len(job.lineage),
                inputs=len(job.input_assertion_ids),
                est_tokens=job.estimated_tokens,
                trust_cap=job.trust_cap,
                model=used_model,
                quarantined=quar,
            )

    retired = 0
    if changes.removed:
        async with ctx.lock(namespace):
            for cid in changes.removed:
                retired += await archive_records(
                    ctx,
                    namespace,
                    prev_records.get(cid, []),
                    "community_summary->superseded",
                    "dissolved",
                )
    totals.update(
        communities=len(changes.communities),
        created=len(changes.created),
        changed=len(changes.changed),
        unchanged=len(changes.unchanged),
        removed=len(changes.removed),
        summaries=written,
        deferred=len(plan.deferred),
        oversized=len(oversized),
        summary_model_calls=model_calls,
    )
    if tap.enabled():
        tap.event(
            "job.communities",
            ns=namespace,
            entities=len({n for e in edges for n in (e.src, e.dst)}),
            edges=len(edges),
            wide_records=wide,
            hubs=len(hubs),
            communities=len(changes.communities),
            created=len(changes.created),
            changed=len(changes.changed),
            unchanged=len(changes.unchanged),
            removed=len(changes.removed),
            oversized=oversized,
            jobs_planned=len(plan.jobs),
            jobs_deferred=len(plan.deferred),
            deferred=list(plan.deferred),
            spent_tokens=plan.spent_tokens,
            budget_tokens=cfg.community_summary_budget_tokens,
            summaries_written=written,
            skipped_parents_gone=skipped_gone,
            quarantined=quarantined,
            retired=retired,
            model_calls=model_calls,
        )


__all__ = ["reorganize_v2"]
