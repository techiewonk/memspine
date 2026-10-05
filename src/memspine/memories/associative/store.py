"""Associative memory (M13.6): explicit links + PPR recall over the graph.

Talks only to the :class:`GraphStore` port and a narrow storage Protocol slice
(D-22 — same pattern as ``procedural/skills.py``). Every mutation is a LINK
event through the injected write door (ADR-015); the graph itself is a
projection this module reads, never writes directly.
"""

from __future__ import annotations

import math
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, ClassVar, Protocol

from memspine.config.constants import LINK_BUDGET, SEARCH_TOP_K
from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord, RecordStatus
from memspine.exceptions import ConfigError, ConflictError
from memspine.memories.associative import links as link_rules
from memspine.memories.associative.entities import (
    MENTIONS_REL,
    canonical_entity,
    is_entity_node,
    parse_entity_node,
)
from memspine.memories.associative.ppr import local_push_ppr, personalized_pagerank
from memspine.memories.base import BaseMemory
from memspine.observability.logging import EVENT_LINK, EVENT_RETRIEVE, get_logger
from memspine.services.embedding.base import EmbeddingService
from memspine.services.graph.base import GraphEdge, GraphStore, capped_neighbors
from memspine.services.lexical.base import LexicalHit, rrf_fuse
from memspine.services.vector.base import VectorStore

__all__ = ["AssociativeMemory"]

_log = get_logger(__name__)

AppendEvent = Callable[[MemoryEvent], Awaitable[None]]

#: E1 traversal strategies for ``related`` (amends D-49).
_STRATEGIES = frozenset({"ppr", "bfs", "rrf"})

#: GP-3: may a graph walk enter this record node? (trust caps, GP-10)
Admit = Callable[[MemoryRecord], bool]

_WORD = re.compile(r"\w+")


def match_key(text: str) -> str:
    """The form entity names and query n-grams are compared in: the canonical
    name's words, punctuation dropped ("Charlotte's Web" -> "charlotte s web")."""
    return " ".join(_WORD.findall(canonical_entity(text)))


def _without_mentions(edges: list[GraphEdge]) -> list[GraphEdge]:
    """GP-2 entity edges are not associations: ``related`` and the reorganizer
    see exactly the graph they saw before the entity layer."""
    return [edge for edge in edges if edge.rel_type != MENTIONS_REL]


class AssociativeStore(Protocol):
    """The storage slice associative needs (ports & adapters, D-22)."""

    async def get_record(self, record_id: str) -> MemoryRecord | None: ...


class AssociativeMemory(BaseMemory):
    name: ClassVar[str] = "associative"
    needs: ClassVar[tuple[str, ...]] = ("semantic",)

    def __init__(
        self,
        storage: AssociativeStore,
        graph: GraphStore,
        append_event: AppendEvent,
        policies: dict[str, Any] | None = None,
        vector: VectorStore | None = None,
        embedder: EmbeddingService | None = None,
    ) -> None:
        self._storage = storage
        self._graph = graph
        self._append_event = append_event
        # E1: memories.associative.policies. ``related`` sub-block selects the
        # traversal strategy + depth. vector/embedder power the ``rrf`` blend;
        # when absent, ``rrf`` degrades to ``ppr`` (graceful, never crashes).
        self._policies = policies or {}
        self._vector = vector
        self._embedder = embedder

    async def link(
        self,
        namespace: str,
        src_id: str,
        dst_id: str,
        *,
        rel: str = "related",
        weight: float = 1.0,
        reason: str = "manual",
        actor: str = "user",
    ) -> None:
        """Create (or re-weight) one associative link, budget-checked (M13.6).

        Both endpoints must exist in ``namespace`` — cross-namespace links are
        refused with the SAME error as a missing record (no existence oracle,
        ADR-014 shape). Quarantined endpoints are refused outright: held
        content must not gain graph reach while under quarantine (E1).
        """
        if rel in link_rules.RESERVED_RELS:
            # H1 (ADR-015 §2): reserved rels are budget-exempt, so accepting
            # them from callers would be an unbounded fan-out bypass.
            raise ConflictError(
                f"rel {rel!r} is reserved for system-written links (ADR-015) — "
                "pick a caller rel such as 'related'"
            )
        if src_id == dst_id:
            raise ConflictError(f"cannot link record {src_id} to itself")
        if not math.isfinite(weight) or weight <= 0.0:
            raise ConflictError(
                f"link weight must be a positive finite number, got {weight!r} "
                "(weight 0.0 is the prune tombstone, ADR-015)"
            )
        src = await self._require_record(src_id, namespace)
        dst = await self._require_record(dst_id, namespace)
        for endpoint in (src, dst):
            if endpoint.quarantined:
                raise ConflictError(
                    f"record {endpoint.record_id} is quarantined (E1) — held content "
                    "cannot enter the association graph until corroborated"
                )
        # Re-weighting an existing edge is an upsert — it must not be
        # double-counted against either endpoint's budget. Links are
        # undirected, so (dst, src) is the same edge as (src, dst): the event
        # keeps the stored orientation so the projector re-weights the one row
        # instead of materializing a mirrored duplicate.
        existing = await self._find_live_edge(src_id, dst_id, rel)
        if existing is None:
            await link_rules.assert_within_budget(self._graph, src_id)
            await link_rules.assert_within_budget(self._graph, dst_id)
        else:
            src_id, dst_id = existing.src, existing.dst
        await self._append_event(
            link_rules.link_event(namespace, src_id, dst_id, rel, weight, reason, actor=actor)
        )
        _log.info(EVENT_LINK, namespace=namespace, src=src_id, dst=dst_id, rel=rel, weight=weight)

    async def related(
        self, namespace: str, record_id: str, k: int = SEARCH_TOP_K, strategy: str | None = None
    ) -> list[MemoryRecord]:
        """Records associated with ``record_id`` (plan §5 Phase 6 / D-40), ranked
        by a configurable traversal **strategy** (E1, amends D-49):

        - ``ppr`` (default): personalized PageRank over the namespace's graph — the
          v0.1 behavior, byte-identical.
        - ``bfs``: breadth-first neighbors within ``depth`` hops (recency of
          connection, not global centrality), inside the namespace; optional
          ``max_degree`` caps the fan-out per node (KB-3).
        - ``rrf``: reciprocal-rank fusion of the PPR graph rank with a vector
          similarity rank (embed the seed, query the vector store) — surfaces
          records that are both graph-close and semantically similar. Falls back
          to ``ppr`` when no vector store/embedder is bound.

        ``strategy`` overrides ``memories.associative.policies.related.strategy``.
        Mirrors the ``engine.search`` gate (E1): only ACTIVATED, never
        quarantined, never cross-namespace. Returned ids ride a RETRIEVE event.
        """
        seed = await self._require_record(record_id, namespace)
        if seed.quarantined or seed.status is RecordStatus.QUARANTINED:
            # Same refusal as link() (E1): a quarantined seed must not gain
            # graph reach — not even read-side — until corroborated.
            raise ConflictError(
                f"record {record_id} is quarantined (E1) — held content "
                "cannot seed graph recall until corroborated"
            )
        related_policy = self._policies.get("related", {})
        chosen = strategy or related_policy.get("strategy", "ppr")
        if chosen not in _STRATEGIES:
            raise ConfigError(f"unknown related strategy {chosen!r} (valid: {sorted(_STRATEGIES)})")
        ranked_ids = await self._rank_related(namespace, record_id, seed, chosen, k, related_policy)
        results: list[MemoryRecord] = []
        for candidate_id in ranked_ids:
            if len(results) >= k:
                break
            candidate = await self._storage.get_record(candidate_id)
            if (
                candidate is None
                or candidate.namespace != namespace
                or candidate.status is not RecordStatus.ACTIVATED
                or candidate.quarantined
            ):
                continue
            results.append(candidate)
        if results:
            await self._append_event(
                MemoryEvent(
                    kind=EventKind.RETRIEVE,
                    namespace=namespace,
                    actor="system",
                    payload={"record_ids": [record.record_id for record in results]},
                )
            )
        _log.info(
            EVENT_RETRIEVE, namespace=namespace, related=True, strategy=chosen, count=len(results)
        )
        return results

    async def _rank_related(
        self,
        namespace: str,
        record_id: str,
        seed: MemoryRecord,
        strategy: str,
        k: int,
        policy: dict[str, Any],
    ) -> list[str]:
        """Ordered candidate ids for a ``related`` query, per E1 strategy.
        Gating/limiting happens in the caller; this only ranks."""
        depth = int(policy.get("depth", 2))
        # KB-1: PPR and BFS stay inside the seed's namespace — another tenant's
        # edges are never scanned, let alone walked.
        raw = await self._graph.edge_list(namespace)
        edges = _without_mentions(raw)
        graph_ids = [rid for rid, _ in personalized_pagerank(edges, {record_id})]
        if strategy == "ppr":
            return graph_ids
        if strategy == "bfs":
            raw_degree = policy.get("max_degree")
            max_degree = int(raw_degree) if raw_degree is not None else None
            if len(edges) != len(raw):
                # GP-2: walk the association edges only, as before the entity layer.
                return _bfs(edges, record_id, max(1, depth), max_degree)
            nodes = await self._graph.neighbors(
                record_id,
                depth=max(1, depth),
                namespace=namespace,
                max_degree=max_degree,
            )
            return [node.node_id for node in nodes]
        # rrf: fuse the graph rank with a vector-similarity rank of the seed.
        if self._vector is None or self._embedder is None:
            _log.info("associative.rrf_fallback_ppr", reason="no vector store/embedder bound")
            return graph_ids
        fetch_k = k * int(policy.get("fetch_multiplier", 4))
        [seed_vec] = await self._embedder.embed([seed.content])
        vector_hits = await self._vector.query(
            namespace, seed_vec, embedder_id=self._embedder.embedder_id, top_k=fetch_k
        )
        # rrf_fuse reads only .record_id; ranking order (not score) is what
        # matters, so the graph leg rides zero-score LexicalHits in PPR order.
        graph_hits = [LexicalHit(rid, 0.0) for rid in graph_ids]
        fused = rrf_fuse(vector_hits, graph_hits)
        # The seed is its own nearest vector neighbor — never return it (PPR/BFS
        # already exclude it, so this keeps rrf consistent).
        return [rid for rid, _ in fused if rid != record_id]

    # -- GP-2/GP-3: the entity layer, read side ---------------------------------

    async def entity_index(self, namespace: str) -> dict[str, list[str]]:
        """``match_key(name)`` -> the entity node ids that a live ``mentions`` edge
        in ``namespace`` points at."""
        index: dict[str, list[str]] = {}
        for edge in await self._graph.edge_list(namespace):
            if edge.rel_type != MENTIONS_REL or edge.weight <= 0:
                continue
            parsed = parse_entity_node(edge.dst)
            if parsed is None or parsed[0] != namespace:
                continue
            nodes = index.setdefault(match_key(parsed[1]), [])
            if edge.dst not in nodes:
                nodes.append(edge.dst)
        return index

    async def entities_of(self, namespace: str, record_ids: Sequence[str]) -> list[str]:
        """The entity nodes the records mention (live edges), in record order."""
        found: list[str] = []
        for record_id in record_ids:
            for edge in await self._graph.edges_of(record_id):
                if (
                    edge.rel_type == MENTIONS_REL
                    and edge.src == record_id
                    and edge.weight > 0
                    and _entity_in(edge.dst, namespace)
                    and edge.dst not in found
                ):
                    found.append(edge.dst)
        return found

    async def entity_communities(self, namespace: str, entities: Sequence[str]) -> list[str]:
        """GP-9 (#23): the community summary parents the ``entities`` belong to.

        Communities are built over association edges (``mentions`` excluded,
        ADR-043), so an entity is a member through the records it mentions: the
        parents of every live member -> parent ``community`` link of a record with
        a live ``mentions`` edge to one of ``entities`` (in ``namespace``), in
        first-seen order. Unfiltered: the caller gates the parent records."""
        mentioned: list[str] = []
        for entity in dict.fromkeys(entities):
            if not _entity_in(entity, namespace):
                continue
            for edge in await self._graph.edges_of(entity):
                if (
                    edge.rel_type == MENTIONS_REL
                    and edge.dst == entity
                    and edge.weight > 0
                    and edge.src not in mentioned
                ):
                    mentioned.append(edge.src)
        parents: list[str] = []
        for record_id in mentioned:
            for edge in await self._graph.edges_of(record_id):
                if (
                    edge.rel_type == "community"
                    and edge.src == record_id
                    and edge.weight > 0
                    and edge.dst not in parents
                ):
                    parents.append(edge.dst)
        return parents

    async def seed_expand(
        self,
        namespace: str,
        seeds: Sequence[str],
        *,
        depth: int,
        admit: Admit,
        max_degree: int | None = None,
    ) -> list[tuple[MemoryRecord, int]]:
        """GP-3: the records a walk from entity ``seeds`` reaches, nearest first.

        ``depth`` counts entity hops (entity -> record -> entity is one), so the
        graph walk goes ``2 x depth - 1`` edges: depth 1 is the seeds' own records,
        depth 2 adds the records of the entities those records name. Returned with
        their graph hop count, in BFS order (per node, strongest edge first, then
        id, capped at ``max_degree``).

        GP-10: the walk stays in ``namespace``, never follows a tombstoned edge,
        and never enters a record ``admit`` refuses (quarantined, erased, below the
        trust floor): such a node is a dead end, so nothing behind it is reached
        through it. Entity nodes of another namespace are never entered either.
        """
        hops_total = 2 * max(1, depth) - 1
        starts = [s for s in dict.fromkeys(seeds) if _entity_in(s, namespace)]
        if not starts:
            return []
        edges = await self._graph.subgraph(starts, hops_total, namespace=namespace)
        adjacency: dict[str, list[GraphEdge]] = {}
        for edge in edges:
            if edge.weight <= 0 or edge.src == edge.dst:
                continue
            adjacency.setdefault(edge.src, []).append(edge)
            adjacency.setdefault(edge.dst, []).append(edge)
        records: dict[str, MemoryRecord | None] = {}

        async def enterable(node_id: str) -> bool:
            if is_entity_node(node_id):
                return _entity_in(node_id, namespace)
            if node_id not in records:
                record = await self._storage.get_record(node_id)
                ok = record is not None and record.namespace == namespace and admit(record)
                records[node_id] = record if ok else None
            return records[node_id] is not None

        seen = set(starts)
        frontier = starts
        reached: list[tuple[MemoryRecord, int]] = []
        for hop in range(1, hops_total + 1):
            next_frontier: list[str] = []
            for node in frontier:
                live = [e for e in adjacency.get(node, []) if await enterable(_other(e, node))]
                for other in capped_neighbors(live, node, max_degree):
                    if other in seen:
                        continue
                    seen.add(other)
                    next_frontier.append(other)
                    record = records.get(other)
                    if record is not None:
                        reached.append((record, hop))
            if not next_frontier:
                break
            frontier = next_frontier
        return reached

    async def graph_proximity(
        self,
        namespace: str,
        seeds: Sequence[str],
        *,
        depth: int,
        admit: Admit,
        mode: str,
        max_degree: int | None = None,
    ) -> dict[str, float]:
        """#22: ``record_id -> proximity`` in (0, 1] to the entity ``seeds``.

        ``distance``: 1 / entity hops of :meth:`seed_expand` (a seed's own records
        score 1, the records one entity further 1/2, ...). ``ppr``: local push-PPR
        (:func:`~memspine.memories.associative.ppr.local_push_ppr`) restarted at
        the seeds over their ``subgraph()``, normalised so the best record scores
        1. Both walk only what :meth:`seed_expand` may enter (GP-10: namespace,
        live edges, ``admit``); a record the walk does not reach is absent.
        """
        reached = await self.seed_expand(
            namespace, seeds, depth=depth, admit=admit, max_degree=max_degree
        )
        if mode == "distance":
            return {record.record_id: 2.0 / (hops + 1) for record, hops in reached}
        if mode != "ppr" or not reached:
            return {}
        allowed = {record.record_id for record, _hops in reached}
        starts = [s for s in dict.fromkeys(seeds) if _entity_in(s, namespace)]
        walked = await self._graph.subgraph(starts, 2 * max(1, depth) - 1, namespace=namespace)
        edges = [
            edge
            for edge in walked
            if all(_entity_in(n, namespace) or n in allowed for n in (edge.src, edge.dst))
        ]
        scores = {
            node: score
            for node, score in local_push_ppr(edges, set(starts)).items()
            if node in allowed
        }
        best = max(scores.values(), default=0.0)
        return {node: score / best for node, score in scores.items()} if best > 0 else {}

    async def prune_weakest(self, namespace: str, record_id: str) -> GraphEdge | None:
        """Free one budget slot on ``record_id`` (weakest live link retired
        via a weight-0 LINK event, ADR-015). Returns the pruned edge or None."""
        await self._require_record(record_id, namespace)
        pruned = await link_rules.prune_weakest(
            self._graph, self._append_event, namespace, record_id
        )
        if pruned is not None:
            _log.info(
                EVENT_LINK,
                namespace=namespace,
                src=pruned.src,
                dst=pruned.dst,
                rel=pruned.rel_type,
                weight=0.0,
                reason="budget_prune",
            )
        return pruned

    async def budget_remaining(self, record_id: str, budget: int = LINK_BUDGET) -> int:
        """How many associative links ``record_id`` may still accept (M13.6)."""
        live = link_rules.live_links(await self._graph.edges_of(record_id))
        return max(budget - len(live), 0)

    async def _require_record(self, record_id: str, namespace: str) -> MemoryRecord:
        record = await self._storage.get_record(record_id)
        # One error for missing, foreign, and deleted records: a leaked id must
        # not become a cross-namespace existence oracle (ADR-014 shape).
        if record is None or record.namespace != namespace or record.status is RecordStatus.DELETED:
            raise ConflictError(f"no such record {record_id!r} in namespace {namespace!r}")
        return record

    async def _find_live_edge(self, src_id: str, dst_id: str, rel: str) -> GraphEdge | None:
        # Direction-insensitive: edges are undirected (ADR-015 §3), so a live
        # (dst, src) edge is the same association as (src, dst).
        for edge in link_rules.live_links(await self._graph.edges_of(src_id)):
            if edge.rel_type == rel and {edge.src, edge.dst} == {src_id, dst_id}:
                return edge
        return None


def _other(edge: GraphEdge, node: str) -> str:
    return edge.dst if edge.src == node else edge.src


def _entity_in(node_id: str, namespace: str) -> bool:
    parsed = parse_entity_node(node_id)
    return parsed is not None and parsed[0] == namespace


def _bfs(edges: list[GraphEdge], start: str, depth: int, max_degree: int | None) -> list[str]:
    """Breadth-first over ``edges`` (live, undirected): nearest first, then by id
    within a hop, each node following its ``max_degree`` strongest neighbours,
    the rule every adapter's ``neighbors`` implements."""
    adjacency: dict[str, list[GraphEdge]] = {}
    for edge in edges:
        adjacency.setdefault(edge.src, []).append(edge)
        adjacency.setdefault(edge.dst, []).append(edge)
    seen = {start}
    frontier = [start]
    found: list[str] = []
    for _ in range(depth):
        level: set[str] = set()
        for node in frontier:
            for other in capped_neighbors(adjacency.get(node, []), node, max_degree):
                if other not in seen:
                    level.add(other)
        if not level:
            break
        ordered = sorted(level)
        seen.update(ordered)
        found.extend(ordered)
        frontier = ordered
    return found
