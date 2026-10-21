"""Derive stage: reorganize (graph communities and their summary parents) (F-15)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial

from memspine.compose.compression import CompressionPolicy
from memspine.config import constants
from memspine.core.policies.community import CommunityOptions, CommunityPolicy
from memspine.core.policies.consolidation import extractive_summary
from memspine.derive.pipeline.stages.context import (
    PipelineContext,
    _parents_gone,
    _policy_options,
    live_record,
    live_records,
    sweep_namespaces,
)
from memspine.derive.pipeline.stages.index import COMMUNITY_PARTITION_MARKER, CommunityState
from memspine.derive.pipeline.stages.offline_door import link_summary, stamp_summary
from memspine.memories.associative.communities import (
    PartitionResult,
    communities_available,
    partition_graph,
)
from memspine.memories.associative.links import link_event
from memspine.observability.logging import get_logger
from memspine.services.graph.base import GraphEdge
from memspine.store.events import EventKind, MemoryEvent, fingerprint_payload
from memspine.store.records import MemoryRecord, RecordStatus, SourceInfo, chrono_key
from memspine.trust.firewall import instruction_shaped

_log = get_logger(__name__)


def _community_algorithm(options: CommunityOptions) -> str | None:
    """KB-12: the algorithm this sweep runs, or None for the D-40 no-op.

    ``auto`` and ``leiden`` need the ``[community]`` extra; without it the
    reorganizer stays the logged no-op it has always been. Only an explicit
    ``lpa`` runs without the extra.
    """
    if options.algorithm == "lpa":
        return "lpa"
    return "leiden" if communities_available() else None


def _anchors(labels: dict[str, int]) -> dict[str, str]:
    """A partition as node -> smallest member of its community: a numbering-free
    form, so a delta between two sweeps only lists nodes that really moved."""
    first: dict[int, str] = {}
    for node in sorted(labels):
        first.setdefault(labels[node], node)
    return {node: first[label] for node, label in labels.items()}


def _from_anchors(anchors: dict[str, str]) -> dict[str, int]:
    order = {anchor: i for i, anchor in enumerate(sorted(set(anchors.values())))}
    return {node: order[anchor] for node, anchor in anchors.items()}


async def _reorganize_records(ctx: PipelineContext, namespace: str) -> list[MemoryRecord]:
    """Every summary parent this pipeline wrote in ``namespace``, any status."""
    return [
        record
        for record in await live_records(ctx, namespace, "semantic")
        if record.source.channel == "reorganize"
    ]


async def _community_links(ctx: PipelineContext, parent_id: str) -> set[str]:
    """The live member -> parent ``community`` links of one summary parent."""
    assert ctx.graph is not None
    return {
        edge.src
        for edge in await ctx.graph.edges_of(parent_id)
        if edge.rel_type == "community" and edge.dst == parent_id and edge.weight > 0.0
    }


async def _summarised_set(ctx: PipelineContext, parent_id: str) -> frozenset[str]:
    """The members a summary parent summarised: its ``derived_from`` targets
    (projected from the WRITE's provenance, never tombstoned)."""
    assert ctx.graph is not None
    return frozenset(
        edge.dst
        for edge in await ctx.graph.edges_of(parent_id)
        if edge.rel_type == "derived_from" and edge.src == parent_id
    )


async def _parent_partition(ctx: PipelineContext, parents: list[MemoryRecord]) -> dict[str, int]:
    """The previous partition as the live summary parents record it (one label
    per parent, in key order). Derived from the event log through the graph
    projection, so a rebuild reproduces it (D0.1)."""
    labels: dict[str, int] = {}
    for label, parent in enumerate(sorted(parents, key=lambda p: p.source.message_id or "")):
        for member in sorted(await _community_links(ctx, parent.record_id)):
            labels.setdefault(member, label)
    return labels


@dataclass
class _SummaryKeeper:
    """#84: match a drifted community to the live parent that summarised
    nearly the same members, so it keeps its summary instead of a rewrite."""

    threshold: float
    #: parent record id -> (parent, the member set it summarised).
    summarised: dict[str, tuple[MemoryRecord, frozenset[str]]]
    claimed: set[str] = field(default_factory=set)

    def match(self, member_ids: set[str]) -> MemoryRecord | None:
        best: MemoryRecord | None = None
        best_score = self.threshold
        for parent_id in sorted(self.summarised):
            if parent_id in self.claimed:
                continue
            parent, summarised = self.summarised[parent_id]
            union = member_ids | summarised
            score = len(member_ids & summarised) / len(union) if union else 0.0
            if score >= best_score and (best is None or score > best_score):
                best, best_score = parent, score
        return best


@dataclass
class _Community:
    """One detected community resolved to its live, summarisable members."""

    members: list[MemoryRecord]
    member_ids: list[str]
    key: str
    namespace: str


async def reorganize(ctx: PipelineContext) -> dict[str, object]:
    """D-40/D-42 background graph reorganizer: communities over each
    namespace's association graph (KB-9) → one consolidation-style summary
    parent per community of >= REORGANIZE_MIN_COMMUNITY_SIZE members, members
    linked to the parent via LINK events (ADR-015).

    The partition is the KB-12 hybrid (ADR-043): Leiden warm-started from the
    previous partition, then LPA refinement; with ``community.incremental`` a
    sleep only places new nodes until a refresh trigger fires. Summary parents
    and their ``community`` links are not partition input, so a summary never
    joins its own community.

    No-op ("skipped") without a graph store (associative disabled) or, unless
    ``algorithm: lpa``, without the ``[community]`` extra (D-40). Idempotent:
    the parent's provenance key fingerprints the full membership, so an
    unchanged community is skipped and a drifted one supersedes its stale
    parent (same pattern as consolidate), unless ``summary_keep_jaccard`` keeps
    it (#84). Parents mirror consolidation summaries: derived trust = min(member
    trust) (D-47 §5), instruction framing stays flagged, quarantined/non-active
    members never contribute.
    """
    if ctx.config.offline.communities_v2:  # O4-06 (LIB-36); off by default
        from memspine.derive.pipeline.stages.communities_v2 import reorganize_v2

        return await reorganize_v2(ctx)
    if ctx.append_event is None:
        return {"status": "skipped", "reason": "read-only context (no write door)"}
    if ctx.graph is None:
        return {"status": "skipped", "reason": "no graph store (associative memory disabled)"}
    community_opts = CommunityPolicy.bind(_policy_options(ctx, "associative", "community")).options
    assert isinstance(community_opts, CommunityOptions)
    algorithm = _community_algorithm(community_opts)
    if algorithm is None:
        return {
            "status": "skipped",
            "reason": "graspologic-native not installed — `pip install memspine[community]` "
            "or set community.algorithm: lpa (D-40)",
        }
    inflate = CompressionPolicy.bind()
    keep_on = community_opts.summary_keep_jaccard < 1.0
    if community_opts.incremental:
        await ctx.session_index.refresh(ctx.storage)
    parents = 0
    superseded = 0
    kept = 0
    detected = 0
    modes: dict[str, str] = {}
    errors: list[str] = []
    fresh_keys: dict[str, set[str]] = {}  # namespace -> live community keys
    # KB-9: one partition per namespace, over that namespace's edges only, so
    # no community (and no summary parent) ever spans two tenants.
    for graph_namespace in await sweep_namespaces(ctx):
        reorg = await _reorganize_records(ctx, graph_namespace)
        parent_ids = {record.record_id for record in reorg}
        active = [record for record in reorg if record.status is RecordStatus.ACTIVATED]
        edges = [
            edge
            for edge in await ctx.graph.edge_list(graph_namespace)
            # GP-2: entity ``mentions`` edges are not associations either.
            if edge.rel_type not in ("community", "mentions")
            and edge.src not in parent_ids
            and edge.dst not in parent_ids
        ]
        try:
            result: PartitionResult | None = await _partition_namespace(
                ctx, graph_namespace, edges, active, algorithm, community_opts
            )
        except Exception as exc:  # one bad namespace must not kill the sweep
            errors.append(f"{graph_namespace}: {exc}")
            _log.warning(
                "reorganize.partition_failed",
                namespace=graph_namespace,
                error=str(exc),
                exc_info=True,
            )
            result = None
        if result is None or result.collapsed:
            # No change: the namespace keeps every live parent it had.
            fresh_keys.setdefault(graph_namespace, set()).update(
                str(record.source.message_id) for record in active
            )
            continue
        if community_opts.incremental:
            modes[graph_namespace] = result.mode
        keeper: _SummaryKeeper | None = None
        if keep_on:
            keeper = _SummaryKeeper(
                community_opts.summary_keep_jaccard,
                {
                    parent.record_id: (parent, await _summarised_set(ctx, parent.record_id))
                    for parent in active
                },
            )
        resolved: list[_Community] = []
        for community in result.communities(community_opts.min_size):
            detected += 1
            try:
                found = await _resolve_community(ctx, inflate, community)
            except Exception as exc:  # one bad community must not kill the sweep
                errors.append(f"{community[0]}…: {exc}")
                _log.warning(
                    "reorganize.community_failed",
                    members=len(community),
                    error=str(exc),
                    exc_info=True,
                )
                continue
            if found is not None:
                resolved.append(found)
        if keeper is not None:
            # An exact match is claimed first, so a near match never takes it.
            live = {record.source.message_id: record.record_id for record in active}
            keeper.claimed.update(live[c.key] for c in resolved if c.key in live)
        for found in resolved:
            try:
                created, live_key = await _reorganize_community(ctx, found, keeper)
            except Exception as exc:  # one bad community must not kill the sweep
                errors.append(f"{found.member_ids[0]}…: {exc}")
                _log.warning(
                    "reorganize.community_failed",
                    members=len(found.member_ids),
                    error=str(exc),
                    exc_info=True,
                )
                continue
            fresh_keys.setdefault(found.namespace, set()).add(live_key)
            parents += created
            kept += int(live_key != found.key)
    # Membership drift: a stale parent whose community dissolved or changed
    # membership is archived — never a silent second active summary (D-42).
    for namespace in await sweep_namespaces(ctx):
        try:
            # Same per-namespace unit as the engine's write verbs (M5): the
            # read-then-archive-then-tombstone pass must not interleave with
            # a concurrent forget cascade in this namespace.
            async with ctx.lock(namespace):
                superseded += await _supersede_stale_parents(
                    ctx, namespace, fresh_keys.get(namespace, set())
                )
        except Exception as exc:  # one bad namespace must not kill the sweep
            errors.append(f"{namespace}: {exc}")
            _log.warning(
                "reorganize.supersede_failed",
                namespace=namespace,
                error=str(exc),
                exc_info=True,
            )
    stats: dict[str, object] = {
        "status": "ok",
        "communities": detected,
        "parents": parents,
        "superseded": superseded,
    }
    if keep_on:
        stats["kept"] = kept
    if modes:
        stats["modes"] = modes
    if errors:
        stats.update(status="partial", errors=errors)
    return stats


async def _partition_namespace(
    ctx: PipelineContext,
    namespace: str,
    edges: list[GraphEdge],
    active: list[MemoryRecord],
    algorithm: str,
    options: CommunityOptions,
) -> PartitionResult:
    """One namespace's partition (KB-12): full (warm-started from the previous
    partition) or, with ``incremental``, new-node placement until a refresh
    trigger fires. Incremental state rides MARKER events, so a rebuild from
    the log reproduces it (D0.1)."""
    state = ctx.session_index.communities.get(namespace) if options.incremental else None
    if state is not None:
        previous = _from_anchors(state.anchors)
    else:
        previous = await _parent_partition(ctx, active)
    run = partial(
        partition_graph,
        edges,
        algorithm="lpa" if algorithm == "lpa" else "leiden",
        previous=previous or None,
        resolution=options.resolution,
        randomness=options.randomness,
        random_seed=options.random_seed,
        max_cluster_size=options.max_cluster_size,
        refine_passes=options.refine_passes,
        incremental_passes=options.incremental_passes,
    )
    # Partitioning is CPU work — keep it off the event loop (same pattern as
    # compress()'s zstd call).
    result: PartitionResult | None = None
    if state is not None and previous and state.sleeps + 1 < options.refresh_every:
        result = await asyncio.to_thread(run, mode="incremental")
        drifted = state.placed + result.placed > options.refresh_fraction * len(result.labels)
        if drifted or result.collapsed:
            # Refresh trigger: too much was placed incrementally, or the
            # incremental step collapsed — a full run decides instead (a
            # collapsed incremental run must never lock the namespace).
            result = None
    if result is None:
        result = await asyncio.to_thread(run, mode="full")
    if options.incremental and (state is not None or not result.collapsed):
        # A collapse is recorded too (no membership change, sleeps + 1), so the
        # sleep counter keeps advancing and ``refresh_every`` still fires.
        await _record_partition(ctx, namespace, state, result)
    return result


async def _record_partition(
    ctx: PipelineContext,
    namespace: str,
    state: CommunityState | None,
    result: PartitionResult,
) -> None:
    """Append the partition delta + refresh counters as a MARKER event."""
    assert ctx.append_event is not None
    before = state.anchors if state is not None else {}
    # A collapsed run changes no membership: its marker only advances counters.
    anchors = dict(before) if result.collapsed else _anchors(result.labels)
    if result.collapsed and state is not None:
        sleeps, placed = state.sleeps + 1, state.placed
    elif result.mode == "incremental" and state is not None:
        sleeps, placed = state.sleeps + 1, state.placed + result.placed
    else:
        sleeps, placed = 0, 0
    await ctx.append_event(
        MemoryEvent(
            kind=EventKind.MARKER,
            namespace=namespace,
            actor="system",
            payload={
                "marker": COMMUNITY_PARTITION_MARKER,
                "stage": "reorganize",
                "mode": result.mode,
                "set": {n: a for n, a in sorted(anchors.items()) if before.get(n) != a},
                "drop": sorted(n for n in before if n not in anchors),
                "sleeps_since_refresh": sleeps,
                "placed_since_refresh": placed,
            },
        )
    )


async def _supersede_stale_parents(
    ctx: PipelineContext, namespace: str, live_keys: set[str]
) -> int:
    """Archive every stale community parent in ``namespace`` and tombstone its
    member→parent ``community`` edges (ADR-015 §2). Returns how many parents
    were superseded. Caller holds the namespace lock."""
    assert ctx.append_event is not None and ctx.graph is not None
    superseded = 0
    for old in await _reorganize_summaries(ctx, namespace):
        if old.source.message_id in live_keys:
            continue
        # The archived parent must also lose its graph reach: weight-0
        # tombstone LINK events retire each live member→parent community edge
        # replay-deterministically (same mechanism as budget pruning) — the
        # graph would otherwise accumulate stale membership edges forever.
        for edge in await ctx.graph.edges_of(old.record_id):
            if edge.rel_type != "community" or edge.weight <= 0.0:
                continue
            await ctx.append_event(
                link_event(
                    namespace,
                    edge.src,
                    edge.dst,
                    edge.rel_type,
                    weight=0.0,
                    reason="reorganize_supersede",
                    actor="system",
                )
            )
        await ctx.append_event(
            MemoryEvent(
                kind=EventKind.DECAY_TRANSITION,
                namespace=namespace,
                actor="system",
                payload={
                    "record_id": old.record_id,
                    "set": {
                        "status": RecordStatus.ARCHIVED.value,
                        "superseded_at": datetime.now(UTC).isoformat(),
                    },
                    "transition": "community_parent->superseded",
                    "reason": "reorganized",
                },
            )
        )
        superseded += 1
    return superseded


async def _reorganize_summaries(ctx: PipelineContext, namespace: str) -> list[MemoryRecord]:
    """Active summary parents this pipeline previously wrote in ``namespace``."""
    return [
        record
        for record in await live_records(ctx, namespace, "semantic")
        if record.source.channel == "reorganize" and record.status is RecordStatus.ACTIVATED
    ]


async def _resolve_community(
    ctx: PipelineContext, inflate: CompressionPolicy, community: list[str]
) -> _Community | None:
    """The live, summarisable members of ``community`` and its membership key;
    None when too few qualify."""
    members: list[MemoryRecord] = []
    for record_id in community:
        record = await live_record(ctx, record_id)
        # Only live namespace truth feeds a summary (E1): quarantined or
        # non-active members are held/derived content, not community evidence,
        # and a summary parent is never a member of a community (no
        # summary-of-summary feedback loop).
        if (
            record is not None
            and record.status is RecordStatus.ACTIVATED
            and not record.quarantined
            and record.source.channel != "reorganize"
        ):
            members.append(inflate.inflate(record))
    if len(members) < constants.REORGANIZE_MIN_COMMUNITY_SIZE:
        return None
    namespaces = {member.namespace for member in members}
    if len(namespaces) > 1:
        # Links never cross namespaces (ADR-015), so a mixed community means
        # corrupted state — refuse loudly rather than pick a tenant.
        raise ValueError(f"community spans namespaces {sorted(namespaces)} — refusing to summarize")
    member_ids = sorted(member.record_id for member in members)
    return _Community(
        members=members,
        member_ids=member_ids,
        key=fingerprint_payload({"community": member_ids}),
        namespace=members[0].namespace,
    )


async def _keep_summary(ctx: PipelineContext, community: _Community, parent: MemoryRecord) -> None:
    """#84: re-point ``parent``'s membership links at ``community`` (new members
    linked, departed ones tombstoned); its summary text is left as it is."""
    assert ctx.append_event is not None
    linked = await _community_links(ctx, parent.record_id)
    wanted = set(community.member_ids)
    for member_id, weight in [
        *((m, 1.0) for m in sorted(wanted - linked)),
        *((m, 0.0) for m in sorted(linked - wanted)),
    ]:
        await ctx.append_event(
            link_event(
                community.namespace,
                member_id,
                parent.record_id,
                "community",
                weight=weight,
                reason="reorganize_keep",
                actor="system",
            )
        )


async def _reorganize_community(
    ctx: PipelineContext, community: _Community, keeper: _SummaryKeeper | None = None
) -> tuple[int, str]:
    """Write (or keep) the summary parent of one community. Returns
    ``(parents_created, live_key)``: the key of the parent that now stands for
    the community (a kept parent's own key under #84)."""
    assert ctx.append_event is not None
    namespace, members, member_ids, key = (
        community.namespace,
        community.members,
        community.member_ids,
        community.key,
    )
    # Same per-namespace unit as the engine's write verbs (M5): the
    # idempotency read, the summary WRITE and the membership LINKs must not
    # interleave with a concurrent forget cascade in this namespace.
    async with ctx.lock(namespace):
        if await _parents_gone(ctx, namespace, member_ids, "reorganize"):  # I72
            return 0, key  # a member was forgotten since the community was read; next sweep
        live = {old.source.message_id: old for old in await _reorganize_summaries(ctx, namespace)}
        if key in live:
            if keeper is not None:
                # A parent kept under #84 may have drifted links; a community
                # back at its summarised set re-points them (no-op otherwise).
                await _keep_summary(ctx, community, live[key])
            return 0, key  # unchanged membership (idempotence)
        match = keeper.match(set(member_ids)) if keeper is not None else None
        if (
            match is not None
            and match.source.message_id in live
            # A kept summary may never claim more trust than a member it now
            # stands for (D-47 §5): a less-trusted newcomer forces a rewrite.
            and min(member.trust for member in members) >= match.trust
        ):
            assert keeper is not None
            keeper.claimed.add(match.record_id)
            await _keep_summary(ctx, community, match)
            return 0, str(match.source.message_id)
        ordered = sorted(members, key=chrono_key)
        summary_text = extractive_summary(
            [member.content for member in ordered], constants.CONSOLIDATION_SUMMARY_MAX_CHARS
        )
        if ctx.summarize is not None:
            try:
                summary_text = await ctx.summarize("\n".join(member.content for member in ordered))
            except Exception as exc:  # LLM is an enhancer, never a gate (N6)
                _log.warning("reorganize.summarize_fallback", namespace=namespace, error=str(exc))
        summary = MemoryRecord(
            namespace=namespace,
            memory_type="semantic",
            content=summary_text,
            # Derivation time belongs in recorded_at. Replaying old conversations
            # must not turn their community summaries into current-day evidence.
            valid_from=max(member.valid_from for member in members),
            source=SourceInfo(
                role=constants.DERIVED_ROLE,
                channel="reorganize",
                message_id=key,
                parents=member_ids,
            ),
            # E1/D-47 §5: derived content is never more trusted than its
            # least-trusted member, and echoed injection framing stays flagged.
            trust=min(member.trust for member in members),
            instruction_flag=instruction_shaped(summary_text),
        )
        summary = stamp_summary(ctx, summary, "reorganize")  # FU-SAFE: layer + derivation
        # The WRITE carries consolidation-shaped provenance, so audit taint and
        # the graph projector's derived_from edges ride existing machinery.
        await ctx.append_event(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace=namespace,
                actor="system",
                payload={
                    "record": summary.model_dump(mode="json"),
                    "consolidation": {"session_key": key, "member_record_ids": member_ids},
                },
            )
        )
        await link_summary(ctx, namespace, summary, member_ids, "reorganize")  # FU-SAFE
        # Member -> parent LINK events (ADR-015). System-written membership
        # links are budget-exempt by design: budget enforcement lives at the
        # memory-layer creation surface, and a 20-member community keeps all
        # 20 links. The rel is RESERVED (H1): callers cannot forge it.
        for member_id in member_ids:
            await ctx.append_event(
                link_event(
                    namespace,
                    member_id,
                    summary.record_id,
                    "community",
                    weight=1.0,
                    reason="reorganize",
                    actor="system",
                )
            )
    return 1, key
