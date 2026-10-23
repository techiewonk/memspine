"""Read path: retrieval legs and overlays moved out of the engine (F-10a)."""

from __future__ import annotations

import math
import re
from collections.abc import (
    Callable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from memspine.compose.pack import (
    AssembledContext,
    estimate_tokens,
)
from memspine.config import constants
from memspine.core.owner_check import (
    entity_note,
    judge,
    missing_names,
    owner_marker,
    unknown_lines,
    user_header,
)
from memspine.core.read_filters import (
    active_as_of,
    active_record_scope,
    person_time_leg,
    time_expr_span,
)
from memspine.core.rule_edges import BECAUSE_REL
from memspine.core.ties import settle_ties
from memspine.memories.associative.entities import (
    MENTIONS_REL,
    canonical_entity,
    entity_node_id,
    is_entity_node,
    parse_entity_node,
)
from memspine.memories.associative.store import match_key
from memspine.models.encoders.query import CueQueryEncoder, NoopQueryEncoder, QueryEncoder
from memspine.models.encoders.query.perspective import (
    active_asker,
    perspective_leg,
    record_view,
)
from memspine.models.encoders.query.shape import (
    content_words,
    core_terms,
    question_shape,
)
from memspine.models.encoders.query.temporal_query import (
    LegHit,
    NamedLeg,
    assistant_leg,
    bridge_phrases,
    cohesion_leg,
    comparison_speaker_legs,
    entity_expand_leg,
    entity_leg,
    has_bridge_cue,
    metadata_leg,
    sentence_leg,
    sentences,
    speaker_leg,
    speaker_of,
    speaker_vector_leg,
    subject_vector_leg,
    temporal_leg,
    view_tag_leg,
)
from memspine.observability import tap
from memspine.observability.logging import get_logger
from memspine.prompts.models import (
    ReadPlan,
)
from memspine.read.contract import (
    contract_header,
)
from memspine.read.entity_leg import (
    DampingConfig,
    EntityIndex,
    EntityMention,
    entity_leg_explained,
)
from memspine.read.pipeline import request as read_request
from memspine.read.pipeline.fusion import (
    _unit,
)
from memspine.read.pipeline.state import (
    _FOCAL_ENTITY,
    _FORENSICS,
    _RERANK_SUPPORT,
    _TAINT_ARCHIVED_TAG,
    CUE_TAG,
    _passive_hidden,
)
from memspine.scope import Scope
from memspine.services.embedding.base import embed_queries
from memspine.services.graph.base import GraphEdge
from memspine.services.graph.qualifiers import (
    CallerVisibility,
    EdgeQualifiers,
    QualifierFilter,
    active_graph_filter,
    edge_qualifiers,
)
from memspine.services.lexical.base import LexicalHit, rrf_fuse
from memspine.services.storage.records_port import RecordsStore
from memspine.services.vector.base import VectorHit
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import (
    MemoryRecord,
    RecordStatus,
    SourceInfo,
    chrono_key,
)
from memspine.trust.visibility import visible_to

_log = get_logger(__name__)


async def _probe_legs(
    eng: Any, ns: str, text: str, vector: list[float], fetch_k: int, lexical: bool
) -> list[list[LegHit]]:
    """#35: one probe's legs: its vector leg, plus its BM25 leg under hybrid."""
    legs = [[LegHit(h.record_id, h.score) for h in await eng._vector_leg(ns, vector, fetch_k)]]
    if lexical and eng._lexical is not None:
        try:
            hits = await eng._lexical_leg(ns, text, fetch_k)
            legs.append([LegHit(h.record_id, 1.0) for h in hits])
        except Exception as exc:  # an enhancer, never a gate
            _log.warning("read.probe_leg_failed", namespace=ns, error=str(exc))
    return [leg for leg in legs if leg]


def _encoder(eng: Any) -> QueryEncoder:
    """#61: the configured query encoder (``read.query_encoder``), built once."""
    if eng._query_encoder is None:
        if eng._config().read.query_encoder == "cues":
            storage = eng._require_started()

            async def load(ns: str) -> list[MemoryRecord]:
                return [r for r in await storage.list_records(ns, "semantic") if CUE_TAG in r.tags]

            eng._query_encoder = CueQueryEncoder(load)
        else:
            eng._query_encoder = NoopQueryEncoder()
    return eng._query_encoder  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt


async def _encoder_legs(eng: Any, ns: str, query: str) -> list[list[LegHit]]:
    """#61: the query encoder's proposals as one fused leg (none: no leg).

    Each match is re-checked here: the cue must be live, unquarantined, in this
    namespace and at or above ``read.cue_min_trust``; its target then passes the
    same read gates as any other hit. An encoder failure degrades to no leg."""
    try:
        encoded = await eng._encoder().encode(ns, query)
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.query_encoder_failed", namespace=ns, error=str(exc))
        return []
    storage = eng._require_started()
    floor = eng._config().read.cue_min_trust
    leg: list[LegHit] = []
    for match in encoded.matches:
        cue = await storage.get_record(match.cue_id)
        if (
            cue is None
            or cue.namespace != ns
            or cue.status is not RecordStatus.ACTIVATED
            or cue.quarantined
            or cue.trust < floor
            or CUE_TAG not in cue.tags
            or match.target_id not in cue.source.parents
        ):
            continue
        leg.append(LegHit(match.target_id, match.score))
    return [leg] if leg else []


async def _metadata_legs(
    eng: Any, ns: str, query: str, fetch_k: int, *, temporal: Sequence[LegHit] = ()
) -> list[list[LegHit]]:
    """C3': the non-empty temporal / metadata legs for this query (opt-in).

    ``temporal`` (FU-TEMPORAL): under ``read.legs_v2`` the registry's temporal leg ran already;
    its hits take the place the old temporal leg held in this list, so fusion sees the legs in
    the same order with the flag on and off."""
    read = eng._config().read
    legs: list[list[LegHit]] = []
    if read.core_terms_leg and eng._lexical is not None:
        # H13: BM25 over the question without interrogative/function words.
        terms = core_terms(query)
        if terms and terms.lower() != query.lower():
            try:
                hits = await eng._lexical_leg(ns, terms, fetch_k)
                legs.append([LegHit(h.record_id, 1.0) for h in hits])
            except Exception as exc:  # an enhancer, never a gate
                _log.warning("read.core_terms_leg_failed", namespace=ns, error=str(exc))
    if not (
        read.temporal_leg
        or read.metadata_leg
        or read.subject_leg
        or read.role_aware
        or read.sentence_leg
        or read.recency_leg
        or read.entity_leg
        or read.entity_leg_v2
        or read.speaker_probe
        or read.view_tag_leg
    ):
        return [leg for leg in legs if leg]
    try:
        live = [
            r
            for r in await eng._records(ns)
            if r.status is RecordStatus.ACTIVATED
            and not r.quarantined
            and r.memory_type != "shared"
        ]
        if read.temporal_leg and read.legs_v2:
            # R3-04 / FU-TEMPORAL: the registry's temporal leg (same keys), passed in
            if temporal:
                legs.append(NamedLeg("temporal", [LegHit(h.record_id, h.score) for h in temporal]))
        elif read.temporal_leg:
            # F2 / W7: relative phrases in the question resolve against the as-of
            # time or the read time; without either the call is unchanged.
            anchored: dict[str, Any] = {}
            if read.temporal_relative or active_as_of() is not None:
                anchored = {
                    "anchor": active_as_of() or eng._clock(),
                    "week": read.relative_week,
                }
            if read.temporal_infer_year and live:
                # N45: a yearless date resolves against the conversation's "now".
                anchored["year_ref"] = active_as_of() or max(
                    r.valid_from if r.valid_from.tzinfo else r.valid_from.replace(tzinfo=UTC)
                    for r in live
                )
            if read.temporal_rank != "midpoint":
                anchored["rank"] = read.temporal_rank
            if read.temporal_soft:
                anchored["soft"] = True
            if read.temporal_leg_mentions:
                anchored["mentions"] = True
            legs.append(
                NamedLeg(
                    "temporal",
                    temporal_leg(
                        query,
                        live,
                        fetch_k,
                        event_dates=read.temporal_leg_event_dates,
                        **anchored,
                    ),
                )
            )
        if read.metadata_leg:
            legs.append(metadata_leg(query, live, fetch_k))
        if read.subject_leg:
            # W8 (plan v3.2): the named speaker's own turns, best word overlap first.
            legs.append(speaker_leg(query, live, fetch_k))
        if read.role_aware:
            # W11 (plan v3.2): "what did you recommend" reads the assistant's turns.
            legs.append(assistant_leg(query, live, fetch_k))
        if read.sentence_leg:
            # N05 (plan v3.2): the best single sentence of each record, lexically.
            legs.append(sentence_leg(query, live, fetch_k))
        if read.recency_leg:
            # N16 (plan v3.2, LaMP RSPG): a recency-only leg, newest first.
            newest = sorted(live, key=chrono_key, reverse=True)[:fetch_k]
            legs.append([LegHit(r.record_id, 1.0) for r in newest])
        if read.view_tag_leg:
            # G-16 (SimpleMem symbolic leg): location / topic / person view tags.
            legs.append(NamedLeg("view_tags", view_tag_leg(query, live, fetch_k)))
        if read.entity_leg_v2 and "entity" not in read_request.legs_off():
            legs += await _entity_leg_v2(eng, ns, query, live, fetch_k)
        if read.entity_leg or read.speaker_probe:
            speakers = {s for r in live if (s := speaker_of(r.content)) is not None}
            if read.entity_leg:
                # N59 (Dakera name boost): proper nouns and years the question
                # names, beyond the speakers, matched in raw turns.
                legs.append(NamedLeg("entity", entity_leg(query, live, fetch_k, speakers)))
            if read.speaker_probe:
                # N40 (MemMachine): the question in stored-turn form, "Name: ...".
                named = sorted(
                    (
                        s
                        for s in speakers
                        if re.search(rf"(?<!\w){re.escape(s)}(?!\w)", query, re.I)
                    ),
                    key=lambda s: query.lower().find(s),  # the first one named
                )
                if named and eng._embedder is not None:
                    text = f"{named[0].title()}: {core_terms(query) or query}"
                    vector = (await embed_queries(eng._embedder, [text]))[0]
                    legs += await eng._probe_legs(ns, text, vector, fetch_k, lexical=False)
    except Exception as exc:  # an enhancer, never a gate: degrade to the base legs
        _log.warning("read.metadata_legs_failed", namespace=ns, error=str(exc))
        return []
    return [leg for leg in legs if leg]


async def registry_base_legs(
    eng: Any,
    ns: str,
    query: str,
    query_vector: Any,
    fetch_k: int,
    use_hybrid: bool,
    probe_texts: Sequence[str],
    probe_vectors: Sequence[Any],
) -> tuple[Any, Any, Any]:
    """R3-04 (``read.legs_v2``; R-05, R-06, R-07): base candidates through the leg registry.

    Same return shape as the inline path in ``router._search``: ``(vector_hits, lexical_hits,
    extra_legs)``. ``vector`` (dense), ``lexical`` (language-aware BM25) and ``temporal`` (event
    intervals in the question's window, anchored to the as-of or the read clock) run through
    ``memspine.read.legs``; the lexical leg runs when ``hybrid`` is on and fills the lexical slot,
    the temporal leg joins the extra legs as the named leg ``temporal``. The metadata, encoder and
    probe legs are the current ones.
    """
    from memspine.read import legs as lib

    read = eng._config().read
    live = [
        r
        for r in await eng._records(ns)
        if r.status is RecordStatus.ACTIVATED and not r.quarantined and r.memory_type != "shared"
    ]
    off = read_request.legs_off()  # R3-03: one rule for every registered leg (FU-TEMPORAL)
    as_of = active_as_of()
    # FU-TEMPORAL: the old read.temporal_* keys decide what the temporal leg may resolve
    anchored = read.temporal_relative or as_of is not None
    infer_year = read.temporal_infer_year and bool(live)

    async def search_vector(k: int) -> list[VectorHit]:
        return await eng._vector_leg(ns, query_vector, k)  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt

    ctx = lib.LegContext(
        query=query,
        namespace=ns,
        fetch_k=fetch_k,
        records=live,
        language=read.lexical_language,
        namespace_languages=read.lexical_namespace_languages,
        lexical_scorer=read.lexical_scorer,
        reference_time=(as_of or eng._clock()) if anchored else None,
        year_ref=(
            as_of
            or max(
                (r.valid_from if r.valid_from.tzinfo else r.valid_from.replace(tzinfo=UTC))
                for r in live
            )
        )
        if infer_year
        else None,
        week=read.relative_week,
        precision_aware=read.temporal_precision_aware,
        temporal_rank=read.temporal_rank,
        temporal_soft=read.temporal_soft,
        temporal_mentions=read.temporal_leg_mentions,
        # the precision flag reads the stored event-time field, so it implies the happened date
        temporal_event_dates=read.temporal_leg_event_dates or read.temporal_precision_aware,
        lexical_dates=bool(eng._config().storage.lexical.settings.get("date_tokens", False)),
        vector_search=search_vector if eng._embedder is not None else None,
    )
    if use_hybrid and read.lexical_strip_names:
        ctx.lexical_query = await eng._strip_speaker_names(ns, query)  # read.lexical_strip_names
    temporal_on = read.temporal_leg and "temporal" not in off
    lexical_on = use_hybrid and "lexical" not in off
    names = ["vector", *(["lexical"] if lexical_on else []), *(["temporal"] if temporal_on else [])]
    registry = lib.REGISTRY
    graph_on = read.graph_leg_v2 and "graph" not in off
    episode_on = read.episode_leg_v2 and "episode" not in off
    if episode_on or graph_on or read.profile_card_header:
        ctx.layer_registry = await read_request.layer_inventory(
            eng,
            ns,
            read,
            selected=[*(["L2"] if episode_on else []), *(["L3"] if graph_on else [])],
        )
    if episode_on:
        # R3-05c: the episode leg (L2) joins
        registry = lib.with_episode_leg(lib.REGISTRY)
        names.append("episode")
    out = await lib.run_legs(registry, names, ctx)
    vector_hits = list(out["vector"].hits)
    lexical_hits = (
        [LexicalHit(h.record_id, h.score) for h in out["lexical"].hits] if lexical_on else []
    )
    temporal = [LegHit(h.record_id, h.score) for h in out["temporal"].hits] if temporal_on else []
    extra_legs: list[Any] = list(await eng._metadata_legs(ns, query, fetch_k, temporal=temporal))
    if episode_on and out["episode"].hits:
        from memspine.read.pipeline.lift import lift_episode_hits

        turns = await lift_episode_hits(
            eng,
            ns,
            out["episode"].hits,
            {r.record_id: r for r in live if r.layer == "L2"},
            max_turns=read.episode_lift_max_turns,
            carry=read.episode_lift_carry,
        )
        if turns:
            extra_legs.append(NamedLeg("episode", turns))
    if graph_on:
        extra_legs += await _graph_hop_leg(eng, ns, query, live, vector_hits, fetch_k, ctx)
    if ctx.layer_registry is not None:
        tap.event("read.layers", namespace=ns, layers=ctx.layer_registry.report())
    if read.query_encoder != "none":
        extra_legs += await eng._encoder_legs(ns, query)
    for text, vector in zip(probe_texts, probe_vectors, strict=True):
        extra_legs += await eng._probe_legs(ns, text, vector, fetch_k, use_hybrid)
    return vector_hits, lexical_hits, extra_legs


def _unqualified_graph_blocked() -> bool:
    """RM-93: while a graph qualifier filter is active, graph paths over links that carry no
    claim qualifiers (association, mention, causal and community walks) contribute nothing,
    so no graph evidence bypasses the filter. Only the compiled-edge leg is filtered."""
    return active_graph_filter() is not None


async def _caller_visibility(eng: Any, ns: str) -> CallerVisibility:
    """RM-88: the reading namespace plus the scopes it holds an active grant on.

    Grants are resolved fresh for every filtered read, so a revoked grant hides its
    edges at once. A grant counts when it covers semantic records (compiled edges
    are projected from semantic facts)."""
    granted: set[str] = set()
    shared = getattr(eng, "_shared", None)
    if shared is not None:
        for grantor, types in (await shared.grants_to(ns)).items():
            if types is None or "semantic" in types:
                granted.add(grantor)
    return CallerVisibility(
        namespace=ns,
        own_scopes=frozenset({Scope.from_namespace(ns).namespace}),
        granted_scopes=frozenset(granted),
    )


async def _caller_visible_edges(
    eng: Any,
    ns: str,
    edges: Sequence[tuple[GraphEdge, EdgeQualifiers | None]],
    by_id: Mapping[str, MemoryRecord],
) -> list[GraphEdge]:
    """RM-88: the edges the caller may see. Both layers must pass: the edge's scope is
    the caller's own or one it holds an active grant on, AND its source record (the
    ``record_id`` property) exists and passes the read's record viewer check (I53
    ``visible_to`` with the read's ``viewer``). A missing source record fails.

    ``edges`` pairs each edge with its already parsed qualifiers (RM-104: parsed once).
    Source records the read has not loaded are fetched in one batch (RM-115,
    ``get_records``) instead of one ``get_record`` per edge."""
    caller = await _caller_visibility(eng, ns)
    record_scope = active_record_scope()
    viewer = record_scope.viewer if record_scope is not None else None
    in_scope = [
        (edge, str(edge.properties.get("record_id") or ""))
        for edge, quals in edges
        if caller.admits_scope(quals)
    ]
    records: dict[str, MemoryRecord] = dict(by_id)
    missing = list(dict.fromkeys(rid for _, rid in in_scope if rid and rid not in records))
    if missing:
        records.update(await _fetch_records(eng._storage, missing))
    seen: dict[str, bool] = {}
    out: list[GraphEdge] = []
    for edge, rid in in_scope:
        if rid not in seen:
            record = records.get(rid) if rid else None
            seen[rid] = record is not None and (viewer is None or visible_to(record.tags, viewer))
        if seen[rid]:
            out.append(edge)
    return out


async def _fetch_records(storage: Any, record_ids: Sequence[str]) -> Mapping[str, MemoryRecord]:
    """RM-115: the existing records among ``record_ids`` in one ``get_records`` call; a
    storage without the batch method (a partial test fake) is read one id at a time."""
    batch = getattr(storage, "get_records", None)
    if callable(batch):
        result: Mapping[str, MemoryRecord] = await batch(record_ids)
        return result
    found: dict[str, MemoryRecord] = {}
    for rid in record_ids:
        record = await storage.get_record(rid)
        if record is not None:
            found[rid] = record
    return found


def _compiled_edges(edges: Sequence[GraphEdge]) -> list[GraphEdge]:
    """R3-05b: the edges the graph leg may use (compiled, live, with a source record)."""
    return [
        e
        for e in edges
        if e.properties.get("compiled") == "edge_compiler_v2"
        and e.weight > 0
        and e.properties.get("record_id")
    ]


@dataclass(frozen=True)
class _LegEdges:
    """The graph leg's edges after selection, filter and caller visibility."""

    edges: list[GraphEdge]
    compiled: int  # compiled live edges fetched (the namespace, or the seeds' edges)
    hidden: int  # admitted by the filter, hidden from the caller (RM-88)
    scope: str  # "namespace" (edge_list) or "seeds" (frontier_edges, RM-104)


def _frontier_capable(graph: Any) -> bool:
    """RM-104: the store can fetch the edges touching given nodes (``frontier_edges``, a
    ``GraphStore`` port method since the 2026-10-23 one-port decision; a partial fake
    without it keeps the namespace scan)."""
    return callable(getattr(graph, "frontier_edges", None))


async def _graph_leg_edges(
    eng: Any,
    ns: str,
    seeds: Sequence[str],
    by_id: Mapping[str, MemoryRecord],
    qualifier_filter: QualifierFilter | None,
    *,
    use_frontier: bool | None = None,
) -> _LegEdges:
    """R3-05b/RM-93/RM-104: the compiled edges the graph one-hop leg walks.

    Without a qualifier filter: every compiled edge of the namespace (``edge_list``), as
    before; the leg's MF-07 readiness reason (``no_edges`` = no compiled edge of the
    namespace has a live record) and the L3 artefact count are namespace-wide, which a
    seed-scoped fetch cannot reproduce, so the unfiltered read keeps that path.

    With a filter, on a store that supports it (``use_frontier`` None = auto): only the
    live edges touching the seeds (``frontier_edges``). ``one_hop`` reads only edges with
    an endpoint in the seed set, so the leg's hits are identical to the namespace scan;
    the diagnostics (``read.graph_filter`` counts, the ``read.graph_hop`` tentative
    count, the L3 artefact count and the empty reason) then describe the seeds' edges
    (``scope="seeds"``). Qualifiers are parsed once per edge for both the filter and the
    caller-scope check."""
    graph = eng._graph
    frontier = (
        qualifier_filter is not None and _frontier_capable(graph)
        if use_frontier is None
        else use_frontier
    )
    raw = (
        await graph.frontier_edges(list(dict.fromkeys(seeds)), namespace=ns)
        if frontier
        else await graph.edge_list(ns)
    )
    compiled = _compiled_edges(raw)
    scope = "seeds" if frontier else "namespace"
    if qualifier_filter is None:
        return _LegEdges(compiled, len(compiled), 0, scope)
    passing: list[tuple[GraphEdge, EdgeQualifiers | None]] = []
    for edge in compiled:
        quals = edge_qualifiers(edge.properties)  # RM-104: one parse per edge
        if qualifier_filter.matches(quals):
            passing.append((edge, quals))
    visible = await _caller_visible_edges(eng, ns, passing, by_id)  # RM-88: both layers
    return _LegEdges(visible, len(compiled), len(passing) - len(visible), scope)


async def _graph_hop_leg(
    eng: Any,
    ns: str,
    query: str,
    live: Sequence[MemoryRecord],
    vector_hits: Sequence[Any],
    fetch_k: int,
    ctx: Any,
) -> list[Any]:
    """R3-05b (``read.graph_leg_v2``, R-09): the graph one-hop leg and its lift to evidence turns.

    Seeds: the entity nodes the query names (``_graph_seeds``) plus the entities of the best
    ``read.graph_leg_seed_hits`` base hits. Edges: the compiled layer of the embedded graph
    (``graph.edge_compiler_v2``; tentative edges are passed on flagged, the leg drops and counts
    them). The leg returns fact records; they are lifted to their evidence turns through lineage and
    re-gated (``lift_episode_hits``: consent, erasure, scope). One named leg ``graph`` (family
    ``structured``)."""
    from memspine.read import legs as lib
    from memspine.read.pipeline.lift import lift_episode_hits

    read = eng._config().read
    by_id = {r.record_id: r for r in live}
    seeds: list[str] = []
    if eng._associative is not None:
        seeds = list(await eng._graph_seeds(ns, query))
    for hit in list(vector_hits)[: read.graph_leg_seed_hits]:
        rec = by_id.get(hit.record_id)
        if rec is not None and rec.entity:
            node = entity_node_id(ns, canonical_entity(rec.entity))
            if node not in seeds:
                seeds.append(node)
    edges: list[Any] = []
    if eng._graph is not None:
        qualifier_filter = active_graph_filter()  # RM-93: typed qualifiers gate each edge
        selected = await _graph_leg_edges(eng, ns, seeds, by_id, qualifier_filter)
        admitted = selected.edges
        if qualifier_filter is not None and tap.enabled():
            tap.event(
                "read.graph_filter",
                namespace=ns,
                edges=selected.compiled,
                admitted=len(admitted),
                caller_hidden=selected.hidden,
                scope=selected.scope,
            )
        edges = [
            lib.HopEdge(
                e.src,
                e.dst,
                str(e.properties.get("record_id") or ""),
                bool(e.properties.get("tentative")),
                bool(e.properties.get("closed")),
            )
            for e in admitted
        ]
    ctx.graph_seeds = seeds
    ctx.graph_edges = edges
    if ctx.layer_registry is not None:  # MF-14: compiled edges are L3's non-record artefacts
        ctx.layer_registry = ctx.layer_registry.with_artifacts("L3", len(edges))
    ctx.graph_min_seeds = read.graph_leg_min_seeds
    out = (await lib.run_legs(lib.with_graph_leg(lib.REGISTRY), ["graph"], ctx))["graph"]
    if not out.hits:
        return []
    turns = await lift_episode_hits(
        eng,
        ns,
        out.hits,
        {h.record_id: by_id[h.record_id] for h in out.hits if h.record_id in by_id},
        max_turns=read.graph_leg_lift_max_turns,
        carry=read.graph_leg_carry,
        source="graph",
    )
    return [NamedLeg("graph", turns)] if turns else []


async def _entity_leg_v2(
    eng: Any, ns: str, query: str, live: Sequence[MemoryRecord], fetch_k: int
) -> list[list[LegHit]]:
    """R3-05a (``read.entity_leg_v2``, LIB-34, R-08): the entity leg over resolved ids.

    The entities the question names are today's graph seeds (``_graph_seeds``: n-gram match
    on the entity index, the focal entity, the optional NER hook); the records that mention
    them come from the associative store's live ``mentions`` edges. Scored by the library:
    the Mem0 boost, max over the matched entities, capped, with hub damping by degree. The
    role of an entity in a record is subject (the record's ``entity``), object (its ``dst:``
    tag) or mention. One named leg ``entity_v2`` (family ``structured``)."""
    if eng._associative is None or _unqualified_graph_blocked():
        return []
    seeds = await eng._graph_seeds(ns, query)
    if not seeds:
        if tap.enabled():
            tap.event("read.leg.entity", matched=[], damping={}, hits=0, seeds=0)
        return []
    by_id = {r.record_id: r for r in live}
    mentions = await eng._associative.mentions_of(ns, seeds)  # RM-110: the seeds' edges only
    index: dict[str, list[EntityMention]] = {}
    for node in seeds:
        parsed = parse_entity_node(node)
        name = parsed[1] if parsed else node
        for rid in mentions.get(node, ()):
            rec = by_id.get(rid)
            if rec is None:  # not live: quarantined, archived, another type
                continue
            if rec.entity and canonical_entity(rec.entity) == name:
                role = "subject"
            elif any(t.startswith("dst:") and canonical_entity(t[4:]) == name for t in rec.tags):
                role = "object"
            else:
                role = "mention"
            index.setdefault(node, []).append(EntityMention(rid, role, rec.valid_from))  # type: ignore[arg-type]  # CI-GREEN: pre-existing typing debt
    result = entity_leg_explained(
        seeds,
        EntityIndex(index, n_records=len(by_id)),
        k=fetch_k,
        damping=DampingConfig(),
    )
    if tap.enabled():
        damping = {
            ev.entity_id: round(ev.damping, 4) for evs in result.provenance.values() for ev in evs
        }
        tap.event(
            "read.leg.entity",
            seeds=len(seeds),
            matched=list(result.matched_entities),
            unknown=list(result.unknown_entities),
            damping=damping,
            hits=len(result.leg.hits),
        )
    hits = [LegHit(h.record_id, h.score) for h in result.leg.hits]  # type: ignore[arg-type]  # CI-GREEN: pre-existing typing debt
    return [NamedLeg("entity_v2", hits)] if hits else []


async def _person_time_leg(eng: Any, ns: str, plan: ReadPlan) -> list[LegHit]:
    """#36: the ``plan@v3`` structured leg: live records whose ``valid_from`` lies in
    the span the plan's ``time_expr`` names and/or that are about its ``persons``.

    The span comes from the H1 rules (an absolute date first, else a relative phrase
    resolved with ``read.relative_week``) anchored on the namespace's newest record
    (the conversation's "now"; the clock when the namespace is empty). At most
    ``read.person_time_leg_k`` hits; the search gates still judge each one."""
    read_cfg = eng._config().read
    if not plan.persons and not (plan.time_expr or "").strip():
        return []
    try:
        live = [
            r
            for r in await eng._records(ns)
            if r.status is RecordStatus.ACTIVATED
            and not r.quarantined
            and r.memory_type != "shared"
            and CUE_TAG not in r.tags
        ]
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.person_time_leg_failed", namespace=ns, error=str(exc))
        return []
    anchor = max((r.valid_from for r in live), default=datetime.now(UTC))
    span = time_expr_span(plan.time_expr, anchor, week=read_cfg.relative_week)
    return person_time_leg(live, plan.persons, span, read_cfg.person_time_leg_k)


def _graph_admit(eng: Any, ns: str, *, history: bool = False) -> Callable[[MemoryRecord], bool]:
    """GP-10: may a graph walk enter (and return) this record?

    Never a record of another namespace, an erased or quarantined one, a cue or
    grant, a taint-rolled-back one, or one below ``read.graph_min_trust``; under
    ``integrity.enabled`` also never one below the admission threshold (stored
    trust; the read gates re-judge the leg's hits on live trust). ``history``
    (the facts block) also admits superseded (ARCHIVED) facts, to show their
    validity range; the read leg admits only live records. A passive-session
    record the read did not ask for, or one the read's purpose may not see
    (#50), is never admitted."""
    floor = eng._config().read.graph_min_trust
    integrity = eng._integrity()
    statuses = (
        (RecordStatus.ACTIVATED, RecordStatus.ARCHIVED) if history else (RecordStatus.ACTIVATED,)
    )

    def admit(record: MemoryRecord) -> bool:
        return (
            record.namespace == ns
            and record.status in statuses
            and not record.quarantined
            and record.memory_type != "shared"
            and CUE_TAG not in record.tags
            and _TAINT_ARCHIVED_TAG not in record.tags
            and record.trust >= floor
            and (not integrity.enabled or integrity.admits(record.trust))
            and not _passive_hidden(record)  # #53, as every other read leg
            and eng._consent_ok(record)  # #50: the read's purpose
        )

    return admit


async def _graph_seeds(
    eng: Any, ns: str, query: str, fallback_ids: Sequence[str] = ()
) -> list[str]:
    """GP-3: the entity nodes a graph walk starts from.

    The entities the query names: its 1..``GRAPH_SEED_MAX_NGRAM``-word n-grams
    matched against the namespace's entity names (canonical words, punctuation
    dropped), plus the names the decision provider's optional ``entities`` hook
    finds (GLiNER2; never required, failures ignored). When nothing is named,
    the entities mentioned by ``fallback_ids`` (the best non-graph hits).

    RM-110: the names come from the maintained entity index (``entity_lookup``), one
    lookup per candidate key, not from a scan of every edge of the namespace."""
    assert eng._associative is not None
    index = await eng._associative.entity_lookup(ns)
    if not await index.nonempty():
        return []
    words = match_key(query).split()
    seeds: list[str] = []
    focal = _FOCAL_ENTITY.get()
    if focal:
        # G-11: the caller's focal entity seeds the walk first (when it is known).
        for node in await index.get(match_key(focal)):
            if node not in seeds:
                seeds.append(node)
    for size in range(min(constants.GRAPH_SEED_MAX_NGRAM, len(words)), 0, -1):
        for start in range(len(words) - size + 1):
            for node in await index.get(" ".join(words[start : start + size])):
                if node not in seeds:
                    seeds.append(node)
    provider = eng._decision_provider()
    hook = getattr(provider, "entities", None) if provider is not None else None
    if callable(hook):
        try:
            for name in await hook(query):
                for node in await index.get(match_key(str(name))):
                    if node not in seeds:
                        seeds.append(node)
        except Exception as exc:  # an enhancer, never a gate
            _log.warning("read.graph_seed_ner_failed", namespace=ns, error=str(exc))
    if not seeds and fallback_ids:
        seeds = await eng._associative.entities_of(ns, fallback_ids)
    return seeds


async def _graph_walk(
    eng: Any, ns: str, query: str, fallback_ids: Sequence[str] = (), *, history: bool = False
) -> list[tuple[MemoryRecord, int]]:
    """GP-3: the records reached from the query's seed entities (nearest first)."""
    if eng._associative is None or _unqualified_graph_blocked():
        return []
    read_cfg = eng._config().read
    seeds = await eng._graph_seeds(ns, query, fallback_ids)
    if not seeds:
        return []
    return await eng._associative.seed_expand(  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
        ns,
        seeds,
        depth=min(read_cfg.graph_depth, constants.GRAPH_LEG_MAX_DEPTH),
        admit=eng._graph_admit(ns, history=history),
        max_degree=constants.GRAPH_LEG_MAX_DEGREE,
    )


async def _graph_leg(
    eng: Any,
    ns: str,
    query: str,
    vector_hits: Sequence[VectorHit],
    lexical_hits: Sequence[LexicalHit],
    extra_legs: list[list[LegHit]],
    use_hybrid: bool,
) -> list[LegHit]:
    """GP-3 (``read.graph_leg``): one more RRF leg from the association graph.

    Seeds are the entities the query names, else the entities of the best
    ``GRAPH_LEG_FALLBACK_HITS`` hits of the other legs. The walk
    (:meth:`AssociativeMemory.seed_expand`, ``read.graph_depth`` entity hops,
    GP-10 trust caps) yields fact records nearest first; each is followed by
    its source turns (``source.parents``) that the same gate admits. The first
    ``read.graph_leg_k`` form the leg. Its hits then pass every search gate
    (status, quarantine, admission) like any other leg's. Failures degrade to
    no leg (an enhancer, never a gate)."""
    if eng._associative is None or _unqualified_graph_blocked():
        return []
    try:
        if use_hybrid or extra_legs:
            prelim = [
                rid
                for rid, _ in rrf_fuse(
                    list(vector_hits),
                    list(lexical_hits),
                    extra=extra_legs,
                    limit=constants.GRAPH_LEG_FALLBACK_HITS,
                )
            ]
        else:
            prelim = [hit.record_id for hit in vector_hits]
        reached = await eng._graph_walk(ns, query, prelim[: constants.GRAPH_LEG_FALLBACK_HITS])
        storage = eng._require_started()
        admit = eng._graph_admit(ns)
        limit = eng._config().read.graph_leg_k
        ids: list[str] = []
        for record, _hops in reached:
            if len(ids) >= limit:
                break
            if record.record_id not in ids:
                ids.append(record.record_id)
            for parent_id in record.source.parents:
                if len(ids) >= limit or parent_id in ids:
                    continue
                parent = await storage.get_record(parent_id)
                if parent is not None and admit(parent):
                    ids.append(parent_id)
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.graph_leg_failed", namespace=ns, error=str(exc))
        return []
    return [LegHit(rid, 1.0) for rid in ids]


async def _graph_node_legs(
    eng: Any, ns: str, query: str, query_vector: list[float], fetch_k: int
) -> list[list[LegHit]]:
    """GR-6 (Graphiti node search): the entity nodes best matching the question
    (embedded names by cosine + text match, fused in the graph store), then the
    records that mention them, best entity first. One named leg ("graph_nodes")."""
    search = getattr(eng._graph, "search_entities", None)
    if search is None or eng._graph is None or _unqualified_graph_blocked():
        return []
    try:
        entities = await search(ns, query_vector, query, constants.GRAPH_NODE_SEARCH_TOP)
        leg: list[LegHit] = []
        seen: set[str] = set()
        for node_id, score in entities:
            for node in await eng._graph.neighbors(node_id, MENTIONS_REL, 1, namespace=ns):
                if is_entity_node(node.node_id) or node.node_id in seen:
                    continue
                seen.add(node.node_id)
                leg.append(LegHit(node.node_id, score))
                if len(leg) >= fetch_k:
                    break
        return [NamedLeg("graph_nodes", leg)] if leg else []
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.graph_node_search_failed", namespace=ns, error=str(exc))
        return []


async def _maxsim_leg(
    eng: Any,
    ns: str,
    query_vector: list[float],
    vector_hits: Sequence[Any],
    lexical_hits: Sequence[Any],
) -> list[list[LegHit]]:
    """N60 (EverMemOS MaxSim, Dakera sentence sub-memories): the first-pass
    candidates with two or more sentences, ranked by their best sentence's cosine
    with the query, so one matching sentence in a long turn is not diluted. Only
    candidates are embedded (no index); sentence vectors are cached per record."""
    if eng._embedder is None:
        return []
    try:
        storage = eng._require_started()
        ids = list(dict.fromkeys(h.record_id for h in [*vector_hits, *lexical_hits]))
        scored: list[tuple[float, str]] = []
        q = _unit(query_vector)
        for rid in ids:
            record = await storage.get_record(rid)
            if record is None or record.namespace != ns or record.quarantined:
                continue
            key = (rid, record.content)
            vectors = eng._sentence_vectors.get(key)
            if vectors is None:
                parts = sentences(record.content)
                vectors = (
                    [_unit(v) for v in await eng._embedder.embed(parts)] if len(parts) > 1 else []
                )
                if len(eng._sentence_vectors) >= constants.MAXSIM_CACHE_MAX:
                    eng._sentence_vectors.clear()
                eng._sentence_vectors[key] = vectors
            if vectors:
                scored.append(
                    (max(sum(a * b for a, b in zip(q, v, strict=False)) for v in vectors), rid)
                )
        scored.sort(key=lambda pair: -pair[0])
        leg = NamedLeg("maxsim", [LegHit(rid, score) for score, rid in scored])
        return [leg] if leg else []
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.maxsim_leg_failed", namespace=ns, error=str(exc))
        return []


async def _asset_overlay(
    eng: Any, query: str, ns: str, context: AssembledContext
) -> AssembledContext:
    """E04 ``read.asset_evidence``: for a question that needs what a picture shows, each
    retrieved turn that carries an attachment gets one evidence line after it: the
    vision description with its ``asset:<hash>`` provenance, or an explicit note of why
    there is none. The source's search hint is never used. Never drops a record; a
    failure keeps the context as it was."""
    mode = eng._config().read.asset_evidence
    if mode == "off":
        return context
    from memspine.services.assets import ASSET_TAG_PREFIX, needs_visual_detail

    if not needs_visual_detail(query):
        return context
    try:
        adapter = eng._asset_adapter()
        records = list(context.records)
        added: list[str] = []
        audit: list[dict[str, Any]] = []
        budget = eng._config().read.asset_max_per_read
        offset = 0
        for pos, record in enumerate(list(records)):
            if budget <= 0:
                break
            ids = [
                t[len(ASSET_TAG_PREFIX) :] for t in record.tags if t.startswith(ASSET_TAG_PREFIX)
            ]
            lines: list[str] = []
            for aid in ids[:budget]:
                entry = await adapter.resolve(ns, aid, allow_network=mode == "fetch")
                if entry is None:
                    continue
                budget -= 1
                audit.append(
                    {
                        "asset": aid,
                        "availability": entry.availability,
                        "evidence": entry.evidence_status,
                        "error": entry.error,
                    }
                )
                if entry.evidence_status == "ok" and entry.evidence:
                    lines.append(
                        f"[image evidence {entry.provenance}, turn {entry.source_turn_id}; "
                        f"read from the original image by a vision model, may be imperfect] "
                        f"{entry.evidence[:600]}"
                    )
                else:
                    why = entry.error or (
                        "not downloaded (read.asset_evidence: cached)"
                        if entry.availability == "unfetched"
                        else "no description"
                    )
                    lines.append(
                        f"[image evidence unavailable for turn {entry.source_turn_id}: {why}; "
                        "the picture was not seen, do not guess what it shows]"
                    )
            if lines:
                block = eng._lead_record(
                    ns, "\n".join(lines), [record], extra_tags=("asset_evidence",)
                )
                at = pos + 1 + offset
                records.insert(at, block)
                offset += 1
                if at <= context.boundary_index:
                    context.boundary_index += 1
                added.extend(lines)
        if added:
            context.records = records
            context.tokens_used += sum(estimate_tokens(t) for t in added)
        if (fx := _FORENSICS.get()) is not None and audit:
            fx["asset_evidence"] = audit
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.asset_evidence_failed", namespace=ns, error=str(exc))
    return context


async def _external_overlay(
    eng: Any, query: str, ns: str, context: AssembledContext
) -> AssembledContext:
    """E03 ``read.external_evidence``: for an invited inference or a recommendation
    request only, one ``[public knowledge]`` block built from a generic public query (the
    question's lowercase topic words, with every name, place and retrieved-context
    capital word removed). The block carries its own clause: it never proves that a
    person did, owns, visited or said anything. Nothing is stored. A failure or refusal
    leaves the context as it was and is recorded in the forensics."""
    mode = eng._config().read.external_evidence
    if mode == "off":
        return context
    if eng._config().read.external_v2:
        return await _external_overlay_v2(eng, query, ns, context, mode)
    from memspine.services.external import (
        format_public_block,
        invites_inference,
        private_terms_from,
    )

    if not invites_inference(query):
        return context
    evidence = [r for r in context.records if constants.LEAD_TAG not in r.tags]
    if not evidence:
        return context  # no personal premise to join the public relation to
    try:
        state = await eng._persp_state_for(ns)
        private = private_terms_from((r.content for r in evidence), names=state["known"])
        result = await eng._external_broker().lookup(query, private, allow_network=mode == "web")
        if (fx := _FORENSICS.get()) is not None:
            fx["external_evidence"] = {
                "status": result.status,
                "public_query": result.public_query,
                "provider": result.provider,
                "n_snippets": len(result.snippets),
            }
        if not result.usable:
            return context
        text = format_public_block(result)
        block = eng._lead_record(ns, text, evidence, extra_tags=("ext:public",))
        context.records = [*context.records, block]
        context.tokens_used += estimate_tokens(text)
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.external_evidence_failed", namespace=ns, error=str(exc))
    return context


class _TokenCounter:
    def count(self, text: str) -> int:
        return estimate_tokens(text)


async def _external_overlay_v2(
    eng: Any, query: str, ns: str, context: AssembledContext, mode: str
) -> AssembledContext:
    """R3-13 / LIB-45 ``read.external_v2`` (needs ``read.external_evidence`` ``cache`` or ``web``):
    the planner (``plan_external``) decides from the typed contract, the broker looks up a
    privacy-filtered generic query, and the snippets become one separate labelled ``public`` block
    (``external_block``: freshness, https, dedupe, per-domain cap, token budget). The block is a
    synthetic context record, never stored; a failure leaves the context as it was."""
    from memspine.read.contract_rules import build_contract as build_typed_contract
    from memspine.read.external import (
        ExternalConfig,
        ExternalSnippet,
        external_block,
        plan_external,
    )
    from memspine.services.external import private_terms_from

    read = eng._config().read
    broker = eng._external_broker()
    plan = plan_external(
        build_typed_contract(query),
        ExternalConfig(
            enabled=True, max_calls=read.external_max_calls, max_snippets=read.external_max_results
        ),
        query=query,
        calls_used=broker.stats.network_calls,
    )
    if not plan:
        tap.event("read.external", planned=False, reason=plan.reason)
        return context
    try:
        evidence = [r for r in context.records if constants.LEAD_TAG not in r.tags]
        state = await eng._persp_state_for(ns)
        private = private_terms_from((r.content for r in evidence), names=state["known"])
        result = await broker.lookup(query, private, allow_network=mode == "web")
        if (fx := _FORENSICS.get()) is not None:
            fx["external_evidence"] = {
                "status": result.status,
                "public_query": result.public_query,
                "provider": result.provider,
                "n_snippets": len(result.snippets),
                "v2": True,
            }
        if not result.usable:
            tap.event(
                "read.external",
                planned=True,
                reason=plan.reason,
                status=result.status,
                kept=0,
                dropped={},
            )
            return context
        snippets = [
            ExternalSnippet(url=sn.url, text=sn.text, title=sn.title) for sn in result.snippets
        ]
        block = external_block(
            snippets,
            read.external_v2_budget_tokens,
            _TokenCounter(),
            config=ExternalConfig(enabled=True, max_snippets=read.external_max_results),
            now=datetime.now(UTC),
        )
        tap.event(
            "read.external",
            planned=True,
            reason=plan.reason,
            status=result.status,
            kept=len(block.lines),
            dropped=dict(block.dropped),
        )
        if block.empty:
            return context
        body = [f"[{block.label}] {block.clause}"]
        body += [
            f"- {ln.title + ': ' if ln.title else ''}{ln.text} ({ln.url})" for ln in block.lines
        ]
        text = chr(10).join(body)
        if evidence:
            rec = eng._lead_record(ns, text, evidence, extra_tags=("ext:public",))
            rec = rec.model_copy(update={"trust": min(rec.trust, block.lines[0].trust)})
        else:
            rec = MemoryRecord(
                namespace=ns,
                memory_type="semantic",
                content=text,
                tags=[constants.LEAD_TAG, "ext:public"],
                trust=block.lines[0].trust,
                source=SourceInfo(role="system", channel="lead", parents=[]),
            ).as_engine_block()
        context.records = [*context.records, rec]
        context.tokens_used += block.tokens
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.external_evidence_failed", namespace=ns, error=str(exc))
    return context


async def _read_overlay(
    eng: Any,
    query: str,
    ns: str,
    context: AssembledContext,
    session_id: str | None,
    *,
    shared: bool = False,
) -> AssembledContext:
    """I59 / I60 / I63 / I64: the read-side attribution layer, after retrieval and before
    the reader. Marks lines with speaker and subject, adds reader notes (nobody in the
    context is about the named person; a named person is not in the store) and the
    user header; records which memories were injected. Never drops a record. All off by
    default; a failure keeps the context as it was."""
    read = eng._config().read
    if shared or not (
        read.owner_check != "off"
        or read.entity_check != "off"
        or read.user_header != "off"
        or read.reinjection_penalty > 0.0
        or (read.query_contract != "off" and "header" in read.query_contract_use)
    ):
        return context
    records = list(context.records)

    def is_evidence(r: MemoryRecord) -> bool:
        return constants.LEAD_TAG not in r.tags and r.source.channel not in ("lead", "persona")

    evid = [i for i, r in enumerate(records) if is_evidence(r)]
    if not evid:
        return context
    try:
        if read.reinjection_penalty > 0.0 and session_id:  # I64
            eng._injections.record(
                ns, session_id, [records[i].record_id for i in evid], read.reinjection_window
            )
        notes: list[str] = []
        audit: dict[str, Any] = {}
        if read.owner_check != "off":
            qp = await eng._question_perspective(ns, query)
            views = [record_view(records[i]) for i in evid]
            tagged = [v if v.annotated else None for v in views]
            verdict = judge(qp, tagged)
            if qp.bound and eng._decider_active("about_target"):
                overrides: dict[int, bool] = {}
                for j in unknown_lines(verdict)[:10]:
                    label = await eng._decided(
                        "about_target", query, records[evid[j]].content, "about"
                    )
                    overrides[j] = label == "about"
                if overrides:
                    verdict = judge(qp, tagged, overrides=overrides)
            if read.owner_check in ("mark", "both"):
                for j, i in enumerate(evid):
                    marker = owner_marker(views[j]) if views[j].annotated else None
                    if marker and not records[i].content.startswith(marker):
                        records[i] = records[i].model_copy(
                            update={"content": f"{marker} {records[i].content}"}
                        )
            if read.owner_check in ("note", "both") and verdict.note:
                notes.append(verdict.note)
            audit["owner"] = {"labels": verdict.labels, "note": verdict.note}
        if read.entity_check != "off":
            names = missing_names(query, await eng._entity_vocabulary(ns))
            text = entity_note(names)
            if text:
                notes.append(text)
            audit["entity_missing"] = names
        parts = [records[i] for i in evid]
        at = min(context.boundary_index, len(records))
        for k, text in enumerate(notes):
            records.insert(at + k, eng._lead_record(ns, text, parts))
        head = (
            user_header(read.perspective_asker or active_asker())
            if read.user_header == "on"
            else None
        )
        if head:
            records.insert(0, eng._lead_record(ns, head, parts))
            context.boundary_index += 1
            audit["user_header"] = head
        answer_line: str | None = None
        if read.query_contract != "off" and "header" in read.query_contract_use:
            contract = await eng._query_contract(query)  # A03
            answer_line = contract_header(contract)
            audit["query_contract"] = contract.as_meta()
            if answer_line:
                records.insert(1 if head else 0, eng._lead_record(ns, answer_line, parts))
                context.boundary_index += 1
                audit["query_contract_header"] = answer_line
        context.records = records
        context.tokens_used += sum(
            estimate_tokens(t) for t in [*notes, head or "", answer_line or ""]
        )
        if (fx := _FORENSICS.get()) is not None and audit:
            fx["read_overlay"] = audit
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.overlay_failed", namespace=ns, error=str(exc))
    return context


async def _strip_speaker_names(eng: Any, ns: str, query: str) -> str:
    """``read.lexical_strip_names``: the query without the speaker names of ``ns``
    (the "Speaker:" prefix of its records), for the BM25 leg only. Names are cached
    per namespace and refreshed when the record count changes. A query that would
    be left empty, or any failure, keeps the original text."""
    try:
        records = await eng._records(ns)
        cached = eng._speaker_names.get(ns)
        if cached is None or cached[0] != len(records):
            found: set[str] = set()
            for r in records:
                head, sep, _ = r.content.partition(":")
                word = head.strip()
                if sep and word.isalpha() and word[:1].isupper():
                    found.add(word.lower())
            cached = (len(records), frozenset(found))
            eng._speaker_names[ns] = cached
        names = cached[1]
        if not names:
            return query
        kept = [
            tok
            for tok in query.split()
            if re.sub(r"(?:['\u2019]s)?\W*$", "", tok.strip("\"'(")).lower() not in names
        ]
        if not any(re.search(r"\w", tok) for tok in kept):
            return query
        return " ".join(kept)
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.lexical_strip_names_failed", namespace=ns, error=str(exc))
        return query


async def _session_leg(
    eng: Any, ns: str, query_vector: list[float], fetch_k: int
) -> list[list[LegHit]]:
    """``read.session_leg``: sessions (``group_id``) ranked by the cosine of the query
    with the mean of their records' vectors; the best ``session_leg_per_session``
    records (by cosine) of each of the top ``session_leg_top_sessions`` sessions form
    the leg. Record vectors are embedded once and cached per (record, content)."""
    read = eng._config().read
    if eng._embedder is None:
        return []
    try:
        records = [
            r for r in await eng._records(ns) if not r.quarantined and r.group_id is not None
        ]
        cache = eng._session_vectors
        missing = [r for r in records if (r.record_id, r.content) not in cache]
        if missing:
            if len(cache) + len(missing) > constants.MAXSIM_CACHE_MAX:
                cache.clear()
                missing = records
            for start in range(0, len(missing), 64):
                chunk = missing[start : start + 64]
                vectors = await eng._embedder.embed([r.content for r in chunk])
                for r, v in zip(chunk, vectors, strict=True):
                    cache[(r.record_id, r.content)] = _unit(v)
        q = _unit(query_vector)

        def cos(v: Sequence[float]) -> float:
            return sum(a * b for a, b in zip(q, v, strict=False))

        groups: dict[str, list[tuple[float, str]]] = {}
        sums: dict[str, list[float]] = {}
        for r in records:
            v = cache[(r.record_id, r.content)]
            gid = str(r.group_id)
            groups.setdefault(gid, []).append((cos(v), r.record_id))
            acc = sums.setdefault(gid, [0.0] * len(v))
            for i, x in enumerate(v):
                acc[i] += x
        ranked = sorted(sums, key=lambda g: (-cos(sums[g]), g))[: read.session_leg_top_sessions]
        hits: list[tuple[float, str]] = []
        for gid in ranked:
            best = sorted(groups[gid], key=lambda pair: (-pair[0], pair[1]))
            hits += best[: read.session_leg_per_session]
        hits.sort(key=lambda pair: -pair[0])
        leg = NamedLeg("session", [LegHit(rid, score) for score, rid in hits[:fetch_k]])
        return [leg] if leg else []
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.session_leg_failed", namespace=ns, error=str(exc))
        return []


async def _speaker_vote_leg(
    eng: Any,
    ns: str,
    query: str,
    query_vector: list[float],
    vector_hits: Sequence[Any],
    fetch_k: int,
) -> list[list[LegHit]]:
    """``read.list_mode``: the vector leg fetched ``list_vote_depth`` deep, kept to the
    turns of the one speaker the question names (top ``list_vote_top_k``), as the
    ``speaker_vote`` leg. Empty when no or several speakers are named."""
    read = eng._config().read
    try:
        depth = max(read.list_vote_depth, fetch_k)
        deep = vector_hits
        if depth > fetch_k:
            deep = await eng._vector_leg(ns, query_vector, depth)
        records = [r for r in await eng._records(ns) if not r.quarantined]
        vote = subject_vector_leg if read.speaker_vote_mode != "name" else speaker_vector_leg
        hits = vote(query, records, deep, read.list_vote_top_k)
        if read.speaker_vote_mode == "perspective":
            # I39: the subject vote on the resolved question perspective (subsumes "subject")
            qp = await eng._question_perspective(ns, query)
            deep_ids = [h.record_id for h in deep]
            ids = perspective_leg(qp, records, deep_ids, read.list_vote_top_k)
            hits = [LegHit(rid, 1.0) for rid in ids] or hits
        if not hits:
            # R2-2: two named speakers with a comparison cue vote once each.
            pair = comparison_speaker_legs(query, records, deep, read.list_vote_top_k)
            legs = [
                NamedLeg(f"speaker_vote_{tag}", votes)
                for tag, (_, votes) in zip("ab", pair, strict=False)
            ]
            return [leg for leg in legs if leg]
        leg = NamedLeg("speaker_vote", hits)
        return [leg] if leg else []
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.speaker_vote_leg_failed", namespace=ns, error=str(exc))
        return []


def _bridge_gate(query: str, read: Any) -> bool:
    """R2-1b (``read.bridge_hop_gate``): whether the bridge hop fires for this read.
    ``always`` (default) fires; ``cue``: the question describes its answer's entity;
    ``weak``: the first pass found one confident anchor without support (the
    second-best raw reranker score is under ``bridge_hop_weak_threshold``);
    ``cue_or_weak``: either. Records ``bridge_gate`` ("always" / "cue" / "weak" /
    "skipped") in the forensics."""
    gate = read.bridge_hop_gate
    why = "always" if gate == "always" else "skipped"
    if gate in ("cue", "cue_or_weak") and has_bridge_cue(query):
        why = "cue"
    elif gate in ("weak", "cue_or_weak"):
        support = _RERANK_SUPPORT.get()
        if support is not None and support < read.bridge_hop_weak_threshold:
            why = "weak"
    if (fx := _FORENSICS.get()) is not None:
        fx["bridge_gate"] = why
    return why != "skipped"


async def _bridge_leg(
    eng: Any,
    query: str,
    ns: str,
    scored: Sequence[tuple[MemoryRecord, float]],
    *,
    session_id: str | None,
    keep_k: int,
    hide: Callable[[MemoryRecord], bool] | None,
    probes: Sequence[str],
) -> NamedLeg | None:
    """``read.bridge_hop``: one extra search for "<subject> <phrase> ..." where the
    phrases are the key noun phrases of the top first-hop hits that the question lacks.
    Fails soft (None). The phrases are recorded as the forensic ``bridge_phrases``."""
    read = eng._config().read
    try:
        phrases = bridge_phrases(
            query, [r.content for r, _ in scored[: constants.SECOND_ROUND_TOP]]
        )
        if (fx := _FORENSICS.get()) is not None:
            fx["bridge_phrases"] = list(phrases)
        if not phrases:
            return None
        names = [w for w in re.findall(r"\b[A-Z][a-z][\w'-]*", query)[1:2]]
        subject = " ".join(names) or core_terms(query)
        extra = await eng._search(
            f"{subject} {' '.join(phrases)}".strip(),
            ns,
            read.bridge_hop_top_k,
            session_id=session_id,
            keep_k=read.bridge_hop_top_k,
            hide=hide,
            probes=[],
        )
        leg = NamedLeg("bridge", [LegHit(r.record_id, float(score)) for r, score in extra])
        return leg if leg else None
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.bridge_hop_failed", namespace=ns, error=str(exc))
        return None


async def _anchor_legs(
    eng: Any,
    ns: str,
    vector_hits: Sequence[Any],
    lexical_hits: Sequence[Any],
    fetch_k: int,
) -> list[list[LegHit]]:
    """N43 cohesion and N32 entity-expansion legs, anchored on the first-pass top
    hits (``ANCHOR_TOP`` from the vector leg, then the lexical leg). Only live,
    unquarantined records anchor or join; the search gates still judge every hit."""
    read = eng._config().read
    try:
        live = [
            r
            for r in await eng._records(ns)
            if r.status is RecordStatus.ACTIVATED
            and not r.quarantined
            and r.memory_type != "shared"
            and CUE_TAG not in r.tags
        ]
        by_id = {r.record_id: r for r in live}
        anchor_ids: list[str] = []
        for hit in [
            *vector_hits[: constants.ANCHOR_TOP],
            *lexical_hits[: constants.ANCHOR_TOP],
        ]:
            if hit.record_id in by_id and hit.record_id not in anchor_ids:
                anchor_ids.append(hit.record_id)
        anchors = [by_id[rid] for rid in anchor_ids[: constants.ANCHOR_TOP]]
        if not anchors:
            return []
        legs: list[list[LegHit]] = []
        if read.cohesion_leg:
            window = timedelta(minutes=constants.COHESION_WINDOW_MINUTES)
            legs.append(NamedLeg("cohesion", cohesion_leg(anchors, live, fetch_k, window)))
        if read.entity_expand_leg:
            speakers = {s for r in live if (s := speaker_of(r.content)) is not None}
            legs.append(
                NamedLeg(
                    "entity_expand",
                    entity_expand_leg(
                        anchors,
                        live,
                        fetch_k,
                        speakers,
                        constants.ENTITY_EXPAND_MAX_SHARE,
                    ),
                )
            )
        return [leg for leg in legs if leg]
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.anchor_legs_failed", namespace=ns, error=str(exc))
        return []


def _leg_weights_for(eng: Any, query: str) -> dict[str, float]:
    """N16 leg weights, then N62's for this question's shape, then N33's lexical
    weight for a very short question. Empty = unweighted RRF (unchanged)."""
    read = eng._config().read
    weights = dict(read.leg_weights)
    if read.leg_weights_by_shape:
        weights.update(read.leg_weights_by_shape.get(question_shape(query), {}))
    if (
        read.short_query_lexical_weight is not None
        and len(content_words(query)) <= constants.SHORT_QUERY_WORDS
    ):
        weights["lexical"] = read.short_query_lexical_weight
    return weights


async def _with_session_neighbours(
    eng: Any,
    ns: str,
    candidates: list[tuple[MemoryRecord, float]],
    documents: list[str],
    width: int,
) -> list[str]:
    """N42: each reranker input with ``width`` neighbouring turns of its session
    on each side (in time order), so a short reply is judged with its question.
    Sessions are the episodic store's (the same ones replay expands). A record in
    no session, a neighbour that is not live, or a failure keeps its own text."""
    if eng._episodic is None:
        return documents
    try:
        session_ids: dict[str, list[str]] = {}
        for s in await eng._episodic.sessions(ns, constants.SESSION_GAP_MINUTES):
            for rid in s.record_ids:
                session_ids.setdefault(rid, s.record_ids)
        text = {
            r.record_id: r.content
            for r in await eng._records(ns)
            if r.status is RecordStatus.ACTIVATED and not r.quarantined
        }
        out: list[str] = []
        for (record, _), doc in zip(candidates, documents, strict=True):
            ids = session_ids.get(record.record_id)
            if not ids:
                out.append(doc)
                continue
            i = ids.index(record.record_id)
            before = [text[x] for x in ids[max(0, i - width) : i] if x in text]
            after = [text[x] for x in ids[i + 1 : i + 1 + width] if x in text]
            out.append("\n".join([*before, doc, *after]))
        return out
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("rerank.context_failed", namespace=ns, error=str(exc))
        return documents


async def _graph_rerank(
    eng: Any, ns: str, query: str, candidates: list[tuple[MemoryRecord, float]]
) -> list[tuple[MemoryRecord, float]]:
    """#22 (``read.graph_rerank``): lift graph-near and often-restated candidates.

    Proximity comes from the graph leg's seeds (the entities the query names,
    else those of the best ``GRAPH_LEG_FALLBACK_HITS`` candidates) through
    :meth:`AssociativeMemory.graph_proximity` (``distance`` or ``ppr``). The
    episode-mentions boost is ``log(1 + n) / log(1 + n_max)`` over the
    candidates' admitted ``edge_source:`` provenance counts ``n`` (GR-9). Each boost
    ``b`` in [0, 1] lifts relevance ``r`` to ``r + w * b * (1 - r)``, so a
    candidate with no boost keeps its score; the stable re-sort keeps ties in
    their fused order. Failures leave the candidates as they were (an
    enhancer, never a gate)."""
    read_cfg = eng._config().read
    weight = read_cfg.graph_rerank_weight
    if weight <= 0.0:
        return candidates
    proximity: dict[str, float] = {}
    if eng._associative is not None and not _unqualified_graph_blocked():
        try:
            fallback = [r.record_id for r, _ in candidates[: constants.GRAPH_LEG_FALLBACK_HITS]]
            seeds = await eng._graph_seeds(ns, query, fallback)
            if seeds:
                proximity = await eng._associative.graph_proximity(
                    ns,
                    seeds,
                    depth=min(read_cfg.graph_depth, constants.GRAPH_LEG_MAX_DEPTH),
                    admit=eng._graph_admit(ns),
                    mode=read_cfg.graph_rerank,
                    max_degree=constants.GRAPH_LEG_MAX_DEGREE,
                )
        except Exception as exc:  # an enhancer, never a gate
            _log.warning("read.graph_rerank_failed", namespace=ns, error=str(exc))
            proximity = {}
    mentions = {
        record.record_id: await eng._admitted_edge_sources(ns, record) for record, _ in candidates
    }
    top = max(mentions.values(), default=0)

    def lift(relevance: float, boost: float) -> float:
        return relevance + weight * boost * (1.0 - relevance) if boost > 0 else relevance

    boosted = []
    for record, relevance in candidates:
        relevance = lift(relevance, proximity.get(record.record_id, 0.0))
        if top > 0:
            relevance = lift(relevance, math.log1p(mentions[record.record_id]) / math.log1p(top))
        boosted.append((record, relevance))
    return sorted(boosted, key=lambda pair: pair[1], reverse=True)


async def _causal_walk(
    eng: Any,
    ns: str,
    candidates: list[tuple[MemoryRecord, float]],
    group_id: str | None,
    tags: list[str] | None,
    memory_type: str | None,
) -> list[tuple[MemoryRecord, float]]:
    """W12/G27 (``read.causal_walk``, ADR-061): a ranked directed walk.

    From the best ``CAUSAL_WALK_SEEDS`` candidates, follow ``because`` (effect
    -> cause) and ``reply_to`` (reply -> answered) LINKs up to
    ``read.causal_walk_hops`` hops (:meth:`AssociativeMemory.rel_walk`, GP-10
    admission). A record reached ``h`` hops from a seed of relevance ``r``
    scores ``r x CAUSAL_WALK_DECAY ** h`` (its best path); it joins the
    candidates, or lifts one already there that scored less, after the same
    search gates as every leg. The stable re-sort keeps ties in fused order.
    Failures leave the candidates as they were (an enhancer, never a gate)."""
    assert eng._associative is not None
    if _unqualified_graph_blocked():
        return candidates
    seeds = candidates[: constants.CAUSAL_WALK_SEEDS]
    seed_score = {record.record_id: relevance for record, relevance in seeds}
    try:
        reached = await eng._associative.rel_walk(
            ns,
            list(seed_score),
            (BECAUSE_REL, constants.REPLY_TO_REL),
            depth=eng._config().read.causal_walk_hops,
            admit=eng._graph_admit(ns),
        )
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.causal_walk_failed", namespace=ns, error=str(exc))
        return candidates
    current = {record.record_id: relevance for record, relevance in candidates}
    best: dict[str, float] = {}
    for record, hops, origin in reached:
        score = seed_score[origin] * constants.CAUSAL_WALK_DECAY**hops
        if score > max(best.get(record.record_id, 0.0), current.get(record.record_id, -1.0)):
            best[record.record_id] = score
    if not best:
        return candidates
    gated = await eng._gate_hits(ns, list(best.items()), group_id, tags, memory_type)
    merged = {record.record_id: (record, relevance) for record, relevance in candidates}
    for record, relevance in gated:
        merged[record.record_id] = (record, relevance)
    return sorted(merged.values(), key=lambda pair: pair[1], reverse=True)


async def _community_gate(
    eng: Any,
    ns: str,
    query: str,
    vector_hits: Sequence[VectorHit],
    lexical_hits: Sequence[LexicalHit],
    extra_legs: list[list[LegHit]],
    use_hybrid: bool,
) -> list[str]:
    """GP-9 (``read.graph_communities``): the community summaries this query may read.

    The live ``reorganize`` parents of the communities a seed entity of the graph
    leg belongs to: a community counts when one of the records the entity
    mentions is a member (communities are built over association edges, so
    membership goes through the mentioned records). Seeds as for the graph leg.
    Parents pass the graph admission gate. Failures admit none (the gate
    stays shut)."""
    if eng._associative is None or _unqualified_graph_blocked():
        return []
    try:
        if use_hybrid or extra_legs:
            prelim = [
                rid
                for rid, _ in rrf_fuse(
                    list(vector_hits),
                    list(lexical_hits),
                    extra=extra_legs,
                    limit=constants.GRAPH_LEG_FALLBACK_HITS,
                )
            ]
        else:
            prelim = [hit.record_id for hit in vector_hits]
        seeds = await eng._graph_seeds(ns, query, prelim[: constants.GRAPH_LEG_FALLBACK_HITS])
        if not seeds:
            return []
        storage = eng._require_started()
        admit = eng._graph_admit(ns)
        allowed: list[str] = []
        for parent_id in await eng._associative.entity_communities(ns, seeds):
            parent = await storage.get_record(parent_id)
            if parent is not None and parent.source.channel == "reorganize" and admit(parent):
                allowed.append(parent_id)
        return allowed
    except Exception as exc:
        _log.warning("read.graph_communities_failed", namespace=ns, error=str(exc))
        return []


async def _admitted_edge_sources(eng: Any, ns: str, fact: MemoryRecord) -> int:
    """#22: the ``edge_source:`` episodes of ``fact`` a graph walk may enter
    (:meth:`_graph_admit`: live, unquarantined, at least
    ``read.graph_min_trust``). A forgotten, quarantined or low-trust source
    lends the rerank's episode-mentions boost nothing."""
    prefix = constants.EDGE_SOURCE_TAG_PREFIX
    ids = [t[len(prefix) :] for t in fact.tags if t.startswith(prefix)]
    if not ids:
        return 0
    storage = eng._require_started()
    admit = eng._graph_admit(ns)
    count = 0
    for record_id in dict.fromkeys(ids):
        source = await storage.get_record(record_id)
        if source is not None and admit(source):
            count += 1
    return count


async def _edge_sources(eng: Any, storage: RecordsStore, fact: MemoryRecord) -> int:
    """GR-9: the live episodes stating ``fact``: its parents and the episodes
    duplicates added (``edge_source:`` tags), erased ones not counted."""
    prefix = constants.EDGE_SOURCE_TAG_PREFIX
    ids = [*fact.source.parents, *(t[len(prefix) :] for t in fact.tags if t.startswith(prefix))]
    count = 0
    for record_id in dict.fromkeys(ids):
        source = await storage.get_record(record_id)
        if source is not None and source.status is not RecordStatus.DELETED:
            count += 1
    return max(count, 1)


async def _vector_leg(
    eng: Any, ns: str, query_vector: list[float], fetch_k: int
) -> list[VectorHit]:
    """The vector leg of :meth:`search` (E4 two-stage rescore when active)."""
    assert eng._embedder is not None and eng._vector is not None
    # E4 (ADR-020): the two-stage quantized rescore replaces the plain cosine
    # query ONLY when active (a quantized/Matryoshka manifest or the
    # vector.quantization override). Off, this is exactly query() — the
    # vector-only pipeline stays byte-identical. Rescore happens here, at the
    # vector leg, BEFORE fusion/gate/rerank compose over the candidates.
    vector_store = eng._vector
    embedder_id = eng._embedder.embedder_id
    if eng._rescore_active:
        try:
            # #87: ANN (quantized) search is approximate by design; its ties
            # are settled like the exact path's, its recall is not (ADR-051).
            return await settle_ties(
                lambda n: vector_store.search_rescore(
                    ns, query_vector, embedder_id=embedder_id, top_k=n
                ),
                fetch_k,
                eng._tie_keys,
            )
        except Exception as exc:
            # Defense in depth (mirrors the lexical leg below): any residual
            # rescore error — e.g. a corrupt code row surviving the scheme/dim
            # guards — degrades to the exact query() path, never crashes search().
            _log.warning("vector.rescore_failed", namespace=ns, error=str(exc))
    return await settle_ties(
        lambda n: vector_store.query(ns, query_vector, embedder_id=embedder_id, top_k=n),
        fetch_k,
        eng._tie_keys,
    )


async def _lexical_leg(eng: Any, ns: str, text: str, fetch_k: int) -> list[LexicalHit]:
    """One BM25 leg, its equal-score runs in content order (#87). Raises what the
    lexical store raises; each caller decides how a failed leg degrades."""
    lexical = eng._lexical
    assert lexical is not None
    return await settle_ties(lambda n: lexical.search(ns, text, top_k=n), fetch_k, eng._tie_keys)


async def _cluster_legs(
    eng: Any, ns: str, scored: list[tuple[MemoryRecord, float]], fetch_k: int
) -> list[list[LegHit]]:
    """N06: one ranked leg per top hit (``CLUSTER_EXPAND_SEEDS`` of them): its
    nearest stored neighbours by embedding. Best-effort, never a gate."""
    if eng._embedder is None or eng._vector is None:
        return []
    seeds = [r for r, _ in scored[: constants.CLUSTER_EXPAND_SEEDS]]
    try:
        vectors = await eng._embedder.embed([r.content for r in seeds])
        legs: list[list[LegHit]] = []
        for vector in vectors:
            hits = await eng._vector_leg(ns, list(vector), fetch_k)
            legs.append([LegHit(h.record_id, 1.0) for h in hits])
        return legs
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.cluster_expand_failed", namespace=ns, error=str(exc))
        return []


def _evict_from_encoder(eng: Any, event: MemoryEvent) -> None:
    """#61: a forgotten, quarantined or archived record leaves the query
    encoder's in-process index (its text must not outlive the erasure)."""
    assert eng._query_encoder is not None
    record_id = event.payload.get("record_id")
    if not isinstance(record_id, str):
        return
    if event.kind is EventKind.FORGET:
        eng._query_encoder.evict(event.namespace, record_id)
    elif event.kind is EventKind.DECAY_TRANSITION:
        changes = event.payload.get("set")
        if isinstance(changes, dict) and (
            changes.get("quarantined") is True
            or changes.get("status", RecordStatus.ACTIVATED.value) != RecordStatus.ACTIVATED.value
        ):
            eng._query_encoder.evict(event.namespace, record_id)
